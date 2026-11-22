#!/usr/bin/env python3
"""派活通知：用**下一棒角色自己的**飞书应用身份，私聊创始人告知「轮到你开工」。

v2.24（创始人拍板 2026-11-19）：交互模型改为**角色主动找创始人**，创始人只与 pm 对话推进。

    ┌─ 创始人 ─┐
    │  私聊回复  │  ← 干活的角色主动私聊创始人
    └───────────┘
    ┌─ 创始人 ─┐
    │ @pm「推进」│  ← 推进永远只回 pm（pm 是唯一调度中枢，§15.15）
    └───────────┘

设计要点：
- **用角色自己的应用身份发**：消息在创始人眼里就是「那个角色来找我了」，
  与角色本人说话没有割裂感；也让消息来源与该角色的权限边界一致。
- **私聊而非群聊**：飞书私聊不需要 @（适配器 `if not is_group:` 分支不走 mention 检查），
  创始人回一句就能继续，不必再点一次 @。
- **凭证只走环境变量**：本模块只读环境变量名，不写任何密钥到文件或输出。
- **失败不阻塞**：私聊发不出去时打印提示并返回非零，流程照常推进（§15.12 降级）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

# 角色的飞书凭证从哪读：环境变量优先，其次按约定的 profile 目录读 .env。
# 只读键名对应的那一行，**不回显值**。
DEFAULT_PROFILE_ROOT = Path.home() / ".hermes" / "profiles"


def _read_env_file(path: Path) -> dict:
    """读 .env → dict（只取 KEY=VALUE，不求值、不展开变量）。"""
    out = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def _profile_env(role: str, profile_root: Path | str | None = None) -> dict:
    """读某角色的 profile .env（凭证来源）。"""
    root = Path(profile_root) if profile_root else DEFAULT_PROFILE_ROOT
    # **必须展开 ~**：配置里的 profile_root 写的是 "~/.hermes/profiles"，
    # 而 Path("~/.hermes/profiles") 是**字面量相对路径**（不展开波浪号）→ 读不到任何 .env。
    # 实测踩坑（2026-11-20）：门铃发不出去、角色凭证取不到，都是这一处。
    root = root.expanduser()
    return _read_env_file(root / role / ".env")


def _token(app_id: str, app_secret: str, timeout: int = 15) -> str:
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    code = data.get("code")
    if code not in (0, None):
        raise RuntimeError("取 tenant_access_token 失败：code=%s msg=%s" % (code, data.get("msg")))
    token = data.get("tenant_access_token")
    if not token:
        raise RuntimeError("取 tenant_access_token 失败：响应里没有 token")
    return token


def send_dm(token: str, receive_id: str, text: str, timeout: int = 20,
            receive_id_type: str = "union_id") -> str:
    """用 tenant_access_token 给指定用户发私聊，返回 message_id。"""
    payload = {"receive_id": receive_id, "msg_type": "text",
               "content": json.dumps({"text": text}, ensure_ascii=False)}
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=" + receive_id_type,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("code") not in (0, None):
        raise RuntimeError("发私聊失败：code=%s msg=%s" % (data.get("code"), data.get("msg")))
    return str((data.get("data") or {}).get("message_id") or "")


def send_group_at(token: str, chat_id: str, founder_id: str, title: str,
                  body_lines: list, timeout: int = 20,
                  receive_id_type: str = "union_id") -> str:
    """在群里发一条 **@ 创始人** 的富文本消息（v2.27）。

    为什么群模式要 @：`require_mention=true` 时，创始人回话只要 @ 某个角色，
    那个角色才收得到（其他角色不醒）——**交互阶段本来就该由创始人点名**。
    但**唤醒阶段**反过来：角色主动 @ 创始人，飞书会给创始人红点提醒，
    创始人**什么都不用点**就能知道「谁在叫我」。

    关键细节：at 节点的 user_id 用 **union_id**（跨应用统一）。
    实测：各角色 bot 用同一份 union_id @ 创始人均 code=0、提醒正常送达。
    """
    content = {"zh_cn": {
        "title": str(title),
        "content": [[
            {"tag": "at", "user_id": str(founder_id), "user_name": "创始人"},
            {"tag": "text", "text": "\n" + "\n".join(str(x) for x in body_lines)},
        ]],
    }}
    payload = {"receive_id": chat_id, "msg_type": "post",
               "content": json.dumps(content, ensure_ascii=False)}
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("code") not in (0, None):
        raise RuntimeError("发群消息失败：code=%s msg=%s" % (data.get("code"), data.get("msg")))
    return str((data.get("data") or {}).get("message_id") or "")


def bot_open_id(role: str, profile_root: Path | None = None,
                 app_id_env: str = "FEISHU_APP_ID",
                 app_secret_env: str = "FEISHU_APP_SECRET") -> str:
    """取某角色 bot 自己在**它自己视角**下的 open_id（跨应用可用于 @ 门铃）。

    为什么必须用「接收方自己的视角」：飞书 @ 门铃靠 mentions[].id.open_id 匹配，
    而接收方适配器比对的是**它自己** /bot/v3/info 的 open_id。发送方若用 app_id
    或别的应用的视角，发出去会被解析成空 user_id（实测 `"user_id":""`），
    门铃彻底失效 —— 这就是 2026-11-20 实测踩到的坑。
    """
    env = dict(os.environ)
    env.update(_profile_env(role, profile_root))
    app_id = (env.get(app_id_env) or "").strip()
    app_secret = (env.get(app_secret_env) or "").strip()
    if not app_id or not app_secret:
        return ""
    try:
        token = _token(app_id, app_secret)
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/bot/v3/info",
            headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return str(((data.get("bot") or {}).get("open_id") or "")).strip()
    except Exception:
        return ""


def send_group_at_open_id(token: str, chat_id: str, at_open_id: str,
                          title: str, body_lines: list, timeout: int = 20,
                          sender_open_id: str = "") -> str:
    """在群里用 open_id @ 一个 bot —— **门铃**（不是给创始人看的通知）。

    与 send_group_at 的区别：后者 @ 创始人（union_id），这里 @ 岗位 bot（open_id）。

    ⚠️ **发送方与被 @ 方必须是不同的 bot**（实测踩坑 2026-11-20）：
    用**目标自己的应用身份**给它发「补充指令」→ 飞书当作「自己发的消息」，
    接收侧按 `allow_bots` / 自身过滤规则**根本收不到**（日志里连 inbound 记录都没有）。
    所以：**指令必须由第三方（pm）身份发出**，或由命令本身以目标身份执行（如 `opc ask`）。
    传 `sender_open_id` 时会做同位检查，避免再次踩坑。
    """
    if sender_open_id and at_open_id and sender_open_id == at_open_id:
        raise ValueError(
            "拒绝发送：发送方与被 @ 方是同一个 bot（飞书会把消息判为「自己发的」而收不到）。"
            "请改用第三方身份发送，或让目标自己执行命令（如 opc ask）。")
    content = {"zh_cn": {"title": str(title), "content": [[
        {"tag": "at", "user_id": str(at_open_id), "user_name": "下一棒"},
        {"tag": "text", "text": "\n" + "\n".join(str(x) for x in body_lines)},
    ]]}}
    payload = {"receive_id": chat_id, "msg_type": "post",
               "content": json.dumps(content, ensure_ascii=False)}
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("code") not in (0, None):
        raise RuntimeError("发群门铃失败：code=%s msg=%s" % (data.get("code"), data.get("msg")))
    return str((data.get("data") or {}).get("message_id") or "")


def notify_next_role(role: str, founder_id: str, title: str, body_lines: list,
                    profile_root: Path | None = None,
                    app_id_env: str = "FEISHU_APP_ID",
                    app_secret_env: str = "FEISHU_APP_SECRET",
                    chat_id: str = "", mode: str = "dm") -> dict:
    """用 role 自己的应用身份通知创始人。

    两种模式（v2.27）：
    - ``mode="dm"``（默认）：**私聊**创始人。私聊不走 mention 检查，创始人回一句即可。
    - ``mode="group"``：**在群里 @ 创始人**。飞书给红点提醒，创始人不用点就知道
      「谁在叫我」；且因为 ``require_mention=true``，创始人后续回话时 @ 该角色，
      **只有那个角色会醒**（其他角色不受打扰）——这正是创始人要的分工：
      **唤醒不需要 @，交互才需要 @。**

    返回 {"ok": bool, "message_id": str, "detail": str}；失败时 ok=False，**不抛异常**
    —— 派活通知发不出去不该拦住流程推进（§15.12 降级）。
    """
    env = dict(os.environ)
    env.update(_profile_env(role, profile_root))
    app_id = (env.get(app_id_env) or "").strip()
    app_secret = (env.get(app_secret_env) or "").strip()
    if not app_id or not app_secret:
        return {"ok": False, "message_id": "",
                "detail": "角色 " + role + " 没有飞书应用凭证（" + app_id_env + "/"
                          + app_secret_env + "），跳过派活通知"}
    if not (founder_id or "").strip():
        return {"ok": False, "message_id": "", "detail": "未配置创始人用户 ID，跳过派活通知"}
    use_group = str(mode or "dm").lower() == "group"
    if use_group and not (chat_id or "").strip():
        return {"ok": False, "message_id": "",
                "detail": "群模式未配置 chat_id（notify.dispatch.chat_id），跳过群内通知"}
    try:
        token = _token(app_id, app_secret)
        if use_group:
            mid = send_group_at(token, chat_id, founder_id, title, body_lines)
            return {"ok": True, "message_id": mid,
                    "detail": "已用 " + role + " 的身份在群里 @ 创始人（message_id=" + mid + "）"}
        text = title + "\n" + "\n".join(str(x) for x in body_lines)
        mid = send_dm(token, founder_id, text)
        return {"ok": True, "message_id": mid,
                "detail": "已用 " + role + " 的身份私聊创始人（message_id=" + mid + "）"}
    except urllib.error.HTTPError as exc:
        detail = ("发群消息失败" if use_group else "发私聊失败") + "：HTTP " + str(exc.code)
        try:
            detail += " " + exc.read().decode("utf-8", "replace")[:160]
        except Exception:
            pass
        return {"ok": False, "message_id": "", "detail": detail}
    except Exception as exc:  # noqa: BLE001 - 通知失败不阻塞流程
        return {"ok": False, "message_id": "",
                "detail": ("发群消息失败" if use_group else "发私聊失败") + "：" + str(exc)}


def founder_id_from_env(var: str = "FEISHU_FOUNDER_UNION_ID") -> str:
    """创始人 ID（union_id，跨应用统一）——只读环境变量，不入库。"""
    return (os.environ.get(var) or "").strip()


def resolve_founder_id(role: str, var: str = "FEISHU_FOUNDER_UNION_ID",
                        profile_root: Path | None = None) -> str:
    """取创始人 ID：进程环境变量优先，其次该角色 profile 的 .env。

    为什么要有 .env 兜底：Hermes 的 terminal 工具只透传 `TERMINAL_*` 前缀的环境变量，
    角色会话里 `opc done` 读不到进程级的 FEISHU_FOUNDER_UNION_ID —— 与 FEISHU_APP_ID
    同理，必须从该身份自己的 .env 读（只读值，不回显，不写入任何文件）。
    """
    val = founder_id_from_env(var)
    if val:
        return val
    return (_profile_env(role, profile_root) or {}).get(var, "").strip()
