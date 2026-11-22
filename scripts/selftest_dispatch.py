#!/usr/bin/env python3
"""v2.24 派活私聊通知自测：交互模型「角色主动私聊创始人 + 推进只回 pm」。

跑法：
    python scripts/selftest_dispatch.py

覆盖：
  1. dispatch 模块：.env 解析、union_id 兜底、凭证缺失降级、发送失败不抛异常
  2. opc 接线：dispatch_next_role_notice / dispatch_done_notice 已挂载到 cmd_go 与 cmd_advance
  3. 配置：notify.dispatch 段可被读取，founder_id_env 指向环境变量（凭证不入库）
  4. 幂等与降级：enabled=false / 无凭证 / 无创始人 ID 时均不抛异常、不阻塞
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  — " + str(detail)) if detail and not cond else ""))


def main() -> int:
    print("=" * 62)
    print("v2.24 派活私聊通知自测")
    print("=" * 62)

    # ---------- 1. dispatch 模块：.env 解析 ----------
    print("\n[1] dispatch.py · .env 解析与凭证读取")
    import dispatch as d

    with tempfile.TemporaryDirectory() as td:
        env_file = Path(td) / "pm" / ".env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(
            "# comment\n"
            "FEISHU_APP_ID=cli_xxx\n"
            "export FEISHU_APP_SECRET='secret-value'\n"
            'FEISHU_WEBHOOK_URL="https://example.com/hook"\n'
            "EMPTY_KEY=\n"
            "MALFORMED_LINE_NO_EQUALS\n",
            encoding="utf-8")
        got = d._read_env_file(env_file)
        check("读取普通键", got.get("FEISHU_APP_ID") == "cli_xxx")
        check("去掉 export 前缀", got.get("FEISHU_APP_SECRET") == "secret-value")
        check("去掉引号", got.get("FEISHU_WEBHOOK_URL") == "https://example.com/hook")
        check("空值保留为空", got.get("EMPTY_KEY") == "")
        check("无等号行忽略", "MALFORMED_LINE_NO_EQUALS" not in got)
        check("注释行忽略", not any(k.startswith("#") for k in got))

        # .env 不存在 → 返回空 dict，不抛异常
        check("缺失 .env 返回空", d._read_env_file(Path(td) / "nope" / ".env") == {})

        # union_id 兜底：进程环境变量优先，其次 .env
        os.environ.pop("FEISHU_FOUNDER_UNION_ID", None)
        uid = d.resolve_founder_id("pm", "FEISHU_FOUNDER_UNION_ID", Path(td))
        check("从 .env 兜底读到创始人 ID", uid == "")
        env2 = Path(td) / "pm" / ".env"
        env2.write_text("FEISHU_FOUNDER_UNION_ID=on_test_union\n", encoding="utf-8")
        uid2 = d.resolve_founder_id("pm", "FEISHU_FOUNDER_UNION_ID", Path(td))
        check("修改后读到 union_id", uid2 == "on_test_union")
        os.environ["FEISHU_FOUNDER_UNION_ID"] = "on_from_process"
        uid3 = d.resolve_founder_id("pm", "FEISHU_FOUNDER_UNION_ID", Path(td))
        check("进程环境变量优先", uid3 == "on_from_process")
        os.environ.pop("FEISHU_FOUNDER_UNION_ID", None)

    # ---------- 2. dispatch：降级路径不抛异常 ----------
    print("\n[2] dispatch.py · 降级路径（不抛异常、不阻塞）")
    with tempfile.TemporaryDirectory() as td:
        res = d.notify_next_role("pm", "", "标题", ["正文"], profile_root=Path(td))
        check("无创始人 ID → 返回失败而非抛异常", res.get("ok") is False)
        check("失败带原因", bool(res.get("detail")))

        # 有创始人 ID 但无凭证
        res2 = d.notify_next_role("pm", "on_x", "标题", ["正文"], profile_root=Path(td))
        check("无应用凭证 → 返回失败", res2.get("ok") is False)
        check("提示是缺凭证", "凭证" in str(res2.get("detail")) or "FEISHU" in str(res2.get("detail")))

    # ---------- 3. opc 接线 ----------
    print("\n[3] opc.py · 接线与配置")
    import opc

    check("dispatch_next_role_notice 已定义", hasattr(opc, "dispatch_next_role_notice"))
    check("dispatch_done_notice 已定义", hasattr(opc, "dispatch_done_notice"))
    check("dispatch_founder_id 已定义", hasattr(opc, "dispatch_founder_id"))

    src = Path(HERE / "opc.py").read_text(encoding="utf-8")
    # 两处推进点都要调用派活通知
    check("cmd_go 调用派活通知", "dispatch_next_role_notice(vault, cfg, project, next_role2" in src)
    check("cmd_advance 调用派活通知",
          src.count("dispatch_next_role_notice(vault, cfg, project, next_role2") >= 2)
    check("cmd_done 调用完成汇报", "dispatch_done_notice(vault, cfg, project, owner" in src)
    # 完成汇报用「本角色」身份
    check("完成汇报用 owner 身份", "dispatch_founder_id(disp, owner)" in src)
    check("派活通知用 next_role 身份", "dispatch_founder_id(disp, next_role)" in src)

    # 降级：enabled=false / 无 ID → 不抛异常。
    # **必须把 profile_root 指向空临时目录**，否则会读到本机真实的 .env 并真的发出私聊。
    with tempfile.TemporaryDirectory() as td:
        empty_root = Path(td) / "profiles"
        empty_root.mkdir()
        cfg = {"notify": {"enabled": True, "channel": "local",
                          "channels": {"local": {"type": "local", "file": "x.md"}},
                          "dispatch": {"enabled": False}}}
        r = opc.dispatch_next_role_notice(Path(td), cfg, "P001", "strategist", "0_约束卡.md", 0)
        check("enabled=false → 不抛异常", isinstance(r, dict))

        cfg2 = {"notify": {"enabled": True, "channel": "local",
                           "channels": {"local": {"type": "local", "file": "x.md"}},
                           "dispatch": {"enabled": True,
                                        "founder_id_env": "FEISHU_FOUNDER_UNION_ID",
                                        "profile_root": str(empty_root)}}}
        os.environ.pop("FEISHU_FOUNDER_UNION_ID", None)
        r2 = opc.dispatch_next_role_notice(Path(td), cfg2, "P001", "strategist", "0_约束卡.md", 0)
        check("无创始人 ID → 不抛异常", isinstance(r2, dict))
        check("无创始人 ID → 未发出（ok=False）", r2.get("ok") is False)
        r3 = opc.dispatch_done_notice(Path(td), cfg2, "P001", "strategist", 0,
                                      Path(td) / "a.md", [], "下一棒", False, "abc1234")
        check("完成汇报无 ID → 不抛异常", isinstance(r3, dict))
        check("完成汇报无 ID → 未发出（ok=False）", r3.get("ok") is False)

    # ---------- 4. 凭证不入库 ----------
    print("\n[4] 安全：凭证与 ID 不入库")
    cfg_path = HERE.parent / "opc.config.json"
    if cfg_path.exists():
        raw = cfg_path.read_text(encoding="utf-8")
        check("模板无 FEISHU_APP_SECRET 值", "FEISHU_APP_SECRET=" not in raw)
        check("模板无 app_secret 明文", "cli_" not in raw)
        check("dispatch 段只引用变量名", "founder_id_env" in raw)
    else:
        check("模板文件存在", False, "找不到 opc.config.json")

    # ---------- 汇总 ----------
    print("\n" + "=" * 62)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAILED: " + f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
