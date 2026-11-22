#!/usr/bin/env python3
"""飞书长连接「收确认」桥（可选组件）。

职责单一：用飞书 SDK 的 WebSocket 长连接接收创始人回复的「继续 / 通过 / 驳回」，
代为执行 `opc go` 推进工作流，并把结果回发到原会话。
**发送交接通知不经过这里**——发通知仍走 notify.channel（webhook / OpenAPI）。

环境变量（值不写入任何配置文件）：
  FEISHU_APP_ID / FEISHU_APP_SECRET   必填，企业自建应用凭证
  FEISHU_ALLOWED_OPEN_ID              可选，允许推进工作流的用户 open_id，多个用逗号分隔
  OPC_VAULT                           可选，Vault 路径；也可用 --vault 指定

长连接仅支持企业自建应用；需在开发者后台开启机器人能力、订阅 im.message.receive_v1，
并在「事件订阅」里选择「使用长连接接收事件」。事件须在 3 秒内处理完，本脚本只做一次
本地调用，不做耗时计算。未安装 lark-oapi 时主链路完全不受影响（降级为手动 opc go）。
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    import lark_oapi as lark
except ImportError:
    lark = None

OPC_CLI = Path(__file__).with_name("opc.py")


def parse_confirm(text):
    """纯函数：把自然语言回复解析成 (action, decision)，便于单测。"""
    t = (text or "").strip().lower()
    if not t:
        return None, None
    if any(k in t for k in ("驳回", "不通过", "打回", "reject")):
        return "reject", "驳回"
    if any(k in t for k in ("需补充", "补充", "再改", "revise")):
        return "reject", "需补充"
    if any(k in t for k in ("通过", "批准", "同意", "approve", "ok")):
        return "continue", "通过"
    if any(k in t for k in ("继续", "下一步", "下一个", "next", "go")):
        return "continue", None
    return None, None


def run_go(vault, decision, opc_cli):
    cmd = [sys.executable, str(opc_cli), "--vault", str(vault), "--non-interactive", "go"]
    if decision:
        cmd += ["--decision", decision]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=25)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return "opc go 执行失败：" + str(exc)


def tenant_token():
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8")).get("tenant_access_token", "")


def reply(chat_id, text):
    """回发结果摘要；失败不影响已完成的推进。"""
    try:
        token = tenant_token()
        if not token:
            return
        body = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json; charset=utf-8"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:
        print("结果回发失败（推进已完成）：" + str(exc), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="飞书长连接接收工作流确认")
    ap.add_argument("--vault", default=os.environ.get("OPC_VAULT"), help="Vault 根目录")
    ap.add_argument("--opc", default=str(OPC_CLI), help="opc.py 路径；由 opc listen 自动注入")
    args = ap.parse_args()
    if not args.vault:
        print("请用 --vault 或环境变量 OPC_VAULT 指定 Vault 路径", file=sys.stderr)
        return 2
    if lark is None:
        print("未安装 lark-oapi；执行 pip install lark-oapi 后重试。"
              "未启用长连接时，可在工作台用自然语言让当前会话代为执行 opc go。", file=sys.stderr)
        return 2
    if not os.environ.get("FEISHU_APP_ID") or not os.environ.get("FEISHU_APP_SECRET"):
        print("缺少环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET", file=sys.stderr)
        return 2
    allowed = [x.strip() for x in os.environ.get("FEISHU_ALLOWED_OPEN_ID", "").split(",") if x.strip()]
    if not allowed:
        print("提示：未设置 FEISHU_ALLOWED_OPEN_ID，任何能联系到机器人的用户都可推进工作流。")

    def on_message(data):
        try:
            msg = data.event.message
            if msg.message_type != "text":
                return
            text = json.loads(msg.content).get("text", "").strip()
            sender = data.event.sender.sender_id.open_id
            chat_id = msg.chat_id
        except Exception:
            return
        if allowed and sender not in allowed:
            print("忽略非白名单发送者")
            return
        action, decision = parse_confirm(text)
        if not action:
            return
        print("收到确认：" + text + " → " + action + ("/" + decision if decision else ""))
        out = run_go(args.vault, decision, args.opc)
        print(out[-800:])
        reply(chat_id, "【OPC 工作流】" + (out[-800:] if out else "已处理"))

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message).build())
    cli = lark.ws.Client(
        lark.ClientOption.builder()
        .app_id(os.environ["FEISHU_APP_ID"])
        .app_secret(os.environ["FEISHU_APP_SECRET"]).build(),
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    print("飞书长连接已启动，等待创始人确认消息……")
    cli.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
