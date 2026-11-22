#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自测 v2.28「群内门铃 + 岗位主动交互」——防回归。

覆盖 4 组真实缺陷（均为 2026-11-20 实测踩坑）：
  W1 `opc new` 建项后必须给第一棒发门铃（否则第一棒永远没人叫醒）
  W2 `opc go` / `opc advance` 推进后必须给下一棒发门铃（否则链断在半路）
  W3 skill 必须提供 `opc ask`（岗位到了交互点要有办法 @ 创始人）
  W4 `profile_root` 必须 expanduser（配置写 "~/.hermes/profiles"，Path() 不展开波浪号）

用法：python3 scripts/selftest_wake.py
"""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dispatch  # noqa: E402
import opc  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):  # noqa: ANN001
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ " + name)
    else:
        FAIL += 1
        print("  ✗ " + name + ("  " + str(extra) if extra else ""))


def test_wake_on_new_and_go():
    """W1 / W2：建项与推进都要发门铃。"""
    print("\n[W1/W2] 门铃挂载点")
    src = (HERE / "opc.py").read_text(encoding="utf-8")

    # cmd_new 里必须有门铃
    i = src.index("def cmd_new(")
    j = src.index("def ", i + 10)
    body_new = src[i:j]
    check("cmd_new 末尾调用 wake_next_role", "wake_next_role(" in body_new)

    # cmd_go 里必须有门铃
    i = src.index("def cmd_go(")
    j = src.index("def ", i + 10)
    body_go = src[i:j]
    check("cmd_go 调用 wake_next_role", "wake_next_role(" in body_go)

    # cmd_advance 里必须有门铃
    i = src.index("def cmd_advance(")
    j = src.index("def ", i + 10)
    body_adv = src[i:j]
    check("cmd_advance 调用 wake_next_role", "wake_next_role(" in body_adv)

    # cmd_version_new（追加版本 / V2 迭代）也必须有门铃 —— 否则 V2 第一棒没人叫醒
    i = src.index("def cmd_version_new(")
    j = src.index("def ", i + 10)
    body_ver = src[i:j]
    check("cmd_version_new 调用 wake_next_role（V2 迭代第一棒也要被叫醒）",
          "wake_next_role(" in body_ver)

    # 拉取式路径（claim / release）**不该**有门铃：
    # claim 是「被叫醒的那一方」自己领活；release 是把任务放回池子。
    i = src.index("def cmd_claim(")
    j = src.index("def ", i + 10)
    check("cmd_claim 不发门铃（pull 式认领，自己领活）",
          "wake_next_role(" not in src[i:j])

    # wake_next_role 必须 @ 目标 open_id，而不是 app_id
    check("wake_next_role 用 bot_open_id 取 open_id",
          "bot_open_id(" in src)
    check("wake_next_role 用 pm 身份发（不是目标自己的身份）",
          '_profile_env("pm"' in src or '_profile_env(\'pm\'' in src)


def test_ask_command():
    """W3：opc ask 必须存在且用本岗身份 @ 创始人。"""
    print("\n[W3] opc ask（岗位主动交互）")
    check("opc.py 定义 cmd_ask", hasattr(opc, "cmd_ask"))
    src = (HERE / "opc.py").read_text(encoding="utf-8")
    check("ask 子命令已注册", 'add_parser("ask"' in src)
    check("ask 用本岗身份（_profile_env(role)）",
          "dispatch_founder_id(disp, role)" in src)
    check("ask 不推进游标（不含 go/advance 状态写入）",
          "cmd_ask" in src and "def cmd_ask(" in src)
    i = src.index("def cmd_ask(")
    j = src.index("def ", i + 10)
    body = src[i:j]
    check("ask 落盘交互留痕", "_交互留痕.md" in body)
    check("ask 不调用 opc done 语义（无资产快照）",
          "asset" not in body.lower() or "资产快照" not in body)


def test_expanduser():
    """W4：profile_root 必须展开 ~。"""
    print("\n[W4] profile_root expanduser")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "profiles"
        (root / "demo").mkdir(parents=True)
        (root / "demo" / ".env").write_text("FEISHU_APP_ID=cli_x\nFEISHU_APP_SECRET=s\n",
                                            encoding="utf-8")
        env = dispatch._profile_env("demo", root)
        check("绝对路径能读到 .env", env.get("FEISHU_APP_ID") == "cli_x", str(env))

        # 用 "~" 形式的 root：必须能展开
        fake_home = Path(td) / "home"
        (fake_home / ".hermes" / "profiles" / "demo").mkdir(parents=True)
        (fake_home / ".hermes" / "profiles" / "demo" / ".env").write_text(
            "FEISHU_APP_ID=cli_y\n", encoding="utf-8")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(fake_home)
        try:
            env2 = dispatch._profile_env("demo", "~/.hermes/profiles")
            check("波浪号 root 也能读到 .env（必须 expanduser）",
                  env2.get("FEISHU_APP_ID") == "cli_y", str(env2))
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home


def test_no_self_send():
    """防自发自收：发送方与被 @ 方同位必须被拒。"""
    print("\n[自发自收守卫]")
    check("send_group_at_open_id 支持 sender_open_id 参数",
          "sender_open_id" in (HERE / "dispatch.py").read_text(encoding="utf-8"))
    src = (HERE / "dispatch.py").read_text(encoding="utf-8")
    check("同位时抛错", "自己发的" in src)


def main():
    print("=" * 62)
    print("自测 v2.28 · 群内门铃与岗位主动交互")
    print("=" * 62)
    test_wake_on_new_and_go()
    test_ask_command()
    test_expanduser()
    test_no_self_send()
    print("\n" + "=" * 62)
    print("通过 %d / 失败 %d" % (PASS, FAIL))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
