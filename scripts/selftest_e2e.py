#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端链路回归：建项 → 逐棒 done → 闸门 go → 推进 → 收尾。

验证 v2.23 的 5 个修复没有破坏原有流程（尤其是链的推进、闸门、快照、索引）。
开发期工具，不随包分发。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  —— " + detail) if detail else ""))


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


def opc(opc_py, *args, vault):
    return run([sys.executable, str(opc_py)] + list(args) + ["--vault", str(vault)])


def set_status(doc: Path, status: str):
    if not doc.is_file():
        return False
    txt = doc.read_text(encoding="utf-8")
    txt2 = re.sub(r"^status:\s*\S+\s*$", "status: " + status, txt, count=1, flags=re.M)
    doc.write_text(txt2, encoding="utf-8")
    return True


def find_doc(vault: Path, docname: str, role: str):
    for p in (vault / "30_项目").rglob(docname):
        if role in str(p.parent):
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True)
    args = ap.parse_args()
    skill = Path(args.skill).resolve()
    opc_py = skill / "scripts" / "opc.py"

    tmp = Path(tempfile.mkdtemp(prefix="opc-e2e-"))
    try:
        vault = tmp / "OPC-Vault"
        r = run([sys.executable, str(opc_py), "init", "--vault", str(vault), "--profile", "app",
                 "--agent-mode", "single", "--non-interactive", "--cloud-repo", "skip"])
        check("init 成功", r.returncode == 0, r.stdout[-200:])

        # 只派 3 岗，跑 0→1→2→3
        v2 = tmp / "V2"
        run([sys.executable, str(opc_py), "init", "--vault", str(v2), "--profile", "app",
             "--non-interactive", "--cloud-repo", "skip"])
        for role, assets in [("strategist", "0_约束卡,2_立项决策,3_BRD"),
                             ("market", "1_MRD"),
                             ("pm", "4_产品定义")]:
            opc(opc_py, "triage", "--project", "P001", "--role", role,
                "--assets", assets, "--reason", "回归测试", vault=v2)
        r = run([sys.executable, str(opc_py), "new", "回归项目", "--tags", "#MVP",
                 "--triage", str(v2 / "99_Agent配置" / "_派活清单" / "P001.json"),
                 "--vault", str(v2)])
        check("按清单建项成功", r.returncode == 0, (r.stdout + r.stderr)[-200:])

        sf = v2 / "99_Agent配置" / "_交接状态" / "P001.json"
        st = json.loads(sf.read_text(encoding="utf-8"))
        chain = st.get("dispatch_chain") or []
        check("链上 5 棒（strategist 3 + market 1 + pm 1）", len(chain) == 5, str(chain))
        check("链含 strategist/market/pm",
              {"strategist", "market", "pm"} <= {c[2] for c in chain}, str(chain))

        # 逐棒：scaffold → 填内容 → status 完成 → done
        for idx, docname, role in chain:
            r = opc(opc_py, "scaffold", "--role", role, "--project", "P001", vault=v2)
            check("scaffold " + role + " 成功", r.returncode == 0, (r.stdout + r.stderr)[-160:])
            d = find_doc(v2, docname, role)
            check("产出落盘 " + docname, d is not None)
            if d is not None and d.suffix == ".md":
                body = d.read_text(encoding="utf-8")
                if "## " not in body:
                    d.write_text(body + "\n\n## 回归测试\n\n占位内容。\n", encoding="utf-8")
                set_status(d, "完成")
            r = opc(opc_py, "done", "--role", role, "--project", "P001", vault=v2)
            check("done " + role + " 成功", r.returncode == 0, (r.stdout + r.stderr)[-200:])
            st = json.loads(sf.read_text(encoding="utf-8"))
            check("状态推进到下一棒 " + str(idx), st.get("stage") == idx, str(st.get("stage")))
            # 非闸门棒用 advance 推进；闸门棒用 go --decision
            is_gate = st.get("gate")
            if is_gate:
                r = opc(opc_py, "go", "--decision", "通过", "--project", "P001", vault=v2)
                check("闸门 go 通过 " + role, r.returncode == 0, (r.stdout + r.stderr)[-160:])
            else:
                r = opc(opc_py, "advance", "--project", "P001", vault=v2)
                check("advance 推进 " + role, r.returncode == 0, (r.stdout + r.stderr)[-160:])

        st = json.loads(sf.read_text(encoding="utf-8"))
        check("链路走到最后", st.get("status") in ("已完成", "待确认"), str(st.get("status")))
        snap = list((v2 / "30_项目").rglob("_资产快照.md"))
        check("资产快照已生成", bool(snap))
        if snap:
            n = snap[0].read_text(encoding="utf-8").count("```json")
            check("快照含 5 个批次", n == 5, str(n))
        idxfile = v2 / "30_项目" / "_产出索引.md"
        check("产出索引有行", idxfile.is_file() and len(idxfile.read_text(encoding="utf-8").splitlines()) > 10)

        # status 自检
        r = opc(opc_py, "status", vault=v2)
        check("status 无报错", r.returncode == 0 and "已生成" in r.stdout, (r.stdout + r.stderr)[-160:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n===== 结果 =====")
    print("通过 " + str(len(PASS)) + " / 失败 " + str(len(FAIL)))
    for f in FAIL:
        print("  失败: " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
