#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.23 五个修复的回归自测（开发期工具，不随包分发）。

跑法：python scripts/selftest_v223.py --skill <技能包目录>

覆盖：
  F7  opc claim 不得覆盖交接状态（dispatch_chain / decisions / asset_batch 保留）
  F9  opc go 在非闸门棒上带 --decision 必须报错
  F1  PTY runner 可用（喂输入能被子进程读到）
  F10 ExecBackend 能从 <HERMES_HOME>/.env 取到凭证（agent 内环境无该变量时）
  F2  triage 对已建项必须如实说明「清单不改变交接链」
  F3  只写「不涉及」的清单，报错信息要说清正确用法
  F5  同阶段并行岗位（architect）能拿到链上槽位
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  —— " + detail) if detail else ""))


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


def make_vault(skill: Path, tmp: Path) -> Path:
    """在一个干净目录里建一个 Vault（用技能包自己的 init）。"""
    vault = tmp / "OPC-Vault"
    r = run([sys.executable, str(skill / "scripts" / "opc.py"), "init",
             "--vault", str(vault), "--profile", "app", "--agent-mode", "single",
             "--non-interactive", "--cloud-repo", "skip"])
    if r.returncode != 0:
        raise SystemExit("init 失败:\n" + r.stdout + r.stderr)
    return vault


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True, help="技能包目录（含 scripts/opc.py）")
    args = ap.parse_args()
    skill = Path(args.skill).resolve()
    opc_py = skill / "scripts" / "opc.py"

    tmp = Path(tempfile.mkdtemp(prefix="opc-selftest-"))
    try:
        print("== 建测试 Vault ==")
        vault = make_vault(skill, tmp)

        # ---------- F7 ----------
        print("\n== F7：opc claim 不覆盖交接状态 ==")
        r = run([sys.executable, str(opc_py), "new", "F7项目", "--tags", "#MVP",
                 "--vault", str(vault)])
        check("F7 建项成功", r.returncode == 0, r.stdout[-200:] if r.returncode else "")
        pid = "P001"
        # 人为往状态里塞「不可丢」的字段
        sf = vault / "99_Agent配置" / "_交接状态" / "P001.json"
        st = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else {}
        st.update({"dispatch_chain": [[0, "0_约束卡.md", "strategist"]],
                   "decisions": [{"stage": 0, "decision": "通过"}],
                   "asset_batch": "A-KEEP-ME", "stage": 0, "role": "strategist",
                   "doc": "0_约束卡.md", "status": "进行中"})
        sf.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        r = run([sys.executable, str(opc_py), "claim", "--role", "strategist",
                 "--vault", str(vault)])
        after = json.loads(sf.read_text(encoding="utf-8"))
        check("F7 claim 不丢 dispatch_chain", after.get("dispatch_chain") == [[0, "0_约束卡.md", "strategist"]],
              str(after.get("dispatch_chain")))
        check("F7 claim 不丢 decisions", bool(after.get("decisions")), str(after.get("decisions")))
        check("F7 claim 不丢 asset_batch", after.get("asset_batch") == "A-KEEP-ME",
              str(after.get("asset_batch")))

        # ---------- F9 ----------
        print("\n== F9：opc go 在非闸门棒上带 --decision 必须报错 ==")
        # 让 P001 处于「待确认」的非闸门棒（阶段 1 market，非闸门）
        st["stage"] = 1
        st["step"] = 1
        st["role"] = "market"
        st["doc"] = "1_MRD.md"
        st["status"] = "待确认"
        st["gate"] = False
        st["token"] = "TESTTOKEN"
        sf.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        r = run([sys.executable, str(opc_py), "go", "--decision", "通过",
                 "--project", "P001", "--token", "TESTTOKEN", "--vault", str(vault)])
        check("F9 非闸门 + --decision 被拒", r.returncode != 0 and "不是人工闸门" in (r.stdout + r.stderr),
              (r.stdout + r.stderr).strip()[-160:])

        # 闸门棒上必须允许
        st["stage"] = 0
        st["step"] = 0
        st["role"] = "strategist"
        st["doc"] = "0_约束卡.md"
        st["status"] = "待确认"
        st["gate"] = True
        st["token"] = "GATETOKEN"
        sf.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        r = run([sys.executable, str(opc_py), "go", "--decision", "通过",
                 "--project", "P001", "--token", "GATETOKEN", "--vault", str(vault)])
        check("F9 闸门 + --decision 通过", r.returncode == 0, (r.stdout + r.stderr).strip()[-160:])

        # ---------- F1 ----------
        print("\n== F1：PTY runner 可用 ==")
        pty_runner = skill / "scripts" / "pty_runner.py"
        check("F1 pty_runner.py 存在", pty_runner.is_file())
        if pty_runner.is_file():
            r = run([sys.executable, str(pty_runner)])
            check("F1 PTY 自检通过", "hello" in r.stdout, r.stdout.strip()[-120:])

        # ---------- F10 ----------
        print("\n== F10：ExecBackend 能从 <HERMES_HOME>/.env 取凭证 ==")
        fake_home = tmp / "fake_profile"
        fake_home.mkdir(parents=True, exist_ok=True)
        (fake_home / ".env").write_text('FEISHU_APP_ID=cli_x\nFEISHU_APP_SECRET="sec_y"\n',
                                        encoding="utf-8")
        env = dict(os.environ)
        env["HERMES_HOME"] = str(fake_home)
        env.pop("FEISHU_APP_ID", None)
        env.pop("FEISHU_APP_SECRET", None)
        code = (
            "import importlib.util,json,sys;"
            "spec=importlib.util.spec_from_file_location('ad', %r);"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
            "from pathlib import Path;"
            "be=m.ExecBackend('notify',{'cli':'python','push_cmd':'x {file}',"
            "'credential_env':['FEISHU_APP_ID']}, Path(%r));"
            "print(be.check()[0]);"
            "print(be.credential('FEISHU_APP_ID'))" % (str(skill / "scripts" / "adapters.py"), str(vault))
        )
        r = run([sys.executable, "-c", code], env=env)
        out = r.stdout.strip().split("\n")
        check("F10 无环境变量时 check 仍通过", "True" in out, r.stdout.strip()[-160:])
        check("F10 从 .env 取到凭证", len(out) > 1 and out[-1].strip() == "cli_x", r.stdout.strip()[-160:])

        # ---------- F2 ----------
        print("\n== F2：triage 对已建项如实说明 ==")
        r = run([sys.executable, str(opc_py), "triage", "--project", "P001",
                 "--role", "market", "--assets", "1_MRD.md", "--reason", "测试",
                 "--vault", str(vault)])
        out = r.stdout
        check("F2 triage 提示交接链不会变", "交接链不会因本次清单变更而改变" in out,
              out.strip()[-200:])

        # ---------- F3 ----------
        print("\n== F3：只写「不涉及」的清单 → 报错要说清用法 ==")
        vault2 = tmp / "OPC-Vault2"
        run([sys.executable, str(opc_py), "init", "--vault", str(vault2), "--profile", "app",
             "--non-interactive", "--cloud-repo", "skip"])
        run([sys.executable, str(opc_py), "triage", "--project", "P002", "--role", "engineer",
             "--involved", "no", "--reason", "不需要", "--vault", str(vault2)])
        r = run([sys.executable, str(opc_py), "new", "只排除", "--tags", "#MVP",
                 "--triage", str(vault2 / "99_Agent配置" / "_派活清单" / "P002.json"),
                 "--vault", str(vault2)])
        combined = r.stdout + r.stderr
        check("F3 建项被拒", r.returncode != 0, combined.strip()[-200:])
        check("F3 报错说明正确用法",
              ("逐条写" in combined) or ("--zero" in combined), combined.strip()[-240:])

        # ---------- F5 ----------
        print("\n== F5：同阶段并行岗位能拿到链上槽位 ==")
        code5 = (
            "import importlib.util,json,sys;"
            "spec=importlib.util.spec_from_file_location('opc', %r);"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
            "from pathlib import Path;"
            "v=Path(%r);cfg=m.load_config(v);"
            "docs=[('5_设计交付.md','design','5_设计交付'),"
            "('5_架构设计.md','architect','5_架构设计'),"
            "('5_API契约.yaml','architect','5_API契约')];"
            "print(json.dumps([[a,b,c] for a,b,c in m._dispatch_chain(cfg,docs)],ensure_ascii=False))"
            % (str(opc_py), str(vault))
        )
        r = run([sys.executable, "-c", code5])
        try:
            chain = json.loads(r.stdout.strip().split("\n")[-1])
        except Exception:
            chain = []
        owners = [c[2] for c in chain]
        check("F5 design 在链上", "design" in owners, str(chain))
        check("F5 architect 也拿到槽位", "architect" in owners, str(chain))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n===== 结果 =====")
    print("通过 " + str(len(PASS)) + " / 失败 " + str(len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败: " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
