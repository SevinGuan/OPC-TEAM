#!/usr/bin/env python3
"""飞书自定义机器人通知桥：把交接通知文本发到群机器人。

仅依赖 Python 标准库。webhook URL 与可选签名密钥只从环境变量读取：
  FEISHU_WEBHOOK_URL     必填，飞书群机器人的 webhook 地址
  FEISHU_WEBHOOK_SECRET  选填，机器人开启「签名校验」时填写

用法：python notify_feishu.py <通知文本文件>
由 opc.config.json 的 notify.channels.feishu.push_cmd 调用，{file} 为通知文本。
同一套 push_cmd 机制也适用于企业微信 / 微信等任意 webhook 网关，换脚本即可。
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from pathlib import Path


def build_payload(text):
    payload = {"msg_type": "text", "content": {"text": text}}
    secret = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip()
    if secret:
        ts = str(int(time.time()))
        raw = (ts + "\n" + secret).encode("utf-8")
        sign = base64.b64encode(hmac.new(raw, digestmod=hashlib.sha256).digest()).decode("utf-8")
        payload["timestamp"] = ts
        payload["sign"] = sign
    return payload


def main():
    if len(sys.argv) < 2:
        print("用法: notify_feishu.py <通知文本文件>", file=sys.stderr)
        return 2
    url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not url:
        print("缺少环境变量 FEISHU_WEBHOOK_URL", file=sys.stderr)
        return 2
    text = Path(sys.argv[1]).read_text(encoding="utf-8")
    req = urllib.request.Request(
        url,
        data=json.dumps(build_payload(text), ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        print("飞书通知发送失败: " + str(exc), file=sys.stderr)
        return 1
    print("飞书通知已发送: " + body[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
