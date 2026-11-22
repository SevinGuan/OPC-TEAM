#!/usr/bin/env python3
"""v2.25 交互闭环自测：创始人私聊回「继续 / 驳回」的完整链路。

跑法：
    python scripts/selftest_confirm_loop.py

覆盖两条闭环（都在临时 Vault 上跑，不碰真实项目、不发真消息）：
  A. 同意链：done → 状态「待确认」→ go（非闸门）→ 游标前移 → 下一棒是正确角色
  B. 驳回链：done → 状态「待确认」→ go --decision 驳回 → 状态回到「进行中」、token 清空
     → 同阶段可重新 done（不卡死）
  C. 闸门链：闸门阶段必须带结论；带「通过」才放行；非闸门棒带「通过」被拒
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPC = HERE / "opc.py"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (("  — " + str(detail)) if detail and not cond else ""))


def run(args, cwd=None):
    r = subprocess.run([sys.executable, str(OPC)] + args, capture_output=True, text=True,
                       cwd=str(cwd or HERE), timeout=180)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def make_vault(td):
    """建一个最小 Vault：按**真实 opc.config.json 的结构**搭（step_docs + step_owner，
    role_bindings 在 profile 内），否则 `opc done` / `opc go` 会因「未定义 step_docs」报错。
    """
    vault = Path(td) / "Vault"
    (vault / "99_Agent配置" / "_交接状态").mkdir(parents=True, exist_ok=True)
    (vault / "30_项目").mkdir(parents=True, exist_ok=True)
    (vault / "20_知识资产").mkdir(parents=True, exist_ok=True)
    (vault / "10_参考资料").mkdir(parents=True, exist_ok=True)
    cfg = {
        "schema": "opc.config/v2",
        "default_backend": "local",
        "active_profile": "app",
        "active_project": "P001",
        "profiles": {
            "app": {
                "label": "APP / 互联网产品",
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
                "knowledge_domains": {},
                "tags_vocab": [],
            }
        },
        "notify": {
            "enabled": True,
            "channel": "local",
            "gates": [0, 2],
            "channels": {"local": {"type": "local", "file": "99_Agent配置/_通知/{project}.md"}},
            "dispatch": {"enabled": False},
        },
    }
    (vault / "opc.config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    return vault


def seed_stage(vault, role, doc, stage, status="待确认", token="ABC123", next_role=None,
               next_doc=None, next_stage=None):
    """把交接状态直接写成「某阶段已报完成、等确认」。

    next_role / next_doc 必须填 —— `opc go` 靠它们推导下一棒（真实 done 流程会填）。
    """
    p = vault / "99_Agent配置" / "_交接状态" / "P001.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if next_role is None:
        nxt = {0: ("market", "1_MRD.md", 1), 1: ("pm", "2_BRD.md", 2),
               2: ("", "", -1)}.get(stage, ("", "", -1))
        next_role, next_doc, next_stage = nxt
    state = {
        "project": "P001", "step": stage, "stage": stage, "role": role, "doc": doc,
        "status": status, "token": token,
        "next_step": next_stage, "next_role": next_role, "next_doc": next_doc,
    }
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def read_state(vault):
    p = vault / "99_Agent配置" / "_交接状态" / "P001.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def main() -> int:
    print("=" * 62)
    print("v2.25 交互闭环自测：同意 → 推进 / 不同意 → 退回重做")
    print("=" * 62)

    with tempfile.TemporaryDirectory() as td:
        vault = make_vault(td)
        V = ["--vault", str(vault)]

        # ---------- A. 同意链（非闸门阶段 1） ----------
        print("\n[A] 同意链：done → 待确认 → go（非闸门）→ 游标前移")
        seed_stage(vault, "market", "1_MRD.md", 1)
        rc, out = run(["go"] + V)
        st = read_state(vault)
        check("go 成功返回 0", rc == 0, out[-300:])
        check("游标前移到阶段 2", st.get("stage") == 2, st)
        check("接手岗位变成 pm", st.get("role") == "pm", st)
        check("状态回到「进行中」", st.get("status") == "进行中", st)
        check("回执已清空", st.get("token") == "", st)

        # ---------- B. 驳回链（非闸门阶段） ----------
        print("\n[B] 驳回链：非闸门阶段也能「不同意」→ 退回本阶段、游标不动")
        seed_stage(vault, "market", "1_MRD.md", 1)
        rc, out = run(["go", "--decision", "驳回", "--basis", "渠道判断不对"] + V)
        st = read_state(vault)
        check("驳回命令成功", rc == 0, out[-300:])
        check("游标**没有**前移（仍在阶段 1）", st.get("stage") == 1, st)
        check("状态回到「进行中」", st.get("status") == "进行中", st)
        check("回执清空（可重新报完成）", st.get("token") == "", st)
        check("拍板结论已留痕", st.get("decision") == "驳回", st)
        check("拍板人是创始人", st.get("decided_by") == "创始人", st)
        check("依据已记录", st.get("decision_basis") == "渠道判断不对", st)

        # 驳回后能不能重新 done（不卡死）——用一个伪造的产出文件试
        print("\n[B2] 驳回后同阶段可重新报完成（不卡死）")
        prod = vault / "30_项目" / "P001" / "1_MRD.md"
        prod.parent.mkdir(parents=True, exist_ok=True)
        prod.write_text(
            "---\nproject: P001\nroles: market\nstatus: 已完成\n---\n\n# MRD\n内容\n",
            encoding="utf-8")
        rc, out = run(["done", "--role", "market"] + V)
        check("驳回后重新 done 被接受", rc == 0, out[-400:])
        st2 = read_state(vault)
        check("重新报完成 → 状态「待确认」", st2.get("status") == "待确认", st2)
        check("重新报完成 → 新回执", bool(st2.get("token")) and st2.get("token") != "ABC123", st2)

        # ---------- C. 闸门链 ----------
        print("\n[C] 闸门链：闸门必须带明确结论")
        seed_stage(vault, "strategist", "0_约束卡.md", 0, token="GATE01")
        rc, out = run(["go"] + V)
        check("闸门不带结论 → 被拒（rc≠0）", rc != 0, out[-200:])
        st = read_state(vault)
        check("闸门被拒后游标未动", st.get("stage") == 0, st)

        rc, out = run(["go", "--decision", "通过"] + V)
        st = read_state(vault)
        check("闸门「通过」→ 放行", rc == 0, out[-300:])
        check("闸门放行后游标前移", st.get("stage") == 1, st)

        print("\n[C2] 非闸门棒带「通过」仍被拒（v2.23 口径不破）")
        seed_stage(vault, "market", "1_MRD.md", 1)
        rc, out = run(["go", "--decision", "通过"] + V)
        check("非闸门 --decision 通过 → 被拒", rc != 0, out[-250:])
        st = read_state(vault)
        check("被拒后游标未动", st.get("stage") == 1, st)

        # ---------- D. 非闸门棒「需补充」 ----------
        print("\n[D] 非闸门棒「需补充」→ 退回本阶段")
        seed_stage(vault, "market", "1_MRD.md", 1)
        rc, out = run(["go", "--decision", "需补充"] + V)
        st = read_state(vault)
        check("需补充被接受", rc == 0, out[-250:])
        check("游标未动", st.get("stage") == 1, st)
        check("结论已留痕", st.get("decision") == "需补充", st)

        # ---------- E. 非闸门棒不带 decision 直接推进 ----------
        print("\n[E] 非闸门棒不带 --decision → 直接推进（常规路径不受影响）")
        seed_stage(vault, "market", "1_MRD.md", 1)
        rc, out = run(["go"] + V)
        st = read_state(vault)
        check("非闸门直接推进成功", rc == 0, out[-250:])
        check("游标前移", st.get("stage") == 2, st)
        check("未误记拍板结论", not st.get("decision"), st)

    print("\n" + "=" * 62)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAILED: " + f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
