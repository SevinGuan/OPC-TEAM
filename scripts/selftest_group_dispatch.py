#!/usr/bin/env python3
"""v2.27 群内唤醒自测：角色在群里 @ 创始人 + 创始人回话时 @ 该角色。

跑法：
    python scripts/selftest_group_dispatch.py

覆盖（全部在临时 Vault 上跑，**不真的发消息**）：
  1. dispatch.send_group_at 的 payload 结构正确（at 节点用 union_id）
  2. notify_next_role 支持 mode=group / mode=dm
  3. group 模式缺 chat_id → 报错不崩
  4. 群模式文案说「在这个群里 @ 我」；私聊模式文案说「在这个私聊里回」
  5. 派活 / 汇报通知都带项目号
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPC = HERE / "opc.py"
sys.path.insert(0, str(HERE))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  — " + str(detail)) if detail and not cond else ""))


def main() -> int:
    print("=" * 62)
    print("v2.27 群内唤醒自测")
    print("=" * 62)

    import opc
    import dispatch as dm

    # ---------- 1. send_group_at 的 payload 结构 ----------
    print("\n[1] send_group_at payload 结构")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["headers"] = req.headers

        class R:
            def read(self): return json.dumps({"code": 0, "data": {"message_id": "MID"}}).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()

    real_urlopen = dm.urllib.request.urlopen
    dm.urllib.request.urlopen = fake_urlopen
    try:
        mid = dm.send_group_at("TOK", "oc_chat", "on_union", "标题", ["正文一行", "正文两行"])
    finally:
        dm.urllib.request.urlopen = real_urlopen

    p = captured["payload"]
    check("receive_id_type=chat_id", "receive_id_type=chat_id" in captured["url"], captured["url"])
    check("msg_type=post", p.get("msg_type") == "post", p.get("msg_type"))
    check("receive_id 是群 id", p.get("receive_id") == "oc_chat", p.get("receive_id"))
    content = json.loads(p["content"])
    nodes = content["zh_cn"]["content"][0]
    check("第一个节点是 at", nodes[0].get("tag") == "at", nodes[0])
    check("at 用 union_id", nodes[0].get("user_id") == "on_union", nodes[0])
    check("正文跟在 at 后面", any(n.get("tag") == "text" for n in nodes), nodes)
    check("返回 message_id", mid == "MID", mid)

    # ---------- 2/3. notify_next_role 模式分支 ----------
    print("\n[2] notify_next_role：group / dm 两种模式")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "profiles"
        (root / "x").mkdir(parents=True)
        (root / "x" / ".env").write_text("FEISHU_APP_ID=cli_x\nFEISHU_APP_SECRET=sec\n", encoding="utf-8")

        # group 模式缺 chat_id → 报错不崩
        r = dm.notify_next_role("x", "on_u", "t", ["b"], profile_root=root, mode="group", chat_id="")
        check("group 缺 chat_id → 失败而非抛异常", r.get("ok") is False, r)
        check("报错文案提到 chat_id", "chat_id" in str(r.get("detail")), r.get("detail"))

        # 无凭证 → 失败不崩
        (root / "y").mkdir()
        (root / "y" / ".env").write_text("", encoding="utf-8")
        r2 = dm.notify_next_role("y", "on_u", "t", ["b"], profile_root=root, mode="group", chat_id="oc_x")
        check("无凭证 → 失败而非抛异常", r2.get("ok") is False, r2)

    # ---------- 4/5. 文案分支 ----------
    print("\n[3] 文案按 mode 分支（群模式说「@ 我」，私聊模式说「私聊里回」）")
    with tempfile.TemporaryDirectory() as td:
        v = Path(td) / "Vault"
        (v / "99_Agent配置").mkdir(parents=True)
        cfg = {"notify": {"enabled": True, "channel": "local",
                          "channels": {"local": {"type": "local", "file": "x.md"}},
                          "dispatch": {"enabled": True, "mode": "group",
                                       "chat_id": "oc_grp",
                                       "founder_id_env": "FEISHU_FOUNDER_UNION_ID",
                                       "profile_root": str(Path(td) / "profiles")}}}
        import os
        os.environ["FEISHU_FOUNDER_UNION_ID"] = "on_test"

        got = {}
        orig = dm.notify_next_role

        def fake(role, fid, title, lines, **kw):
            got["mode"] = kw.get("mode")
            got["chat_id"] = kw.get("chat_id")
            got["lines"] = list(lines)
            got["title"] = title
            return {"ok": True, "message_id": "M"}

        dm.notify_next_role = fake
        try:
            opc.dispatch_next_role_notice(v, cfg, "P001", "strategist", "0_约束卡.md", 0)
            grp = dict(got)
            got.clear()
            cfg["notify"]["dispatch"]["mode"] = "dm"
            opc.dispatch_next_role_notice(v, cfg, "P001", "strategist", "0_约束卡.md", 0)
            dmn = dict(got)
        finally:
            dm.notify_next_role = orig

        check("群模式传了 mode=group", grp.get("mode") == "group", grp.get("mode"))
        check("群模式传了 chat_id", grp.get("chat_id") == "oc_grp", grp.get("chat_id"))
        check("私聊模式传了 mode=dm", dmn.get("mode") == "dm", dmn.get("mode"))
        grp_txt = " ".join(grp.get("lines", []))
        dmn_txt = " ".join(dmn.get("lines", []))
        check("群文案说「在这个群里 @ 我」", "在这个群里 @ 我" in grp_txt, grp_txt[:150])
        check("私聊文案说「在这个私聊里」", "在这个私聊里" in dmn_txt, dmn_txt[:150])
        check("群文案提到项目号", "P001" in grp_txt, "")
        check("群标题含项目号", "P001" in grp.get("title", ""), grp.get("title"))

    print("\n" + "=" * 62)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAILED: " + f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
