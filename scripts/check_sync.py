#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""多副本一致性体检——查「改了不生效」。

背景（2026-11-11 实测）：同一个 skill 包在机器上存在**多份副本**（开发源目录、
项目级安装目录、Vault 内的文档副本），彼此**没有同步机制**。
改了开发源目录 → 装在别处的副本仍是旧的 → 命令行为与文档对不上，且极难察觉。

**默认范围 = 本项目内**：只比对当前工作区内的副本（源目录 / 项目级安装 /
Vault 文档副本）。工作区**外**（用户主目录下的全局技能目录）**默认不扫**——
本项目的同步边界就在项目内，扫到外面的副本只会得到误报。
需要时用 `--include-home` 显式打开。

本脚本只做**只读比对**，不改任何文件：
  1. 找出所有副本目录（见下方「副本定位」）
  2. 逐个关键文件算哈希
  3. 输出差异清单

跑法：
  python scripts/check_sync.py                      # 自动发现本项目内的副本
  python scripts/check_sync.py --vault <Vault路径>    # 顺带比对 Vault 内文档副本
  python scripts/check_sync.py --include-home        # 额外扫用户主目录（默认不扫）
  python scripts/check_sync.py --extra <副本目录>    # 手动追加副本（可重复）
  python scripts/check_sync.py --json                # 机器可读输出

退出码：0 = 全部一致；1 = 存在差异；2 = 参数错误。

── 副本定位（四级，可配置覆盖优先于任何默认）────────────────────────
  ① 环境变量 OPC_SKILL_DIRS（多目录，os.pathsep 分隔）
  ② 环境变量 OPC_SKILL_DIR（单目录）
  ③ 目录结构约定：见 HOST_DIR_CANDIDATES（**示例清单，不构成默认依赖**）
  ④ 文件搜索：从当前目录向下查找名为 opc-team 的包目录
                （用户主目录**仅在 --include-home 时**纳入）
"""
import hashlib
import json
import os
import sys
from pathlib import Path


def _force_utf8_io():
    """Windows 控制台默认 GBK，中文输出会花屏——强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

HERE = Path(__file__).resolve().parent
PKG = HERE.parent  # opc-team 包根

# 宿主目录名候选 —— **并列示例，不构成对任何宿主的默认依赖**。
# 只用来「自动发现已存在的副本」，找不到就跳过；任何位置都可用
# OPC_SKILL_DIR / OPC_SKILL_DIRS / --extra 覆盖。
HOST_DIR_CANDIDATES = [
    ".codebuddy/skills/opc-team",   # 示例：CodeBuddy（项目级 / 用户级各拼一次）
    ".claude/skills/opc-team",      # 示例：Claude Code
    ".cursor/skills/opc-team",      # 示例：Cursor
    ".windsurf/skills/opc-team",    # 示例：Windsurf
    ".workbuddy/skills/opc-team",   # 示例：另一款 IDE 的用户级技能目录
]

# 包内文件 → Vault 内文档副本的路径映射（Vault 结构与宿主无关）
VAULT_MAP = {
    "references/OPC-架构规范.md": "OPC-架构规范.md",
    "references/命令与配置手册.md": "99_Agent配置/命令与配置手册.md",
}

# 参与比对的固定文件（相对包根，正斜杠）
FIXED_FILES = [
    "SKILL.md",
    "VERSION.md",
    "opc.config.json",
    "references/OPC-架构规范.md",
    "references/命令与配置手册.md",
    "scripts/opc.py",
    "scripts/check_sync.py",
    # 编排器是包的关键脚本（v2.14 起）：漏同步 = 用户装到旧编排器，
    # 而旧编排器**不会**卡闸门 → 拍板权被悄悄绕过。故必须进比对清单。
    # 注意：**自测脚本（scripts/selftest_*.py）不随包分发**
    # （tools/build_package.py 明确排除：自测工具只在仓库里跑），
    # 因此**不参与**本比对 —— 列进来只会永远报「缺失」。
    "scripts/orchestrator.py",
    # 飞书表结构真相源（v2.15 起）：漏同步 = 改了 schema 定义但装到旧副本，
    # 改定义没跟着走，飞书侧表结构与仓库不一致。故必须进比对清单。
    "scripts/base_schema.py",
    # 演示数据清理器（v2.15 起）：漏同步 = 装到旧副本没有清理入口，
    # 要清演示数据只能回仓库手动跑，规矩（先备份、只清业务表）就废了。
    "scripts/base_clean.py",
    # 职业身份代建扩展（v2.16 起）：核心 + 宿主适配器。漏同步 = 装到旧副本
    # 就没有「把职业落成独立身份」这条动作，multi-profile 仍是纯手工。
    "scripts/provision_profiles.py",
    "scripts/hosts/hermes.py",
    "scripts/hosts/manual.py",
    # 人话入口细则（v2.19 起）：漏同步 = 装到旧副本的人拿不到「创始人只说人话
    # → 命令」的完整映射，能力残一半（SKILL.md 里只有摘要）。故必须进比对清单。
    "assets/人话入口.md",
    "EXTENSIONS.md",
]


def sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def rel_files(pkg):
    """列出参与比对的包内文件（岗位卡 / 命令文件按目录整体纳入）。"""
    out = list(FIXED_FILES)
    cards = pkg / "assets" / "岗位卡"
    if cards.is_dir():
        out += ["assets/岗位卡/" + p.name for p in sorted(cards.glob("*.md"))]
    cmds = pkg / "commands"
    if cmds.is_dir():
        out += ["commands/" + str(p.relative_to(cmds)).replace("\\", "/")
                for p in sorted(cmds.rglob("*.md"))]
    return out


def discover_copies(source, extra_dirs, include_home=False):
    """按四级定位找副本目录，返回 [(标签, 路径)]。

    默认**只在本项目内**找；include_home=True 时额外纳入用户主目录。
    """
    found = []
    seen = {str(source.resolve())}

    def add(label, path):
        if not path:
            return
        p = Path(path).expanduser()
        if not p.is_dir():
            return
        key = str(p.resolve())
        if key in seen:
            return
        seen.add(key)
        found.append((label, p))

    # ① / ② 环境变量（优先级最高）
    env_multi = os.environ.get("OPC_SKILL_DIRS", "")
    for i, part in enumerate([x for x in env_multi.split(os.pathsep) if x.strip()]):
        add("env:OPC_SKILL_DIRS[%d]" % i, part)
    add("env:OPC_SKILL_DIR", os.environ.get("OPC_SKILL_DIR"))

    # ③ 目录结构约定（示例清单）
    for rel in HOST_DIR_CANDIDATES:
        base = Path.cwd()
        for _ in range(6):  # 从当前目录向上找项目级安装目录
            add("约定:" + rel, base / rel)
            if base.parent == base:
                break
            base = base.parent
        if include_home:
            add("约定(HOME):" + rel, Path.home() / rel)

    # ④ 文件搜索：**有界**查找名为 opc-team 的包目录
    #    （有界 = 限深度 + 跳过大目录，避免全盘扫描）
    #    默认只扫当前目录（= 本项目内）；用户主目录仅在 --include-home 时纳入。
    SKIP_DIRS = {"node_modules", ".git", "AppData", "Windows", "Program Files",
                 "Program Files (x86)", "venv", ".venv", "__pycache__", "dist"}

    def bounded_rglob(base, max_depth=4):
        if not base.is_dir():
            return
        base_depth = len(base.parts)
        for dirpath, dirnames, filenames in os.walk(base):
            p = Path(dirpath)
            if len(p.parts) - base_depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
            if p.name == "opc-team":
                yield p
                dirnames[:] = []

    bases = [Path.cwd()]
    if include_home:
        bases.append(Path.home())
    for base in bases:
        try:
            for p in bounded_rglob(base):
                add("搜索:" + str(p), p)
        except (OSError, PermissionError):
            pass

    # 手动追加
    for i, e in enumerate(extra_dirs or []):
        add("extra[%d]" % i, e)
    return found


def main(argv):
    _force_utf8_io()
    args = argv[1:]
    source = PKG
    vault = None
    extra = []
    as_json = False
    include_home = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--source" and i + 1 < len(args):
            source = Path(args[i + 1]); i += 2
        elif a == "--vault" and i + 1 < len(args):
            vault = Path(args[i + 1]); i += 2
        elif a == "--extra" and i + 1 < len(args):
            extra.append(args[i + 1]); i += 2
        elif a == "--include-home":
            include_home = True; i += 1
        elif a == "--json":
            as_json = True; i += 1
        else:
            print("未知参数：" + a, file=sys.stderr)
            print(__doc__, file=sys.stderr)
            return 2
    source = source.resolve()
    if not source.is_dir():
        print("源目录不存在：" + str(source), file=sys.stderr)
        return 2

    copies = discover_copies(source, extra, include_home)
    files = rel_files(source)
    src_hash = {f: sha256(source / f) for f in files}

    rows = []
    diffs = []
    for label, cdir in copies:
        for f in files:
            h = sha256(cdir / f)
            if h is None:
                rows.append({"副本": label, "文件": f, "状态": "缺失", "哈希": None})
                diffs.append((label, f, "缺失"))
            elif h != src_hash[f]:
                rows.append({"副本": label, "文件": f, "状态": "不一致", "哈希": h[:12]})
                diffs.append((label, f, "不一致"))

    vault_rows = []
    if vault:
        vault = vault.resolve()
        if not vault.is_dir():
            print("Vault 不存在：" + str(vault), file=sys.stderr)
            return 2
        for f, vf in VAULT_MAP.items():
            h = sha256(vault / vf)
            if h is None:
                vault_rows.append({"副本": "vault", "文件": vf, "状态": "缺失", "哈希": None})
                diffs.append(("vault", vf, "缺失"))
            elif h != src_hash.get(f):
                vault_rows.append({"副本": "vault", "文件": vf, "状态": "不一致", "哈希": h[:12]})
                diffs.append(("vault", vf, "不一致"))
        cards = source / "assets" / "岗位卡"
        if cards.is_dir():
            for p in sorted(cards.glob("*.md")):
                h = sha256(vault / "99_Agent配置" / "岗位卡" / p.name)
                rel = "assets/岗位卡/" + p.name
                if h is None:
                    vault_rows.append({"副本": "vault", "文件": "99_Agent配置/岗位卡/" + p.name,
                                      "状态": "缺失", "哈希": None})
                    diffs.append(("vault", "岗位卡/" + p.name, "缺失"))
                elif h != src_hash.get(rel):
                    vault_rows.append({"副本": "vault", "文件": "99_Agent配置/岗位卡/" + p.name,
                                      "状态": "不一致", "哈希": h[:12]})
                    diffs.append(("vault", "岗位卡/" + p.name, "不一致"))

    if as_json:
        print(json.dumps({
            "source": str(source),
            "copies": [{"label": l, "path": str(p)} for l, p in copies],
            "files": files,
            "diffs": [{"副本": a, "文件": b, "状态": c} for a, b, c in diffs],
        }, ensure_ascii=False, indent=2))
        return 1 if diffs else 0

    print("多副本一致性体检（只读，不改任何文件）")
    print("源目录：" + str(source))
    print("扫描范围：" + ("本项目内 + 用户主目录（--include-home）"
                         if include_home else "本项目内（工作区外默认不扫）"))
    print("参与比对文件：" + str(len(files)) + " 个")
    print("")
    print("发现副本：" + str(len(copies)) + " 个" +
          ("（含 Vault 文档副本）" if vault else ""))
    for l, p in copies:
        print("  - [" + l + "] " + str(p))
    print("")
    if not rows and not vault_rows:
        print("结论：所有已发现副本与源目录一致。")
        if not copies:
            print("提示：没发现任何副本目录。若你装在别处，用 --extra <目录> 指定。")
        return 0
    print("差异明细：")
    for r in rows + vault_rows:
        print("  [" + r["状态"] + "] " + r["副本"] + " :: " + r["文件"])
    print("")
    print("结论：存在 " + str(len(diffs)) + " 处差异 —— **改了不生效**的风险点。")
    print("修法：把源目录的文件复制到对应副本（覆盖前先确认副本里有你自己的改动）。")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
