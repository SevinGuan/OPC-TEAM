#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""职业身份（Profile）代建 —— 宿主无关的核心 + 可插拔宿主适配器。

做什么
------
把 OPC 的各职业在**宿主**里落成「独立身份」（一职业 = 一个持久身份 + 独立记忆）。
这是 `multi-profile` 运行模式的落地动作；`single` 模式不需要它。

三条映射（全部有出处，不臆造）
----------------------------
1. 身份 description ← 岗位卡 §1「职业定位」
2. 身份人设文件（SOUL.md）← `Bot配置卡_<role>.md` §一「系统提示词」整段（不裁剪，见配置卡第五节）
3. 工作目录（workspace / cwd）← Vault 绝对路径（**必须是同一个 Vault**，否则 §15.3 文件交接断）

宿主适配器（可插拔）
--------------------
核心只管「计划 → 落地 → 核验」，**具体怎么建身份由适配器决定**：

- `scripts/hosts/<适配器>.py` —— 一个文件一个适配器，实现
  `HOST_NAME` / `detect()` / `create_profile()` / `write_identity()` / `set_workspace()` /
  `setup_messaging()` / `messaging_states()`。
- `--host auto`（默认）：按序探测适配器，第一个探测通过即采用。
- `--host manual`：**通用回退** —— 不代建任何身份，只产出「落地清单」，
  任何宿主（哪怕不支持 Profile 概念）都能照着手工做。

消息网关（可选）
----------------
`--feishu-bots now` 让适配器逐个职业打开**宿主自己的**官方向导，为每个职业建**独立**的
飞书应用（一职业一应用，禁止复用凭证）；`later`（默认）只留下待配命令。
**云端仓库 ≠ 消息网关**：前者同步产出，后者用于收发消息，两者互不阻塞。

边界（宪法 §8.1）
----------------
不指定模型、不授予工具权限、不直接调用平台注册 API、不复制 / 不回显凭证、
不代扫码、不写业务文档。默认 **dry-run**，加 `--apply` 才执行。

用法
----
    python scripts/provision_profiles.py --vault <Vault>                       # 看计划（什么都不写）
    python scripts/provision_profiles.py --vault <Vault> --roles a,b,c --apply  # 真建身份
    python scripts/provision_profiles.py --vault <Vault> --update --apply       # 同步已有身份的 SOUL/cwd
    python scripts/provision_profiles.py --vault <Vault> --host manual --apply  # 产出落地清单（任何宿主）
    python scripts/provision_profiles.py --vault <Vault> --feishu-bots now      # 逐身份开消息网关向导
    python scripts/provision_profiles.py --vault <Vault> --json

只依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
OPC_PY = PKG_ROOT / "scripts" / "opc.py"
HOSTS_DIR = Path(__file__).resolve().parent / "hosts"

# 默认建议的职业范围（前段五岗）；其余岗位以后可单独补建。
RECOMMENDED_ROLES = ["strategist", "market", "pm", "design", "architect"]


def _force_utf8_io():
    """Windows 控制台默认 GBK，中文输出会花屏 —— 强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _width(text: str) -> int:
    """显示宽度：中日韩全角字符占 2 列（否则表格会歪）。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(1, width - _width(text))


# ---------------------------------------------------------------- vault / 角色

def load_vault_config(vault: Path) -> dict:
    p = vault / "opc.config.json"
    if not p.is_file():
        sys.exit("[x] 找不到 " + str(p) + "；先跑 opc init --vault " + str(vault))
    return json.loads(p.read_text(encoding="utf-8"))


def role_cards(vault: Path) -> list[Path]:
    d = vault / "99_Agent配置" / "岗位卡"
    return sorted(p for p in d.glob("*.md")) if d.is_dir() else []


def _clean_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def role_description(card: Path, limit: int = 150) -> str:
    """岗位卡 §1「职业定位」→ 身份 description。"""
    raw = card.read_text(encoding="utf-8")
    m = re.search(r"职业定位[：:]\s*(.+)", raw)
    if not m:
        return "OPC " + card.stem + " 岗位（取自岗位卡 §1）"
    desc = _clean_md(m.group(1).split("\n")[0])
    if len(desc) > limit:
        cut = desc[:limit]
        desc = cut[: cut.rfind("；")] if "；" in cut else cut
    return desc.rstrip("；，, 。") + "。"


def soul_prompt(vault: Path, role: str) -> str | None:
    """Bot 配置卡「一、系统提示词」的整段（不裁剪，见配置卡第五节）。"""
    card = vault / "99_Agent配置" / "Bot配置卡" / ("Bot配置卡_" + role + ".md")
    if not card.is_file():
        return None
    raw = card.read_text(encoding="utf-8")
    m = re.search(r"##\s*一[、.].*?```[a-zA-Z]*\r?\n(.*?)\r?\n```", raw, re.S)
    return m.group(1).strip() if m else None


def ensure_bot_cards(vault: Path, roles: list[str]) -> bool:
    """Bot 配置卡是 Vault 内的前置产物（不是宿主动作），dry-run 也一并导出。"""
    missing = [r for r in roles
               if not (vault / "99_Agent配置" / "Bot配置卡" / ("Bot配置卡_" + r + ".md")).is_file()]
    if not missing:
        return True
    print("→ 缺 " + str(len(missing)) + " 份 Bot 配置卡（" + ", ".join(missing) + "），先导出（Vault 内产物，dry-run 也做）")
    r = subprocess.run([sys.executable, str(OPC_PY), "bots", "--vault", str(vault)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print((r.stdout or "").strip() or (r.stderr or "").strip())
    return r.returncode == 0


# ---------------------------------------------------------------- 宿主适配器

def load_adapters() -> dict:
    """扫描 `scripts/hosts/*.py`：一个文件一个适配器（`_` 开头的是共享模块，跳过）。"""
    out = {}
    if not HOSTS_DIR.is_dir():
        return out
    for p in sorted(HOSTS_DIR.glob("*.py")):
        if p.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location("opc_host_" + p.stem, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as exc:  # 适配器坏了不该拖垮核心
            print("[!] 适配器 " + p.name + " 加载失败：" + str(exc))
            continue
        out[str(getattr(mod, "HOST_NAME", p.stem))] = mod
    return out


def pick_adapter(requested: str, adapters: dict):
    """返回 (适配器模块, 名字)。auto = 按序探测，第一个探测通过即采用。"""
    if requested == "auto":
        for name, mod in adapters.items():
            try:
                info = mod.detect() or {}
            except Exception as exc:
                print("[!] 适配器 " + name + " 探测异常：" + str(exc))
                continue
            if info.get("ok"):
                return mod, name
        return None, ""
    mod = adapters.get(requested)
    return (mod, requested) if mod else (None, requested)


# ---------------------------------------------------------------- 落地动作

def run_host_command(cmd: list[str], apply: bool, label: str) -> bool:
    print("   $ " + label)
    if not apply:
        return True
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    for line in (out or err).splitlines()[-6:]:
        print("     " + line)
    if r.returncode != 0:
        print("     [x] 退出码 " + str(r.returncode))
    return r.returncode == 0


def set_env_var(env_path, key: str, value: str, apply: bool) -> bool:
    """写 .env（同 key 覆盖，不重复）。只写变量名与路径，不涉及任何凭证。"""
    if env_path is None:
        print("   [!] 当前宿主适配器不提供 .env 位置，跳过 " + key)
        return False
    line = key + "=" + value
    print("   .env ← " + line)
    if not apply:
        return True
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    kept = [ln for ln in lines if not ln.strip().startswith(key + "=")]
    kept.append(line)
    env_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return True


def provision_roles(adapter, vault: Path, roles: list[str], args) -> list[dict]:
    """逐职业落地身份；具体动作全交给适配器。"""
    results = []
    can_create = bool(getattr(adapter, "CAN_CREATE", False))
    for role in roles:
        card = vault / "99_Agent配置" / "岗位卡" / (role + ".md")
        desc = role_description(card)
        prompt = soul_prompt(vault, role)
        print("\n● " + role)
        print("   description ← 岗位卡 §1：" + desc)
        if prompt is None:
            print("   [x] 取不到系统提示词（Bot 配置卡 §一 缺失），跳过该职业")
            results.append({"role": role, "status": "skipped", "reason": "no bot card", "description": desc})
            continue

        exists = bool(adapter.profile_exists(role))
        row = {"role": role, "profile": role, "description": desc}
        if exists and not args.update:
            print("   已存在身份 `" + role + "` → 跳过（要同步 SOUL/cwd 加 --update）")
            row["status"] = "exists"
            results.append(row)
            continue

        if not exists:
            if not can_create:
                print("   [i] 当前适配器不代建身份（通用回退）：只产出落地清单")
                row["status"] = "checklist"
                results.append(row)
                continue
            res = adapter.create_profile(role, desc, not args.no_clone) or {}
            print("   " + str(res.get("message", "")))
            if not res.get("ok"):
                row["status"] = "failed"
                row["reason"] = res.get("reason", "create_failed")
                results.append(row)
                continue
            row["status"] = "created" if args.apply else "planned"
        else:
            row["status"] = "updated" if args.apply else "planned(update)"

        res = adapter.write_identity(role, "SOUL.md", prompt) or {}
        print("   " + str(res.get("message", "")))
        if not res.get("ok") and args.apply:
            row["status"] = "failed"
            row["reason"] = res.get("reason", "write_identity_failed")
            results.append(row)
            continue

        res = adapter.set_workspace(role, str(vault)) or {}
        print("   " + str(res.get("message", "")))
        if not res.get("ok") and args.apply:
            row["status"] = "failed"
            row["reason"] = res.get("reason", "set_workspace_failed")
            results.append(row)
            continue

        if args.set_skill_dir:
            set_env_var(adapter.env_path(role), "OPC_SKILL_DIR", str(PKG_ROOT), args.apply)
        results.append(row)
    return results


def configure_messaging(adapter, roles: list[str], apply: bool) -> list[dict]:
    """消息网关：逐个职业配置消息应用（一身份一应用，禁止复用凭证）。

    注意：向导是 curses 菜单，**无 TTY 环境（Agent 会话）会假成功**——
    适配器会检测并跳过向导，返回 needs_founder。完成路径 b 是
    **链接代建 + 凭证回填表单**（v2.29.4）：Agent 发链接
    （open.feishu.cn/page/launcher?from=backend_oneclick，一键 Agent 应用，
    机器人能力/事件/权限预配）+ 应用名清单——**同一入口地址建全部应用**，
    登录态持久，建完一个接着填下一个，页面跳走重开同一链接即可，不用重新
    扫码/登录；同时把 assets/凭证回填表单模板.html 复制到
    <Vault>/99_Agent配置/凭证回填表单.html、改顶部 ROLES 数组为本轮 --roles，
    用宿主内嵌预览（Hermes 桌面 ::preview）随链接一起交付，创始人建完应用
    填表回传 App ID / Secret，Agent 写该身份 .env → 重跑本命令（幂等）。
    ⚠️ Agent 不要用自己代管的浏览器登录控制台代建：无人值守会话里浏览器随时
    被回收，扫码确认永远送不到（2026-11-21 Hermes 宿主实测复现 3 次）。
    建应用只能走控制台 UI：应用管理接口只收 tenant token，user token 报
    99991668 user access token not support —— 这不代表路不通，别误判后回头走截图。
    """
    print("\n── 消息网关：一身份一应用 ──")
    print("云端仓库 ≠ 消息网关；这里配置的是后者。一个宿主网关进程通常可承载全部身份。")
    if not apply:
        for role in roles:
            print("   [dry-run] " + str(adapter.messaging_command(role))
                     + "   # 在向导里选飞书/Lark 并扫码")
        return [{"role": r, "status": "planned"} for r in roles]
    rows = []
    for role in roles:
        res = adapter.setup_messaging(role) or {}
        status = str(res.get("status", "failed"))
        print("   " + role + "：" + str(res.get("message", "")))
        rows.append({"role": role, "status": status, "reason": res.get("reason", "")})
        if status == "blocked":
            for remaining in roles[roles.index(role) + 1:]:
                rows.append({"role": remaining, "status": "blocked", "reason": res.get("reason", "blocked")})
            break
    return rows


def deferred_messaging_plan(adapter, roles: list[str]) -> list[dict]:
    print("\n── 消息网关：稍后配置 ──")
    print("本次不创建消息应用，不影响身份、Vault 或云端仓库。以后逐个运行：")
    for role in roles:
        print("   " + str(adapter.messaging_command(role)) + "   # 一身份一应用，凭证不复用")
    return [{"role": r, "status": "deferred"} for r in roles]


def write_checklist(adapter, vault: Path, roles: list[str], results: list[dict], apply: bool) -> str:
    """通用回退（manual）：产出「落地清单」，任何宿主照着手工做。"""
    lines = ["# 职业身份落地清单（通用回退）", "",
             "> 由 `scripts/provision_profiles.py --host manual` 生成：**不代建任何身份**，",
             "> 只把三条映射写清楚，照着在宿主里手工做即可。",
             "> 硬前提：所有身份的工作目录必须指向**同一个 Vault**，否则 §15.3 文件交接断。", "",
             "Vault 绝对路径：" + str(vault), ""]
    by_role = {r["role"]: r for r in results}
    for role in roles:
        card = vault / "99_Agent配置" / "岗位卡" / (role + ".md")
        desc = role_description(card) if card.is_file() else ""
        prompt = soul_prompt(vault, role) or ""
        lines.append("## " + role)
        lines.append("")
        lines.append("- **description**（贴到身份的人设/描述栏）：" + desc)
        lines.append("- **工作目录 / workspace**（必须等于上面那个 Vault 绝对路径）：" + str(vault))
        lines.append("- **SOUL.md**（人设文件，整段贴入，不裁剪）：")
        lines.append("")
        lines.append("```")
        lines.append(prompt)
        lines.append("```")
        lines.append("")
        lines.append("- 状态：" + str(by_role.get(role, {}).get("status", "planned")))
        lines.append("")
    text = "\n".join(lines)
    target = vault / "99_Agent配置" / "Profile落地清单.md"
    if apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print("\n落地清单已写入：" + str(target))
    else:
        print("\n[dry-run] 落地清单将写入：" + str(target))
    return str(target)


# ---------------------------------------------------------------- 主流程

def main(argv: list[str] | None = None) -> int:
    _force_utf8_io()
    ap = argparse.ArgumentParser(description="把 OPC 职业落成独立身份（默认 dry-run；宿主可插拔）")
    ap.add_argument("--vault", required=True, help="Vault 绝对路径")
    ap.add_argument("--roles", default="", help="要落的职业（逗号分隔；缺省=全部岗位卡，脚本会提示推荐值）")
    ap.add_argument("--host", default="auto",
                    help="宿主适配器：auto（自动探测，默认）/ <适配器名> / manual（通用回退，只出落地清单）")
    ap.add_argument("--apply", action="store_true", help="真正执行（不加时只打印计划）")
    ap.add_argument("--update", action="store_true", help="身份已存在时也同步 SOUL.md 与工作目录")
    ap.add_argument("--no-clone", action="store_true", help="建身份时不继承当前身份的配置/密钥/技能")
    ap.add_argument("--set-skill-dir", action="store_true", help="在各身份的 .env 写 OPC_SKILL_DIR")
    ap.add_argument("--feishu-bots", choices=("later", "now"), default="later",
                    help="消息网关：later=只留后续命令（默认）；now=逐身份配置消息应用"
                           "（无 TTY 时向导会假成功，改走浏览器代建）")
    ap.add_argument("--gateway-verify-timeout", type=float, default=45,
                    help="配置后等待各身份消息通道连通的秒数（默认 45；0=只检查一次）")
    ap.add_argument("--json", action="store_true", help="输出结构化结果")
    args = ap.parse_args(argv)

    vault = Path(args.vault).expanduser().resolve()
    cfg = load_vault_config(vault)

    print("OPC → 独立身份（Profile）代建")
    print("Vault: " + str(vault))

    mode = str(cfg.get("agent_mode") or "single")
    print("运行模式: agent_mode = " + mode)
    if mode != "multi-profile":
        print("[x] 当前不是 multi-profile：一职业一个独立身份的形态没被启用。")
        print("    先执行: python " + str(OPC_PY) + " mode multi-profile --vault " + str(vault))
        return 3

    adapters = load_adapters()
    if not adapters:
        print("[x] 找不到宿主适配器（" + str(HOSTS_DIR) + "）；请检查脚本安装是否完整。")
        return 4
    adapter, host_name = pick_adapter(args.host, adapters)
    if adapter is None:
        print("[x] 找不到宿主适配器：" + args.host + "；可用：" + ", ".join(sorted(adapters)))
        return 4
    info = adapter.detect() or {}
    label = str(getattr(adapter, "HOST_LABEL", host_name))
    print("宿主适配器: " + label + "（" + host_name + "）")
    print("  探测: " + str(info.get("detail", "")))
    if not info.get("ok"):
        print("[x] 当前环境不可用该适配器。可改用 --host manual 产出落地清单（任何宿主通用），"
              "或改用 opc mode single。")
        return 4
    if hasattr(adapter, "setup"):
        try:
            adapter.setup({"vault": str(vault), "pkg_root": str(PKG_ROOT), "apply": bool(args.apply)})
        except Exception as exc:
            print("[!] 适配器初始化异常：" + str(exc))

    cards = role_cards(vault)
    if not cards:
        print("[x] 找不到岗位卡：" + str(vault / "99_Agent配置" / "岗位卡") + "（先跑 opc init）")
        return 5
    all_roles = [c.stem for c in cards]
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    if not roles:
        roles = all_roles
        print("[i] 未指定 --roles → 按全部岗位卡（" + str(len(roles)) + " 岗）准备。")
        print("    推荐只建前段五岗：--roles " + ",".join(RECOMMENDED_ROLES))
    unknown = [r for r in roles if r not in all_roles]
    if unknown:
        print("[x] 岗位未登记：" + ", ".join(unknown) + "；可选：" + ", ".join(all_roles))
        return 5

    ensure_bot_cards(vault, roles)

    results = provision_roles(adapter, vault, roles, args)

    messaging_roles = [r["role"] for r in results if r.get("status") not in ("failed", "skipped")]
    if args.feishu_bots == "now":
        messaging_results = configure_messaging(adapter, messaging_roles, args.apply)
    else:
        messaging_results = deferred_messaging_plan(adapter, messaging_roles)

    gateway_states = {}
    if args.apply and args.feishu_bots == "now":
        verify_roles = [r["role"] for r in messaging_results
                        if r.get("status") in ("configured", "already_configured")]
        if verify_roles:
            print("\n── 核验消息通道（最多等待 " + str(max(0.0, args.gateway_verify_timeout)) + " 秒）──")
            gateway_states = adapter.messaging_states(verify_roles) or {}
            for role in verify_roles:
                print("   " + role + " = " + str(gateway_states.get(role, "missing")))
            if any(gateway_states.get(r) != "connected" for r in verify_roles):
                print("   [!] 尚未全部 connected：请确认消息应用已发布/获批，且宿主主网关正在运行。")

    checklist = ""
    if not getattr(adapter, "CAN_CREATE", False):
        checklist = write_checklist(adapter, vault, roles, results, args.apply)

    summary = {"vault": str(vault), "mode": mode, "applied": bool(args.apply),
               "host": host_name, "host_label": label, "results": results,
               "messaging": args.feishu_bots, "messaging_results": messaging_results,
               "gateway_states": gateway_states, "checklist": checklist}
    print("\n── 汇总 ──")
    print(_pad("role", 12) + _pad("身份状态", 18) + _pad("消息网关", 20) + "通道")
    messaging_by_role = {r["role"]: r for r in messaging_results}
    for r in results:
        role = r["role"]
        messaging = messaging_by_role.get(role, {})
        print(_pad(role, 12) + _pad(str(r.get("status")), 18)
              + _pad(str(messaging.get("status", "")), 20) + str(gateway_states.get(role, "")))
    if args.apply:
        print("\n下一步核验：")
        print("  查看身份列表与工作目录是否都指向同一个 Vault（各宿主命令不同，按其官方文档）")
        print("  python " + str(OPC_PY) + " handoff --vault " + str(vault))
    else:
        print("\n[提醒] 这是 dry-run，什么都没写。确认无误后加 --apply 执行。")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    profiles_ok = all(r.get("status") not in ("failed", "skipped") for r in results)
    messaging_ok = all(r.get("status") not in ("failed", "blocked") for r in messaging_results)
    gateway_ok = all(state == "connected" for state in gateway_states.values())
    return 0 if profiles_ok and messaging_ok and gateway_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
