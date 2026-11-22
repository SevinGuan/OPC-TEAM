#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 opc-team 的斜杠命令装到任意宿主。

命令文件在 `commands/opc/*.md`，安装后即成为 `/opc:init`、`/opc:new` …。
本脚本只用标准库，可在任何装了 Python 3.8+ 的机器上跑。

用法：
    python scripts/install_commands.py                     # 自动检测并安装到所有已识别的宿主目录
    python scripts/install_commands.py --dry-run           # 只打印将要做什么，不落盘
    python scripts/install_commands.py --list              # 列出候选宿主目录与是否存在
    python scripts/install_commands.py --target <目录>      # 手动指定最终目录（可重复，任意宿主都能用）
    python scripts/install_commands.py --copy-skill <目录>  # 连同 skill 包一起复制过去
    python scripts/install_commands.py --print-script-path  # 打印包内脚本绝对路径

说明：
- 幂等：同名 .md 会被覆盖，其它文件不动。
- 不删除任何东西：目标目录不存在时才创建。
- **不绑定任何宿主**：内置的候选表只是常见宿主的快捷方式；任何其它宿主都用 `--target <该宿主的自定义命令目录>` 装，命令名与内容完全一致。
- 宿主若不支持 `commands` 目录式命令，直接把 `commands/opc/*.md` 的内容当提示词贴进该宿主的自定义命令功能即可（内容自洽，与宿主无关）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PKG_ROOT / "commands" / "opc"
CMD_NAMESPACE = "opc"

# 常见宿主的快捷候选（只是快捷方式；其它宿主一律用 --target 手动指定）
# (显示名, 宿主根目录, 官方文档是否已确认该层级)
HOSTS = [
    ("CodeBuddy（项目级）", Path.cwd() / ".codebuddy", "未在官方文档确认（若 / 列表不出现，改用用户级）"),
    ("CodeBuddy（用户级）", Path.home() / ".codebuddy", "官方文档已确认"),
    ("Claude Code（项目级）", Path.cwd() / ".claude", "官方文档已确认"),
    ("Claude Code（用户级）", Path.home() / ".claude", "官方文档已确认"),
    ("Cursor（项目级）", Path.cwd() / ".cursor", "官方文档已确认"),
]


def source_files() -> list[Path]:
    if not SRC_DIR.is_dir():
        print(f"[x] 找不到命令源目录：{SRC_DIR}", file=sys.stderr)
        return []
    return sorted(SRC_DIR.glob("*.md"))


def target_dir_for(host_root: Path) -> Path:
    return host_root / "commands" / CMD_NAMESPACE


def detect_targets() -> list[tuple[str, Path, str]]:
    out = []
    for label, root, note in HOSTS:
        if root.is_dir():
            out.append((label, target_dir_for(root), note))
    return out


def install(files: list[Path], target: Path, dry_run: bool) -> int:
    target.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in files:
        dst = target / f.name
        if dry_run:
            print(f"  [dry-run] {f.name} -> {dst}")
        else:
            shutil.copy2(f, dst)
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跨宿主安装 opc-team 斜杠命令")
    ap.add_argument("--target", action="append", default=[], help="手动指定命令最终目录（可重复）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不落盘")
    ap.add_argument("--list", action="store_true", help="列出候选宿主目录")
    ap.add_argument("--copy-skill", default="", help="把整个 opc-team 包复制到该目录")
    ap.add_argument("--print-script-path", action="store_true", help="打印包内脚本绝对路径后退出")
    args = ap.parse_args(argv)

    files = source_files()
    if not files:
        return 2

    if args.print_script_path:
        print(str(PKG_ROOT / "scripts" / "opc.py"))
        return 0

    if args.list:
        print(f"命令源目录：{SRC_DIR}（{len(files)} 个）")
        for label, root, note in HOSTS:
            state = "存在" if root.is_dir() else "不存在"
            print(f"  {label:<22} {root}  [{state}] {note}")
        return 0

    if args.copy_skill:
        dst = Path(args.copy_skill).expanduser().resolve() / "opc-team"
        if args.dry_run:
            print(f"[dry-run] skill 包 -> {dst}")
        else:
            shutil.copytree(PKG_ROOT, dst, dirs_exist_ok=True)
            print(f"[ok] skill 包已复制到 {dst}")

    targets: list[tuple[str, Path, str]] = []
    for t in args.target:
        targets.append(("手动指定", Path(t).expanduser().resolve(), ""))
    if not targets:
        targets = detect_targets()

    if not targets:
        print("[!] 未检测到任何宿主目录。")
        print("    手动指定：python scripts/install_commands.py --target <你的 commands 目录>")
        return 1

    for label, target, note in targets:
        print(f"→ {label}：{target}")
        n = install(files, target, args.dry_run)
        print(f"  [{'dry-run' if args.dry_run else 'ok'}] {n} 个命令"
              + (f"　（{note}）" if note else ""))
        print(f"  命令名：/{CMD_NAMESPACE}:<文件名去掉 .md>")
    print(f"  脚本：{PKG_ROOT / 'scripts' / 'opc.py'}")
    print("提示：命令列表通常在会话启动时扫描，装完请新开会话再输入 / 查看。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
