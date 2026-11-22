#!/usr/bin/env python3
"""外部编排器（OPC v2.14 · 收口「唯一硬缺口」：创始人回「继续」这一步）。

**它是什么**：一个**脱离对话**的调度进程。读 Vault 里的交接状态 → 判断当前棒
能不能自动推进 → 推进 → 把下一棒派出去。它不是 Agent，不参与生产内容。

**它绝不做什么**（宪法 §15.3 / §7 / §23.4）：

1. **不代拍闸门**。阶段 0 / 2 / 7 是人工闸门，必须创始人给「通过 / 驳回 / 需补充」。
   编排器遇到闸门只做两件事：落盘「待拍板」+ 推送通知给创始人，然后**停下**。
   拍板后由创始人执行 `opc go --decision …`，编排器再继续。
2. **不唤醒别的 Agent**。交接只走文件落盘 + 通知通道（`notify.channels`），
   禁止 Agent 互相 @ 唤醒。下一棒仍是**自己领活**（`opc queue --role <role> --claim`）。
3. **不跳步**。只推进一棒，复用 `opc advance` 的状态机与校验，不另造推进语义。
4. **不碰产出内容**。只读项目文档的 frontmatter 状态用于判断，不改任何阶段文档。

**它替代的是哪一步**：现状是 `opc done` → 创始人回「继续」→ `opc go`。
编排器把中间那句「继续」在**非闸门阶段**自动化；闸门阶段的话仍然要人说。

**形态**：可长驻轮询，也可单次跑（`--once`）。所有动作**幂等** —— 重复跑不会
重复推进、不会重复通知（推进靠回执 token + 状态变化双重判定；通知靠编排状态去重）。

**并发**：编排器本身也是并发写者（多个编排器 / 会话同时在跑），因此
- 业务状态**只由子进程 `opc.py advance` 写**（复用它已有的校验与锁）；
- 编排器只写自己的编排状态文件 `99_Agent配置/_编排状态.json`，写入进 `VaultLock`；
- **绝不在持有锁时调用子进程**（Vault 锁不可重入，跨进程会互等到超时）。

用法::

    python scripts/orchestrator.py --vault <vault> --once            # 跑一轮就退出
    python scripts/orchestrator.py --vault <vault> --interval 60     # 长驻轮询
    python scripts/orchestrator.py --vault <vault> --once --dry-run  # 只报告不动作
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import opc as opcd  # noqa: E402  （与 opc.py 同目录，import 只取函数，不跑 CLI）

ORCH_STATE_REL = "99_Agent配置/_编排状态.json"


# ---------------------------------------------------------------------------
# 编排状态（编排器自己的账本：去重 + 观测，不参与业务状态机）
# ---------------------------------------------------------------------------

def orch_state_path(vault):
    return Path(vault) / ORCH_STATE_REL


def read_orch(vault):
    p = orch_state_path(vault)
    if not p.exists():
        return {"version": 1, "projects": {}}
    try:
        d = json.loads(opcd.read_text(p))
    except (ValueError, OSError):
        return {"version": 1, "projects": {}}
    if not isinstance(d, dict):
        return {"version": 1, "projects": {}}
    d.setdefault("version", 1)
    d.setdefault("projects", {})
    return d


def write_orch(vault, st):
    """编排状态写入：**独立文件 + 锁内写**（不与业务状态混、不与业务锁嵌套）。"""
    with opcd.VaultLock(vault):
        opcd.write_text(orch_state_path(vault),
                        json.dumps(st, ensure_ascii=False, indent=2) + "\n")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 项目枚举与状态读取
# ---------------------------------------------------------------------------

def list_projects(vault):
    """项目枚举：以**产出索引**为准（v2.17 起目录名不再带 `P001_` 前缀）。

    靠「目录名像不像项目号」来认项目已经失效 —— 实测会把所有项目判成不存在，
    编排器直接不干活（静默失效比报错更难查）。
    """
    try:
        cfg = opcd.load_config(vault)
        return [pid for pid, _pdir in opcd.project_dirs(vault, cfg)]
    except Exception:
        return []


def project_finished(vault, proj):
    """项目是否已收尾 —— 收尾的项目**不再编排**（不推进、不提醒）。

    判据取**产出索引**的「状态」列 ==「已归档」，**不看 `0_约束卡.md` 的 status**：
    后者是**文档级**状态 —— 阶段 0 的产出文档就是约束卡本身，阶段 0 跑完它会变成
    「已完成」，与「项目已收尾」同值同形；拿它判断会把**刚跑完阶段 0、正要推进
    的项目**误判成已收尾而永远跳过 —— 编排器直接失效。
    项目级的收尾信号是索引状态「已归档」（`opc close --archive/--link` 写入，
    零岗位项目建项即置）。
    """
    try:
        rows = opcd.read_index(vault, opcd.load_config(vault))
    except Exception:
        return False
    key = opcd.project_key(proj)
    for r in rows:
        if opcd.project_key(r.get("project", "") or "") == key:
            return r.get("状态") == "已归档"
    return False


def run_advance(vault, project, token):
    """调 `opc.py advance` 推进一棒（**子进程** —— 避免与编排器的锁嵌套）。

    返回 (returncode, 合并输出)。业务状态由子进程写，编排器不碰。
    """
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "opc.py"),
           "--vault", str(vault), "advance",
           "--project", project, "--token", str(token or "")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           encoding="utf-8", errors="replace")
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 99, "调用 opc advance 失败：" + str(exc)


# ---------------------------------------------------------------------------
# 三个动作：闸门待拍板 / 自动推进 / 停滞提醒
# ---------------------------------------------------------------------------

def gate_action(vault, cfg, proj, state, rec, args, channel):
    """人工闸门：**只催拍板，绝不代拍**（拍板权始终在创始人手上）。"""
    token = str(state.get("token") or "")
    last_at = parse_iso(rec.get("gate_notified_at"))
    last_token = str(rec.get("gate_notified_token") or "")
    already = (last_token == token and token != "")
    if already and last_at is not None:
        elapsed = (datetime.now() - last_at).total_seconds()
        if elapsed < max(60, args.remind):
            return None          # 已通知过，还没到重推间隔
    i = state.get("stage", state.get("step"))
    notice = "\n".join([
        "【OPC 人工闸门待拍板】项目 " + proj + " · 阶段 " + str(i)
        + "（" + str(state.get("role", "")) + "）",
        "该阶段是人工闸门，需创始人给结论：通过 / 驳回 / 需补充。",
        "拍板命令：opc go --project " + proj + " --decision 通过",
        "（拍板权始终在创始人手上，§7；编排器不得代拍，§15.3）",
        "回执：" + token,
    ])
    if not args.dry_run:
        used, msg = opcd.push_notice(vault, cfg, channel, notice, proj, state.get("role"))
        rec["gate_notified_token"] = token
        rec["gate_notified_at"] = now_iso()
        rec["gate_channel"] = used
    return {"action": "闸门待拍板（已停下，等创始人）", "project": proj,
            "stage": i, "role": state.get("role"), "dry_run": bool(args.dry_run)}


def advance_action(vault, cfg, proj, state, rec, args, channel):
    """非闸门阶段：静置期满后**自动推进一棒**（替代创始人那句「继续」）。"""
    token = str(state.get("token") or "")
    old_step = state.get("step")
    done_at = parse_iso(state.get("done_at") or state.get("confirmed_at"))
    if args.min_wait > 0 and done_at is not None:
        waited = (datetime.now() - done_at).total_seconds()
        if waited < args.min_wait:
            return {"action": "待推进（静置期 " + str(int(args.min_wait - waited)) + " 秒）",
                    "project": proj, "stage": state.get("stage"), "pending": True}
    if args.dry_run:
        return {"action": "将推进一棒", "project": proj, "stage": state.get("stage"),
                "role": state.get("role"), "dry_run": True}
    rc, out = run_advance(vault, proj, token)
    new_state = opcd.read_handoff(vault, cfg, proj)
    # 判据看**状态变化**，不靠退出码（校验失败与闸门都返回 2，靠 stdout 猜不可靠）
    advanced = (new_state.get("status") == "进行中"
                and new_state.get("step") != old_step)
    turned_gate = (bool(new_state.get("gate")) and not bool(state.get("gate")))
    if advanced:
        rec["last_advanced_at"] = now_iso()
        rec["last_advanced_to_stage"] = new_state.get("stage")
        rec["last_advanced_to_role"] = new_state.get("role")
        notice = "\n".join([
            "【OPC 编排派活】项目 " + proj + " · 阶段 " + str(new_state.get("stage"))
            + " 已就绪",
            "接手职业：" + str(new_state.get("role", "")),
            "必读文件：" + str(new_state.get("doc", "")),
            "领活方式：opc queue --role " + str(new_state.get("role", "")) + " --claim"
            + "（pull 模式：自己领活，不由别的 Agent 唤醒，§15.3）",
        ])
        # 通道：显式指定优先，否则用配置里的 notify.channel（缺省 local）——
        # 「没指定通道」不等于「不通知」。
        used, msg = opcd.push_notice(vault, cfg, channel, notice, proj,
                                     new_state.get("role"))
        rec["dispatch_channel"] = used
        return {"action": "已推进一棒", "project": proj,
                "stage": new_state.get("stage"), "role": new_state.get("role")}
    if turned_gate:
        rec["gate_notified_at"] = now_iso()
        return {"action": "转人工闸门（已停下，等创始人拍板）", "project": proj,
                "stage": new_state.get("stage"), "role": new_state.get("role")}
    return {"action": "推进失败", "project": proj, "stage": state.get("stage"),
            "rc": rc, "error": (out or "")[-400:]}


def stall_action(vault, cfg, proj, state, rec, args, channel):
    """进行中但久无进展 → 提醒（不推进、不打断正在干活的人）。"""
    base = parse_iso(state.get("confirmed_at") or state.get("done_at"))
    if base is None:
        return None
    idle = (datetime.now() - base).total_seconds()
    if idle < max(60, args.idle_timeout):
        return None
    last_at = parse_iso(rec.get("stalled_notified_at"))
    if last_at is not None and (datetime.now() - last_at).total_seconds() < max(60, args.remind):
        return None
    notice = "\n".join([
        "【OPC 编排提醒】项目 " + proj + " · 阶段 " + str(state.get("stage"))
        + "（" + str(state.get("role", "")) + "）已 " + str(int(idle // 60)) + " 分钟无进展",
        "编排器不代推进、不代拍板；请确认该棒是否仍在进行。",
    ])
    if not args.dry_run:
        used, msg = opcd.push_notice(vault, cfg, channel, notice, proj, state.get("role"))
        rec["stalled_notified_at"] = now_iso()
        rec["stalled_channel"] = used
    return {"action": "停滞提醒", "project": proj, "stage": state.get("stage"),
            "role": state.get("role"), "idle_min": int(idle // 60),
            "dry_run": bool(args.dry_run)}


# ---------------------------------------------------------------------------
def _role_claimed(vault, proj, stage, role):
    """下一棒是否已经自己领活（领了 = 正在干活，不需要再敲门）。

    判据：任务池 `claims[role]` 的项目与阶段都匹配当前交接状态。
    读不到任务池时按「未领」处理（多敲一次只是多一条 @，不产生重复工作 ——
    真正防重复的是状态机：done 靠回执 token，claim 靠冲突检查）。
    """
    try:
        pool = opcd.read_task_pool(vault)
        c = (pool.get("claims") or {}).get(str(role))
        if not c:
            return False
        return (opcd.project_key(str(c.get("project") or "")) == opcd.project_key(proj)
                and str(c.get("stage")) == str(stage))
    except Exception:  # noqa: BLE001
        return False


def ring_action(vault, cfg, proj, state, rec, args, channel):
    """v2.29 重敲门铃：门铃发了但下一棒一直没来领活 → 按间隔有限次重敲。

    三护栏（防重复骚扰）：
      1. 已领活（任务池 claims 匹配项目+阶段）→ 不敲，交给停滞提醒；
      2. 距上次门铃不足 --remind 秒 → 不敲（与闸门 / 停滞提醒同一去重模式）；
      3. 同一棒累计 --max-rings 次 → 不再敲，升级为「叫不醒」提醒创始人（每棒只升一次）。
    门铃台账由 opc.wake_next_role 落在 `_编排状态.json` 的
    `projects/<项目>/doorbells/<阶段>/<角色>`（v2.29 起，每次真正尝试都记）。
    每轮只做文件读；真正敲门才发飞书消息 —— 对轮询性能无感。
    """
    role = str(state.get("role") or "")
    stage = state.get("stage", state.get("step"))
    if not role:
        return None
    if _role_claimed(vault, proj, stage, role):
        return None
    disp = (opcd.notify_conf(cfg).get("dispatch") or {})
    if (disp.get("enabled") is False or not opcd._dispatch_group_mode(disp)
            or not str(disp.get("chat_id") or "").strip()):
        return None                      # 门铃通路不存在，交给停滞提醒兜底
    key = str(stage) + "/" + role
    bells = rec.setdefault("doorbells", {})
    ent = bells.get(key) or {}
    last_at = parse_iso(ent.get("last_at"))
    count = int(ent.get("count") or 0)
    if last_at is not None and (datetime.now() - last_at).total_seconds() < max(60, args.remind):
        return None                      # 还没到重敲间隔
    max_rings = int(getattr(args, "max_rings", 3) or 3)
    if count >= max_rings:
        if rec.get("escalated_key") == key:
            return None                  # 本棒已升级过，后续靠停滞提醒
        notice = "\n".join([
            "【OPC 叫不醒升级】项目 " + proj + " · 阶段 " + str(stage)
            + "（" + role + "）门铃已敲 " + str(count) + " 次无人领活",
            "该角色可能掉线 / 没看到；请人工确认：",
            "  · 在群里直接 @" + role + " 手动开工；",
            "  · 或 opc release --role " + role + " 后换人接棒。",
        ])
        if not args.dry_run:
            opcd.push_notice(vault, cfg, channel, notice, proj, role)
            rec["escalated_key"] = key
            rec["escalated_at"] = now_iso()
        return {"action": "叫不醒升级提醒", "project": proj, "stage": stage,
                "role": role, "ring_count": count, "dry_run": bool(args.dry_run)}
    if args.dry_run:
        return {"action": "将重敲门铃", "project": proj, "stage": stage,
                "role": role, "ring_count": count, "dry_run": True}
    opcd.wake_next_role(vault, cfg, proj, role, state.get("doc"), stage)
    # wake_next_role 内部会把本次尝试写进 _编排状态.json（锁内读改写）；
    # 编排器 tick 末尾会整份回写该文件，所以这里必须把新账目并回内存 rec，
    # 否则会把刚记的门铃台账冲掉。
    fresh = read_orch(vault)
    fbells = ((fresh.get("projects") or {}).get(opcd.project_key(proj)) or {}).get("doorbells") or {}
    if fbells:
        bells.update(fbells)
    ent2 = bells.get(key) or ent
    return {"action": "重敲门铃", "project": proj, "stage": stage, "role": role,
            "ring_count": int(ent2.get("count") or count + 1)}


# 一轮 tick
# ---------------------------------------------------------------------------

def tick(vault, args):
    cfg = opcd.load_config(vault)
    channel = args.channel or opcd.notify_conf(cfg).get("channel", "local")
    orch = read_orch(vault)
    actions = []
    for proj in list_projects(vault):
        # 已收尾的项目不再编排（避免对着结束的项目推「停滞」假提醒）
        if project_finished(vault, proj):
            continue
        state = opcd.read_handoff(vault, cfg, proj)
        if not state:
            continue
        status = state.get("status")
        rec = orch["projects"].setdefault(opcd.project_key(proj), {})
        if status == "待确认":
            if state.get("gate"):
                a = gate_action(vault, cfg, proj, state, rec, args, channel)
            else:
                a = advance_action(vault, cfg, proj, state, rec, args, channel)
        elif status == "进行中":
            a = ring_action(vault, cfg, proj, state, rec, args, channel)
            if a is None:
                a = stall_action(vault, cfg, proj, state, rec, args, channel)
        else:
            a = None
        if a:
            actions.append(a)
    orch["ticks"] = int(orch.get("ticks", 0)) + 1
    orch["last_tick"] = now_iso()
    if not args.dry_run:
        write_orch(vault, orch)
    return actions


def report(actions, as_json):
    if as_json:
        print(json.dumps({"actions": actions}, ensure_ascii=False, indent=2))
        return
    if not actions:
        print("编排器：一切正常，没有需要动作的项目（无待推进、无待拍板、无停滞）。")
        return
    print("编排器动作：")
    for a in actions:
        line = "  - " + str(a.get("action")) + "：项目 " + str(a.get("project"))
        if a.get("stage") is not None:
            line += " · 阶段 " + str(a.get("stage"))
        if a.get("role"):
            line += " · " + str(a.get("role"))
        if a.get("idle_min") is not None:
            line += " · 停滞 " + str(a.get("idle_min")) + " 分钟"
        if a.get("error"):
            line += "\n      " + str(a.get("error")).replace("\n", "\n      ")
        print(line)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="OPC 外部编排器：读交接状态 → 非闸门自动推进一棒 → 闸门停下等创始人拍板")
    ap.add_argument("--vault", default=None, help="Vault 根目录")
    ap.add_argument("--once", action="store_true", help="跑一轮就退出（默认长驻轮询）")
    ap.add_argument("--interval", type=float, default=60.0, help="轮询间隔秒（默认 60）")
    ap.add_argument("--min-wait", type=float, default=30.0,
                    help="非闸门阶段：报完成后再等多少秒才自动推进（默认 30，给创始人留喊停窗口）")
    ap.add_argument("--remind", type=float, default=900.0,
                    help="同一闸门/停滞的重复提醒间隔秒（默认 900）")
    ap.add_argument("--idle-timeout", type=float, default=1800.0,
                    help="进行中多久无进展算停滞（秒，默认 1800）")
    ap.add_argument("--max-rings", type=int, default=3,
                    help="同一棒重敲门铃次数上限（默认 3；超限升级提醒创始人，不再敲）")
    ap.add_argument("--channel", default=None, help="通知通道名；默认取配置里的 notify.channel")
    ap.add_argument("--project", default=None, help="只处理指定项目（默认全部项目）")
    ap.add_argument("--dry-run", action="store_true", help="只报告不动作（不推进、不写任何文件）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出动作列表")
    args = ap.parse_args(argv)
    if not args.vault:
        print("错误：必须带 --vault <Vault 路径>")
        return 2
    vault = Path(args.vault).resolve()
    if not (vault / "30_项目").exists():
        print("错误：不是 OPC Vault（缺 30_项目）：" + str(vault))
        return 2
    if args.project:
        args.project = opcd.project_key(args.project)
    print("外部编排器启动：" + str(vault)
          + "｜模式 " + ("单次" if args.once else "常驻")
          + "｜静置 " + str(int(args.min_wait)) + "s｜提醒 " + str(int(args.remind)) + "s"
          + "｜重敲门铃 ≤" + str(int(getattr(args, "max_rings", 3) or 3)) + "次/棒"
          + ("｜dry-run（不动作）" if args.dry_run else ""))
    while True:
        actions = tick(vault, args)
        report(actions, args.json)
        if args.once:
            return 0
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())
