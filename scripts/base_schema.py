#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""飞书多维表格 · 结构定义与同步器（OPC v2.15）

**仓库是飞书表结构的真相源，飞书侧是副本**——与「本地 Vault 是产出真相源」同一条原则：
表结构写在代码里，可 diff、可回滚、可评审；飞书只是这份定义的一处投影。
以后加字段＝改本文件 + 跑一次 `--apply`，而不是在飞书界面手点。

本脚本只做两件事：**加列**、**把枚举补齐到目标集合**。写操作刻意保守，一律不越线：
  · 只加列、只补枚举项；
  · **不删列、不删行、不改已有列的类型、不改已有列的其它属性**；
  · 唯一例外是 REPLACE_ENUMS 白名单里的枚举做**整体替换**——那套通用研发角色词表
    与 OPC 的 8 个岗位不是同一套，两套并存会让看板含义漂移。替换前自动导出一份备份。

用法：
  python scripts/base_schema.py --check              # 只读比对，输出差异
  python scripts/base_schema.py --apply              # 应用差异（幂等，可重复跑）
  python scripts/base_schema.py --export backup.json # 导出当前结构（改前备份）
  python scripts/base_schema.py --apply --dry-run    # 走一遍写流程但不真写

退出码：0 = 已一致 / 应用成功；1 = 有差异（--check）；2 = 参数错误；3 = 飞书接口调用失败。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# 目标多维表格：飞书「敏捷项目管理」模板的副本（v2.15 改造对象）。
#
# **台账地址不入仓**（v2.15.1 修正）：app_token / table_id 是**某个租户的资源标识**。
# 写死进发布物 = 让所有用户默认指向**开发者的表** —— 谁跑一次 --apply 就去改谁的数据。
# 本仓库只留「表结构定义」（列名与枚举 = OPC 与台账的共同约定，可评审可 diff），
# 地址一律现取，优先级由高到低：
#   1) 命令行 --base-token；环境变量 FEISHU_BASE_TOKEN / FEISHU_BASE_TABLE_<KEY>
#   2) 配置文件 opc.config.json 的 backends.feishu.base_token / base_tables
BASE_TOKEN_DEFAULT = ""  # 兼容旧引用；真实值只来自配置，**不留默认**

# ── OPC 侧词表 ──────────────────────────────────────────────────────────────────
# 唯一出处：岗位卡 role 编码 / 手册阶段表 / pm 卡输出契约。
# 改这里 = 改 OPC 与飞书的共同约定，两边同时生效。

# OPC 9 阶段（对齐手册阶段表与各产出文档 frontmatter 的 roles）
STAGES = [
    "0-约束卡",
    "1-市场洞察",
    "2-立项决策",
    "3-商业模式",
    "4-产品定义",
    "5-设计交付",
    "6-实现与测试",
    "7-上线",
    "8-复盘",
]

# OPC 8 岗位 role 编码（对齐 assets/岗位卡/*.md）
ROLES = [
    "strategist",
    "market",
    "pm",
    "design",
    "engineer",
    "growth",
    "architect",
    "governance",
]

# 「进展」——模板原有 5 值（未开始/进行中/已交付/已归档/已停滞）全保留，
# 另补 OPC 需要而模板缺的 3 值：可开工（认领入口）、待确认（人工闸门）、阻塞。
# 「已停滞」与「阻塞」并存属刻意：改名会丢历史行值，新增不会。
PROGRESS = ["未开始", "可开工", "进行中", "待确认", "阻塞", "已交付", "已归档"]

# 「需求来源」——沿用 ①需求池 原有 6 值，pm 侧模板同步补同一列
REQ_SOURCES = ["业务创新", "客户反馈", "用户调研", "竞品分析", "跨团队合作", "日常迭代"]

# 闸门状态 / 项目状态
GATE_STATUS = ["无", "待拍板", "通过", "否决"]
PROJECT_STATUS = ["进行中", "已归档"]


def _sel(name, values):
    return {
        "name": name,
        "type": "select",
        "multiple": False,
        "options": [{"name": v} for v in values],
    }


# ── 五张业务表的目标结构 ─────────────────────────────────────────────────────────
# add_fields：需要新建的列（按顺序追加到表尾）
# enums：需要补齐/替换的枚举列
# table_id：**留空，运行时按表名从配置注入**（见 resolve_base）—— 地址是租户资源，不入仓。
TABLES = {
    "项目汇总": {
        "table_id": "",
        "add_fields": [
            _sel("OPC 阶段", STAGES),
            _sel("闸门状态", GATE_STATUS),
            _sel("项目状态", PROJECT_STATUS),
            {"name": "规范钉扎", "type": "text"},
            {"name": "产出索引", "type": "text"},
        ],
        "enums": {},
    },
    "①需求池（关联项目）": {
        "table_id": "",
        "add_fields": [
            {"name": "需求编号", "type": "text"},
            {"name": "验收标准", "type": "text"},
        ],
        "enums": {"需求来源": REQ_SOURCES},
    },
    "②版本开发计划": {
        "table_id": "",
        "add_fields": [
            _sel("OPC 阶段", STAGES),
            {"name": "产出文档", "type": "text"},
            {"name": "上游文档", "type": "text"},
            {"name": "闸门", "type": "checkbox"},
            {"name": "拍板结论", "type": "text"},
            # v2.29.5：立项是创始人决策点 —— 追加立项（V2、V3…）必须带创始人同意依据，留痕同步。
            {"name": "立项批准", "type": "text"},
            # 认领留痕：手册 §23.2 外部表建议字段里的「认领时间」。
            # 「任务执行人」(user) ≡ 认领人，「认领时间」= 他什么时候接的活。
            {"name": "认领时间", "type": "datetime"},
            # v2.20 层级重构（创始人拍板）：一个版本一行（顶层）+ 阶段任务=子记录。
            # 版本键 = "<短ID>-<版本>"，是顶层行的幂等匹配键，子记录也带同键便于检索。
            {"name": "版本键", "type": "text"},
            # 关联父任务 = 本表自关联 link（link_table 传**表名**，CLI 口径），
            # 阶段子记录挂到版本顶层行下（与①需求池同构）。
            {"name": "关联父任务", "type": "link", "link_table": "②版本开发计划"},
        ],
        "enums": {"进展": PROGRESS, "负责角色": ROLES},
    },
    "③版本 Bug 记录": {
        "table_id": "",
        "add_fields": [],
        "enums": {},
    },
    "发布版本": {
        "table_id": "",
        "add_fields": [],
        "enums": {},
    },
}

# 枚举**整体替换**白名单（其余一律「只补不缺」，绝不丢历史值）
REPLACE_ENUMS = {("②版本开发计划", "负责角色")}

BACKUP_DIR = Path(tempfile.gettempdir()) / "opc-base-schema-backup"  # 运行时产物，禁止写进 Skill 安装目录（否则污染发布包）

# ── 台账地址：配置化（不入仓）────────────────────────────────────────────────────
# 表逻辑名 → 环境变量后缀（FEISHU_BASE_TABLE_<KEY> 可单表覆盖）
TABLE_ENV_KEYS = {
    "项目汇总": "SUMMARY",
    "①需求池（关联项目）": "REQUIREMENTS",
    "②版本开发计划": "PLAN",
    "③版本 Bug 记录": "BUGS",
    "发布版本": "RELEASES",
}


def config_candidates(explicit="", vault_hint=""):
    """配置文件候选（优先级由高到低）。

    显式 --config > 环境变量 OPC_CONFIG > --vault 指向的目录 > 当前目录 > 包内默认。
    注意包内默认那份 base_token 是**空的**（地址不入仓），所以下面取的是
    「第一个含 base_token 的」而不是「第一个存在的」—— 否则空模板会挡住真配置。
    """
    cands = []
    if explicit:
        cands.append(Path(explicit))
    env = os.environ.get("OPC_CONFIG")
    if env:
        cands.append(Path(env))
    if vault_hint:
        cands.append(Path(vault_hint) / "opc.config.json")
    cands.append(Path("opc.config.json"))
    cands.append(Path(__file__).resolve().parent.parent / "opc.config.json")
    return cands


def load_base_conf(explicit="", vault_hint=""):
    """读出台账配置。返回 (base_token, {表名: table_id}, 实际用到的配置文件或 None)。"""
    fallback = None
    for c in config_candidates(explicit, vault_hint):
        if not c.is_file():
            continue
        if fallback is None:
            fallback = c
        try:
            conf = json.loads(c.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print("配置文件读取失败 %s：%s" % (c, exc), file=sys.stderr)
            continue
        fs = ((conf.get("backends") or {}).get("feishu") or {})
        if fs.get("base_token"):
            return fs.get("base_token"), dict(fs.get("base_tables") or {}), c
    return "", {}, fallback


def resolve_base(token_arg="", config_arg="", vault_hint=""):
    """定出 (base_token, 缺 table_id 的表名, 配置文件路径)。

    优先级：命令行 > 环境变量 > 配置文件。
    成功后 TABLES[表]["table_id"] 已就地注入，调用方直接用即可。
    """
    token = token_arg or os.environ.get("FEISHU_BASE_TOKEN") or ""
    conf_token, conf_tables, path = load_base_conf(config_arg, vault_hint)
    if not token:
        token = conf_token
    missing = []
    for name, spec in TABLES.items():
        env_key = TABLE_ENV_KEYS.get(name)
        tid = (os.environ.get("FEISHU_BASE_TABLE_%s" % env_key) if env_key else "") \
            or conf_tables.get(name) or ""
        spec["table_id"] = tid
        if not tid:
            missing.append(name)
    return token, missing, path


class LarkError(RuntimeError):
    """飞书接口调用失败（认证、限流、NOTEXIST、参数错误等）。"""


def _identity(override=None):
    """lark-cli 调用身份：默认 user；可用环境变量 LARK_CLI_IDENTITY 覆盖。

    bot 严格模式宿主（整体环境只放行 bot 身份）会在 lark() 里自动回退 bot。
    """
    return str(override or os.environ.get("LARK_CLI_IDENTITY") or "user").strip().lower()


def _force_utf8_io():
    """Windows 控制台默认 GBK，打不出 ✓/△ 与中文标点——强制 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_force_utf8_io()


# lark-cli 的输出带 ANSI 颜色码，直接 json.loads 会失败——先剥掉。
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _native_binary():
    """找 lark-cli 的原生二进制（node_modules/@larksuite/cli/bin/lark-cli[.exe]）。"""
    roots = []
    shim = shutil.which("lark-cli") or shutil.which("lark-cli.cmd")
    if shim:
        base = Path(shim).parent
        roots += [base / "node_modules" / "@larksuite" / "cli", base / "@larksuite" / "cli"]
    # v2.20 补：用户级 npm 安装（~/.local/lib/node_modules/...）——实测环境
    # lark-cli 装在 ~/.local/bin（shim→scripts/run.js），原生二进制在
    # ~/.local/lib/node_modules/@larksuite/cli/bin/，上面按 shim 同级探测会漏。
    for home_root in (
        Path.home() / ".local" / "lib" / "node_modules" / "@larksuite" / "cli",
        Path.home() / ".npm-global" / "lib" / "node_modules" / "@larksuite" / "cli",
    ):
        roots.append(home_root)
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "npm" / "node_modules" / "@larksuite" / "cli")
    names = ["lark-cli.exe", "lark-cli"] if os.name == "nt" else ["lark-cli", "lark-cli.exe"]
    for root in roots:
        for name in names:
            cand = root / "bin" / name
            if cand.exists():
                return cand
    return None


def lark_entry():
    """解析 lark-cli 的真实入口，**优先原生二进制**。

    为什么不用 node + run.js：npm 包装链 run.js 用
    `execFileSync(bin, args, {stdio:"inherit"})` 转发参数，stdout 直接继承父进程，
    被 Python subprocess 捕获时拿到的是**空字符串**——不是超时也不是报错，最容易误判成
    「飞书接口有问题」。直接调原生二进制就没有这层转发，输出可被捕获。

    回退顺序：环境变量 LARK_CLI_ENTRY → 原生二进制 → node + run.js。
    """
    override = os.environ.get("LARK_CLI_ENTRY")
    if override and Path(override).exists():
        p = Path(override)
        if p.suffix.lower() == ".js" or p.suffix.lower() == ".cjs":
            node = shutil.which("node")
            if not node:
                raise LarkError("找不到 node 可执行文件（装 lark-cli 时自带）")
            return [node, str(p)]
        return [str(p)]

    native = _native_binary()
    if native:
        return [str(native)]

    node = shutil.which("node")
    if not node:
        raise LarkError("找不到 node 可执行文件（装 lark-cli 时自带）")
    candidates = []
    shim = shutil.which("lark-cli") or shutil.which("lark-cli.cmd")
    if shim:
        base = Path(shim).parent
        candidates += [
            base / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js",
            base / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.cjs",
        ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            Path(appdata) / "npm" / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
        )
    for c in candidates:
        if c.exists():
            return [node, str(c)]
    raise LarkError("找不到 lark-cli 可执行入口（可用环境变量 LARK_CLI_ENTRY 指向它）")


def lark(args, base_token, retries=3, backoff=5, identity=None):
    """调 lark-cli 并解析返回的 JSON。失败重试若干次（退避），仍失败则抛 LarkError。

    注意：lark-cli 不加 --jq 时 stdout 就是内层 JSON（带 ok/identity/data），
    加 --jq 会多包一层 wrapper——本脚本一律不加 --jq，直接吃内层。
    """
    cmd = lark_entry() + list(args) + ["--base-token", base_token, "--as", _identity(identity), "--format", "json"]
    last = ""
    for attempt in range(retries):
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        raw = ANSI_RE.sub("", (proc.stdout or "")).strip()
        # v2.20：bot 严格模式宿主（--as user 会被整环境拒绝）自动回退 bot 身份重试。
        blob = ANSI_RE.sub("", ((proc.stdout or "") + (proc.stderr or "")))
        if '"subtype": "invalid_argument"' in blob and "strict mode" in blob and "bot" in blob \
                and _identity() != "bot":
            return lark(args + [], base_token, retries=retries, backoff=backoff, identity="bot")
        # lark-cli 可能在 JSON 前后夹带日志行——只取最外层那个 JSON 对象。
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            head = raw[:160]
            tail = raw[-160:] if len(raw) > 160 else ""
            err = ANSI_RE.sub("", (proc.stderr or "")).strip()
            last = "无法解析输出（%s）：头=%r 尾=%r%s" % (
                exc, head, tail,
                ("；stderr=%r" % err[:200]) if err else ("；stdout 为空——检查是否走了 stdio 继承的转发链"
                                                        if not raw else ""),
            )
        else:
            if obj.get("ok"):
                return obj.get("data") or {}
            last = "接口报错：%s" % json.dumps(obj.get("error") or {}, ensure_ascii=False)
        if attempt < retries - 1:
            time.sleep(backoff * (attempt + 1))
    raise LarkError(last)


def list_fields(base_token, table_id):
    """取一表全部字段。"""
    data = lark(["base", "+field-list", "--table-id", table_id], base_token)
    return data.get("fields") or []


def get_field(base_token, table_id, field_id):
    """取单个字段的完整定义（PUT 前必读，field-update 是 full PUT 语义）。"""
    data = lark(["base", "+field-get", "--table-id", table_id, "--field-id", field_id], base_token)
    fld = data.get("field")
    if fld is None:
        fld = data.get("field_get")
    if fld is None:
        fld = data
    return fld if isinstance(fld, dict) else {}


def create_fields(base_token, table_id, fields):
    """新建列（数组一次建完）。"""
    payload = json.dumps(fields, ensure_ascii=False)
    lark(["base", "+field-create", "--table-id", table_id, "--json", payload], base_token)


def update_field(base_token, table_id, field_id, definition):
    """整体替换列定义（high-risk-write，带 --yes）。"""
    payload = json.dumps(definition, ensure_ascii=False)
    lark(
        ["base", "+field-update", "--table-id", table_id, "--field-id", field_id,
         "--json", payload, "--yes"],
        base_token,
    )


def option_names(field):
    return [o.get("name") for o in (field.get("options") or [])]


def diff_table(table_name, spec, current):
    """算差异：缺哪些列、哪些枚举缺哪些值。返回 (缺列, 枚举差异)。"""
    by_name = {f.get("name"): f for f in current}
    missing = [w for w in spec["add_fields"] if w["name"] not in by_name]

    enum_diffs = []
    for fname, values in spec["enums"].items():
        fld = by_name.get(fname)
        if fld is None:
            enum_diffs.append({"field": fname, "mode": "缺列", "want": values, "have": []})
            continue
        have = option_names(fld)
        if (table_name, fname) in REPLACE_ENUMS:
            if have != values:
                enum_diffs.append({"field": fname, "mode": "整体替换", "want": values, "have": have})
        else:
            lack = [v for v in values if v not in have]
            if lack:
                enum_diffs.append({"field": fname, "mode": "补齐", "want": lack, "have": have})
    return missing, enum_diffs


def export_current(base_token, path):
    """导出当前结构——改前备份。"""
    snap = {"base_token": base_token, "exported_at": datetime.now().isoformat(timespec="seconds"), "tables": {}}
    for name, spec in TABLES.items():
        fields = list_fields(base_token, spec["table_id"])
        snap["tables"][name] = {
            "table_id": spec["table_id"],
            "fields": [
                {"id": f.get("id"), "name": f.get("name"), "type": f.get("type"),
                 "options": option_names(f)}
                for f in fields
            ],
        }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def apply_table(base_token, table_name, spec, current, dry_run):
    """把一张表补齐到目标结构。返回做了什么。"""
    done = []
    by_name = {f.get("name"): f for f in current}
    missing, enum_diffs = diff_table(table_name, spec, current)

    if missing:
        names = [m["name"] for m in missing]
        if not dry_run:
            create_fields(base_token, spec["table_id"], missing)
        done.append("加列 %d 个：%s" % (len(missing), "、".join(names)))

    for d in enum_diffs:
        fname = d["field"]
        if d["mode"] == "缺列":
            # 枚举列整体缺失——当作缺列处理（不猜类型，静跳过并报告）
            done.append("跳过：枚举列「%s」不存在，需人工确认类型" % fname)
            continue
        fld = by_name.get(fname)
        if fld is None:
            continue
        if d["mode"] == "整体替换":
            if not dry_run:
                # full PUT：先读现状，把 id 去掉，只发目标态
                cur = get_field(base_token, spec["table_id"], fld.get("id") or fname)
                definition = {k: v for k, v in cur.items() if k not in ("id", "option_id")}
                definition["name"] = fname
                definition["type"] = cur.get("type") or "select"
                definition["multiple"] = bool(cur.get("multiple"))
                definition["options"] = [{"name": v} for v in d["want"]]
                update_field(base_token, spec["table_id"], fld.get("id") or fname, definition)
            done.append("替换枚举「%s」：%s → %s" % (fname, "、".join(d["have"]) or "空", "、".join(d["want"])))
        else:
            if not dry_run:
                cur = get_field(base_token, spec["table_id"], fld.get("id") or fname)
                definition = {k: v for k, v in cur.items() if k not in ("id", "option_id")}
                definition["name"] = fname
                definition["type"] = cur.get("type") or "select"
                definition["multiple"] = bool(cur.get("multiple"))
                opts = list(cur.get("options") or [])
                have = {o.get("name") for o in opts}
                for v in d["want"]:
                    if v not in have:
                        opts.append({"name": v})
                definition["options"] = opts
                update_field(base_token, spec["table_id"], fld.get("id") or fname, definition)
            done.append("补齐枚举「%s」：+%s" % (fname, "、".join(d["want"])))
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description="飞书多维表格结构定义与同步器")
    ap.add_argument("--check", action="store_true", help="只读比对，输出差异")
    ap.add_argument("--apply", action="store_true", help="应用差异（幂等）")
    ap.add_argument("--export", metavar="PATH", help="导出当前结构到文件")
    ap.add_argument("--dry-run", action="store_true", help="配合 --apply：走流程但不真写")
    ap.add_argument("--base-token", default="", help="多维表格 token（默认取环境变量或配置文件）")
    ap.add_argument("--config", default="", help="opc.config.json 路径（默认自动查找）")
    ap.add_argument("--vault", default="", help="Vault 目录（取其中的 opc.config.json）")
    args = ap.parse_args(argv)

    if not (args.check or args.apply or args.export):
        ap.print_help()
        return 2

    token, missing_tables, cfg_path = resolve_base(args.base_token, args.config, args.vault)
    if not token:
        print("未配置台账地址（app_token）。任选一种：\n"
              "  1) 环境变量 FEISHU_BASE_TOKEN=<app_token>\n"
              "  2) 配置文件 %s 的 backends.feishu.base_token"
              % (cfg_path or "opc.config.json"), file=sys.stderr)
        return 2
    if missing_tables:
        print("以下表缺 table_id（配置 backends.feishu.base_tables，"
              "或用环境变量 FEISHU_BASE_TABLE_* 单表覆盖）：\n  %s"
              % "、".join(missing_tables), file=sys.stderr)
        return 2
    try:
        if args.export:
            path = export_current(token, args.export)
            print("已导出当前结构 → %s" % path)
            return 0

        if args.apply and not args.dry_run:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            bpath = BACKUP_DIR / ("before-%s.json" % stamp)
            export_current(token, bpath)
            print("改前备份 → %s" % bpath)

        total = 0
        for name, spec in TABLES.items():
            current = list_fields(token, spec["table_id"])
            missing, enum_diffs = diff_table(name, spec, current)
            if not missing and not enum_diffs:
                print("✓ %s：已一致（%d 列）" % (name, len(current)))
                continue
            total += len(missing) + len(enum_diffs)
            print("△ %s：" % name)
            for m in missing:
                print("    缺列：%s（%s）" % (m["name"], m["type"]))
            for d in enum_diffs:
                if d["mode"] == "整体替换":
                    print("    枚举「%s」需整体替换：现有 %s → 目标 %s"
                          % (d["field"], "、".join(d["have"]) or "空", "、".join(d["want"])))
                elif d["mode"] == "补齐":
                    print("    枚举「%s」缺：%s（现有 %s）"
                          % (d["field"], "、".join(d["want"]), "、".join(d["have"])))
                else:
                    print("    枚举列「%s」不存在" % d["field"])

            if args.apply:
                done = apply_table(token, name, spec, current, args.dry_run)
                for line in done:
                    print("    → %s" % line)

        if args.check:
            print("\n合计差异 %d 项" % total)
            return 1 if total else 0
        print("\n%s" % ("试跑完成（未写飞书）" if args.dry_run else "应用完成"))
        return 0
    except LarkError as exc:
        print("飞书接口调用失败：%s" % exc, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
