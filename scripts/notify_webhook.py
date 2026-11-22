#!/usr/bin/env python3
"""通用 Webhook 通知桥：把交接通知文本发到任意 HTTP webhook（钉钉 / 云效 / Slack / 自建网关等）。

用法（由 opc.config.json 的 notify.channels.<name>.push_cmd 调用）：
    python notify_webhook.py --file <通知文本文件> [--url <webhook>] [--kind dingtalk|yunxiao|slack|feishu|raw]

环境变量（凭证只走环境变量，不写入配置与产出）：
    OPC_WEBHOOK_URL      通用 webhook 地址（--url 未给时读它）
    OPC_WEBHOOK_KIND     消息体格式（--kind 未给时读它；默认 raw）
    或按通道专用：
    FEISHU_WEBHOOK_URL   （kind=feishu 时使用，兼容原飞书桥）
    DINGTALK_WEBHOOK_URL （kind=dingtalk 时使用）
    YUNXIAO_WEBHOOK_URL  （kind=yunxiao 时使用）

说明：
- 只发文本，不做任何账号代持或群发；失败以非零退出码返回，由 opc 自动降级为 local，通知不丢。
- 消息体按 kind 适配：feishu/dingtalk/slack 用各自的 text 字段；raw 直接 POST {"text": "..."}。
"""
import argparse
import json
import os
import sys
import urllib.request

KIND_ENV = {
    "feishu": "FEISHU_WEBHOOK_URL",
    "dingtalk": "DINGTALK_WEBHOOK_URL",
    "yunxiao": "YUNXIAO_WEBHOOK_URL",
}


def build_payload(kind: str, text: str) -> dict:
    if kind == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    if kind == "slack":
        return {"text": text}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    # raw / yunxiao / 自建网关：最常见的最小约定
    return {"text": text}


def main() -> int:
    ap = argparse.ArgumentParser(description="通用 webhook 通知桥")
    ap.add_argument("--file", required=True, help="通知文本文件路径")
    ap.add_argument("--url", default=None, help="webhook 地址；省略则读环境变量")
    ap.add_argument("--kind", default=None,
                    choices=["dingtalk", "yunxiao", "slack", "feishu", "raw"],
                    help="消息体格式；省略则读 OPC_WEBHOOK_KIND，默认 raw")
    args = ap.parse_args()

    kind = args.kind or os.environ.get("OPC_WEBHOOK_KIND") or "raw"
    url = args.url or os.environ.get("OPC_WEBHOOK_URL")
    if not url:
        # 按 kind 找专用变量，再退回任意 *_WEBHOOK_URL
        url = os.environ.get(KIND_ENV.get(kind, ""), "")
    if not url:
        for v in ("FEISHU_WEBHOOK_URL", "DINGTALK_WEBHOOK_URL", "YUNXIAO_WEBHOOK_URL"):
            if os.environ.get(v):
                url = os.environ[v]
                break
    if not url:
        print("未配置 webhook 地址（OPC_WEBHOOK_URL 或对应 *_WEBHOOK_URL）；"
              "已跳过推送，opc 将降级为 local。", file=sys.stderr)
        return 2

    try:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        print("读取通知文件失败：" + str(exc), file=sys.stderr)
        return 2

    payload = build_payload(kind, text)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "ignore")
        print("已推送（kind=" + kind + "）：" + body[:200])
        return 0
    except Exception as exc:  # noqa: BLE001 - 桥接脚本，失败即返回非零让 opc 降级
        print("推送失败（kind=" + kind + "）：" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
