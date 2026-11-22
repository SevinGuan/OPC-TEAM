#!/usr/bin/env python3
"""v2.26 多项目防错闸自测：私聊场景下不能推进错项目。

跑法：
    python scripts/selftest_multiproject.py

覆盖：
  1. 多项目待确认 + 未显式指定项目 → **报错**（不猜、不推进错）
  2. 显式 --project 指向正确项目 → 只推进那一个
  3. 单项目场景（无 --project）→ 仍然照常工作（不破坏既有行为）
  4. 派活 / 汇报通知文本里**带项目号**
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def make_cfg(vault):
    cfg = {
        "schema": "opc.config/v2", "default_backend": "local",
        "active_profile": "app", "active_project": "P001",
        "profiles": {"app": {
            "label": "APP",
            "step_docs": ["0_约束卡.md", "1_MRD.md", "2_BRD.md"],
            "step_owner": {
                "0_约束卡.md": {"role": "strategist", "stage": "0"},
                "1_MRD.md": {"role": "market", "stage": "1"},
                "2_BRD.md": {"role": "pm", "stage": "2"},
            },
            "role_bindings": {
                "strategist": {"artifacts": {"0_约束卡": "30_项目/{project}/0_约束卡.md"}},
                "market": {"artifacts": {"1_MRD": "30_项目/{project}/1_MRD.md"}},
                "pm": {"artifacts": {"2_BRD": "30_项目/{project}/2_BRD.md"}},
            },
            "knowledge_domains": {}, "tags_vocab": [],
        }},
        # 闸门只留 2 —— 阶段 1 是**非闸门**，用来测「不带 --decision 直接推进」；
        # 阶段 0 是闸门，必须带 --decision，否则 go 会按设计拒绝。
        "notify": {"enabled": True, "channel": "local", "gates": [2],
                   "channels": {"local": {"type": "local", "file": "99_Agent配置/_通知/{project}.md"}},
                   "dispatch": {"enabled": False}},
    }
    (vault / "opc.config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                           encoding="utf-8")


def seed(vault, pid, role, stage, status="待确认", token="T"):
    d = vault / "99_Agent配置" / "_交接状态"
    d.mkdir(parents=True, exist_ok=True)
    (d / (pid + ".json")).write_text(json.dumps({
        "project": pid, "step": stage, "stage": stage, "role": role,
        "doc": str(stage) + "_x.md", "status": status, "token": token + pid,
        "next_step": stage + 1,
        "next_role": "design" if stage == 0 else "pm",
        "next_doc": str(stage + 1) + "_y.md"}, ensure_ascii=False, indent=2), encoding="utf-8")


def state(vault, pid):
    p = vault / "99_Agent配置" / "_交接状态" / (pid + ".json")
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def run(args):
    r = subprocess.run([sys.executable, str(OPC)] + args, capture_output=True, text=True, timeout=180)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    print("=" * 62)
    print("v2.26 多项目防错闸自测")
    print("=" * 62)

    # ---------- 1. 多项目 + 未指定 → 报错 ----------
    print("\n[1] 两个项目都待确认，未显式指定 --project → 必须报错（不猜）")
    with tempfile.TemporaryDirectory() as td:
        v = Path(td) / "Vault"
        (v / "99_Agent配置").mkdir(parents=True)
        (v / "30_项目").mkdir(parents=True)
        make_cfg(v)
        seed(v, "P001", "strategist", 0)
        seed(v, "P002", "market", 1)
        rc, out = run(["go", "--vault", str(v)])
        check("返回非 0（拒绝推进）", rc != 0, out[-300:])
        check("报错文案提到多个项目", "多个项目" in out, out[-300:])
        check("报错文案提示 --project", "--project" in out, out[-300:])
        check("P001 未被推进", state(v, "P001").get("stage") == 0, state(v, "P001"))
        check("P002 未被推进", state(v, "P002").get("stage") == 1, state(v, "P002"))

    # ---------- 2. 显式指定 → 只推进那一个 ----------
    print("\n[2] 显式 --project P001 → 只推进 P001")
    with tempfile.TemporaryDirectory() as td:
        v = Path(td) / "Vault"
        (v / "99_Agent配置").mkdir(parents=True)
        (v / "30_项目").mkdir(parents=True)
        make_cfg(v)
        seed(v, "P001", "strategist", 0)
        seed(v, "P002", "market", 1)
        rc, out = run(["go", "--project", "P001", "--vault", str(v)])
        check("返回 0", rc == 0, out[-300:])
        check("P001 已推进", state(v, "P001").get("stage") == 1, state(v, "P001"))
        check("P002 未被碰", state(v, "P002").get("stage") == 1, state(v, "P002"))
        check("P002 仍是待确认", state(v, "P002").get("status") == "待确认", state(v, "P002"))

    # ---------- 3. 单项目 → 不受影响 ----------
    print("\n[3] 单项目场景不加 --project → 照常工作（既有行为不破）")
    with tempfile.TemporaryDirectory() as td:
        v = Path(td) / "Vault"
        (v / "99_Agent配置").mkdir(parents=True)
        (v / "30_项目").mkdir(parents=True)
        make_cfg(v)
        seed(v, "P001", "strategist", 0)
        rc, out = run(["go", "--vault", str(v)])
        check("返回 0", rc == 0, out[-300:])
        check("P001 已推进到阶段 1", state(v, "P001").get("stage") == 1, state(v, "P001"))

    # ---------- 4. 通知文本带项目号 ----------
    print("\n[4] 派活 / 汇报通知文本必须带项目号")
    import opc
    src = Path(HERE / "opc.py").read_text(encoding="utf-8")
    check("派活通知带「项目号」", "（项目号 " in src)
    check("派活通知带 --project 提示", "opc go，请带 --project " in src)
    check("汇报通知带 --project 提示", "执行 opc go 时请带 --project " in src)
    check("派活 done 命令带 --project", ("opc done --role " in src) and ("--project " in src))

    # 实测渲染文本（dispatch 段要 enabled，否则函数提前返回）
    with tempfile.TemporaryDirectory() as td:
        v = Path(td) / "Vault"
        (v / "99_Agent配置").mkdir(parents=True)
        make_cfg(v)
        cfg = opc.load_config(v)
        cfg["notify"]["dispatch"] = {"enabled": True,
                                     "founder_id_env": "FEISHU_FOUNDER_UNION_ID",
                                     "profile_root": str(Path(td) / "profiles")}
        # 渲染测试需要收件人 —— 直接给环境变量（.env 兜底路径另测）
        os.environ["FEISHU_FOUNDER_UNION_ID"] = "on_test_union"
        got = {}
        import dispatch as dm
        orig = dm.notify_next_role

        def fake(role, fid, title, lines, **kw):
            got["title"] = title
            got["lines"] = list(lines)
            return {"ok": True, "detail": "captured"}

        dm.notify_next_role = fake
        try:
            opc.dispatch_next_role_notice(v, cfg, "P001", "strategist", "0_约束卡.md", 0)
        finally:
            dm.notify_next_role = orig
        check("派活标题含项目号", "P001" in got.get("title", ""), got.get("title"))
        check("派活正文含项目号", any("P001" in l for l in got.get("lines", [])), got.get("lines"))
        check("派活标题以「项目」开头", got.get("title", "").startswith("【OPC 派活】项目"), got.get("title"))

    print("\n" + "=" * 62)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAILED: " + f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
