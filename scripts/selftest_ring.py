#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自测 v2.29「重敲门铃」——防回归。

覆盖 6 组（都在临时 Vault 上跑，不碰真实项目、不发真消息 —— 测试环境无角色凭证，
门铃发送必然走「目标角色无身份」失败分支，台账照记，正好测失败计数）：
  R1 首轮：未领活 + 无台账 → 重敲一次，台账 count=1
  R2 间隔去重：remind 间隔内重复 tick → 不重敲、count 不涨
  R3 间隔期满 → 重敲第二次，count=2
  R4 超限升级：count ≥ --max-rings → 不再敲，升级「叫不醒」提醒创始人（每棒只升一次）
  R5 已领活：任务池 claims 匹配项目+阶段 → 永不重敲
  R6 dry-run：只报告，不写任何文件

用法：python3 scripts/selftest_ring.py
"""
import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import opc as opcd  # noqa: E402
import orchestrator as orch  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name
          + (("  — " + str(detail)) if detail and not cond else ""))


def make_vault(td):
    vault = Path(td) / "Vault"
    (vault / "99_Agent配置" / "_交接状态").mkdir(parents=True, exist_ok=True)
    (vault / "30_项目" / "P001_test").mkdir(parents=True, exist_ok=True)
    (vault / "20_知识资产").mkdir(parents=True, exist_ok=True)
    profiles = Path(td) / "profiles"          # 空凭证目录 → 门铃走失败分支（不联网）
    profiles.mkdir(exist_ok=True)
    cfg = {
        "schema": "opc.config/v2",
        "default_backend": "local",
        "active_profile": "app",
        "active_project": "P001",
        "profiles": {
            "app": {
                "label": "APP / 互联网产品",
                "step_docs": ["5_设计交付.md"],
                "step_owner": {"5_设计交付.md": {"role": "engineer", "stage": "5"}},
                "role_bindings": {"engineer": {"artifacts": {"5_设计交付": "30_项目/{project}/5_设计交付.md"}}},
                "knowledge_domains": {},
                "tags_vocab": [],
            }
        },
        "notify": {
            "enabled": True,
            "channel": "local",
            "gates": [0, 2],
            "channels": {"local": {"type": "local"}},
            "dispatch": {
                "enabled": True, "mode": "group", "chat_id": "oc_test",
                "profile_root": str(profiles),
                "app_id_env": "FEISHU_APP_ID", "app_secret_env": "FEISHU_APP_SECRET",
            },
        },
    }
    (vault / "opc.config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    return vault


def seed_state(vault, status="进行中", role="engineer", stage=5, idle_min=0):
    p = vault / "99_Agent配置" / "_交接状态" / "P001.json"
    base = (datetime.now() - timedelta(minutes=idle_min)).isoformat(timespec="seconds")
    p.write_text(json.dumps({
        "project": "P001", "step": stage, "stage": stage, "role": role,
        "doc": "5_设计交付.md", "status": status, "token": "",
        "confirmed_at": base,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def bell_path(vault):
    return vault / "99_Agent配置" / "_编排状态.json"


def read_bells(vault):
    d = json.loads(bell_path(vault).read_text(encoding="utf-8"))
    return (((d.get("projects") or {}).get("P001") or {}).get("doorbells") or {})


def backdate(vault, key, count=None, minutes=20):
    d = json.loads(bell_path(vault).read_text(encoding="utf-8"))
    ent = d["projects"]["P001"]["doorbells"][key]
    ent["last_at"] = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    if count is not None:
        ent["count"] = count
    bell_path(vault).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def tick(vault, dry_run=False):
    args = argparse.Namespace(once=True, min_wait=30.0, remind=900.0, idle_timeout=1800.0,
                              max_rings=3, channel="local", project=None, dry_run=dry_run,
                              interval=60.0, json=False)
    return orch.tick(vault, args)


def main() -> int:
    print("=" * 62)
    print("v2.29 重敲门铃自测：未领活 → 间隔重敲 → 超限升级；领活即停")
    print("=" * 62)
    td = tempfile.mkdtemp()
    try:
        vault = make_vault(td)

        # ---------- R1 首轮重敲 ----------
        print("\n[R1] 未领活 + 无台账 → 重敲一次，count=1")
        seed_state(vault)
        actions = tick(vault)
        kinds = [a.get("action") for a in actions]
        check("产生「重敲门铃」动作", "重敲门铃" in kinds, kinds)
        bells = read_bells(vault)
        ent = bells.get("5/engineer") or {}
        check("台账已记录（count=1）", ent.get("count") == 1, bells)

        # ---------- R2 间隔去重 ----------
        print("\n[R2] remind 间隔内再 tick → 不重敲")
        actions = tick(vault)
        kinds = [a.get("action") for a in actions]
        check("无重敲动作", "重敲门铃" not in kinds and "叫不醒升级提醒" not in kinds, kinds)
        check("count 仍为 1", (read_bells(vault).get("5/engineer") or {}).get("count") == 1)

        # ---------- R3 间隔期满重敲 ----------
        print("\n[R3] 间隔期满 → 重敲第二次，count=2")
        backdate(vault, "5/engineer", minutes=20)
        actions = tick(vault)
        kinds = [a.get("action") for a in actions]
        check("再次重敲", "重敲门铃" in kinds, kinds)
        check("count=2", (read_bells(vault).get("5/engineer") or {}).get("count") == 2)

        # ---------- R4 超限升级 ----------
        print("\n[R4] count=3 → 不再敲，升级提醒创始人（只升一次）")
        backdate(vault, "5/engineer", count=3, minutes=20)
        actions = tick(vault)
        kinds = [a.get("action") for a in actions]
        check("产生「叫不醒升级提醒」", "叫不醒升级提醒" in kinds, kinds)
        check("count 停在 3（没有再敲）",
              (read_bells(vault).get("5/engineer") or {}).get("count") == 3)
        d = json.loads(bell_path(vault).read_text(encoding="utf-8"))
        rec = (d.get("projects") or {}).get("P001") or {}
        check("升级已去重（escalated_key 落账）",
              rec.get("escalated_key") == "5/engineer", rec)
        notice_file = vault / "99_Agent配置" / "_交接通知.md"
        check("升级通知已写 local 通道", notice_file.exists()
              and "叫不醒" in notice_file.read_text(encoding="utf-8"))
        actions = tick(vault)
        kinds = [a.get("action") for a in actions]
        check("再次 tick 不重复升级", "叫不醒升级提醒" not in kinds and "重敲门铃" not in kinds, kinds)

        # ---------- R5 已领活不敲 ----------
        print("\n[R5] engineer 已领活（claims 匹配项目+阶段）→ 永不重敲")
        opcd.write_task_pool(vault, {"claims": {"engineer": {
            "project": "P001", "stage": 5, "doc": "5_设计交付.md",
            "claimed_at": datetime.now().isoformat(timespec="seconds")}}, "history": []})
        backdate(vault, "5/engineer", count=1, minutes=20)
        # 清掉升级去重，验证领活优先于升级
        d = json.loads(bell_path(vault).read_text(encoding="utf-8"))
        d["projects"]["P001"].pop("escalated_key", None)
        bell_path(vault).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        actions = tick(vault)
        kinds = [a.get("action") for a in actions]
        check("已领活 → 无重敲、无升级", "重敲门铃" not in kinds and "叫不醒升级提醒" not in kinds, kinds)
        check("count 不再增长", (read_bells(vault).get("5/engineer") or {}).get("count") == 1)
        opcd.write_task_pool(vault, {"claims": {}, "history": []})

        # ---------- R6 dry-run ----------
        print("\n[R6] dry-run → 只报告，不写台账")
        backdate(vault, "5/engineer", count=1, minutes=20)
        before = bell_path(vault).read_text(encoding="utf-8")
        actions = tick(vault, dry_run=True)
        kinds = [a.get("action") for a in actions]
        check("报告「将重敲门铃」", "将重敲门铃" in kinds, kinds)
        check("台账未被修改", bell_path(vault).read_text(encoding="utf-8") == before)

    finally:
        shutil.rmtree(td, ignore_errors=True)

    print("\n" + "=" * 62)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAILED: " + f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
