#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基线完整性校验：逐文件 SHA256 比对官方发布包。

用途：pre-commit 的第二道门禁。自测脚本只覆盖「行为」，本脚本覆盖「文件本身」——
自测测不出的改动（改注释、调参数、删分支）由这里拦下。

用法：
    python3 scripts/check_baseline.py            # 检查
    python3 scripts/check_baseline.py --update   # 修完 bug 后更新基线（有意为之）

退出码：0 = 通过；1 = 有文件偏离基线（被拦截）。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / ".baseline-hashes.json"

# 本地维护工具：不属于官方发布包，允许存在且**不受基线哈希约束**
# （它们是本仓库为「防 bug 复现」新增的本地设施，改动是正常维护行为）。
LOCAL_TOOLS = {
    ".gitignore",
    ".baseline-hashes.json",
    "scripts/check_baseline.py",
}

# Python 缓存等编译产物：由 .gitignore 排除，不属于「新增未登记文件」，
# 否则每次跑过脚本后基线检查都会假阳性。
IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def main() -> int:
    if not MANIFEST.exists():
        print(f"[baseline] 缺少基线清单：{MANIFEST}")
        return 1

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    changed, missing, extra = [], [], []

    for rel, expected in manifest.items():
        f = ROOT / rel
        if not f.exists():
            missing.append(rel)
            continue
        if expected is None:
            continue
        actual = hashlib.sha256(f.read_bytes()).hexdigest()
        if actual != expected:
            changed.append(rel)

    for f in ROOT.rglob("*"):
        if not f.is_file():
            continue
        rel = str(f.relative_to(ROOT))
        if rel.startswith(".git/") or rel in LOCAL_TOOLS:
            continue
        parts = set(f.relative_to(ROOT).parts)
        if parts & IGNORED_DIRS:
            continue
        if rel not in manifest:
            extra.append(rel)

    if "--update" in sys.argv:
        new = {}
        for rel in manifest:
            f = ROOT / rel
            new[rel] = hashlib.sha256(f.read_bytes()).hexdigest() if f.exists() else None
        MANIFEST.write_text(
            json.dumps(new, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print(f"[baseline] 基线已更新（{len(new)} 项）。")
        return 0

    problems = []
    if changed:
        problems.append(f"内容偏离基线（{len(changed)} 个）：" + "、".join(changed[:10]))
    if missing:
        problems.append(f"文件缺失（{len(missing)} 个）：" + "、".join(missing[:10]))
    if extra:
        problems.append(f"新增未登记文件（{len(extra)} 个）：" + "、".join(extra[:10]))

    if problems:
        print("[baseline] 偏离官方基线：")
        for p in problems:
            print("  - " + p)
        print("若这是有意修复的 bug，先提交，再用 --update 更新基线。")
        return 1

    print(f"[baseline] {len(manifest)} 个文件与基线一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
