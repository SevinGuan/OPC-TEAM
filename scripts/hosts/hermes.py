#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿主适配器（示例实现）：把 OPC 职业落成**独立持久身份 + 独立记忆**的 Profile。

这是一个**宿主适配器**，不是一个 OPC 规则层文件：OPC 只定义「三条映射」，
「怎么建身份」由宿主自己决定。这里演示的是「Profile 型宿主」的落地方式 ——
命令行建身份 → 写人设文件 → 指向同一个 Vault →（可选）逐个配独立消息应用。

可插拔约定
----------
适配器实现以下接口（`scripts/provision_profiles.py` 按约定调用）：

    HOST_NAME / HOST_LABEL / CAN_CREATE
    detect()                      -> {"ok": bool, "home": str, "detail": str}
    profile_exists(role)          -> bool
    create_profile(role, desc, clone) -> {"ok": bool, "status": str, "message": str}
    write_identity(role, file, text)  -> {"ok": bool, "status": str, "message": str}
    set_workspace(role, path)     -> {"ok": bool, "status": str, "message": str}
    env_path(role)                -> Path | None
    messaging_command(role)       -> str
    setup_messaging(role)         -> {"ok": bool, "status": str, "message": str}
    messaging_states(roles)       -> {role: state}

换宿主只需另写一个同接口的文件放进 `scripts/hosts/`；**核心与规则层不用动**。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOST_NAME = "hermes"
HOST_LABEL = "Hermes（示例适配器）"
CAN_CREATE = True

# 消息应用凭证的键名（只用于**存在性检查与指纹查重**，绝不打印值）。
CREDENTIAL_KEYS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")

_STATE = {"home": None, "profiles_dir": None, "binary": None}


# ---------------------------------------------------------------- 探测

def _base_home(home: Path) -> Path:
    """宿主 home 可能已指向 profiles/<name>，回到基根。"""
    parts = home.parts
    if "profiles" in parts:
        i = parts.index("profiles")
        return Path(*parts[:i])
    return home


def detect() -> dict:
    binary = shutil.which("hermes")
    home = _base_home(Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")))
    profiles_dir = home / "profiles"
    _STATE.update({"home": home, "profiles_dir": profiles_dir, "binary": binary})
    detail = ("命令=" + str(binary) + " | home=" + str(home)
              + " | profiles=" + ("存在" if profiles_dir.is_dir() else "不存在"))
    return {"ok": bool(binary), "home": str(home), "detail": detail}


def _require_state() -> None:
    if _STATE["profiles_dir"] is None:
        detect()


# ---------------------------------------------------------------- 身份读写

def profile_exists(role: str) -> bool:
    _require_state()
    d = Path(_STATE["profiles_dir"]) / role
    if not d.is_dir():
        return False
    return any((d / n).exists() for n in ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db"))


def create_profile(role: str, description: str, clone: bool = True) -> dict:
    _require_state()
    cmd = [str(_STATE["binary"] or "hermes"), "profile", "create", role, "--description", description]
    if clone:
        cmd.append("--clone")
    label = "hermes profile create " + role + (" --clone" if clone else "") + " --description …"
    print("   $ " + label)
    if not _STATE.get("apply", True):
        return {"ok": True, "status": "planned", "message": "[dry-run] " + label}
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    for line in (out or err).splitlines()[-6:]:
        print("     " + line)
    if r.returncode != 0:
        return {"ok": False, "status": "failed", "reason": "create_failed",
                "message": "[x] 建身份失败，退出码 " + str(r.returncode)}
    return {"ok": True, "status": "created", "message": "身份已创建" + ("（继承当前身份配置/密钥）" if clone else "")}


def write_identity(role: str, filename: str, content: str) -> dict:
    _require_state()
    target = Path(_STATE["profiles_dir"]) / role / filename
    if not _STATE.get("apply", True):
        return {"ok": True, "status": "planned",
                "message": filename + " ← Bot 配置卡 §一 系统提示词（" + str(len(content)) + " 字）"
                           + "；[dry-run] 将写入 " + str(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_suffix("." + filename + ".bak-" + time.strftime("%Y%m%dT%H%M%SZ"))
        shutil.copy2(target, backup)
        print("     旧 " + filename + " 备份 → " + backup.name)
    target.write_text(content + "\n", encoding="utf-8")
    return {"ok": True, "status": "written",
            "message": filename + " ← Bot 配置卡 §一 系统提示词（" + str(len(content)) + " 字）"}


def set_workspace(role: str, path: str) -> dict:
    _require_state()
    cmd = [str(_STATE["binary"] or "hermes"), "-p", role, "config", "set", "terminal.cwd", str(path)]
    label = "hermes -p " + role + " config set terminal.cwd " + str(path)
    print("   $ " + label)
    if not _STATE.get("apply", True):
        return {"ok": True, "status": "planned", "message": "[dry-run] 工作目录 → " + str(path)}
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    for line in (out or err).splitlines()[-4:]:
        print("     " + line)
    if r.returncode != 0:
        return {"ok": False, "status": "failed", "reason": "set_workspace_failed",
                "message": "[x] 设置工作目录失败，退出码 " + str(r.returncode)}
    return {"ok": True, "status": "set", "message": "工作目录 → " + str(path) + "（必须与其它身份同一个 Vault）"}


def env_path(role: str) -> Path:
    _require_state()
    return Path(_STATE["profiles_dir"]) / role / ".env"


# ---------------------------------------------------------------- 消息网关

def messaging_command(role: str) -> str:
    return "hermes -p " + role + " gateway setup"


def _env_value(env_path: Path, key: str) -> str | None:
    """读一个 .env 值；只用于存在性与查重，绝不打印值。"""
    if not env_path.is_file():
        return None
    found = None
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            found = value or None
    return found


def credential_conflicts() -> list:
    """跨默认身份 / 全部命名身份查重；返回值指纹，不返回凭证原文。"""
    _require_state()
    home = Path(_STATE["home"])
    profiles_dir = Path(_STATE["profiles_dir"])
    envs = [("default", home / ".env")]
    if profiles_dir.is_dir():
        envs.extend((p.name, p / ".env") for p in sorted(profiles_dir.iterdir()) if p.is_dir())
    owners = {}
    for profile, path in envs:
        for key in CREDENTIAL_KEYS:
            value = _env_value(path, key)
            if value:
                owners.setdefault((key, value), []).append(profile)
    conflicts = []
    for (key, value), names in owners.items():
        if len(names) > 1:
            conflicts.append({
                "key": key,
                "profiles": names,
                "fingerprint": hashlib.sha256(value.encode("utf-8")).hexdigest()[:12],
            })
    return conflicts


def _print_conflicts(conflicts: list) -> None:
    print("[x] 检测到同一飞书凭证属于多个身份；这会导致 duplicate_credential，已停止消息网关配置。")
    for item in conflicts:
        print("    " + item["key"] + " | identities=" + ", ".join(item["profiles"])
              + " | 指纹=" + item["fingerprint"])
    print("    每个职业必须使用不同的飞书应用；脚本不会复制或覆盖凭证。")


def _run_setup_wizard(role: str) -> tuple[int, str]:
    """在 PTY 里跑 `hermes -p <role> gateway setup`，并按脚本喂「8 + 回车」。

    菜单项「8. Feishu / Lark」是 Hermes 各版本里的固定序号（实测输出：
    「(○)  8. 🪽 Feishu / Lark (not configured)」）。若向导结构变化导致
    选不中，返回的输出里会保留菜单文本，便于人工判断。
    """
    try:
        import importlib.util
        here = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("opc_pty", str(here / "pty_runner.py"))
        pty_runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pty_runner)
    except Exception as exc:
        # PTY 不可用 → 退回普通 subprocess（行为退回 v2.22：非交互，跳过）
        try:
            r = subprocess.run([str(_STATE["binary"] or "hermes"), "-p", role, "gateway", "setup"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except OSError as exc2:
            return 127, str(exc2)
    cmd = [str(_STATE["binary"] or "hermes"), "-p", role, "gateway", "setup"]
    return pty_runner.run_in_pty(cmd, ["8\n"], timeout=900.0)



def setup_messaging(role: str) -> dict:
    _require_state()
    conflicts = credential_conflicts()
    if conflicts:
        _print_conflicts(conflicts)
        return {"ok": False, "status": "blocked", "reason": "duplicate_credential",
                "message": "同一飞书凭证被多个身份共用，已停止"}

    path = env_path(role)
    if all(_env_value(path, key) for key in CREDENTIAL_KEYS):
        collab = ensure_collab_config(role)
        return {"ok": True, "status": "already_configured",
                "message": "已有完整凭证，保持原配置（不重建应用、不回显凭证）；"
                           + collab.get("detail", "")}

    print("   即将为该职业配置消息应用：一身份一应用（凭证不复用），扫码创建该职业自己的应用。")
    print("   若扫码不可用，可在向导中手工输入该职业独立应用的 APP_ID / APP_SECRET。")
    # v2.29.1：向导选完菜单后还有 **交互提示（App ID / APP_SECRET / 扫码确认）**，
    # 这些只能由创始人本人回答。无人值守环境（Agent 会话 / cron / 管道，父进程 stdin
    # 不是真终端）里启动它只会干等到超时（实测每个身份挂 ~15 分钟）。
    # 所以：stdin 不是 TTY 就**不启动**向导，改为返回 needs_founder + 后续命令，
    # 让创始人自己在真终端里逐身份跑（或走链接代建：创始人自己的浏览器建应用，
    # 发回凭证，Agent 写 .env —— v2.29.3，见下方路径 b 的实测原因）。
    if not sys.stdin.isatty():
        print("   [needs_founder] 当前是无人值守环境（stdin 非终端），已跳过交互向导（不挂起）。")
        print("   完成方式（二选一）：")
        print("   a) 创始人在真终端逐身份运行：hermes -p " + role + " gateway setup")
        print("   b) 链接代建（推荐）：Agent 把链接 + 应用名清单发给创始人，创始人**自己的浏览器**打开——")
        print("      https://open.feishu.cn/page/launcher?from=backend_oneclick")
        print("      （一键 Agent 应用：机器人能力/事件订阅/权限全部预配好，填名字点 Create 即成，")
        print("       替代旧的 6 步控制台清单）逐个建应用——**应用名随意**（绑定只认凭证")
        print("       不认名字；建议 OPC·<role> 只是为了对得上岗位）。同一入口地址建")
        print("       全部应用：登录态持久，建完一个接着填下一个；页面跳走就重开同一链接，")
        print("       不用重新扫码/登录。")
        print("      凭证回填用**结构化表单**（v2.29.4，到这一步就必须做好这个交互）：Agent 把包内")
        print("      assets/凭证回填表单模板.html 复制到 <Vault>/99_Agent配置/凭证回填表单.html，")
        print("      只改顶部 ROLES 数组为本轮 --roles 的岗位清单，用宿主内嵌预览（Hermes 桌面：")
        print("      ::preview 指令）随链接一起发给创始人；创始人建完应用把凭证填进表单点「发送给")
        print("      Agent」，凭证按「<role>: cli_xxx / <secret>」结构化发回；无法内嵌预览的宿主，")
        print("      表单自动退化为生成文本让创始人复制粘贴，流程不中断。Agent 收到后写入 "
              + str(env_path(role)))
        print("      并重跑 provision（幂等，已配置身份自动跳过）；值只落 .env，不回显。")
        print("      落盘后的**首响采集**（v2.29.4）：allowlist 未配时群里 @ 机器人必然无响应")
        print("      （安全设计：allowlist 空 = 拒收所有人；这是采集机会，不是故障）——让创始人")
        print("      @ 全部机器人发一条，Agent 从网关日志抓各应用视角 ou_xxx（Unauthorized user 行）")
        print("      与群 chat_id（Inbound group message 行），写 FEISHU_ALLOWED_USERS 与")
        print("      notify.dispatch.chat_id，约 30 秒重扫生效后再发一条确认有回复；创始人")
        print("      union_id 同场可取（lark-cli 用户身份读群成员 member_id_type=union_id）。")
        print("      ⚠️ Agent 不要用自己代管的浏览器登录控制台代建：无人值守会话里浏览器随时被回收，")
        print("      扫码确认发生在轮次间隙、接收页面已死，手机显示「成功」而浏览器永远收不到")
        print("      （v2.29.3 实测复现 3 次）。账号侧动作一律留在创始人手里。")
        print("   每个职业必须独立应用，凭证不复用。")
        return {"ok": True, "status": "needs_founder", "reason": "no_tty",
                "message": "无人值守环境已跳过向导；需创始人在真终端逐身份配置，"
                           "或走链接代建（创始人自己的浏览器建应用 + 凭证回填表单、Agent 写 .env）"}
    # v2.23：向导是 **curses 菜单**（prompt_toolkit raw mode），必须挂在真终端上
    # 才能收按键。普通 subprocess 没有 TTY → 读到空输入 → 菜单把空输入当「跳过」，
    # 向导秒退（实测 5 个身份全部「向导结束但未检测到完整的 FEISHU_APP_ID / APP_SECRET」）。
    # 现在用 scripts/pty_runner.py 给它一个 PTY，并按脚本喂「8 + 回车」选 Feishu/Lark。
    rc, out = _run_setup_wizard(role)
    if not out.strip():
        out = "（向导无输出）"
    # 状态判定仍以 .env 里是否真拿到凭证为准，不信任退出码。
    if not all(_env_value(path, key) for key in CREDENTIAL_KEYS):
        tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-4:])
        return {"ok": False, "status": "failed", "reason": "credentials_missing",
                "message": "向导结束但未检测到完整的 " + " / ".join(CREDENTIAL_KEYS)
                           + (("；向导末尾输出：\n" + tail) if tail else "")}
    conflicts = credential_conflicts()
    if conflicts:
        _print_conflicts(conflicts)
        return {"ok": False, "status": "failed", "reason": "duplicate_credential",
                "message": "配置后发现凭证重复，已停止"}
    # v2.27：凭证到位后，顺手把**多 Bot 协作必查配置**写好。
    # 不写这三个 → 群里 @ 该角色毫无反应（ALLOW_BOTS=none 时在 mention 检查前就被拒）。
    collab = ensure_collab_config(role)
    return {"ok": True, "status": "configured",
            "message": "已检测到该身份独立的飞书凭证（值未输出）；" + collab.get("detail", "")}


# v2.27：**多 Bot 协作必查配置**（不是凭证，是行为开关）。
# 不写这三个 → 群里 @ 任何角色都没反应（根因，见 references/命令与配置手册.md §10）。
COLLAB_ENV = {
    # 放行「被 @ 的机器人消息」；none 时 bot 消息在 mention 检查前就被拒
    "FEISHU_ALLOW_BOTS": "mentions",
    # 群里只在被 @ 时才响应 → 保证只有目标角色醒，其余不受打扰
    "FEISHU_REQUIRE_MENTION": "true",
    # 允许群内任意成员发言（默认 allowlist 会拦）
    "FEISHU_GROUP_POLICY": "open",
}

# v2.28：**allowlist 必须按应用分别填 open_id**（实测踩坑，最难自查的一个坑）。
# 背景：`FEISHU_ALLOWED_USERS` 匹配的是**该应用视角下的 open_id**（ou_ 开头），
# 而 open_id **按应用隔离** —— 同一个创始人，在 5 个职业应用下有 5 个不同的 ou_。
# union_id（on_ 开头）跨应用统一，但那是**派活私聊的收件人**（notify.dispatch 用），
# 填进 allowlist **不生效**。填错的症状：网关日志
#   `Unauthorized user: ou_xxx (None) on feishu`
# 群里 @ 该角色永远不醒，且界面上没有任何提示（opc done 照常成功）。
# 所以：**每个职业身份各填各的 open_id**，不许复制粘贴。
ALLOWLIST_ENV_KEY = "FEISHU_ALLOWED_USERS"


def resolve_open_id_for_role(role: str, app_id: str = "", app_secret: str = "") -> str:
    """取该角色应用视角下「创始人」的 open_id（ou_ 开头）。

    口径：open_id 按应用隔离，所以必须**用该角色自己的应用**去查创始人的 open_id，
    查到的值才能填进该角色的 FEISHU_ALLOWED_USERS。

    取值顺序：
      1) 该角色 .env 里已有的 FEISHU_ALLOWED_USERS（已是 ou_ 开头则直接采用）；
      2) 建应用时扫码人的 open_id（扫二维码的那位就是创始人）——从凭证旁的
         扫描产物取；
      3) 都取不到则返回 ""，由调用方提示操作者手工填。

    找不到就返回空串——**绝不猜、绝不填 union_id**：填错比不填更难自查。
    """
    role_env = env_path(role)
    existing = _env_value(role_env, ALLOWLIST_ENV_KEY)
    if existing and existing.startswith("ou_"):
        return existing
    # 扫码产物：建应用时会把创始人的 open_id 落在 profile 目录里
    for candidate_name in ("feishu_registration.json", ".feishu_registration.json",
                            "feishu_open_id.txt"):
        candidate = role_env.parent / candidate_name
        if candidate.is_file():
            try:
                raw = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if raw.startswith("ou_"):
                return raw
            # 可能是 JSON：找第一个 ou_ 开头的字符串值
            for token in raw.replace('"', " ").replace("'", " ").split():
                if token.startswith("ou_"):
                    return token
    return ""


def ensure_collab_config(role: str) -> dict:
    """给该角色写入多 Bot 协作必查配置（幂等）。

    返回 {"ok": bool, "written": [...], "detail": str}。
    **不写 union_id**（那是创始人标识，要按部署单独提供），只写行为开关 +
    本应用视角的 open_id allowlist（见 ALLOWLIST_ENV_KEY 注释）。
    """
    path = env_path(role)
    written = []
    for key, val in COLLAB_ENV.items():
        if _env_value(path, key) == val:
            continue
        if _set_env_value(path, key, val):
            written.append(key)
    # v2.28：allowlist（open_id，按应用隔离）。已有正确值则不动。
    allow = _env_value(path, ALLOWLIST_ENV_KEY)
    if not allow or not allow.startswith("ou_"):
        resolved = resolve_open_id_for_role(role)
        if resolved and _set_env_value(path, ALLOWLIST_ENV_KEY, resolved):
            written.append(ALLOWLIST_ENV_KEY)
        elif not allow:
            return {"ok": True, "written": written,
                    "detail": "已写入协作配置：" + "、".join(written)
                              + "；⚠️ 缺 FEISHU_ALLOWED_USERS（本应用视角的创始人 open_id，ou_ 开头）"
                              + " → 群里 @ 该角色不会有任何反应。"
                              + "取法：Hermes 向导里选「Only allow listed user IDs」会自动填；"
                              + "或从网关日志 `Unauthorized user: ou_xxx ... on feishu` 里取那个 ou_xxx。"}
    if written:
        return {"ok": True, "written": written,
                "detail": "已写入协作配置：" + "、".join(written)
                          + "（改 .env 后需从外部终端重启网关才生效）"}
    return {"ok": True, "written": [], "detail": "协作配置已就绪"}


def _set_env_value(env_path: Path, key: str, value: str) -> bool:
    """往 .env 写一个键（幂等：已存在且同值则不动；值不同则覆盖该行）。
    **只用于非凭证的协作开关**；凭证一律走向导，不经这里。
    """
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
        replaced = False
        for i, raw in enumerate(lines):
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, sep, _old = line.partition("=")
            if sep and name.strip() == key:
                lines[i] = key + "=" + value
                replaced = True
                break
        if not replaced:
            lines.append(key + "=" + value)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def messaging_states(roles: list) -> dict:
    """从运行态状态文件读连接状态；missing/retrying 不冒充 connected。"""
    _require_state()
    home = Path(_STATE["home"])
    state_path = home / "gateway_state.json"
    platforms = {}
    if state_path.is_file():
        try:
            platforms = json.loads(state_path.read_text(encoding="utf-8")).get("platforms") or {}
        except (OSError, ValueError):
            platforms = {}
    return {role: str((platforms.get(role + ":feishu") or {}).get("state") or "missing") for role in roles}


def setup(ctx: dict) -> None:
    """核心调用一次：把 apply 开关等上下文交给适配器。"""
    _STATE["apply"] = bool(ctx.get("apply", True))
    detect()
