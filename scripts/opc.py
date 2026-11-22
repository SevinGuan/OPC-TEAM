#!/usr/bin/env python3
"""opc-team CLI：初始化、地址绑定、建项、相似检索、收尾与自检。

仅依赖 Python 标准库。所有路径以 Vault 根目录为基准，
逻辑键 -> URI 的解析由 opc.config.json 与 adapters.py 完成。
"""

import argparse
import errno
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters import get_backend, ExecBackend  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_TEMPLATE = SKILL_DIR / "opc.config.json"
ASSETS = SKILL_DIR / "assets"
REFERENCES = SKILL_DIR / "references"

BASE_DIRS = [
    "00_收件箱/agent-output",
    # v2.17：10_经营与决策/ 取消 —— 脑爆没立项 = 零留痕（ADR-0005 D2）；
    # 立项记录落在项目目录的 _版本台账.md；v2.20 起约束卡也随版本落
    # <版本目录>/strategist/0_约束卡.md，不再单独开目录。
    "20_知识资产",
    "30_项目",
    "40_模板与规范",
    "50_归档",
    "99_Agent配置/岗位卡",
    "99_Agent配置/履历",
    "99_Agent配置/scope",
    "99_Agent配置/评测",
]

DEFAULT_KNOWLEDGE_DOMAINS = ["行业洞察", "商业模式", "产品方法", "设计规范", "技术架构", "增长与运营", "合规与财务"]

STEP_OWNER = {  # 旧版配置（无 profiles 段）的兼容回退
    "0_约束卡.md": ("strategist", "0"),
    "1_MRD.md": ("market", "1"),
    "2_立项决策.md": ("strategist", "2"),
    "3_BRD.md": ("strategist", "3"),
    "4_产品定义.md": ("pm", "4"),
    "5_设计交付.md": ("design", "5"),
    "6_实现与测试.md": ("engineer", "6"),
    "7_上线.md": ("growth", "7"),
    "8_复盘.md": ("pm", "8"),
}

# v2.17（ADR-0005 D10 / §4.4）：索引列重排 —— **首列给人读的项目名**，
# 短 ID 退到末列「ID」继续作机器主键（交接状态、逻辑键都按它定位），
# 末尾追加「时间」「版本」两列。
INDEX_COLUMNS = ["项目", "类型标签", "role", "阶段", "逻辑键", "物理地址 URI",
                  "简历条目", "关联规则", "状态", "时间", "版本", "ID"]
# v2.16 旧列序（表头首格为 project 时按此解析，保证老 Vault 不迁移也能读）
LEGACY_INDEX_COLUMNS = ["project", "项目名", "类型标签", "role", "阶段", "逻辑键",
                        "物理地址 URI", "简历条目", "关联规则", "状态"]
INDEX_SEP = "|---" * len(INDEX_COLUMNS) + "|"


DEFAULT_VAULT = "./OPC-Vault"


def prompt(message, default=""):
    try:
        answer = input(message).strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    return answer or default


def choose_directory():
    """尝试弹出系统目录选择框；无 GUI / 无 tkinter 时返回空串由调用方回退。"""
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        folder = filedialog.askdirectory(title="选择 OPC Vault 保存目录")
        root.destroy()
        return folder or ""
    except Exception:
        return ""


def interactive_enabled(args):
    return not getattr(args, "non_interactive", False)


def read_text(path):
    return io.open(str(path), encoding="utf-8", newline="").read()


def write_text(path, text):
    """原子写：先写同目录临时文件，再 `os.replace` 换掉目标。

    v2.13：并发会话共享状态（任务池 / 配置 / 交接状态）实测出现过
    ①「后写覆盖先写」（认领记录丢失）②「读到写了一半的文件」。
    原子写保证读者永远只看到完整版本。
    Windows 上目标正被别的进程打开时 `os.replace` 报 PermissionError，
    这里做短暂重试后仍失败才抛出。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp." + str(os.getpid()))
    with io.open(str(tmp), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    last = None
    for _ in range(40):
        try:
            os.replace(str(tmp), str(path))
            return
        except PermissionError as exc:
            last = exc
            time.sleep(0.05)
    try:
        os.remove(str(tmp))
    except OSError:
        pass
    raise last


class VaultLock:
    """Vault 级跨进程锁：把共享状态的「读 → 改 → 写」变成临界区。

    v2.13 修正（实测复现的并发缺陷）：`_职业任务池.json` 是**共享单文件**，
    两个会话同时 `claim` 会各自读到旧内容，后写覆盖先写 →
    **认领记录丢失**（实测一轮只认领成功 1 条）。

    实现：`O_CREAT|O_EXCL` 抢占 + 自旋重试；超过 60 秒的残留锁按
    「持锁进程已死」处理并自动打破（否则一次崩溃就永久堵死认领）。
    锁文件放在 Vault 内 → Vault 挂在共享盘时跨机器也互斥。
    """

    STALE_SECONDS = 60.0

    def __init__(self, vault, timeout=20.0):
        self.path = Path(vault) / "99_Agent配置" / ".opc.lock"
        self.timeout = timeout
        self.acquired = False

    def _break_stale(self):
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return
        if age > self.STALE_SECONDS:
            try:
                os.remove(str(self.path))
            except OSError:
                pass

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                self.acquired = True
                return self
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.EACCES, errno.EPERM):
                    raise
                self._break_stale()
                if time.time() > deadline:
                    raise ValueError("Vault 正被另一个会话写入（锁等待超时 "
                                     + str(self.timeout) + " 秒）；请稍后重试")
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.acquired:
            try:
                os.remove(str(self.path))
            except OSError:
                pass
        return False


def load_config(vault):
    path = Path(vault) / "opc.config.json"
    if path.exists():
        return json.loads(read_text(path))
    return json.loads(read_text(CONFIG_TEMPLATE))


def save_config(vault, cfg):
    write_text(Path(vault) / "opc.config.json", json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


def schema_version(cfg):
    return cfg.get("schema", "opc-team/v2.4").split("/")[-1]


def constitution_version(vault, cfg=None):
    """Vault 里**宪法文件的实际版本**（读 `| 版本 | vX.Y |` 行）；读不到再退回配置声明。

    v2.13 修正：`pinned_refs` 此前取 `opc.config.json` 的**静态** `schema` 字段，
    而该字段只在 init 时写一次 —— 换包升版后老 Vault 仍写着旧版本号，
    于是**新建项目会钉到一个 Vault 里并不存在的规范版本**（实测：宪法已是 v2.13，
    新项目却钉 `OPC-架构规范@v2.12`）。钉扎必须钉「Vault 里真实存在的那一份」。
    """
    p = Path(vault) / "OPC-架构规范.md"
    if p.exists():
        try:
            m = re.search(r"^\|\s*版本\s*\|\s*(v[\d.]+)\s*\|", read_text(p), re.M)
            if m:
                return m.group(1)
        except OSError:
            pass
    return schema_version(cfg or {})


def available_profiles(cfg):
    return list(cfg.get("profiles", {}).keys())


def role_bindings_ref(cfg):
    """返回当前 profile 的 role_bindings 实际字典（可变引用，改动会随 save_config 落盘）。

    新版配置（含 profiles 段）按 active_profile 取；旧版配置回退顶层 role_bindings。
    """
    profiles = cfg.get("profiles")
    if profiles:
        name = cfg.get("active_profile", "app")
        if name not in profiles:
            raise ValueError("未登记的 profile：" + name + "；可选：" + ", ".join(profiles))
        return profiles[name].setdefault("role_bindings", {})
    return cfg.setdefault("role_bindings", {})


def profile_view(cfg):
    """返回当前 profile 的只读视图：label / step_docs / step_owner / knowledge_domains / tags_vocab。"""
    profiles = cfg.get("profiles")
    if not profiles:
        return {
            "name": "(legacy)",
            "label": "默认（旧版配置，无 profiles 段）",
            "step_docs": cfg.get("step_docs", list(STEP_OWNER.keys())),
            "step_owner": None,
            "knowledge_domains": DEFAULT_KNOWLEDGE_DOMAINS,
            "tags_vocab": cfg.get("tags_vocab", []),
        }
    name = cfg.get("active_profile", "app")
    if name not in profiles:
        raise ValueError("未登记的 profile：" + name + "；可选：" + ", ".join(profiles))
    prof = profiles[name]
    return {
        "name": name,
        "label": prof.get("label", name),
        "step_docs": prof.get("step_docs", []),
        "step_owner": prof.get("step_owner"),
        "knowledge_domains": prof.get("knowledge_domains", []),
        "tags_vocab": prof.get("tags_vocab", cfg.get("tags_vocab", [])),
    }


def step_owner_map(prof):
    """doc.md -> (role, stage)。新版取 profile 的 step_owner；旧版回退内置 STEP_OWNER。"""
    if prof.get("step_owner"):
        return {doc: (v["role"], v.get("stage", doc.split("_", 1)[0])) for doc, v in prof["step_owner"].items()}
    return dict(STEP_OWNER)


def index_file(vault, cfg):
    return Path(vault) / cfg.get("index_path", "30_项目/_产出索引.md")


def parse_tags(raw):
    if not raw:
        return []
    found = re.findall(r"#?([^\s#,，]+)", raw.strip())
    seen, out = set(), []
    for t in found:
        t = t.lstrip("#")
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _index_row_name(row):
    """取行里的项目显示名：新格式在「项目」列，老格式在「项目名」列。"""
    return (row.get("项目") or row.get("项目名") or "").strip()


def _norm_index_row(row):
    """把行规整成新列序（老行迁移到新格式时用）：补「项目」列、补尾列。

    新旧键同时留着：`ID`（新末列）与 `project`（老首列 / 内部简称）都指向短 ID，
    这样读侧的老代码不用逐处改，写出去的文件是新列序。
    """
    out = dict(row)
    out["项目"] = _index_row_name(row)
    _pid = (row.get("ID") or row.get("project") or "").strip()
    out["ID"] = _pid
    out["project"] = _pid
    for c in INDEX_COLUMNS:
        out.setdefault(c, "")
    return out


def read_index(vault, cfg):
    path = index_file(vault, cfg)
    if not path.exists():
        return []
    rows = []
    cols = list(LEGACY_INDEX_COLUMNS)
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] in ("project", "项目"):
            # 表头行 → 决定本文件的列序（老表头 project 在首列 → 老列序）
            cols = list(LEGACY_INDEX_COLUMNS) if cells[0] == "project" else list(INDEX_COLUMNS)
            continue
        if len(cells) < len(cols):
            cells += [""] * (len(cols) - len(cells))
        row = dict(zip(cols, cells))
        row = _norm_index_row(row)
        if not (row.get("ID") or "").strip():
            continue
        rows.append(row)
    return rows


def write_index(vault, cfg, rows):
    lines = [
        "# 项目产出索引",
        "",
        "> 唯一结构化出处：相似项目检索、溯源、外部引用一律查本表（规范 §15.8）。",
        "> 建项写占位行（状态=进行中），收尾补齐 URI / 简历条目 / 关联规则（状态=已归档）。",
        "> 首列=项目名（人读），末列 ID=短 ID（机器主键）；末三列=立项时间 / 版本号 / 短 ID。",
        "",
        "| " + " | ".join(INDEX_COLUMNS) + " |",
        INDEX_SEP,
    ]
    for r in rows:
        nr = _norm_index_row(r)
        lines.append("| " + " | ".join(nr.get(c, "") for c in INDEX_COLUMNS) + " |")
    write_text(index_file(vault, cfg), "\n".join(lines) + "\n")


def project_dir_name(vault, project):
    """项目目录名。

    v2.19（ADR-0007）：目录名 = **项目名**（同名项目加 `-2` 后缀），**不再带年月** ——
    时间维度下沉到版本目录（`YYYY-MM-DD-V<N>`），项目名不再被建项时间污染，
    迭代出来的 V2/V3 横着排开就是时间轴。这里按四级查找：
      ① 传入值本身就是目录名 → 直接用
      ② 传入短 ID → 查产出索引：优先从「物理地址 URI」里取目录段（同名项目靠它区分），
         再退回「项目名 + 时间」还原（存量老格式）
      ③ 扫 `30_项目`：目录名 == 项目名，或 `项目名-<数字>`（`-2` 后缀）
      ④ 存量回退：`项目名_` 前缀（老格式 `项目名_YYYY-MM` / `P001_名`）
         —— 存量目录不迁移（ADR-0007 D4），但必须仍能定位
    """
    project = str(project or "")
    if not project:
        return project
    base = Path(vault) / "30_项目"
    if not base.exists():
        return project
    if (base / project).is_dir():
        return project
    pid = project_key(project)
    if pid:
        try:
            cfg = load_config(vault)
            for r in read_index(vault, cfg):
                if project_key(r.get("project")) == pid:
                    seg = _project_dir_from_uri(r)
                    if seg and (base / seg).is_dir():
                        return seg
                    nm = _index_row_name(r)
                    tm = (r.get("时间") or "").strip()
                    cand = (nm + "_" + tm) if (nm and tm) else nm
                    if cand and (base / cand).is_dir():
                        return cand
        except Exception:
            pass
    for p in sorted(base.iterdir()):
        if p.is_dir() and (p.name == project
                           or re.match(r"^" + re.escape(project) + r"-\d+$", p.name)):
            return p.name
    for p in sorted(base.iterdir()):
        if p.is_dir() and p.name.startswith(project + "_"):
            return p.name
    return project


def _project_dir_from_uri(row):
    """从索引行的「物理地址 URI」里取项目目录段（`30_项目/<目录>/…`）。"""
    uri = str(row.get("物理地址 URI") or "").strip()
    if "://" in uri:
        uri = uri.split("://", 1)[1]
    uri = uri.replace("\\", "/")
    if "30_项目/" not in uri:
        return ""
    return uri.split("30_项目/", 1)[1].split("/")[0].strip()


def _unique_project_dir(base, name):
    """项目目录名去重：`名` → `名-2` → `名-3`（同名项目靠它区分）。

    v2.19（ADR-0007）：目录名不再带年月，同名项目只能靠 `-<数字>` 后缀区分。
    """
    base = Path(base)
    if not (base / name).exists():
        return name
    i = 2
    while (base / (name + "-" + str(i))).exists():
        i += 1
    return name + "-" + str(i)


VERSION_LEDGER_NAME = "_版本台账.md"


def version_ledger_path(vault, project):
    """版本台账落点：<项目目录>/_版本台账.md。"""
    d = project_dir_name(vault, project)
    return Path(vault) / "30_项目" / d / VERSION_LEDGER_NAME


def read_versions(vault, project):
    """读版本台账 → [版本, 立项时间, 需求, 状态, 产出份数, 沉淀状态, 沉淀时间]。"""
    p = version_ledger_path(vault, project)
    if not p.exists():
        return []
    rows = []
    for line in read_text(p).splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or not re.match(r"^V\d+$", cells[0]):
            continue
        cells += [""] * (8 - len(cells))
        rows.append({"版本": cells[0], "时间": cells[1], "需求": cells[2], "状态": cells[3],
                     "产出份数": cells[4], "沉淀状态": cells[5], "沉淀时间": cells[6],
                     "立项批准": cells[7]})
    return rows


def write_versions(vault, project, rows, title=""):
    lines = [
        "# 版本台账" + ((" · " + title) if title else ""),
        "",
        "> 一个版本 = 一次立项：① 创建项目；② 项目内确认「这个需求要做」。",
        "> 立项不占闸门游标，但**必须创始人同意**（v2.29.5：`--founder-approved` 留痕）；版本号只增不复用。",
        "",
        "| 版本 | 立项时间 | 需求 | 状态 | 产出份数 | 沉淀状态 | 沉淀时间 | 立项批准 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append("| " + " | ".join([
            r.get("版本", ""), r.get("时间", ""), r.get("需求", ""), r.get("状态", ""),
            r.get("产出份数", ""), r.get("沉淀状态", "") or "—", r.get("沉淀时间", "") or "—",
            r.get("立项批准", "") or "—",
        ]) + " |")
    p = version_ledger_path(vault, project)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text(p, "\n".join(lines) + "\n")
    return p


def next_version(vault, project):
    """下一个版本号（项目内递增，只增不复用）。"""
    rows = read_versions(vault, project)
    mx = 0
    for r in rows:
        m = re.match(r"^V(\d+)$", r.get("版本", ""))
        if m:
            mx = max(mx, int(m.group(1)))
    return "V%d" % (mx + 1)


def current_version(vault, project):
    """当前版本号 = 台账最后一行；无台账（存量项目）按 V1 处理。"""
    rows = read_versions(vault, project)
    return rows[-1]["版本"] if rows else "V1"


def update_version_row(vault, project, version, **fields):
    """更新某个版本行的字段（状态 / 产出份数 / 沉淀状态 …），写回台账。"""
    rows = read_versions(vault, project)
    if not rows:
        return None
    hit = False
    for r in rows:
        if r.get("版本") == version:
            for k, v in fields.items():
                r[k] = v
            hit = True
    if hit:
        write_versions(vault, project, rows, title=project_name_of(vault, project))
    return hit


def project_name_of(vault, project):
    """项目显示名（取索引里的「项目」列）。"""
    try:
        cfg = load_config(vault)
        for r in read_index(vault, cfg):
            if project_key(r.get("project")) == project_key(project):
                nm = _index_row_name(r)
                if nm:
                    return nm
    except Exception:
        pass
    d = project_dir_name(vault, project)
    if not d:
        return str(project or "")
    d = re.sub(r"^P\d{3}_", "", d)        # 存量老格式：`P001_名`
    d = re.sub(r"_\d{4}-\d{2}$", "", d)   # 存量 v2.17 格式：`名_YYYY-MM`
    return d


def project_label_map(vault, cfg):
    """{短ID: 项目名} —— 人读标识表。

    v2.17（ADR-0005 D10）：**人读处不带 P00X** —— 面板、交接视图、履历标题
    一律显示项目名；短 ID 退为机器主键（状态文件名 / 逻辑键 / 索引第二列）。
    命令行 `--project` 仍接受项目名（`resolve_project` 负责归一）。

    一次读完索引，避免逐个项目重复读盘。
    """
    out = {}
    try:
        for r in read_index(vault, cfg):
            pid = project_key(r.get("project"))
            nm = _index_row_name(r)
            if pid and nm and pid not in out:
                out[pid] = nm
    except Exception:
        pass
    return out


def project_time_of(vault, project):
    """项目立项时间（索引「时间」列）。"""
    try:
        cfg = load_config(vault)
        for r in read_index(vault, cfg):
            if project_key(r.get("project")) == project_key(project):
                return (r.get("时间") or "").strip()
    except Exception:
        pass
    return ""


def apply_version_prefix(docs, version):
    """（v2.18 兼容壳）版本信息改由目录段承担，文件名不再加 `V<N>_` 前缀。

    保留签名以便旧调用点不炸；实际返回原样（版本目录由 resolve_uri 展开）。
    """
    return list(docs)


def project_seq_path(vault):
    return Path(vault) / "99_Agent配置" / "_项目号.seq"


def next_project_id(vault, cfg):
    """下一个项目号 Pxxx：取 产出索引 / 项目目录 / 已分配序号 三者最大 +1。"""
    mx = 0
    for r in read_index(vault, cfg):
        m = re.match(r"P(\d{3})", r.get("project", ""))
        if m:
            mx = max(mx, int(m.group(1)))
    base = Path(vault) / "30_项目"
    if base.exists():
        for p in base.iterdir():
            m = re.match(r"P(\d{3})_", p.name)
            if m:
                mx = max(mx, int(m.group(1)))
    sp = project_seq_path(vault)
    nxt = mx + 1
    if sp.exists():
        try:
            # 序号文件存的是「下一个可分配号」，不能当成「已用最大号」再 +1
            nxt = max(nxt, int(read_text(sp).strip() or 0))
        except (ValueError, OSError):
            pass
    return "P%03d" % nxt


def reserve_project_id(vault, cfg):
    """**原子占号**：在锁内取一个未被占用的项目号并落盘记名。

    v2.13：并发建项时两个会话各自算 `next_project_id` 会拿到**同一个号**
    → 目录撞名、项目混在一起。这里用「已分配序号文件」当占号凭据：
    先占号再干活，号不会重；中途失败会留下号段空缺（编号只作标识，空缺可接受）。
    """
    with VaultLock(vault):
        pid = next_project_id(vault, cfg)
        write_text(project_seq_path(vault), str(int(pid[1:]) + 1) + "\n")
        return pid


def expand_ref(tpl, vault, project, role, key, version="", dirname=""):
    """展开地址模板。

    `dirname` —— 显式给定项目目录名（建项时目录**还没进索引**，`project_dir_name`
    查不到，只能回退成短 ID；那样写出来的 URI 会指向不存在的路径，
    后面「按索引找本岗资产」就全落空）。传了就用它。
    """
    values = {
        "{vault}": str(vault),
        "{project}": dirname or project_dir_name(vault, project),
        "{project_id}": project,
        "{role}": role,
        "{key}": key,
        "{version}": version or "",
    }
    for k, v in values.items():
        tpl = tpl.replace(k, v)
    # 空 version 时清理「/{version}/」留下的双斜杠（如 cmd_resolve 不带版本）
    tpl = re.sub(r"(?<!:)/{2,}", "/", tpl)
    return tpl


def is_version_scoped(key):
    """该产出是否**版本级**（按版本分文件、落 `<版本目录>/<owner>/`）。

    v2.20（创始人拍板，修订 ADR-0007）：阶段 0/1（约束卡、MRD）**也随版本落盘** ——
    一次立项 = 一个版本，版本目录下按职业归档本次全部数据资产产出；
    取消「项目级产出落项目根」的旧设计。存量老项目根的 0/1 文件
    直接不管、不迁移，读取时用 `find_project_docs` 兜底定位。
    """
    st = key.split("_", 1)[0] if "_" in key else ""
    return st.isdigit()


def version_prefix(version):
    """版本文件名前缀（v2.18 兼容壳）：V1 不加前缀，V2+ 加 `V<N>_`。

    新目录结构下版本信息由 `V<N>/` 目录段承担，此函数仅供存量探测回退。
    """
    m = re.match(r"[vV]?(\d+)", str(version or ""))
    if not m:
        return ""
    n = int(m.group(1))
    return "" if n <= 1 else "V%d_" % n


VERSION_DIR_RE = re.compile(r"^(?:(\d{4}-\d{2}-\d{2})-)?V(\d+)$")


def version_no(version):
    """版本号 → 整数（`V2` / `2` → 2；认不出来 → 0）。"""
    m = re.match(r"^[vV]?(\d+)", str(version or ""))
    return int(m.group(1)) if m else 0


def _version_date_at(pdir, version):
    """该版本的**立项日期**（台账「时间」列 → `YYYY-MM-DD`）；查不到返回空串。

    日期**只从版本台账取**（台账 = 版本链唯一真相源），不现算「今天」——
    否则同一个版本在不同日期解析出两个目录名，事后 `done/queue/pm` 就找不到文件了。
    """
    n = version_no(version)
    led = Path(pdir) / VERSION_LEDGER_NAME
    if not n or not led.exists():
        return ""
    for line in read_text(led).splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or not re.match(r"^V\d+$", cells[0]):
            continue
        if version_no(cells[0]) != n:
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", cells[1] if len(cells) > 1 else "")
        return m.group(1) if m else ""
    return ""


def version_dir_at(pdir, version):
    """版本目录段（在指定项目目录下解析）。

    v2.19（ADR-0007）：新式 `YYYY-MM-DD-V<N>`（日期 = 该版本**立项日**），
    存量老式 `V<N>` 回退。判定顺序：**先看目录是否真的存在** → 再退老式 →
    都没有才按「台账日期 / 今天」拼新式名（新建版本时目录刚建、还没落台账）。
    """
    n = version_no(version)
    if not n:
        return ""
    pdir = Path(pdir)
    d = _version_date_at(pdir, version) or date.today().isoformat()
    cand = "%s-V%d" % (d, n)
    if (pdir / cand).is_dir():
        return cand
    old = "V%d" % n
    if (pdir / old).is_dir():
        return old
    return cand


def version_dir(vault, project, version, dirname=""):
    """版本目录段（按项目目录定位）。"""
    pdir = Path(vault) / "30_项目" / (dirname or project_dir_name(vault, project))
    return version_dir_at(pdir, version)


def version_dirs(pdir):
    """项目目录下的全部版本目录（升序）：新式 `YYYY-MM-DD-V<N>` + 存量 `V<N>`。"""
    out = []
    pdir = Path(pdir)
    if not pdir.is_dir():
        return out
    for p in pdir.iterdir():
        if not p.is_dir():
            continue
        m = VERSION_DIR_RE.match(p.name)
        if m:
            out.append((int(m.group(2)), p.name, p))
    out.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in out]


def find_project_docs(pdir, fname):
    """项目文档（如 `0_约束卡.md`）的全部现存物理路径，按版本升序，项目根平铺排最后。

    v2.20：阶段 0/1 随版本落 `<版本目录>/<owner>/<fname>`；存量老项目平铺在
    项目根 —— 直接不管、不迁移（创始人拍板），读取 / 重钉按本函数兜底定位。
    同名文件在多个版本都存在时全部返回（调用方自行取最新或逐份处理）。
    """
    pdir = Path(pdir)
    out = []
    for vdir in version_dirs(pdir):
        for rd in sorted(vdir.iterdir()):
            if rd.is_dir() and (rd / fname).exists():
                out.append(rd / fname)
    root = pdir / fname
    if root.exists():
        out.append(root)
    return out


def artifact_ref(cfg, role, key):
    bindings = role_bindings_ref(cfg)
    binding = bindings.get(role)
    if not binding:
        raise ValueError("未登记的 role：" + role + "；当前 profile 可选：" + ", ".join(bindings.keys()))
    arts = binding.get("artifacts") or {}
    if key not in arts:
        raise ValueError("role " + role + " 未定义产出键 " + key + "；可选：" + ", ".join(arts.keys()))
    return binding.get("backend", cfg.get("default_backend", "local")), arts[key]


def resolve_uri(cfg, vault, role, key, project, version="", dirname=""):
    """解析逻辑键为 URI。

    v2.19（ADR-0007）：版本级产出按 `<版本目录>/<role>/` 目录组织 —— 版本目录 =
    `YYYY-MM-DD-V<N>`（日期 = 该版本立项日）；存量老项目仍是 `V<N>/`。
    v2.20（创始人拍板）：阶段 0/1（约束卡、MRD）也随版本落
    `<版本目录>/<owner>/`（约束卡 → strategist/，MRD → market/），
    不再落项目根；存量项目根的平铺副本读取时由 `find_project_docs` 兜底。
    `{version}` 占位符展开为版本目录段；`{role}` 占位符展开为职业名。
    config 模板未写占位符时 → 自动在文件名前注入 `/<版本目录>/<role>/` 目录段
    （兼容未升级模板的存量配置）。

    **目录段取该文件的 owner**（宪法 §15.5 一文件一 owner）：链上主文档用
    `step_owner` 里登记的 owner，其余资产用登记它的职业。
    这样阶段 7 的 `7_上线.md`（growth 是 owner、engineer 补 `## 上线与回滚` 章节）
    仍是**同一个文件**，协作章节不会被拆成两份。
    """
    backend_name, tpl = artifact_ref(cfg, role, key)
    vdir = version_dir(vault, project, version, dirname) if is_version_scoped(key) else ""
    dir_role = role
    base = tpl.rsplit("/", 1)[-1]
    so = (profile_view(cfg).get("step_owner") or {}).get(base) or {}
    if so.get("role"):
        dir_role = so["role"]
    if vdir and "{version}" not in tpl:
        # 模板未写 {version} / {role} 占位 → 注入版本 + 职业目录段
        tpl = re.sub(r"/([^/]+)$",
                     lambda m: "/" + vdir + "/" + dir_role + "/" + m.group(1), tpl)
    ref = expand_ref(tpl, vault, project, dir_role, key, vdir, dirname)
    backend = get_backend(backend_name, cfg, vault)
    return backend_name, backend.resolve(ref)


def step_frontmatter(project, role, key, today, uri, source="", pinned=None):
    """生成项目文档 frontmatter。

    source / pinned_refs 按宪法 §5 必填：source 写上游交接文件（禁止为空），
    pinned_refs 只在 0_约束卡.md 上写（宪法 §10 钉扎）。
    """
    lines = [
        "---",
        "type: 项目文档",
        "project: " + project,
        "roles: [" + role + "]",
        "status: 草稿",
        "version: v1.0",
        "source: \"" + (source or "") + "\"",
        "project_refs: []",
    ]
    if pinned:
        lines.append("pinned_refs: [" + ", ".join(pinned) + "]")
    lines += [
        "artifact_uri: \"" + uri + "\"",
        "has_pii: false",
        "created: " + today,
        "updated: " + today,
        "---",
        "",
        "# " + key,
        "",
        "> 由 opc-team Skill 生成。按 Vault 根目录 OPC-架构规范.md §3 路由表与对应岗位卡填写。",
        "> 产出地址按 §15.8 由 opc.config.json 解析；变更后端无需修改本文件。",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# §15.14 需求分诊：pm 派活清单是路由的唯一真相源
# ---------------------------------------------------------------------------

TRIAGE_HEADING = "本次涉及岗位与产出资产清单"
TRIAGE_ROLES = ("涉及", "是", "✅", "√", "Y", "y", "TRUE", "true")
TRIAGE_NO = ("不涉及", "否", "无", "—", "-", "N", "n", "FALSE", "false")


def _triage_cells(line):
    if not line.strip().startswith("|"):
        return None
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if not cells or all(set(c) <= set("-: ") for c in cells):
        return None
    if cells[0] in ("岗位", "role", "职业", "---"):
        return None
    return cells


def parse_triage(text, known_roles=None):
    """解析 pm 的「本次涉及岗位与产出资产清单」（Markdown 表格形态）。

    返回 {
      "found": bool,          是否找到清单
      "roles": [role...],     本次涉及的岗位
      "assets": {role: [产出键...]},  每个岗位本次要产出的资产
      "zero": bool,           pm 判定「零岗位」
      "reason": str,          分诊理由（零岗位时必填）
      "raw": [(role, 涉及?, 资产, 理由)...]
      "errors": [str...],     解析过程中发现的问题（v2.11 新增）
    }
    找不到清单时 found=False —— 调用方按全量处理，不得静默猜路由。
    **解析问题一律收集进 errors，不再静默跳过**（v2.11：静默跳过 = 悄悄派错活）。
    """
    out = {"found": False, "roles": [], "assets": {}, "zero": False,
           "reason": "", "raw": [], "errors": []}
    if not text:
        return out
    lines = text.splitlines()
    # 定位清单所在的小节：从标题行往下，直到下一个 H2
    start = -1
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("#") and TRIAGE_HEADING in ln:
            start = i
            break
    if start < 0:
        # 也接受正文里直接出现该标题的情况
        for i, ln in enumerate(lines):
            if TRIAGE_HEADING in ln:
                start = i
                break
        if start < 0:
            return out
    out["found"] = True
    seg = []
    for ln in lines[start + 1:]:
        if ln.startswith("## ") and not ln.startswith("### "):
            break
        seg.append(ln)
    # 第一遍：先认「零岗位」这类显式声明（它可能在表格前，也可能在表格后）
    for ln in seg:
        if _triage_cells(ln):
            continue
        s = ln.strip()
        if re.search(r"零\s*岗位|无需任何岗位|不涉及任何岗位|本次无需岗位", s):
            out["zero"] = True
            out["reason"] = s.lstrip("#-*> ").strip()

    # 第二遍：处理表格行
    for ln in seg:
        cells = _triage_cells(ln)
        if not cells:
            continue
        if len(cells) < 2:
            out["errors"].append("表格行少于 2 列（至少要有「岗位」和「本次是否涉及」两列）："
                                 + ln.strip()[:60])
            continue
        role = cells[0].strip("`* ")
        if not role:
            continue
        verdict = cells[1].strip()
        assets_raw = cells[2] if len(cells) > 2 else ""
        reason = cells[3] if len(cells) > 3 else ""
        # 判定「涉及 / 不涉及」
        involved = None
        if verdict in TRIAGE_ROLES:
            involved = True
        elif verdict in TRIAGE_NO:
            involved = False
        else:
            low = verdict.lower()
            if low in ("涉及", "true"):
                involved = True
            elif low in ("不涉及", "false"):
                involved = False
        if involved is None:
            out["errors"].append("岗位「" + role + "」的『本次是否涉及』列写了「" + verdict
                                 + "」，不认识 —— 只认「涉及 / 不涉及」（或 是/否）")
            continue
        out["raw"].append((role, involved, assets_raw, reason))
        if known_roles and role not in known_roles:
            # 「零岗位」是合法的声明式写法（不是岗位名），不算错
            if out["zero"] or re.search(r"零\s*岗位|无需任何岗位", role):
                continue
            out["errors"].append("岗位「" + role + "」未登记 —— 可用岗位："
                                 + "、".join(sorted(known_roles)))
            continue
        if not involved:
            continue
        out["roles"].append(role)
        keys = []
        for a in re.split(r"[、,，/;；\s]+", assets_raw):
            a = a.strip().strip("`").strip()
            if a and a not in ("—", "-", "无"):
                keys.append(a)
        # 允许 pm 写 "5_原型.html" / "5_原型" 两种形式，统一去掉扩展名再比对
        keys = [k.rsplit(".", 1)[0] if re.search(r"\.(md|html|yaml|yml)$", k) else k for k in keys]
        out["assets"][role] = keys
    if out["zero"]:
        out["roles"] = []
        out["assets"] = {}
    elif out["found"] and not out["roles"]:
        # 清单小节在，但没有一行判出「涉及」——多半是表格写坏了。
        # 静默降级成全量 9 步 = 悄悄派了 8 个不该派的岗位（v2.11 起显式报错）。
        out["errors"].append("清单小节存在，但没有一行判出「涉及」的岗位"
                             + ("（另有 " + str(len(out["errors"])) + " 处问题）" if out["errors"] else ""))
    return out


def triage_source_doc(vault, project, key="4_产品基线"):
    """在项目目录里找 pm 的分诊清单所在文档，返回 (路径, 文本)；找不到返回 (None, "")。

    v2.19（ADR-0007）：产出按 `<版本目录>/<role>/` 目录组织 —— 4_产品基线 是 pm 的版本级产出，
    落 `<YYYY-MM-DD-V<N>>/pm/4_产品基线.md`；兼容存量平铺（项目根同名文件）与老式 `V<N>/`。
    """
    pdir = Path(vault) / "30_项目" / project_dir_name(vault, project)
    if not pdir.exists():
        return None, ""
    cands = []
    for vdir in version_dirs(pdir):
        cands.append(vdir / "pm" / (key + ".md"))
    cands.append(pdir / (key + ".md"))  # 存量平铺兜底
    seen = set()
    for p in cands:
        if p in seen or not p.exists():
            continue
        seen.add(p)
        try:
            txt = read_text(p)
        except OSError:
            continue
        if TRIAGE_HEADING in txt:
            return p, txt
    return None, ""


# ---------------------------------------------------------------------------
# v2.11：结构化派活清单（派活入口）
# ---------------------------------------------------------------------------
# 痛点（2026-11-11 实测）：pm 起草派活清单时**没有落点** —— 只能随便写个临时
# md 文件再把路径喂给 `opc new --triage`；而且清单解析失败会**静默降级成全量
# 9 步**（= 悄悄派了 8 个不该派的岗位）。
# 解法：固定落点 + 结构化格式 + 解析失败显式报错。

TRIAGE_FILE = "_派活清单.json"


def triage_staging_dir(vault):
    """待建区：项目还没建起来时，派活清单先落这里（不散落在临时目录里）。"""
    return Path(vault) / "99_Agent配置" / "_派活清单"


def triage_json_path(vault, project):
    """结构化派活清单的固定落点。

    项目目录已存在 → 项目目录内 `_派活清单.json`；
    项目还没建     → 待建区 `99_Agent配置/_派活清单/<项目号>.json`。
    两种落点都在 Vault 内，**不散落在任意临时目录**（散落 = 下次找不到）。
    """
    pdir = Path(vault) / "30_项目" / project_dir_name(vault, project)
    if pdir.is_dir():
        return pdir / TRIAGE_FILE
    return triage_staging_dir(vault) / (str(project) + ".json")


def read_triage_json(vault, project):
    """读结构化派活清单；不存在返回 None。"""
    p = triage_json_path(vault, project)
    if not p.exists():
        return None
    try:
        return json.loads(read_text(p))
    except (ValueError, OSError):
        return None


def write_triage_json(vault, project, data):
    p = triage_json_path(vault, project)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text(p, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return p


def parse_triage_json(text, known_roles=None):
    """解析结构化派活清单（opc triage 写的 JSON）。

    结构：
      {"project": "P001", "zero": false, "reason": "",
       "entries": [{"role": "design", "involved": true,
                    "assets": ["5_设计交付.md"], "reason": "改首页视觉"}]}
    返回结构与 parse_triage 一致（含 errors）。
    """
    out = {"found": False, "roles": [], "assets": {}, "zero": False,
           "reason": "", "raw": [], "errors": []}
    try:
        data = json.loads(text)
    except ValueError as e:
        out["errors"].append("JSON 解析失败：" + str(e))
        return out
    if not isinstance(data, dict):
        out["errors"].append("结构化派活清单必须是 JSON 对象")
        return out
    out["found"] = True
    out["zero"] = bool(data.get("zero"))
    out["reason"] = str(data.get("reason") or "")
    entries = data.get("entries")
    if not isinstance(entries, list):
        out["errors"].append("结构化派活清单缺 entries 数组")
        return out
    if out["zero"] and not out["reason"]:
        out["errors"].append("判定为零岗位，但没写理由")
    for e in entries:
        if not isinstance(e, dict):
            out["errors"].append("entries 里有非对象条目：" + str(e)[:40])
            continue
        role = str(e.get("role") or "").strip()
        if not role:
            out["errors"].append("entries 里有条目没写 role")
            continue
        involved = e.get("involved")
        if not isinstance(involved, bool):
            out["errors"].append("岗位「" + role + "」的 involved 必须是 true / false")
            continue
        assets = e.get("assets") or []
        if not isinstance(assets, list):
            out["errors"].append("岗位「" + role + "」的 assets 必须是数组")
            continue
        keys = []
        for a in assets:
            a = str(a).strip().strip("`")
            if a and a not in ("—", "-", "无"):
                keys.append(a.rsplit(".", 1)[0] if re.search(r"\.(md|html|yaml|yml)$", a) else a)
        reason = str(e.get("reason") or "")
        out["raw"].append((role, involved, "、".join(assets), reason))
        out.setdefault("all_roles", [])
        if role not in out["all_roles"]:
            out["all_roles"].append(role)
        if known_roles and role not in known_roles:
            out["errors"].append("岗位「" + role + "」未登记 —— 可用岗位："
                                 + "、".join(sorted(known_roles)))
            continue
        if not involved:
            continue
        out["roles"].append(role)
        out["assets"][role] = keys
    if out["zero"]:
        out["roles"] = []
        out["assets"] = {}
    elif out["found"] and not out["roles"]:
        # v2.23：说清正确用法 —— 只写「不涉及」的岗位不算一份有效清单，
        # 清单必须**把仍然涉及的岗位逐条写全**；真的一个岗位都不派，用 --zero。
        listed = "、".join(sorted(out.get("all_roles") or [])) or "（无）"
        out["errors"].append(
            "结构化派活清单里没有任何岗位判为「涉及」（当前只写了这些："
            + listed + "）。"
            "派活清单是**全量声明**：要派活的岗位必须逐条写 "
            "--role <岗位> --assets <资产> --reason \"...\"；"
            "只写要排除的岗位不够。"
            "若本次真的一个岗位都不派，用 --zero --reason \"...\"。")
    return out


def triage_markdown_section(data):
    """把结构化清单渲染成 Markdown 表格小节（人可读形态，格式同 §5.1）。"""
    lines = ["## " + TRIAGE_HEADING, "",
             "| 岗位 | 本次是否涉及 | 产出资产 | 分诊理由 |",
             "|---|---|---|---|"]
    if data.get("zero"):
        lines.append("")
        lines.append("零岗位：" + str(data.get("reason") or "（未写理由）"))
        return "\n".join(lines) + "\n"
    for e in data.get("entries") or []:
        role = str(e.get("role") or "")
        involved = "涉及" if e.get("involved") else "不涉及"
        assets = "、".join(e.get("assets") or []) or "—"
        reason = str(e.get("reason") or "")
        lines.append("| " + role + " | " + involved + " | " + assets + " | " + reason + " |")
    return "\n".join(lines) + "\n"


def triage_to_json(project, tri):
    """把解析出来的派活清单转成**结构化 JSON**（`_派活清单.json` 的形状）。

    建项时来源常常是 Markdown 清单 —— 直接把 Markdown 原文写进 `.json` 文件，
    后面 `read_triage_json` 读出来是 `None`：台账显示「无清单」、追加立项时
    找不到派活依据，于是**静默退化成全量 9 步**（路由被悄悄改掉）。
    """
    entries = []
    for role, involved, assets_raw, reason in (tri.get("raw") or []):
        assets = [a.strip().strip("`") for a in re.split(r"[、,，]", str(assets_raw or ""))
                  if a.strip() and a.strip() not in ("—", "-", "无")]
        entries.append({"role": role, "involved": bool(involved),
                        "assets": assets, "reason": str(reason or "")})
    return {"project": project, "zero": bool(tri.get("zero")),
            "reason": str(tri.get("reason") or ""), "entries": entries,
            "updated_at": date.today().isoformat()}


def sync_triage_markdown(vault, project, data):
    """把人可读形态写出去。

    两处，缺一不可：
      ① `<项目目录>/_派活清单.md` —— 永远写（与 JSON 同目录，一眼看出是同一份的两种形态）
      ② `<项目目录>/V*/pm/4_产品基线.md`（v2.18 目录结构；存量平铺在项目根）的同名 H2 小节
         —— 该文件存在时才写（只派了 design 这类岗位时 pm 根本没参与，文件不存在）

    回写只动这一个 H2 小节，别的小节一律不碰。
    """
    pdir = Path(vault) / "30_项目" / project_dir_name(vault, project)
    if not pdir.is_dir():
        return None
    section = triage_markdown_section(data)
    sidecar = pdir / ("_派活清单.md")
    write_text(sidecar, "# 派活清单（人可读形态，机器真相源见 _派活清单.json）\n\n" + section)

    # v2.19：先按 triage_source_doc 找（含清单标题的文件）；
    # 找不到时回退到标准落点 <版本目录>/pm/4_产品基线.md（scaffold 生成的骨架也能回写）
    fp = triage_source_doc(vault, project)[0]
    if not fp or not fp.exists():
        for vdir in version_dirs(pdir):
            cand = vdir / "pm" / "4_产品基线.md"
            if cand.exists():
                fp = cand
                break
    if not fp or not fp.exists():
        return sidecar
    txt = read_text(fp)
    lines = txt.splitlines()
    start = -1
    end = len(lines)
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("#") and TRIAGE_HEADING in ln:
            start = i
            break
    if start >= 0:
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("## ") and not lines[j].startswith("### "):
                end = j
                break
        new = lines[:start] + section.rstrip("\n").splitlines() + [""] + lines[end:]
    else:
        new = lines + [""] + section.rstrip("\n").splitlines() + [""]
    write_text(fp, "\n".join(new).rstrip("\n") + "\n")
    return sidecar


def copy_if_absent(src, dst):
    if Path(src).exists() and not Path(dst).exists():
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
        return 1
    return 0


def gen_jobcard_skeleton(role, prof, version):
    """为 profile 中没有手写岗位卡的 role 生成九节骨架卡。"""
    owner = step_owner_map(prof)
    mine = [(doc, stage) for doc, (r, stage) in sorted(owner.items()) if r == role]
    others = sorted({r for r, _ in owner.values() if r != role})
    mine_docs = "、".join("阶段 " + stage + "《" + doc[:-3] + "》" for doc, stage in mine) or "（无主产出文档，见岗位卡 §5）"
    inputs = []
    docs_sorted = sorted(owner.keys())
    for doc, (r, _stage) in sorted(owner.items()):
        if r != role and doc in prof.get("step_docs", []):
            inputs.append(doc[:-3])
    lines = [
        "# 岗位卡 · " + role,
        "",
        "> 当前 " + version + " | profile: " + prof.get("label", prof.get("name", "")) + " | 本卡由 init 自动生成骨架。",
        "> 九节结构不可删改；带「待补」的条目由创始人 + Agent 在首个项目开工前补全（可参照 app profile 的同名卡）。",
        "",
        "## 1 身份",
        "- 职业名称：" + role,
        "- 一句话定位：待补",
        "",
        "## 2 所属阶段",
        "- 只负责：" + mine_docs,
        "- 明确不负责：其余全部阶段与文档；涉及 " + "、".join(others) + " 的产出只读不写",
        "",
        "## 3 工作区",
        "- 常驻目录：30_项目/{project}/（本岗阶段文档） + 20_知识资产/<domain>/（只写已生效规则）",
        "",
        "## 4 输入契约",
        "- 上游文档（只读）：" + ("、".join(inputs) if inputs else "待补") + "（约束卡钉扎字段由上游逐级透传其中，见规范 §15.3）",
        "",
        "## 5 输出契约",
        "- 主产出：见 opc.config.json 中本 role 的 artifacts 键；用 `opc resolve " + role + " <key> --project PXXX` 解析地址",
        "",
        "## 6 权限",
        "- 写：本岗阶段文档（一文件一 owner，共享文件只写自己的 H2 章节）",
        "- 读：本岗 §8 scope 授权范围（上游交接文件 + 本岗专属知识库；不越权直读 0_约束卡.md）",
        "- 禁改：OPC-架构规范.md、其他岗位卡、其他职业简历",
        "",
        "## 7 越界红线",
        "- 上游文档缺输入 → 停下来指名催办，禁止代写",
        "- 被要求做其他职业的判断 → 拒绝并指明应由哪个 role 处理",
        "",
        "## 8 验收标准",
        "- 待补（建议：上游契约齐 / frontmatter 完整 / 地址可解析 / 交付物可被下游直接使用）",
        "",
        "## 9 启动提示词",
        "```",
        "本次会话以 " + role + " 身份工作。开工前读 OPC-架构规范.md、本岗位卡、99_Agent配置/履历/" + role + ".md、30_项目/_产出索引.md；",
        "新项目先查相似经历（opc similar --tags ...），收尾把经验追加进本岗简历（opc link）。不属于本岗位的任务一律拒绝。",
        "```",
        "",
        "## 沉淀时你要做什么（v2.17 · 宪法 §15.18）",
        "",
        "> 沉淀是**你**的活：工具只写标题与骨架，**正文你自己写**（禁止代写）。",
        "",
        "- **触发**：创始人说「可以沉淀知识了」→ 立即执行（你也可反问「是不是要沉淀知识了？」）；**不需要**再找人审批。",
        "- **粒度**：只沉淀**当前最新版本**的增量产出；同一项目可沉淀多次，条目**追加**不覆盖。",
        "- **只写自己的**：别人有产出 → 你不写；你没产出 → 你不写。",
        "- **四步**：① `opc distill --project <项目名>` 拿派发清单 → ② 自己写正文（一句话摘要 + 三小节「怎么落地的 / 坑与解法 / 结果」+ 出处）→ ③ `opc link --project <项目名> --version <V<N>> --role " + role + "` 落到本岗履历卡 → ④ 补写旧条目加 `--修改 \"<说明>\"` 留痕。",
        "- **不写**：总结稿 / 中间转抄文件 / 别人的条目 / 你没产出的版本。",
        "",
    ]
    return "\n".join(lines)


def gen_resume_skeleton(role, version):
    """为 profile 中没有手写简历的 role 生成五节骨架简历。"""
    return (
        "---\n"
        "type: 职业简历\n"
        "role: " + role + "\n"
        "project_refs: []\n"
        "created: " + date.today().isoformat() + "\n"
        "---\n\n"
        "# " + role + " · 职业简历\n\n"
        "> 当前 " + version + " | 本文件由 init 自动生成骨架。只增不改；C/D 节由 `opc link` 维护。\n\n"
        "## A 基础档案\n\n- 定位：待补\n- 专长：待补\n\n"
        "## B 能力矩阵\n\n- 待补（收尾时按项目实练更新）\n\n"
        "## C 项目经历\n\n"
        "> 每个项目的经验条目由 `opc link` 自动生成（v2.17 起标题为 `### <项目名> · <YYYY-MM> · <V<N>>`，不带项目号），\n"
        "> 手写会被 `resume` 误认；「当时怎么做 + 证据指针」写在这里，结论性规则走 L4。\n\n"
        "## D 相似经验速查\n\n| 类型标签 | 相关项目 | 推荐先看 |\n|---|---|---|\n| （暂无） | — | — |\n\n"
        "## E 成长轨迹\n\n- 待补\n"
    )


AGENT_MODES = {
    "single": "单 Bot 切换身份（默认）：一个 Agent 按阶段切换岗位，每次开工开新会话",
    "multi": "多 Bot 并行：每职业一个独立 Agent 实例，交接走文件",
    "multi-profile": "多 Profile 增强：一职业 = 一个独立持久身份 + 独立记忆（需宿主支持 Profile 类机制）",
}


def agent_mode_of(cfg):
    return (cfg.get("agent_mode") or "single").strip() or "single"


def set_agent_mode(vault, mode):
    """写入运行模式到 opc.config.json —— **唯一的写入入口**（v2.13）。

    此前 `agent_mode` 只被读（onboard / role / status），没有任何写入路径：
    用户只能手改 JSON。v2.13 起 init 引导与 `opc mode` 共用本函数。
    """
    mode = (mode or "").strip()
    if mode not in AGENT_MODES:
        raise ValueError("未登记的运行模式：" + str(mode) + "；可选：" + " / ".join(AGENT_MODES))
    vault = Path(vault).resolve()
    cfg = load_config(vault)
    old = agent_mode_of(cfg)
    cfg["agent_mode"] = mode
    save_config(vault, cfg)
    return old


def choose_agent_mode(vault, cfg, args):
    """init 时选定运行模式：--agent-mode 优先；交互式询问；已配置则不覆盖（幂等）。

    返回**选定后的**模式（不是旧值）—— 调用方据此回显。
    """
    picked = getattr(args, "agent_mode", None)
    if picked:
        set_agent_mode(vault, picked)
        return picked
    existing = (cfg.get("agent_mode") or "").strip()
    if existing:
        return existing
    if not interactive_enabled(args):
        set_agent_mode(vault, "single")
        return "single"
    print()
    print("── 运行模式（团队形态）──")
    print("OPC 的「多 Agent 协同」有三种落地方式（GLOSSARY 第五节 / ADR-001）：")
    for k, v in AGENT_MODES.items():
        print("  · " + k + " —— " + v)
    print("  提示：multi / multi-profile 提供的是**独立身份**，不等于流水线自动并行；")
    print("        并行度取决于波次数量（不同项目）与交接状态是否按项目拆分（已支持）。")
    answer = prompt("选择运行模式（回车用 single）：", "single").strip().lower()
    while answer and answer not in AGENT_MODES:
        print("  只认这三个值：" + " / ".join(AGENT_MODES))
        answer = prompt("选择运行模式（回车用 single）：", "single").strip().lower()
    mode = answer or "single"
    set_agent_mode(vault, mode)
    return mode


def choose_profile(tpl_cfg, args):
    """init 时选择行业 profile；--profile 优先，交互式列出可选，默认 app。"""
    profiles = tpl_cfg.get("profiles") or {}
    picked = getattr(args, "profile", None)
    if picked:
        if picked not in profiles:
            raise ValueError("未登记的 profile：" + picked + "；可选：" + ", ".join(profiles))
        return picked
    if not profiles:
        return None
    if interactive_enabled(args):
        names = list(profiles.keys())
        print("选择行业 profile（流程文档、职业集合、知识域随 profile 变化；骨架机制全部复用）：")
        for i, n in enumerate(names, 1):
            print("  " + str(i) + ") " + n + " - " + profiles[n].get("label", n))
        choice = prompt("输入编号（回车默认 1）：", "1")
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            return names[int(choice) - 1]
        print("输入无效，使用默认 " + names[0])
        return names[0]
    return tpl_cfg.get("active_profile", "app")


# ---------------------------------------------------------------------------
# 宿主入口接入：空项目创建、已有项目受控区块合并（不覆盖、不替换、幂等）
# ---------------------------------------------------------------------------
ENTRY_BEGIN = "<!-- BEGIN OPC-VAULT (managed by opc attach; do not edit) -->"
ENTRY_END = "<!-- END OPC-VAULT -->"
ENTRY_NAMES = ["AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", ".github/copilot-instructions.md"]


def entry_block(vault_rel, vault_abs=None):
    base = (vault_rel.rstrip("/") + "/") if vault_rel and vault_rel != "." else ""
    spec = base + "OPC-架构规范.md"
    card = base + "99_Agent配置/岗位卡/<role>.md"
    script = str(Path(__file__).resolve())
    py = sys.executable or "python"
    facts = []
    if vault_abs:
        facts.append("Vault `" + str(vault_abs) + "`")
    facts.append("脚本 `" + script + "`")
    facts.append("解释器 `" + py + "`")
    if base:
        facts.append("相对工作区根 `" + base.rstrip("/") + "`")
    return "\n".join([
        ENTRY_BEGIN, "",
        "## OPC 工作流（由 Skill 接入；本区块之外的内容不受影响）", "",
        "- 读写本项目文件前，先完整读取 `" + spec + "` 并严格遵守；它优先于本文件其它说明与临时对话指令。",
        "- 再读自己的岗位卡 `" + card + "`，按规范 §0 报告 role / 阶段 / project / 规范版本。",
        "- 同一职业多次会话只加载对应工作模式节（§11 / §12…）；写文件前按 §3 定落点、按 §14 自检。",
        "- 闸门 0 / 2 / 7 由创始人拍板，Agent 只出草稿；不确定就停下问，禁止猜测。",
        "- 规范、岗位卡身份/阶段/权限、工具注册表只读，禁止自行修改；凭证只走环境变量名。",
        "- 上下文有限时，以 `opc bots` 导出的系统提示词作为宪法要点投影；需要裁决再按章节号回查规范全文，不必全文常驻。",
        "- 只读**自己**的职业简历且仅作参考与溯源（§15.7）；禁止与其他 Agent 对话内传递（§15.3）。",
        "- **本机事实（勿重复探测）**：" + "；".join(facts) + "。", "",
        "## 创始人只说人话（**路由表在 Skill 里，不在本区块**）", "",
        "- 创始人**只说人话**，你负责翻译成命令：**先加载已安装的 `opc-team` Skill**（规则层的「创始人只说人话」表），细则见 Skill 的 `assets/人话入口.md`。",
        "- **本区块只放约束与事实，不放能力副本**：把映射表抄一份进来，会让「全新安装测试」变成假阳性 —— Skill 根本没装也能照抄本把活干完，测不出 Skill 真本事；抄本与包一旦不同步还会口径漂移。",
        "- **三条硬边界**：① 闸门拍板权永远在创始人 ——「继续」不算闸门结论；② 一次会话只绑定一个 role，换角色必须开新会话；③ Agent 之间禁止互相唤醒，下一棒等创始人确认。", "",
        "> 本区块由 `opc attach` 维护，可重复执行（幂等）；`opc attach --remove` 可移除。",
        ENTRY_END,
    ])


def workspace_root_of(vault, explicit=None):
    if explicit:
        return Path(explicit).resolve()
    return vault.parent if vault.name == "OPC-Vault" else vault


def vault_rel_to(vault, ws):
    try:
        return vault.relative_to(ws).as_posix()
    except ValueError:
        return os.path.relpath(str(vault), str(ws)).replace("\\", "/")


def entry_plan(vault, ws, names):
    rows = []
    for name in names:
        p = ws / name
        if not p.is_file():
            rows.append((name, "不存在", p))
        elif ENTRY_BEGIN in read_text(p):
            rows.append((name, "已接入（幂等更新受控区块）", p))
        else:
            rows.append((name, "追加受控区块，原内容一行不动", p))
    return vault_rel_to(vault, ws), rows


def cmd_attach(args):
    """已有项目接入：受控区块合并到宿主入口，绝不覆盖或替换原文件。"""
    vault = Path(args.vault).resolve()
    if not (vault / "OPC-架构规范.md").exists():
        raise ValueError("Vault 未初始化：找不到 " + str(vault / "OPC-架构规范.md"))
    ws = workspace_root_of(vault, getattr(args, "workspace_root", None))
    names = ([x.strip() for x in args.entry.split(",") if x.strip()]
             if getattr(args, "entry", None) else ENTRY_NAMES)
    rel, rows = entry_plan(vault, ws, names)
    block = entry_block(rel, str(vault))
    print("Vault:", vault)
    print("工作区根:", ws, "| 规范:", (rel + "/" if rel and rel != "." else "") + "OPC-架构规范.md")
    print("\n入口文件检测：")
    for name, action, _ in rows:
        print("  -", name, "->", action)
    existing = [r for r in rows if r[1] != "不存在"]
    if not existing:
        print("  - 未检测到任何宿主入口；--apply 时只新建 AGENTS.md")
    if getattr(args, "remove", False):
        for name, action, p in rows:
            if action == "不存在":
                continue
            txt = read_text(p)
            if ENTRY_BEGIN in txt:
                txt = re.sub(r"\n*" + re.escape(ENTRY_BEGIN) + r".*?" + re.escape(ENTRY_END) + r"\n*",
                             "\n", txt, count=1, flags=re.S)
                write_text(p, txt)
                print("已移除受控区块:", p.relative_to(ws))
        return
    if not getattr(args, "apply", False):
        print("\n[只读诊断] 未写入任何文件。确认后加 --apply 合并；加 --remove 可卸载。")
        return
    if not existing:
        p = ws / "AGENTS.md"
        write_text(p, "# 项目入口\n\n" + block + "\n")
        print("已新建:", p)
    else:
        for name, action, p in existing:
            txt = read_text(p)
            if ENTRY_BEGIN in txt and ENTRY_END in txt:
                txt = re.sub(re.escape(ENTRY_BEGIN) + r".*?" + re.escape(ENTRY_END),
                             lambda _m: block, txt, count=1, flags=re.S)
            else:
                txt = txt.rstrip() + "\n\n" + block + "\n"
            write_text(p, txt)
            print("已合并:", p.relative_to(ws), "（原有内容保留）")
    print("完成。入口只是指针，规范全文仍在 Vault 内；改规则只改一处。")





def task_pool_path(vault):
    return Path(vault) / "99_Agent配置/_职业任务池.json"


def read_task_pool(vault):
    p = task_pool_path(vault)
    if not p.exists():
        return {"claims": {}, "history": []}
    try:
        d = json.loads(read_text(p))
        d.setdefault("claims", {})
        d.setdefault("history", [])
        return d
    except (ValueError, OSError):
        return {"claims": {}, "history": []}


def write_task_pool(vault, pool):
    write_text(task_pool_path(vault), json.dumps(pool, ensure_ascii=False, indent=2) + "\n")


def project_dirs(vault, cfg):
    """枚举 vault 里的项目 → [(短 ID, 项目目录)]。

    v2.17（ADR-0005 D10）：目录名不再带 `P001_` 前缀，**不能**再靠
    「目录名像不像项目号」来认项目（实测会把所有项目判成不存在 → 任务池、
    pm 面板、编排器全部空转）。项目清单以**产出索引**为准；无索引时回退老格式目录。
    """
    base = Path(vault) / "30_项目"
    out = []
    if not base.exists():
        return out
    seen = set()
    try:
        for r in read_index(vault, cfg):
            pid = project_key(r.get("project"))
            if not pid or pid in seen:
                continue
            pdir = base / project_dir_name(vault, pid)
            if pdir.is_dir():
                out.append((pid, pdir))
                seen.add(pid)
    except Exception:
        pass
    if not out:
        for pdir in sorted(base.iterdir()):
            nm = pdir.name
            if pdir.is_dir() and re.match(r"P\d{3}", nm):
                out.append((nm, pdir))
    return out


def infer_tasks(vault, cfg):
    """从各项目 9 步文档的 frontmatter status 推断当前阶段与接手 role。"""
    prof = profile_view(cfg)
    docs = prof.get("step_docs") or []
    owners = step_owner_map(prof)
    gates = notify_conf(cfg).get("gates") or [0, 2, 7]
    base = Path(vault) / "30_项目"
    out = []
    if not base.exists():
        return out
    # v2.17：项目清单以**产出索引**为准 —— 目录名不再带 `P001_` 前缀，
    # 靠「目录名像不像项目号」来认项目已经失效（实测会把所有项目判成不存在）。
    targets = project_dirs(vault, cfg)
    for nm, pdir in targets:
        done_upto = -1
        st0 = read_handoff(vault, cfg, nm)
        _ver = st0.get("round") or ""
        for i, d in enumerate(docs):
            _r = owners.get(d, ("", ""))[0]
            fp = _main_doc_path(pdir, _r, d, _ver)
            if not fp.exists():
                continue
            m = re.search(r"^status:\s*(\S+)", read_text(fp), re.M)
            if m and m.group(1) != "草稿":
                done_upto = i
        cur = done_upto + 1
        if cur >= len(docs):
            out.append({"project": nm, "stage": len(docs) - 1, "doc": "—", "role": "—", "state": "已走完"})
            continue
        st = read_handoff(vault, cfg, nm)
        out.append({"project": nm, "stage": cur, "doc": docs[cur],
                    "role": owners.get(docs[cur], ("", ""))[0],
                    "state": "待人工闸门" if bool(st.get("gate")) else "可开工"})
    return out


def cmd_roster(args):
    """职业状态仓库视图：每个职业当前是空闲 / 认领中 / 可领几条活。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    pool = read_task_pool(vault)
    tasks = infer_tasks(vault, cfg)
    claimed_projects = {project_key(v.get("project")) for v in pool["claims"].values()}
    avail = [x for x in tasks if x["state"] == "可开工" and project_key(x["project"]) not in claimed_projects]
    print("职业状态仓库（pull 模式：谁空闲谁去池子里领活，不由别的 Agent 唤醒）")
    print("项目总数:", len(tasks), "| 可领任务:", len(avail))
    print("")
    print("  role".ljust(18) + "状态".ljust(14) + "当前任务".ljust(28) + "可领")
    for role in (role_bindings_ref(cfg).keys()):
        c = pool["claims"].get(role)
        n = len([x for x in avail if x["role"] == role])
        if c:
            st, cur = "进行中", c.get("project") + " · 阶段 " + str(c.get("stage"))
        else:
            st, cur = ("空闲（有活）" if n else "空闲"), "—"
        print("  " + role.ljust(16) + st.ljust(14) + cur.ljust(28) + str(n))
    print("")
    print("同一项目内串行（§15.3）；不同项目可同时被不同 role 认领 → 跨项目并行。")
    print("领活：opc claim --role <role> [--project PXXX]  ｜ 释放：opc release --role <role>")


def cmd_claim(args):
    """职业领任务：写任务池 + 设置当前项目 + 输出本次会话身份声明。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    role = args.role
    if role not in role_bindings_ref(cfg):
        raise ValueError("未登记的 role：" + role)
    # v2.13：任务池是共享单文件，「读→判→写」必须在锁内完成，
    # 否则并发会话会互相覆盖认领记录（实测丢过一条）。
    with VaultLock(vault):
        pool = read_task_pool(vault)
        if role in pool["claims"]:
            raise ValueError(role + " 已认领 " + str(pool["claims"][role].get("project"))
                             + "；先 opc release --role " + role)
        tasks = infer_tasks(vault, cfg)
        claimed = {project_key(v.get("project")) for v in pool["claims"].values()}
        cand = [x for x in tasks if x["state"] == "可开工" and x["role"] == role
                and project_key(x["project"]) not in claimed]
        if getattr(args, "project", None):
            cand = [x for x in cand if x["project"] == args.project
                    or x["project"].startswith(args.project + "_")]
        if not cand:
            raise ValueError(role + " 当前没有可领任务；先 opc roster 看池子")
        task = cand[0]
        now = datetime.now().isoformat(timespec="seconds")
        state = read_handoff(vault, cfg, task["project"])
        if state.get("status") == "待确认":
            pool["history"].append(dict(state, archived_at=now))
            write_task_pool(vault, pool)
            print("提示：原项目处于「待确认」，已归档其回执；请回来处理该闸门。")
        pool["claims"][role] = {"project": task["project"], "stage": task["stage"],
                                "doc": task["doc"], "claimed_at": now}
        write_task_pool(vault, pool)
        cfg["active_project"] = task["project"]
        save_config(vault, cfg)
    # v2.23：领活**不得重建**交接状态。
    # 过去这里无条件 `write_handoff(state)`，只写 7 个字段，把项目原有的
    # dispatch_chain / dispatch_roles / decisions（闸门拍板留痕）/ asset_batch /
    # gate / confirmed_at 全部冲掉 —— 实测把 P002 从「阶段 5 · design」改写成
    # 「阶段 0 · strategist」，两次闸门拍板留痕归零。
    # 更糟的是自我强化：claim 改写状态 → 下一轮按新状态推算 → 反复认领同一项目。
    #
    # 现在：**只在状态缺失或与本次领活不一致时**补齐游标字段，
    # 其余字段一律保留。状态由 opc done / opc advance 维护，不由领活维护。
    prev = read_handoff(vault, cfg, task["project"])
    state = dict(prev or {})
    state.update({
        "project": task["project"], "stage": task["stage"],
        "role": role, "doc": task["doc"], "status": "进行中", "token": "",
        "next_role": "", "claimed_at": now,
    })
    # v2.29.6：step 统一存**链内下标**而非全局 9 步阶段号（与 done/go/advance 同口径）。
    # 实测缺陷：claim 写全局阶段号（如 5），派活项目链长只有 2-3，
    # cmd_done 拿它当下标比链长 → 误判「流程已走完」，报完成被拒。
    state["step"] = chain_pos(project_chain(vault, cfg, task["project"]), state)
    write_handoff(vault, cfg, state)
    print("已领任务：" + role + " ← " + str(project_label_map(vault, cfg).get(task["project"])
                                       or task["project"])
          + " · 阶段 " + str(task["stage"]) + " · " + task["doc"])
    print("本次会话身份声明：")
    cmd_role(argparse.Namespace(vault=args.vault, role=role))


def cmd_release(args):
    """释放认领（不改项目文档、不动闸门回执）。"""
    vault = Path(args.vault).resolve()
    role = args.role
    # v2.13：与 claim 同理，「读→改→写」放进锁内，避免并发释放吃掉别人的认领。
    with VaultLock(vault):
        pool = read_task_pool(vault)
        if role not in pool["claims"]:
            print(role + " 当前没有认领任务。")
            return
        c = pool["claims"].pop(role)
        pool["history"].append({"role": role, "released": c,
                                "at": datetime.now().isoformat(timespec="seconds")})
        write_task_pool(vault, pool)
    print("已释放：" + role + " ← " + str(c.get("project")) + "；任务回到可领池。")

def cmd_queue(args):
    """流水线视图（只读）：列出每个项目当前停在哪个阶段、该哪个 role 接手。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    prof = profile_view(cfg)
    docs = prof.get("step_docs") or []
    owners = step_owner_map(prof)
    gates = notify_conf(cfg).get("gates") or [0, 2, 7]
    rows = []
    # v2.17：项目清单以产出索引为准（目录名不再带 P001_ 前缀）
    for _pid, pdir in project_dirs(vault, cfg):
        done_upto = -1
        # v2.12：同 opc pm —— 按本项目交接链算当前棒（派活项目链上只有被派的岗位）
        chain = project_chain(vault, cfg, _pid)
        _st0 = read_handoff(vault, cfg, _pid)
        _ver = _st0.get("round") or ""
        for i, (_s, d, _r) in enumerate(chain):
            fp = _main_doc_path(pdir, _r, d, _ver)
            if not fp.exists():
                continue
            m = re.search(r"^status:\s*(\S+)", read_text(fp), re.M)
            if m and m.group(1) != "草稿":
                done_upto = i
        cur = done_upto + 1
        if cur >= len(chain):
            rows.append((_pid, "已走完", "—", "—", "完成"))
            continue
        stage_no, doc, role = chain[cur]
        st = read_handoff(vault, cfg, _pid)
        state = "待人工闸门" if bool(st.get("gate")) else "可开工"
        rows.append((_pid, "阶段 " + str(stage_no), doc, role, state))
    role = getattr(args, "role", None)
    shown = [r for r in rows if not role or r[3] == role]
    labels = project_label_map(vault, cfg)
    print("流水线视图（只读推断，依据各项目 9 步文档的 frontmatter status）")
    print("项目数:", len(rows), ("| 过滤 role=" + role + " → " + str(len(shown)) + " 条") if role else "")
    print("")
    print("  " + "项目".ljust(20) + "阶段".ljust(10) + "文档".ljust(22) + "接手 role".ljust(14) + "状态")
    pool = read_task_pool(vault)
    who = {v.get("project"): k for k, v in pool["claims"].items()}
    for r in shown:
        mark = " [已认领:" + who[r[0]] + "]" if r[0] in who else ""
        print("  " + str(labels.get(r[0]) or r[0]).ljust(20) + r[1].ljust(10)
              + r[2].ljust(22) + r[3].ljust(14) + r[4] + mark)
    if not shown:
        print("  （无匹配任务；先 opc new 建项）")
    print("")
    print("同一项目：必须等上一阶段落盘 + 闸门通过才可推进（§15.3 / opc go 单步游标）。")
    print("不同项目：互不阻塞；空闲的 role 可接另一项目当前阶段的活，这就是流水线。")
    _mode = agent_mode_of(load_config(vault))
    _mode_line = {
        "single": "当前 single 模式：切换靠人工开新会话 + opc role <role>。",
        "multi": "当前 multi 模式：每 role 一个独立 Agent 实例，按本表领活。",
        "multi-profile": "当前 multi-profile 模式：每 role 一个独立 Profile，按本表领活（workspace 须指向同一 Vault）。",
    }.get(_mode, "当前运行模式：" + _mode)
    print(_mode_line + "流水线并行度取决于项目数（§22.2 / §23.4）。")
    print("推进仍以 opc done 通知 + 创始人确认为准；Agent 之间禁止直接唤醒（§15.3 / §15.9）。")

def cmd_onboard(args):
    """安装期引导（协调职责归 Skill，不新增 Agent）：幂等检测 + 下一步清单。"""
    vault = Path(args.vault).resolve()
    ws = workspace_root_of(vault, getattr(args, "workspace_root", None))
    rel = vault_rel_to(vault, ws)
    spec = vault / "OPC-架构规范.md"
    cards_dir = vault / "99_Agent配置/岗位卡"
    boots_dir = vault / "99_Agent配置/Bot配置卡"
    cards = sorted(cards_dir.glob("*.md")) if cards_dir.exists() else []
    boots = sorted(boots_dir.glob("*.md")) if boots_dir.exists() else []
    entry_files = [n for n in ENTRY_NAMES if (ws / n).is_file()]
    entry_ok = any(ENTRY_BEGIN in read_text(ws / n) for n in entry_files)
    print("OPC 接入引导（只读诊断，不修改任何文件）")
    print("Vault:", vault)
    print("工作区根:", ws, "| 规范:", (rel + "/" if rel and rel != "." else "") + "OPC-架构规范.md")
    print("")
    steps = []
    steps.append(("1", "Vault 骨架", "已初始化" if spec.exists() else "未初始化",
                  "—" if spec.exists() else "opc init --vault " + str(vault)))
    steps.append(("2", "宿主入口区块", "已接入" if entry_ok else ("已有入口未合并" if entry_files else "无入口"),
                  "—" if entry_ok else "opc attach --vault " + str(vault) + " --apply"))
    steps.append(("3", "岗位卡", str(len(cards)) + " 份", "由 init 自动下发；缺失时重新 init（幂等，不覆盖）"))
    steps.append(("4", "Bot 配置卡", str(len(boots)) + " 份",
                  "—" if boots else "opc bots --vault " + str(vault)))
    try:
        cfg = load_config(vault)
        state = read_handoff(vault, cfg)
        proj = state.get("project") or cfg.get("active_project") or "（未建项）"
        step = str(state.get("step", 0))
        steps.append(("5", "当前项目", proj + " · 阶段 " + step + " · " + str(state.get("status", "未开始")),
                      "opc handoff --vault " + str(vault)))
    except Exception:
        steps.append(("5", "当前项目", "Vault 未就绪", "先完成第 1 步"))
    for num, name, status, nxt in steps:
        print("  " + num + ". " + name + "：" + status)
        if nxt != "—":
            print("     -> " + nxt)
    try:
        cfg0 = load_config(vault)
    except Exception:
        cfg0 = {}
    mode = agent_mode_of(cfg0)
    print("")
    print("运行模式：agent_mode = " + mode + "（opc.config.json；single 为默认，不要求宿主支持多 Bot）")
    roles = [p.stem for p in cards] or ["strategist", "market", "pm", "design", "architect", "engineer", "growth", "governance"]
    if mode == "multi-profile":
        print("独立身份 + 独立记忆（Profile 型宿主）：**一职业 = 一个 Profile**。")
        for r in roles:
            has = (boots_dir / ("Bot配置卡_" + r + ".md")).exists()
            print("  - " + r + "：" + ("配置卡已导出，第五节含 SOUL.md / 共享 workspace / 记忆归属"
                                     if has else "先跑 opc bots 导出配置卡"))
        print("  **必须让所有 Profile 的 workspace 指向同一个 Vault**，否则 §15.3 的文件交接链会断。")
        print("  Profile 的 MEMORY.md 是它自己的；职业履历仍以 99_Agent配置/履历/<role>.md 为准（别造第二份真相）。")
        print("  领活靠 pull：opc queue --role <role> --claim；**禁止互相 @ 唤醒**（§15.3）。")
    elif mode == "multi":
        print("创建 Bot（宿主不支持自动建，需你逐个手动创建；下面只是引导，不代为创建）")
        for r in roles:
            has = (boots_dir / ("Bot配置卡_" + r + ".md")).exists()
            print("  - " + r + "：" + ("配置卡已导出，复制其系统提示词到宿主新建 Bot" if has else "先跑 opc bots 导出配置卡")
                  + "；每次开工执行 opc role " + r + " 取身份声明")
        print("  若宿主支持独立持久身份（Profile 类机制）：改用 opc mode multi-profile，会按 Profile 适配段引导。")
    else:
        print("单 Bot 切换（推荐）：**只需一个 Bot**，不必创建 8 个。")
        print("  每次开工开新会话 → opc role <role> 取身份声明贴入 → 本次会话只按该岗位卡执行（§15.1）。")
        print("  岗位卡是身份边界，scope 契约 + §14 自检是权限边界，不靠物理隔离的 Bot（手册 §8.1/§8.2）。")
        print("  确实需要并行或持久隔离身份时，执行 opc mode multi（或 multi-profile）。")
    print("")
    print("已有项目：第 2 步用 attach 受控合并，不覆盖原有 AGENTS.md / CLAUDE.md；OPC 文件只在 " + (rel or ".") + "/ 内。")
    print("重复运行本命令不会重复初始化；已完成项显示「已初始化 / 已接入」即可跳过。")
    print("协调职责：安装期由本命令引导，运行期由 §15.3 文件交接 + §15.9 通知 + 创始人闸门承担，不设协调者 Agent。")

def cmd_init(args):
    target = getattr(args, "vault", None) or DEFAULT_VAULT
    if getattr(args, "choose", False):
        picked = choose_directory()
        if picked:
            target = picked
            print("已选择目录:", picked)
        else:
            print("系统目录选择框不可用，改为输入路径。")
            target = prompt("请输入 Vault 保存目录（回车使用 " + DEFAULT_VAULT + "）：", DEFAULT_VAULT)
    elif interactive_enabled(args) and not getattr(args, "vault_explicit", False):
        target = prompt("Vault 要保存在哪里？（回车使用 " + DEFAULT_VAULT + "）：", DEFAULT_VAULT)
    vault = Path(target).resolve()
    tpl_cfg = json.loads(read_text(CONFIG_TEMPLATE))
    profile = choose_profile(tpl_cfg, args)
    prof = None
    if profile:
        prof = dict(tpl_cfg["profiles"][profile])
        prof["name"] = profile
    domains = (prof or {}).get("knowledge_domains", DEFAULT_KNOWLEDGE_DOMAINS)
    all_dirs = BASE_DIRS + ["20_知识资产/" + d for d in domains]
    for d in all_dirs:
        (vault / d).mkdir(parents=True, exist_ok=True)
    version = schema_version(tpl_cfg)
    copied, generated = 0, 0
    copied += copy_if_absent(REFERENCES / "OPC-架构规范.md", vault / "OPC-架构规范.md")
    roles = list((prof or {}).get("role_bindings", tpl_cfg.get("role_bindings", {})).keys())
    for role in roles:
        card = ASSETS / "岗位卡" / (role + ".md")
        if card.exists():
            copied += copy_if_absent(card, vault / "99_Agent配置/岗位卡" / (role + ".md"))
        elif not (vault / "99_Agent配置/岗位卡" / (role + ".md")).exists():
            write_text(vault / "99_Agent配置/岗位卡" / (role + ".md"), gen_jobcard_skeleton(role, prof or profile_view(tpl_cfg), version))
            generated += 1
        resume = ASSETS / "职业简历" / (role + ".md")
        if resume.exists():
            copied += copy_if_absent(resume, vault / "99_Agent配置/履历" / (role + ".md"))
        elif not (vault / "99_Agent配置/履历" / (role + ".md")).exists():
            write_text(vault / "99_Agent配置/履历" / (role + ".md"), gen_resume_skeleton(role, version))
            generated += 1
    # 命令与配置手册：**唯一给 Agent 看的操作说明**，必须进 Vault（否则 Agent 只能看宪法，
    # 不知道 --sync-self / --queue / --flush 怎么用）。自测也直接读 Vault 里这份。
    copied += copy_if_absent(REFERENCES / "命令与配置手册.md",
                             vault / "99_Agent配置" / "命令与配置手册.md")
    copied += copy_if_absent(ASSETS / "产出索引模板.md", vault / "30_项目/_产出索引.md")
    copied += copy_if_absent(ASSETS / "工具注册表模板.md", vault / "99_Agent配置/工具注册表.md")
    copied += copy_if_absent(SKILL_DIR / "scripts" / "notify_feishu.py",
                             vault / "99_Agent配置/通知桥" / "notify_feishu.py")
    copied += copy_if_absent(SKILL_DIR / "scripts" / "notify_webhook.py",
                             vault / "99_Agent配置/通知桥" / "notify_webhook.py")
    copied += copy_if_absent(SKILL_DIR / "scripts" / "sync_feishu.py",
                             vault / "99_Agent配置/通知桥" / "sync_feishu.py")
    cfg_path = vault / "opc.config.json"
    if not cfg_path.exists():
        cfg = tpl_cfg
        cfg["vault_root"] = str(vault)
        cfg["backends"]["local"]["root"] = str(vault)
        if profile:
            cfg["active_profile"] = profile
        save_config(vault, cfg)
        copied += 1
    elif profile and json.loads(read_text(cfg_path)).get("active_profile") != profile:
        cfg = json.loads(read_text(cfg_path))
        if "profiles" in cfg and profile in cfg["profiles"]:
            cfg["active_profile"] = profile
            save_config(vault, cfg)
            print("已切换 active_profile ->", profile, "（既有项目索引行保留为历史记录）")
    print("Vault:", vault)
    print("profile:", (prof or {}).get("label", "默认") + ("（" + str(len(roles)) + " 个职业）" if roles else ""))
    print("目录:", len(all_dirs), "个；复制文件:", copied, "个；生成骨架卡:", generated, "份（已存在的文件未覆盖）")
    # v2.13：团队形态是 init 握手的一部分 —— 选定运行模式并落盘（幂等：已有配置不覆盖）
    try:
        mode = choose_agent_mode(vault, load_config(vault), args)
        print("运行模式:", mode, "-", AGENT_MODES[mode])
    except Exception as exc:
        print("运行模式设置失败（不影响本地使用）：" + str(exc))
    # v2.7：初始化时问一次要不要配云端仓库；配了之后产出落盘自动同步，不再逐次问
    if cfg_path.exists():
        try:
            cfg_now = load_config(vault)
        except Exception:
            cfg_now = json.loads(read_text(cfg_path))
        setup_cloud_repo(vault, cfg_now, args)
        # v2.20（创始人拍板）：初始化时问一次多维表格台账——进度跟进线，与云盘资产同步是两码事
        setup_base_ledger(vault, cfg_now, args)
    # v2.27：多 Bot 协作自检 —— 群内唤醒还差什么，一次说清（否则新用户卡在「@ 了没反应」）
    print_multibot_checklist(vault)
    print("下一步: python scripts/opc.py status --vault", str(vault))
    if getattr(args, "no_attach", False):
        print("已跳过宿主入口（--no-attach）；需要时执行: python scripts/opc.py attach --vault " + str(vault) + " --apply")
    else:
        ws = workspace_root_of(vault, getattr(args, "workspace_root", None))
        rel = vault_rel_to(vault, ws)
        if not any((ws / n).is_file() for n in ENTRY_NAMES):
            write_text(ws / "AGENTS.md", "# 项目入口\n\n" + entry_block(rel, str(vault)) + "\n")
            print("已创建宿主入口:", ws / "AGENTS.md", "（不需要时：python scripts/opc.py attach --remove --apply）")
        else:
            print("检测到已有入口文件，未改动。合并请执行：")
            print("  python scripts/opc.py attach --vault " + str(vault) + " --apply")


def setup_base_ledger(vault, cfg, args):
    """初始化时配置多维表格台账（v2.20 · 创始人拍板）。

    §15.17 的云端台账（项目汇总/需求池/②版本开发计划/Bug/发布版本五张表）此前
    **没有初始化入口**——init 从不问 base_token，照引导走完的新 Vault 台账必然
    未配置，整节沦为死条文（实测踩坑）。本步补上握手。

    - 进度线（台账）：每次推进/报完成/拍板即同步②版本开发计划——版本没做完也跟进；
    - 资产线（云盘 sync）：文件产出等版本结束后统一推——**两码事**。
    可选项：不配置则台账功能不可用（`opc pm` 会提示），流程照常（§15.12 降级）。
    """
    fs = (cfg.get("backends") or {}).get("feishu") or {}
    if not fs:
        return cfg
    base_token = str(getattr(args, "base_token", "") or "").strip()
    print()
    print("── 多维表格台账（可选） ──")
    print("用途：项目进度跟进——每根棒谁在做/什么状态/闸门拍板，实时写进飞书多维表格。")
    print("      （与「云端文件同步」是两码事：文件资产等版本结束再推，进度要实时。）")
    if not base_token and interactive_enabled(args):
        answer = prompt("是否配置多维表格台账？输入 y 配置，其它键跳过：", "").strip()
        if answer.lower() not in ("y", "yes", "是", "配置"):
            print("已跳过台账配置。之后可在 opc.config.json 的 backends.feishu.base_token 填入。")
            return cfg
        base_token = prompt("粘贴多维表格的 app_token 或链接（飞书表格 URL 里 /base/<token> 这段）：", "").strip()
    if not base_token:
        print("（未提供台账地址，跳过；需要时填 opc.config.json 的 backends.feishu.base_token）")
        return cfg
    m = re.search(r"/base/([A-Za-z0-9]+)", base_token)
    if m:
        base_token = m.group(1)
    fs["base_token"] = base_token
    cfg.setdefault("backends", {})["feishu"] = fs
    save_config(vault, cfg)
    print("已写入 base_token。")
    # v2.20 修（通用缺口）：结构同步器要求五张表的 table_id 预先在配置里，
    # 否则 --apply 直接退出——此前这一步没有入口，握手断在半路。
    # 现在自动按表名解析（「敏捷项目管理」模板的表名 = base_schema.TABLES 的键）。
    resolved = 0
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import base_schema as bs
        token, _missing, _p = bs.resolve_base(vault_hint=str(vault))
        if token:
            data = bs.lark(["base", "+table-list"], token)
            tables = (data.get("tables") or data.get("items") or []) if isinstance(data, dict) else []
            mapping = fs.setdefault("base_tables", {})
            for t in tables:
                name = str(t.get("name") or "")
                if name in bs.TABLES and not mapping.get(name):
                    mapping[name] = t.get("table_id") or t.get("id") or ""
                    resolved += 1
            if resolved:
                save_config(vault, cfg)
    except Exception as exc:
        print("table_id 自动解析失败（不阻塞）：", str(exc)[:200])
    if resolved:
        print("已自动登记 " + str(resolved) + " 张表的 table_id。")
        print("  下一步建议：python scripts/base_schema.py --apply --vault " + str(vault)
              + "（幂等，对齐列口径与枚举）")
    else:
        print("  提示：未自动解析到五张表——请确认表名与 base_schema.py TABLES 一致，"
              "或手动填 opc.config.json 的 backends.feishu.base_tables。")
    print("  之后 pm 每次推进/报完成会自动更新②版本开发计划的进展。")
    print("  ⚠️ 一次性手动动作（UI-only，API 无此开关）：首次使用层级显示时，在飞书"
          "「①需求池/②版本开发计划」两张表的视图设置里开启「子记录」并选择"
          "「关联父任务」字段——否则数据虽在、界面仍平铺显示。")
    return cfg


def cmd_bind(args):
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    bindings = role_bindings_ref(cfg)
    if args.role not in bindings:
        raise ValueError("未登记的 role：" + args.role + "；当前 profile 可选：" + ", ".join(bindings.keys()))
    backend = getattr(args, "backend", None)
    if interactive_enabled(args) and not backend:
        print("产出位置只有两种：1) local 本地（默认，规范 §2 目录）  2) feishu 飞书 CLI / 自定义外部 CLI")
        choice = prompt("选择 1 或 2（回车默认 1）：", "1")
        backend = "local" if choice == "1" else "feishu"
    if backend not in cfg.get("backends", {}):
        raise ValueError("未登记的 backend：" + str(backend) + "；只提供 local 与 feishu 两种")
    binding = bindings[args.role]
    binding["backend"] = backend
    binding.setdefault("artifacts", {})[args.key] = args.ref
    save_config(vault, cfg)
    print("已绑定", args.role, "/", args.key, "->", backend, ":", args.ref)
    print("验证: python scripts/opc.py resolve", args.role, args.key, "--project P001 --vault", str(vault))


def cmd_bind_cli(args):
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    backend = cfg.get("backends", {}).get(args.backend)
    if not backend:
        raise ValueError("未登记的 backend：" + args.backend)
    if backend.get("type") != "exec":
        raise ValueError(args.backend + " 不是 exec 类型后端，无需绑定 CLI")
    backend["cli"] = args.cli
    backend["push_cmd"] = args.push_cmd
    backend["url_prefix"] = args.url_prefix or ""
    save_config(vault, cfg)
    print("已绑定外部 CLI 配置（命令模板来自用户填写，以该系统官方文档为准）")
    need = backend.get("credential_env", [])
    print("需设置环境变量（值不写入配置、不打印）：")
    for name in need:
        print("  -", name, "=", "已设置" if os.environ.get(name) else "未设置")


def cmd_resolve(args):
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    name, uri = resolve_uri(cfg, vault, args.role, args.key, args.project)
    print("backend:", name)
    print("artifact_uri:", uri)


def _artifact_key_for_doc(role, doc_name, bindings):
    """流程文档名 → 该岗位实际登记的产出键。

    两者可能不同名但指向同一文件（如流程文档 7_上线.md ↔ 产出键 7_获客与客服）。
    优先精确匹配，否则按目标路径的文件名（去扩展名）匹配。
    """
    arts = (bindings.get(role) or {}).get("artifacts") or {}
    if doc_name in arts:
        return doc_name
    for k, tpl in arts.items():
        fname = str(tpl).rsplit("/", 1)[-1]
        if fname == doc_name or fname.rsplit(".", 1)[0] == doc_name:
            return k
    return doc_name


def _docs_for_roles(cfg, roles, known_assets=None):
    """给定本次涉及的岗位集合，算出要生成哪些文档。

    返回 [(文件名, role, 产出键)]，产出键用于写索引与解析 URI。
    known_assets: {role: [产出键...]} —— pm 在派活清单里点名的资产。
      · pm 点了名 → 只出点名的那些（含配套），不多出
      · pm 没点名（该岗位在清单里但资产列为空/—）→ 只出主文档
    """
    prof = profile_view(cfg)
    bindings = role_bindings_ref(cfg)
    owners = step_owner_map(prof)
    step_order = prof.get("step_docs") or []
    out = []
    seen = set()
    # 主文档按 step_docs 顺序遍历（不是 step_owner 的字典序），
    # 保证生成顺序 = 交接顺序，source 回填与索引阶段列才对得上。
    for doc in step_order:
        entry = owners.get(doc)
        if not entry:
            continue
        role = entry[0]
        if role not in roles:
            continue
        key = _artifact_key_for_doc(role, doc[:-3], bindings)
        if known_assets is not None:
            want = set(known_assets.get(role) or [])
            if want and key not in want and doc[:-3] not in want:
                continue
        out.append((doc, role, key))
        seen.add((role, key))
    # 主文档之外的配套资产
    extras = {
        "pm": ["4_产品基线", "4_里程碑", "4_功能清单", "4_交互与原型", "4_非功能需求"],
        "design": ["5_设计token", "5_原型", "5_设计交接清单"],
        "architect": ["5_架构设计", "5_API契约", "5_时序与数据模型", "5_任务档案"],
    }
    for role, keys in extras.items():
        if role not in roles:
            continue
        arts = (bindings.get(role) or {}).get("artifacts") or {}
        want = set(known_assets.get(role) or []) if known_assets is not None else None
        for key in keys:
            if (role, key) in seen:
                continue
            if want is not None and key not in want:
                continue
            if key not in arts:
                continue
            suffix = ".html" if key == "5_原型" else (".yaml" if key == "5_API契约" else ".md")
            out.append((key + suffix, role, key))
            seen.add((role, key))
    return out


def _extra_asset_rows(cfg, roles, docs):
    """把不在 step_docs、但岗位登记了的产出（如 governance 评测）也补进索引。"""
    bindings = role_bindings_ref(cfg)
    doc_roles = set(r for _d, r, _k in docs)
    out = []
    for role in roles:
        if role in doc_roles:
            continue
        for key, tpl in ((bindings.get(role) or {}).get("artifacts") or {}).items():
            out.append((role, key, tpl))
            break
    return out


def _index_rows_for(vault, cfg, project, name, tags, docs, status="进行中", version="V1",
                    when="", dirname=""):
    """按文档清单写索引行 —— 每个产出资产一行，阶段取自 step_owner，不靠猜。

    `dirname` —— 项目目录名（建项时目录还没进索引，不显式传就会退化成短 ID，
    写出来的 URI 指向不存在的路径）。
    """
    prof = profile_view(cfg)
    owners = step_owner_map(prof)
    stage_of = {}
    for doc, (_r, stage) in owners.items():
        stage_of[doc[:-3]] = stage
    bindings = role_bindings_ref(cfg)
    rows = []
    for fname, role, key in docs:
        arts = (bindings.get(role) or {}).get("artifacts") or {}
        uri = ""
        if key in arts:
            try:
                _b, uri = resolve_uri(cfg, vault, role, key, project, version, dirname)
            except Exception as exc:
                uri = "解析失败:" + str(exc)
        rows.append({
            "项目": name,
            "project": project,
            "类型标签": " ".join("#" + t for t in tags),
            "role": role,
            "阶段": stage_of.get(key, key.split("_", 1)[0] if "_" in key else ""),
            "逻辑键": "L2/" + project + "/" + key,
            "物理地址 URI": uri,
            "简历条目": "[[履历/" + role + "#" + name + " · " + when + " · " + version + "]]",
            "关联规则": "—",
            "状态": status,
            "时间": when,
            "版本": version,
        })
    # 不在 step_docs 的岗位产出（如 governance 评测）也登记，但阶段留空
    for role, key, tpl in _extra_asset_rows(cfg, set(r for _f, r, _k in docs), docs):
        try:
            _b, uri = resolve_uri(cfg, vault, role, key, project, version, dirname)
        except Exception as exc:
            uri = "解析失败:" + str(exc)
        rows.append({
            "项目": name,
            "project": project,
            "类型标签": " ".join("#" + t for t in tags),
            "role": role,
            "阶段": "",
            "逻辑键": "L2/" + project + "/" + key,
            "物理地址 URI": uri,
            "简历条目": "[[履历/" + role + "#" + name + " · " + when + " · " + version + "]]",
            "关联规则": "—",
            "状态": status,
            "时间": when,
            "版本": version,
        })
    return rows


def _dispatch_chain(cfg, docs):
    """按本次实际生成的文档构造本项目的交接链 [(stage, doc, role)]。

    与固定 step_docs 的差别：每阶段的主文档取**本次实际生成的那份**。
    例如 pm 只点名 4_产品基线（没点名 4_产品定义），阶段 4 的主文档就是
    4_产品基线.md，而不是固定流程名 4_产品定义.md——否则 `opc done` 会
    因为「主产出未落盘」永远卡住（§15.14 派活语义）。

    只保留本次派活岗位的阶段。

    v2.23：**同一阶段的并行岗位**（典型是阶段 5 的 design + architect）现在也进链 ——
    过去它们不在固定 9 步的 step_owner 里，于是在链上落选，`opc done --role architect`
    必然「跨步被拒绝」，而它们的产出在索引里有行、也被 pm 派了活。
    """
    gchain = step_chain(cfg)
    by_stage = {}
    for fname, role, key in docs:
        stage = key.split("_", 1)[0] if "_" in key else ""
        by_stage.setdefault((role, stage), []).append((fname, role, key))
    out = []
    for idx, doc, owner in gchain:
        cand = by_stage.get((owner, str(idx)))
        if not cand:
            continue
        pick = None
        for fname, _r, _k in cand:
            if fname == doc:
                pick = fname
                break
        if pick is None:
            pick = cand[0][0]
        out.append((idx, pick, owner))
    # v2.23：**同一阶段的并行岗位**（典型是阶段 5 的 design + architect）
    # 过去被整段落选 —— 它们不在固定 9 步的 step_owner 里，于是拿不到链上的槽位，
    # `opc done --role architect` 必然「跨步被拒绝」；但它们的产出在索引里有行、
    # 也被 pm 派了活。现在给它们各补一个槽位（阶段号相同、文档取该岗该阶段的主产出）。
    # 顺序：同阶段内按岗位名字典序，保证确定性。
    chained_owners = set()
    chain_stages = set()
    for _i, _d, _o in out:
        chained_owners.add(_o)
        chain_stages.add(int(_i))
    extras = []
    for key in sorted(by_stage):
        role = key[0]
        stage = key[1]
        if role in chained_owners or not stage.isdigit():
            continue
        st = int(stage)
        if st not in chain_stages:
            continue  # 该阶段整体没被派活 → 不补
        items = sorted(by_stage[key], key=lambda x: x[2])
        extras.append((st, items[0][0], role))
    for item in sorted(extras, key=lambda x: (x[0], x[2])):
        out.append(item)
    out.sort(key=lambda x: (x[0], x[2]))

    return out


def project_chain(vault, cfg, project):
    """本项目的交接链。派活项目用实际生成文档构成的链；全量项目用固定 9 步链。"""
    state = read_handoff(vault, cfg, project)
    dc = state.get("dispatch_chain") or []
    if dc:
        return [(int(a), b, c) for a, b, c in dc]
    return step_chain(cfg)


def chain_pos(chain, state):
    """把 `state.step` 解析成 dispatch_chain 内下标（v2.29.6 派活游标修复）。

    实测缺陷（P001 派活项目）：`step` 被两种口径写过 ——
    done / go / advance 写**链内下标**，cmd_claim 写**全局 9 步阶段号**；
    消费端直接拿 step 当下标用，派活项目里全局阶段号 ≥ 链长时被误判
    「流程已走完」（done 被拒、advance 推不动）。
    解析顺序（从严到宽）：
      ① stage+role+doc 三元组精确匹配 ② role 匹配（链内一职业一棒）
      ③ step 视为全局阶段号匹配 ④ 兜底当链内下标（全量项目 step==下标==阶段号）
      ⑤ 都对不上 → len(chain)（视为已走完，保持原判语义）。
    """
    step = int(state.get("step", 0))
    role = str(state.get("role") or "")
    doc = str(state.get("doc") or "")
    stage = state.get("stage")
    if stage is not None and role:
        for k, (stg, d, owner) in enumerate(chain):
            if int(stg) == int(stage) and owner == role and (not doc or d == doc):
                return k
    if role:
        for k, (stg, d, owner) in enumerate(chain):
            if owner == role:
                return k
    for k, (stg, d, owner) in enumerate(chain):
        if int(stg) == step:
            return k
    if 0 <= step < len(chain):
        return step
    return len(chain)


def skipped_roles_between(cfg, cur_stage, next_stage, dispatch):
    """列出「本次未派活而被跳过」的岗位。

    必须在**全局 9 步链**上算：派活项目的链里只剩被派活的阶段，
    直接在项目链上找会把「后面还要干的岗位」误报成「跳过」。
    """
    if not dispatch:
        return []
    gchain = step_chain(cfg)
    allowed = set(dispatch)
    pos_cur = pos_next = None
    for k, (idx, _doc, _owner) in enumerate(gchain):
        if idx == cur_stage:
            pos_cur = k
        if idx == next_stage and pos_next is None:
            pos_next = k
    if pos_cur is None or pos_next is None or pos_next <= pos_cur:
        return []
    out = []
    for k in range(pos_cur + 1, pos_next):
        owner = gchain[k][2]
        if owner and owner not in allowed and owner not in out:
            out.append(owner)
    return out


def cmd_pm(args):
    """**产品经理的项目总览（v2.9）**：统管多个项目的统筹者视角。

    创始人明确（v2.9 原话，v2.11 保留不改）：「这个项目的就是推进，包括上送，
    包括这整个项目的管理，都是由 PM 去做的……PM 是统管项目多个项目的主理人。」
    术语说明：v2.11 起「主理人」指创始人本人，pm 的正式称谓是「统筹者」。

    本命令是 pm 的**统筹台账**（只读）：
    - 列出**所有**项目（不是只当前项目），各自的阶段、接手岗位、状态；
    - 标出哪些项目**在等 pm 推进**（状态「待确认」且非人工闸门）；
    - 标出哪些项目**在等创始人拍闸门**（pm 不得代拍，§7）；
    - 标出哪些项目**还没上送到云端**（上送是 pm 的职责，v2.9）；
    - 标出**跨项目冲突**：同一个岗位被多个项目同时占用（需要排期决策）。

    只读，不改本地任何文件，不动状态机。
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    prof = profile_view(cfg)
    docs = prof.get("step_docs") or []
    owners = step_owner_map(prof)
    gates = notify_conf(cfg).get("gates") or [0, 2, 7]
    base = vault / "30_项目"
    rows = []
    labels = project_label_map(vault, cfg)
    if base.exists():
        for _pid, pdir in project_dirs(vault, cfg):
            st = read_handoff(vault, cfg, _pid)
            _label = labels.get(_pid) or _pid
            if st.get("zero_role"):
                rows.append({"dir": pdir.name, "project": _pid, "name": _label, "stage": -1,
                             "doc": "（零岗位项目）", "role": "—", "status": "已归档",
                             "gate": False, "gate_wait": False, "pending_pm": False,
                             "uploaded": True, "reason": st.get("reason", "")})
                continue
            done_upto = -1
            # v2.12：按**本项目的交接链**算当前棒，而不是固定 9 步序号。
            # 派活项目链上只有被派活的岗位 —— 否则台账会说「该 strategist 接手」，
            # 而 strategist 根本不在 pm 的派活清单里（实测：派 design+engineer，
            # 台账却报 strategist 可接）。口径与 opc new / opc done / opc go 一致。
            chain = project_chain(vault, cfg, _pid)
            for i, (_s, d, _r) in enumerate(chain):
                fp = _main_doc_path(pdir, _r, d, st.get("round") or "")
                if not fp.exists():
                    continue
                m = re.search(r"^status:\s*(\S+)", read_text(fp), re.M)
                if m and m.group(1) != "草稿":
                    done_upto = i
            cur = done_upto + 1
            if cur >= len(chain):
                rows.append({"dir": pdir.name, "project": _pid, "name": _label, "stage": -1,
                             "doc": "（已走完）", "role": "—", "status": "已完成",
                             "gate": False, "gate_wait": False, "pending_pm": False,
                             "uploaded": True})
                continue
            stage_no, doc, role = chain[cur]
            status = str(st.get("status") or "进行中")
            # 闸门口径的**唯一真相源** = 交接状态里的 gate 标记（由 opc done 在
            # 「刚完成的阶段是闸门」时写入）。这里**不再**用「下一个阶段号 ∈ gates」——
            # 那个口径差一格，会把该拍板的项目漏报、把不该拍板的误报（2026-11-11 实测）。
            gate_wait = bool(st.get("gate")) and status == "待确认"
            # 待 pm 推进：已报完成、状态「待确认」、且不在等创始人拍板
            pending_pm = (status == "待确认") and (not gate_wait)
            rows.append({
                "dir": pdir.name, "project": _pid, "name": _label,
                "stage": stage_no, "doc": doc, "role": role,
                "status": ("待人工闸门·阶段" + str(st.get("stage"))) if gate_wait else status,
                "gate": gate_wait, "gate_wait": gate_wait, "pending_pm": pending_pm,
                "uploaded": bool(st.get("pushed_by") == "pm"),
            })
    # v2.10：各项目待推送的队列条数（增量同步）
    qcount = {}
    for it in read_queue(vault):
        if it.get("status") == "已推送":
            continue
        qcount[it.get("project", "")] = qcount.get(it.get("project", ""), 0) + 1
    print("产品经理统筹台账（只读）——你统管全部项目，不只是当前这个")
    print("项目数：" + str(len(rows)))
    print("")
    print("  " + "项目".ljust(18) + "阶段".ljust(10) + "接手岗位".ljust(12)
          + "状态".ljust(14) + "待 pm 推进".ljust(12) + "待推送".ljust(10) + "已推送")
    for r in rows:
        stage = ("阶段 " + str(r["stage"])) if r["stage"] >= 0 else "—"
        qn = qcount.get(r["project"], 0)
        print("  " + str(r.get("name") or r["project"]).ljust(18) + stage.ljust(10)
              + str(r["role"]).ljust(12) + str(r["status"]).ljust(14)
              + ("是" if r["pending_pm"] else "").ljust(12)
              + (str(qn) + " 条" if qn else "").ljust(10)
              + ("是" if r["uploaded"] else "否"))
    pend = [r for r in rows if r["pending_pm"]]
    gate = [r for r in rows if r["gate_wait"]]
    notup = [r for r in rows if not r["uploaded"] and r["stage"] >= 0]
    print("")
    print("待你推进（收到创始人完成通知后执行 opc advance）："
          + (", ".join(r.get("name") or r["project"] for r in pend) if pend else "无"))
    print("待创始人拍闸门（拍板权在创始人，pm 不得代拍）："
          + (", ".join(r.get("name") or r["project"] for r in gate) if gate else "无"))
    print("人工闸门阶段（这些阶段做完后必须创始人给结论）："
          + (", ".join("阶段" + str(g) for g in gates) if gates else "无"))
    print("未上送云端（远程写入权归 pm，执行 opc sync --flush）："
          + (", ".join(r.get("name") or r["project"] for r in notup) if notup else "无"))
    # 跨项目岗位冲突：同一岗位被多个项目同时压在同阶段
    conflict = {}
    for r in rows:
        if r["stage"] >= 0 and r["role"] and r["role"] != "—":
            conflict.setdefault(r["role"], []).append(r["project"])
    clash = {k: v for k, v in conflict.items() if len(v) > 1}
    if clash:
        print("")
        print("跨项目岗位占用（需排期决策；不同项目互不阻塞，但同一岗位同时被占要排优先级）：")
        for k in sorted(clash):
            print("  " + k + "：" + "、".join(clash[k]))
    print("")

    # —— 派活决策面板（v2.11）：现在该派谁 ——
    # 口径 = step_owner 推出的接手岗位 × 任务池里的岗位占用 × 闸门状态。
    pool = read_task_pool(vault)
    claims_by_role = {}
    for k, v in (pool.get("claims") or {}).items():
        claims_by_role[k] = v.get("project")
    actionable = [r for r in rows if r["stage"] >= 0 and r["role"] and r["role"] != "—"]
    if actionable:
        print("现在该派谁（接手岗位 × 岗位占用 × 闸门状态）：")
        print("  " + "项目".ljust(14) + "阶段".ljust(8) + "接手岗位".ljust(13) + "派活结论")
        for r in actionable:
            role = r["role"]
            if r["gate_wait"]:
                verdict = "等创始人拍板 → 暂不派活"
            elif r["pending_pm"]:
                verdict = "待 pm 推进（收到通知后 opc advance）"
            else:
                busy = claims_by_role.get(role)
                if not busy:
                    verdict = "可接（" + role + " 现在空闲）"
                elif busy == r["project"]:
                    verdict = "进行中（" + role + " 已接手本项目）"
                elif len(conflict.get(role, [])) > 1:
                    verdict = ("冲突（" + role + " 同时被 " + "、".join(conflict[role])
                               + " 需要 → 先排优先级）")
                else:
                    verdict = "排队（" + role + " 正忙：" + str(labels.get(busy) or busy) + "）"
            print("  " + str(r.get("name") or r["project"]).ljust(14)
                  + ("阶段" + str(r["stage"])).ljust(8)
                  + role.ljust(13) + verdict)
        idle = [x for x in role_bindings_ref(cfg).keys()
                if x not in conflict and x not in claims_by_role]
        if idle:
            print("  本轮无人要、也没人占的岗位：" + "、".join(idle))
        print("")

    # —— 派活清单状态（v2.11）：有没有清单、清单里派了谁 ——
    tri_rows = []
    for r in rows:
        if r["stage"] < 0:
            continue
        d = read_triage_json(vault, r["dir"])
        label = r.get("name") or r.get("project") or r["dir"]
        if not d:
            tri_rows.append((label, "无清单", ""))
        elif d.get("zero"):
            tri_rows.append((label, "零岗位", str(d.get("reason") or "")))
        else:
            who = [e.get("role") for e in d.get("entries") or [] if e.get("involved")]
            tri_rows.append((label, ("、".join(who) if who else "清单为空"),
                             str(d.get("updated_at") or "")))
    if tri_rows:
        print("派活清单状态（结构化落点：项目目录 _派活清单.json）：")
        for pid, who, extra in tri_rows:
            print("  " + str(pid).ljust(14) + str(who) + ("  (" + extra + ")" if extra else ""))
        print("")

    print("推进命令：opc advance --project <项目名>（只推进一棒，不跳步）")
    print("推送命令：opc sync --flush（把待推送队列增量推到远端，每次产出版本目录）")
    print("查看队列：opc sync --queue（只读，不碰远程）")
    if not str((((load_config(vault).get("backends")) or {}).get("feishu") or {}).get("base_token") or "").strip():
        print("提示：多维表格台账未配置（可选，进度跟进线）——init 时配置，"
              "或往 opc.config.json 的 backends.feishu.base_token 填表地址。")
    print("闸门命令：opc go --decision 通过（拍板权始终在创始人手上，§7）")

def _load_triage(vault, cfg, triage_file, from_project=None):
    """pm 派活清单：路由的唯一真相源（§15.14）。

    **解析失败一律显式报错**，绝不静默降级为全量 9 步（那会把「只派
    design + engineer」悄悄变成「8 个岗位全派」，创始人毫不知情）。
    """
    tri = None
    bindings = role_bindings_ref(cfg)
    if triage_file:
        tf = Path(triage_file)
        if not tf.is_absolute():
            # 相对路径先按当前工作目录找，再回退到 vault 根
            if not tf.exists():
                tf = Path(vault) / triage_file
        if not tf.exists():
            raise ValueError("pm 派活清单不存在：" + str(triage_file)
                             + "（相对路径按当前目录与 vault 根解析）")
        raw = read_text(tf)
        if tf.suffix.lower() == ".json":
            tri = parse_triage_json(raw, set(bindings.keys()))
        else:
            tri = parse_triage(raw, set(bindings.keys()))
        tri["file"] = str(tf)
        errs = tri.get("errors") or []
        if errs:
            raise ValueError(
                "pm 派活清单解析失败（已中止，**没有**按全量 9 步建项）：\n  - "
                + "\n  - ".join(errs)
                + "\n清单文件：" + str(tf)
                + "\n格式见 pm 岗位卡 §5.1；或用 opc triage 起草（自动生成规范格式）。")
        if not tri["found"]:
            raise ValueError(
                "pm 派活清单里没找到「" + TRIAGE_HEADING + "」小节（已中止，"
                "**没有**按全量 9 步建项）：\n清单文件：" + str(tf))
        if not tri["roles"] and not tri["zero"]:
            raise ValueError(
                "pm 派活清单没判出任何「涉及」的岗位（已中止，**没有**按全量 9 步建项）：\n清单文件："
                + str(tf))
    elif from_project:
        # 从上一版项目继承 pm 的分诊结论
        tp, txt = triage_source_doc(vault, from_project)
        if txt:
            tri = parse_triage(txt, set(bindings.keys()))
            tri["file"] = str(tp) if tp else ""
            if tri.get("errors"):
                print("提示：继承的派活清单有 " + str(len(tri["errors"]))
                      + " 处解析问题（本次按全量处理）：" + "；".join(tri["errors"][:3]))
    return tri


def _version_role_assets(vault, cfg, pid, version):
    """本项目该版本在产出索引里登记的资产：[(key, role, stage)]，按 step_docs 顺序。

    v2.18（ADR-0006）：派活项目只登记 pm 点名的资产（如 4_产品基线），
    阶段代表键必须取**登记到的**那个，否则 source 会指到没派活的资产上。
    """
    prof = profile_view(cfg)
    order = {}
    for i, d in enumerate(prof.get("step_docs") or []):
        order.setdefault(d[:-3], i)
    rows = []
    for r in read_index(vault, cfg):
        if project_key(r.get("project")) != project_key(pid):
            continue
        if (r.get("版本") or "").strip() != (version or "").strip():
            continue
        key = (r.get("逻辑键") or "").rsplit("/", 1)[-1].strip()
        if not key:
            continue
        rows.append((key, (r.get("role") or "").strip(),
                     (r.get("阶段") or "").strip()))
    rows.sort(key=lambda x: (order.get(x[0], 9999), x[0]))
    return rows


def _gen_project_docs(vault, cfg, project, docs, pdir, today, version=""):
    """生成项目/版本文档（首次建项与追加立项共用）。

    v2.19（ADR-0007）：版本级产出落 `<版本目录>/<role>/` 目录（`YYYY-MM-DD-V<N>`），
    文件名不带前缀。v2.20（创始人拍板）：阶段 0/1（约束卡、MRD）也随版本落
    `<版本目录>/<owner>/`，不再落项目根。目标路径取自 `resolve_uri`，
    保证「索引写的地址」与「文件实际落点」一致。
    """
    upstream_by_doc = {}
    if not docs:
        return upstream_by_doc
    prof = profile_view(cfg)
    bindings = role_bindings_ref(cfg)
    # —— 上游（source）推导（v2.18）——
    # ① 全量 step_docs 顺序 → 每个阶段的**前一个阶段**
    prev_stage_of = {}
    _last_st = None
    for doc in prof.get("step_docs") or []:
        st = doc.split("_", 1)[0]
        if st and st.isdigit():
            prev_stage_of[st] = _last_st
            _last_st = st
    # ② 本项目该版本**登记到的**资产 → 每阶段的代表键（派活项目只登记 pm 点名的）
    stage_first = {}
    for _key, _role, _st in _version_role_assets(vault, cfg, project, version):
        if _st and _st not in stage_first:
            stage_first[_st] = _key
    # ③ 全量 step_docs 里每阶段的默认代表键（登记里没有该阶段时兜底）
    default_first = {}
    for doc in prof.get("step_docs") or []:
        st = doc.split("_", 1)[0]
        if st and st.isdigit() and st not in default_first:
            default_first[st] = doc[:-3]
    for fname, role, key in docs:
        arts = (bindings.get(role) or {}).get("artifacts") or {}
        uri = ""
        target = None
        if key in arts:
            try:
                _b, uri = resolve_uri(cfg, vault, role, key, project, version, pdir.name)
                target = _uri_to_path(vault, uri)
            except Exception:
                uri = ""
        # 兜底：resolve_uri 失败时按目录约定落盘（版本级 <版本目录>/role/，项目级根）
        if target is None:
            vdir = version_dir_at(pdir, version) if is_version_scoped(key) else ""
            target = (pdir / vdir / role / fname) if vdir else (pdir / fname)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        st = key.split("_", 1)[0] if "_" in key else ""
        prev_st = prev_stage_of.get(st)
        up_key = stage_first.get(prev_st) or default_first.get(prev_st)
        src = "[[" + project + "/" + up_key + "]]" if up_key else ""
        pinned = None
        if key == "0_约束卡":
            # 规则钉扎由 init_project_tools 统一补齐（连带 toolset_snapshot）
            pinned = ["[OPC-架构规范@" + str(constitution_version(vault, cfg)) + "]"]
        write_text(target, step_frontmatter(project, role, key, today, uri, src, pinned))
        upstream_by_doc[key] = src
    return upstream_by_doc


def _uri_to_path(vault, uri):
    """把 `local://` / 相对路径 URI 换算成 Vault 内绝对路径。"""
    rel = uri.split("://", 1)[-1] if "://" in uri else uri
    return Path(vault) / rel


def _main_doc_path(pdir, role, doc, version=""):
    """主产出实际落点（v2.19 / ADR-0007）。

    版本级（阶段 ≥ 2）→ `pdir / <版本目录> / <role> / <doc>`；
    版本目录 = `YYYY-MM-DD-V<N>`（新式，日期 = 该版本立项日）/ `V<N>`（存量）；
    项目级（阶段 0/1）→ `pdir / <doc>`。
    兼容存量：版本级文件若在项目根（旧平铺结构）也能读到。
    """
    vdir = version_dir_at(pdir, version)
    if vdir:
        cand = pdir / vdir / role / doc
        if cand.exists():
            return cand
    return pdir / doc


def cmd_ask(args):
    """**本岗主动问创始人**：用**本岗自己的**飞书应用身份，在群里 @ 创始人。

    为什么需要它（2026-11-20 实测）：岗位卡 §11.5 写了「需要交互就在群里 @ 创始人」，
    但 skill 里**没有任何命令能做这件事** —— 岗位到了交互点（阶段 0 缺三项输入、
    约束草案要拍板、架构技术栈要研讨…）只能把问题烂在本地文件里，
    创始人**在群里根本不知道有人卡住了**。实测：strategist 被叫醒后卡在阶段 0，
    创始人零感知。

    与 `opc done`（报完成）的区别：
      · `opc done` = 报完成，通知内容是产出与下一棒；
      · `opc ask`   = **提问/要拍板**，交互点专用，不推进游标、不产生资产批次。
    与 pm 的门铃（wake_next_role）的区别：
      · 门铃 = pm @ 下一棒叫它起床；
      · `opc ask` = 岗位 @ 创始人问问题（§15.9 允许：通知对象是创始人）。
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    role = str(getattr(args, "role", "") or "")
    if role not in role_bindings_ref(cfg):
        raise ValueError("请用 --role 指定本岗（如 strategist / design / engineer）")
    text = str(getattr(args, "text", "") or "").strip()
    if not text:
        raise ValueError("请用 --text 写清要问什么（这是要 @ 创始人的交互消息）")
    conf = notify_conf(cfg)
    disp = (conf.get("dispatch") or {})
    if disp.get("enabled") is False:
        raise ValueError("notify.dispatch.enabled=false，无法在群里 @ 创始人")
    if not _dispatch_group_mode(disp):
        raise ValueError("当前 notify.dispatch.mode=" + str(disp.get("mode") or "dm")
                         + "，ask 只在群模式（mode=group）下可用")
    chat_id = str(disp.get("chat_id") or "").strip()
    if not chat_id:
        raise ValueError("notify.dispatch.chat_id 未配置，无法在群里 @ 创始人")
    try:
        import dispatch as dispatch_mod
    except Exception as exc:  # noqa: BLE001
        raise ValueError("派活通知模块不可用：" + str(exc))
    root = Path(disp.get("profile_root") or (Path.home() / ".hermes" / "profiles"))
    founder_id = dispatch_founder_id(disp, role)
    if not founder_id:
        raise ValueError("未配置创始人用户 ID（FEISHU_FOUNDER_UNION_ID），无法 @ 创始人")
    title = "【OPC 交互】" + str(role) + " 有事要确认"
    lines = [
        "我是 " + str(role) + "，有一件事需要你拍板 / 补充：",
        text,
    ]
    proj = str(getattr(args, "project", "") or "").strip()
    if proj:
        lines.insert(1, "项目：" + proj)
    lines.append("（我用**本岗自己的身份**在这条消息里 @ 你 —— 不用你再 @ 我。）")
    res = dispatch_mod.notify_next_role(
        str(role), founder_id, title, lines,
        profile_root=root,
        app_id_env=str(disp.get("app_id_env") or "FEISHU_APP_ID"),
        app_secret_env=str(disp.get("app_secret_env") or "FEISHU_APP_SECRET"),
        chat_id=chat_id, mode="group")
    if res.get("ok"):
        print("已在群里 @" + "创始人" + "（用 " + str(role) + " 的身份，message_id="
              + str(res.get("message_id")) + "）")
        print("对方回「继续 / 通过 / 驳回 / 需补充」即可；回话时 @ " + str(role) + " 就能叫醒本岗。")
    else:
        print("未能送达：" + str(res.get("detail")))
        print("降级：请创始人在群里 @ " + str(role) + " 交互；或把问题写进项目的 _待确认.md。")
    # 同时落盘一份，交互内容可溯源（不依赖群消息留存）
    try:
        stamp = datetime.now().isoformat(timespec="seconds")
        inbox = Path(vault) / "99_Agent配置" / "_交互留痕.md"
        old = read_text(inbox) if inbox.exists() else "# OPC 交互留痕\n"
        write_text(inbox, old.rstrip() + "\n\n## " + stamp + " · " + str(role)
                   + ((" · " + proj) if proj else "") + "\n\n" + text + "\n")
        print("交互留痕：" + str(inbox.relative_to(vault)))
    except OSError:
        pass
    return res


def cmd_new(args):
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    name = getattr(args, "name", None)
    fully_interactive = interactive_enabled(args) and not name
    if fully_interactive:
        name = prompt("项目名称：", "")
        if not name:
            raise ValueError("项目名称不能为空")
    raw_tags = getattr(args, "tags", "") or ""
    if fully_interactive and not raw_tags:
        raw_tags = prompt("类型标签（2–4 个，空格分隔，如：订阅制 高频工具 B端结算）：", "")
    project = reserve_project_id(vault, cfg)
    tags = parse_tags(raw_tags)
    if not tags:
        print("提示：未提供标签，相似检索将无结果。建议带 2–4 个标签。")
    today = date.today().isoformat()
    stamp = today[:7]
    # v2.19（ADR-0007）：目录名 = **项目名**（同名项目加 `-2` 后缀），**不再带年月** ——
    # 时间维度下沉到版本目录（`YYYY-MM-DD-V<N>`），项目名不再被建项时间污染。
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or project
    base_dir = vault / "30_项目"
    dirname = _unique_project_dir(base_dir, safe_name)
    pdir = base_dir / dirname
    prof = profile_view(cfg)
    bindings = role_bindings_ref(cfg)
    from_project = getattr(args, "from_project", None)
    # —— 变更性质标签（--scope）：只是标签，不再决定谁干活 ——
    scope = getattr(args, "scope", None) or getattr(args, "light", None)
    if scope and scope not in ("画面", "后端", "前端", "文案", "数据", "全量", "迭代", "其他"):
        raise ValueError("未知变更性质：" + scope + "（可选：画面/后端/前端/文案/数据/迭代/其他/全量）")

    # —— pm 派活清单：路由的唯一真相源（§15.14）——
    triage_file = getattr(args, "triage", None)
    tri = _load_triage(vault, cfg, triage_file, from_project)
    if tri and tri["found"] and not tri["zero"] and tri["roles"]:
        print("pm 派活清单（" + str(tri.get("file") or "继承自 " + str(from_project)) + "）："
              + "、".join(tri["roles"]))
    if tri and tri["zero"]:
        print("pm 判定本次零岗位：" + (tri["reason"] or "（未写理由）"))
    if scope and tri and tri["found"] and tri["roles"]:
        scope_roles = _scope_roles(scope)
        if scope_roles and not set(tri["roles"]).issubset(set(scope_roles)):
            print("提示：pm 派活清单（" + "、".join(tri["roles"]) + "）与 --scope "
                  + scope + " 的常规范围不一致 —— **以 pm 的派活清单为准**（§15.14 分诊权在 pm）。")

    # —— 决定本次要生成哪些文档 ——
    # v2.11：派活清单校验通过**之后**才建目录 —— 报错中止时不留空项目壳。
    pdir.mkdir(parents=True, exist_ok=True)
    if tri and tri["found"] and tri["zero"]:
        docs = []
        roles = []
    elif tri and tri["found"] and tri["roles"]:
        roles = list(dict.fromkeys(tri["roles"]))
        docs = _docs_for_roles(cfg, roles, tri["assets"])
    else:
        if not triage_file and not from_project:
            print("提示：未提供 pm 派活清单 → 本次按**全量 9 步**建项（8 个岗位全部派活）。")
            print("      若只想派部分岗位：先 opc triage 起草清单，再 opc new --triage <清单文件>。")
        roles = list(bindings.keys())
        docs = _docs_for_roles(cfg, roles, None)

    # —— 版本目录：v2.19 只登记不预生成（ADR-0006 / ADR-0007）——
    # 建 `<YYYY-MM-DD>-V1` 目录占位（版本=目录，日期 = 本次立项日），
    # **不生成任何空壳文档**；各岗位真正产出时用 opc scaffold 生成自己的那份。
    (pdir / (today + "-V1")).mkdir(parents=True, exist_ok=True)

    # —— 产出索引 ——
    # v2.13：索引是**共享单文件**，读改写必须在锁内（并发建项会互相覆盖行）。
    with VaultLock(vault):
        rows = read_index(vault, cfg)
        zero = bool(tri and tri["found"] and tri["zero"])
        if zero:
            # 零岗位：也要记一行，留痕「本次无需岗位投入 + 为什么」
            rows.append({
                "项目": name,
                "project": project,
                "类型标签": " ".join("#" + t for t in tags),
                "role": "—",
                "阶段": "",
                "逻辑键": "L2/" + project + "/（零岗位）",
                "物理地址 URI": "",
                "简历条目": "—",
                "关联规则": "pm 分诊：零岗位 —— " + (tri["reason"] or "（未写理由）"),
                "状态": "已归档",
                "时间": stamp,
                "版本": "V1",
            })
        else:
            rows.extend(_index_rows_for(vault, cfg, project, name, tags, docs, "进行中",
                                        version="V1", when=stamp, dirname=dirname))
        write_index(vault, cfg, rows)

    # —— 版本台账：创建项目 = 第一次立项 = V1（ADR-0005 D1）——
    write_versions(vault, project,
                   [{"版本": "V1", "时间": today, "需求": name, "状态": "进行中",
                     "产出份数": "0", "沉淀状态": "—", "沉淀时间": "",
                     "立项批准": "创始人发起建项（opc new）"}],
                   title=name)

    # —— 项目关系登记 ——
    if from_project or scope:
        rel_path = vault / "30_项目" / "_项目关系.md"
        rel_path.parent.mkdir(parents=True, exist_ok=True)
        if rel_path.exists():
            rt = read_text(rel_path)
        else:
            rt = "# 项目关系\n\n| 项目 | 名称 | 继承自 | 变更性质 | 派活岗位 |\n|---|---|---|---|---|\n"
        rt += "| " + project + " | " + name + " | " + (from_project or "—") + " | " \
              + (scope or "—") + " | " + ("、".join(roles) if roles else "零岗位") + " |\n"
        write_text(rel_path, rt)

    print("已建项目:", project, dirname)
    print("目录:", pdir)
    # v2.11：把派活清单搬进项目目录 —— 建项后清单有固定落点，opc pm 台账能直接看到。
    # v2.17：落盘的是**结构化 JSON**（不是 Markdown 原文）—— 原文写进 .json 会让
    # `read_triage_json` 读出 None，台账显示「无清单」，路由被悄悄改掉。
    if tri and tri.get("file") and Path(tri["file"]).exists():
        dest = pdir / TRIAGE_FILE
        try:
            data = triage_to_json(project, tri)
            dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print("派活清单已入库：" + str(dest))
            fp2 = sync_triage_markdown(vault, project, data)
            if fp2:
                print("已写人可读形态：" + str(fp2))
        except OSError:
            pass
    if tri and tri["found"] and tri["zero"]:
        print("派活：无岗位（pm 判定零岗位）| 索引已记一行并直接归档，不建交接链、不发通知。")
        print("分诊理由：" + (tri["reason"] or "（未写理由）"))
    else:
        print("profile:", prof["label"], "| 本次岗位:", ("、".join(roles) if roles else "（无）"),
              "| 登记产出:", len(docs), "项 | 索引行:", len(docs), "条")
        print("产出文件不预生成（v2.18）：各岗位开工时用 opc scaffold --role <role> 生成自己的产出骨架。")
    print("")
    similar_rows = match_similar(rows, tags, project)
    print_similar(similar_rows, tags)

    # —— 当前项目 / 工具配置 / 交接状态 ——
    cfg["active_project"] = project
    save_config(vault, cfg)
    init_project_tools(vault, cfg, project)
    if tri and tri["found"] and tri["zero"]:
        # 零岗位：无交接链、不发通知、不阻塞流程（§15.14）
        write_handoff(vault, cfg, {
            "project": project, "step": 0, "stage": 0,
            "role": "pm", "doc": "4_产品基线.md",
            "status": "已完成", "token": "", "next_role": "",
            "zero_role": True,
            "reason": tri["reason"],
            "round": "V1",
            "created": date.today().isoformat(),
        })
        print("零岗位项目已归档。当前项目已设为 " + project + "。")
        return
    # 派活项目的交接链按「本次实际生成的文档」构建，
    # 全量项目用固定 9 步链。
    if tri and tri["found"] and tri["roles"]:
        chain = _dispatch_chain(cfg, docs)
    else:
        chain = step_chain(cfg)
    if not chain:
        raise ValueError("本次派活没能构成任何交接阶段；请检查派活清单的岗位与产出资产列。")
    # 交接链起点：
    #   · 有 pm 派活清单 → 从清单里第一个涉及的岗位起步（pm 分诊排除了谁就不找谁）
    #   · 无派活清单（全量）→ 从链条第一步起步，完整走 9 步
    start_idx = 0
    if tri and tri["found"] and tri["roles"]:
        for i, (idx, doc, owner) in enumerate(chain):
            if owner in roles:
                start_idx = i
                break
    write_handoff(vault, cfg, {
        "project": project, "step": start_idx, "stage": chain[start_idx][0],
        "role": chain[start_idx][2], "doc": chain[start_idx][1],
        "status": "进行中", "token": "", "next_role": "",
        "dispatch_roles": roles if (tri and tri["found"]) else [],
        "dispatch_chain": [[a, b, c] for a, b, c in chain] if (tri and tri["found"] and tri["roles"]) else [],
        "round": "V1",
        "created": date.today().isoformat(),
    })
    print("当前项目已设为 " + project + "；后续 opc done / go / handoff 均无需再传 --project。")
    if tri and tri["found"] and tri["roles"]:
        print("交接链起点：" + chain[start_idx][1] + " · " + chain[start_idx][2]
              + "（按 pm 派活清单，链上无关岗位自动跳过）")
    else:
        print("交接链起点：" + chain[start_idx][1] + " · " + chain[start_idx][2]
              + "（全量流程，9 步完整走）")
    # v2.28（2026-11-20 实测修）：**建项后必须给第一棒发门铃**。
    # 过去 opc new 只落盘不发通知 → 第一棒永远没人叫醒，链卡在阶段 0。
    wake_next_role(vault, cfg, project, chain[start_idx][2], chain[start_idx][1], chain[start_idx][0])


# ---------------------------------------------------------------------------
# v2.17：版本链与沉淀（ADR-0005）
#
# 版本 = 一次立项：① 创建项目（V1）；② 项目内确认「这个需求要做」（V2、V3…）。
# 立项不占闸门游标，但**必须创始人同意**（v2.29.5：--founder-approved 留痕）；版本号只增不复用。
# 沉淀 = 各职业 Agent 把一个版本的产出**按岗拆开**，自己概括、自己写进自己的履历卡
# —— 不建中间层文件（ADR-0005 D3/D4/D5/D8）。
# ---------------------------------------------------------------------------


def ensure_version_ledger(vault, project):
    """存量项目兼容：没有版本台账就补一条 V1（其现有文件视为 V1，且不加前缀）。"""
    rows = read_versions(vault, project)
    if rows:
        return rows
    name = project_name_of(vault, project)
    tm = project_time_of(vault, project) or date.today().isoformat()[:7]
    rows = [{"版本": "V1", "时间": tm, "需求": name + "（初始立项）", "状态": "进行中",
             "产出份数": "—", "沉淀状态": "—", "沉淀时间": "",
             "立项批准": "存量补记（建项即 V1）"}]
    write_versions(vault, project, rows, title=name)
    return rows


def resolve_version(vault, project, version):
    """版本号归一：空 → 当前最新**有效**版本；写错 → 报错（不静默当 V1）。

    v2.29.6 修（实测踩坑）：旧写法取 `known[-1]`（台账最后一行），
    作废版本排在最后时会把默认版本指向**已作废**的版本 → distill 报「无可沉淀素材」
    并把该作废版本的沉淀状态误标「已沉淀」。现在默认版本跳过作废行。
    """
    rows = read_versions(vault, project)
    known = [r.get("版本") for r in rows]
    live = [r.get("版本") for r in rows if "作废" not in (r.get("状态") or "")]
    v = (version or "").strip()
    if not v:
        return (live or known)[-1] if (live or known) else "V1"
    if v.isdigit():
        v = "V" + v
    if v not in known:
        raise ValueError("版本 " + v + " 不存在（本项目版本："
                         + ("、".join(known) if known else "无（存量项目按 V1）") + "）。")
    return v


def _asset_produced(vault, uri):
    """这份产出**真的做出来了吗**（占位草稿不算）。

    建项时模板就落了一堆 `status: 草稿` 的空壳文件 —— 只看「文件在不在」会把
    所有人都算成「有产出」，沉淀清单就废了。
    """
    rel = uri.split("://", 1)[-1] if "://" in uri else uri
    fp = Path(vault) / rel
    if not fp.exists():
        return False
    if fp.suffix.lower() not in (".md", ".markdown"):
        return True          # 非文档类资产（html / yaml…）落盘即产出
    m = re.search(r"^status:\s*(\S+)", read_text(fp), re.M)
    return bool(m and m.group(1) != "草稿")


def _version_assets(vault, cfg, pid, version):
    """该版本**实际产出**的素材，按岗分组：{role: [{uri, key, stage}]}。

    素材来源 = 资产快照里的实际产出，**不是**建项计划表 —— 计划表会把没参与的人算进去。
    """
    out = {}
    for r in read_index(vault, cfg):
        if project_key(r.get("project")) != project_key(pid):
            continue
        if (r.get("版本") or "").strip() != version:
            continue
        role = (r.get("role") or "").strip()
        if not role or role == "—":
            continue
        uri = (r.get("物理地址 URI") or "").strip()
        if not uri or not _asset_produced(vault, uri):
            continue
        out.setdefault(role, []).append({"uri": uri, "key": r.get("逻辑键", ""),
                                         "stage": r.get("阶段", "")})
    return out


def cmd_migrate(args):
    """存量 Vault 迁移：目录名去 `P00X_` 前缀 + 补版本台账 V1 + 索引补时间/版本列。

    v2.17（ADR-0005 §4.1/§4.7）：老 Vault 的项目目录叫 `P001_项目名`，
    人读处要变成项目名；v2.19（ADR-0007）起目录名**不再带年月**（时间在下沉到
    版本目录），所以目标名 = `项目名`（同名加 `-2`）。老项目**视为单版本 V1**
    （文件不加前缀）。迁移只动**目录名与索引元数据**，不碰任何岗位产出文件。
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    base = vault / "30_项目"
    dry = bool(getattr(args, "dry_run", False))
    if not base.exists():
        print("没有 30_项目 目录，无需迁移。")
        return
    rows = read_index(vault, cfg)
    by_pid = {}
    for r in rows:
        by_pid.setdefault(project_key(r.get("project")), []).append(r)
    plans = []
    for pdir in sorted(base.iterdir()):
        if not pdir.is_dir():
            continue
        m = re.match(r"^(P\d{3})_(.+)$", pdir.name)
        if not m:
            continue
        pid, pname = m.group(1), m.group(2)
        tm = ""
        for r in by_pid.get(pid, []):
            if (r.get("时间") or "").strip():
                tm = r["时间"].strip()
                break
        if not tm:
            tm = datetime.fromtimestamp(pdir.stat().st_mtime).strftime("%Y-%m")
        plans.append((pid, pname, pdir, _unique_project_dir(base, pname), tm))
    if not plans:
        print("没有需要迁移的旧格式项目目录。")
        return
    changed_index = False
    for pid, pname, pdir, new, tm in plans:
        target = base / new
        if target.exists():
            print("跳过（目标目录已存在）：" + pdir.name + " → " + new)
            continue
        print(("将迁移：" if dry else "已迁移：") + pdir.name + " → " + new)
        if dry:
            continue
        stamp = datetime.fromtimestamp(pdir.stat().st_mtime).strftime("%Y-%m-%d")
        pdir.rename(target)
        led = target / VERSION_LEDGER_NAME
        if not led.exists():
            write_text(led, "# 版本台账 · " + pname + "\n\n"
                       + "> 一个版本 = 一次立项：① 创建项目；② 项目内确认「这个需求要做」。\n"
                       + "> 立项不占闸门游标，但**必须创始人同意**（v2.29.5：`--founder-approved` 留痕）；版本号只增不复用。\n\n"
                       + "| 版本 | 立项时间 | 需求 | 状态 | 产出份数 | 沉淀状态 | 沉淀时间 | 立项批准 |\n"
                       + "|---|---|---|---|---|---|---|---|\n"
                       + "| V1 | " + stamp + " | " + pname + "（初始立项） | 进行中 | — | — | — | 存量补记（migrate） |\n")
            print("  已补版本台账（V1）：" + str(led))
        for r in by_pid.get(pid, []):
            if not (r.get("项目") or "").strip():
                r["项目"] = pname
                changed_index = True
            if not (r.get("时间") or "").strip():
                r["时间"] = tm
                changed_index = True
            if not (r.get("版本") or "").strip():
                r["版本"] = "V1"
                changed_index = True
            # 目录改名 → 索引里的物理地址 URI 必须同步改，否则全部指向不存在的路径
            uri = (r.get("物理地址 URI") or "").strip()
            if uri:
                newuri = re.sub(r"/" + re.escape(pdir.name) + r"(/|$)",
                                "/" + new + r"\1", uri)
                if newuri != uri:
                    r["物理地址 URI"] = newuri
                    changed_index = True
    if changed_index and not dry:
        with VaultLock(vault):
            write_index(vault, cfg, rows)
        print("产出索引已补齐：项目名 / 时间 / 版本 三列（物理地址 URI 随目录改名同步）。")
    if dry:
        print("")
        print("（试运行：没有改动任何文件。去掉 --dry-run 执行。）")


def cmd_version_list(args):
    """列出版本台账：一个项目 = 若干版本，一个版本 = 一次立项。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    project = getattr(args, "project", None) or cfg.get("active_project") or ""
    if not project:
        raise ValueError("请用 --project 指定项目；或先 opc new 建项。")
    pid = resolve_project(vault, cfg, project)
    name = project_name_of(vault, pid)
    rows = read_versions(vault, pid)
    print("项目：" + name + "（" + pid + "）  目录：" + project_dir_name(vault, pid))
    if not rows:
        print("无版本台账 —— 按兼容规则视为单版本 V1（现有文件即 V1，不加前缀）。")
        return
    print("")
    print("  " + "版本".ljust(7) + "立项时间".ljust(12) + "需求".ljust(30)
          + "状态".ljust(10) + "产出".ljust(6) + "沉淀状态".ljust(11) + "沉淀时间".ljust(11) + "立项批准")
    for r in rows:
        # v2.18（ADR-0006 D4）：产出份数按**实际落盘且非草稿**统计，不按计划数
        cnt = _version_produced_count(vault, cfg, pid, str(r.get("版本") or ""))
        print("  " + str(r.get("版本")).ljust(7) + str(r.get("时间")).ljust(12)
              + str(r.get("需求") or "")[:28].ljust(30) + str(r.get("状态") or "").ljust(10)
              + str(cnt).ljust(6)
              + str(r.get("沉淀状态") or "—").ljust(11) + str(r.get("沉淀时间") or "—").ljust(11)
              + str(r.get("立项批准") or "—")[:40])


def _version_produced_count(vault, cfg, pid, version):
    """统计某版本**实际产出**份数：索引该版本行中，URI 指向的文件真实存在且非草稿。"""
    n = 0
    for r in read_index(vault, cfg):
        if project_key(r.get("project")) != project_key(pid):
            continue
        if (r.get("版本") or "").strip() != version:
            continue
        uri = (r.get("物理地址 URI") or "").strip()
        if uri and _asset_produced(vault, uri):
            n += 1
    return n


def cmd_version_new(args):
    """项目内**追加一次立项** = 新版本号（V2、V3…）。

    - 立项**不占闸门游标**，但**必须创始人同意**（v2.29.5，实测 pm 曾未经同意追加立项）：
      未带 `--founder-approved "<同意依据>"` 一律拒绝、不占号；同意依据留痕进版本台账。
    - 版本号**只增不复用**：废号不回收。
    - 走哪些阶段、派哪些岗位仍由 **pm 派活清单**说了算（D9）；缺省沿用本项目上一版清单。
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    project = getattr(args, "project", None) or cfg.get("active_project") or ""
    if not project:
        raise ValueError("请用 --project 指定项目；或先 opc new 建项。")
    pid = resolve_project(vault, cfg, project)
    pdir = Path(vault) / "30_项目" / project_dir_name(vault, pid)
    if not pdir.is_dir():
        raise ValueError("项目目录不存在：" + str(pdir))
    name = project_name_of(vault, pid)
    need = (getattr(args, "need", "") or "").strip()
    if not need:
        raise ValueError("请用 --需求 说明这次要做什么（立项必须有需求描述）。")
    approved = (getattr(args, "founder_approved", "") or "").strip()
    if not approved:
        raise ValueError(
            "立项是**创始人决策点**（v2.29.5）：新版本号 = 新立项，未经创始人同意不得执行。\n"
            "  正确流程：先问创始人「这个需求要不要立项」，拿到明确同意后，带\n"
            "  --founder-approved \"<同意依据>\" 重跑（依据留痕进版本台账，可审计）。\n"
            "  本命令不代拍板、不默认放行——任何岗位（含 pm / strategist）都一样。")

    ensure_version_ledger(vault, pid)
    version = next_version(vault, pid)
    today = date.today().isoformat()
    bindings = role_bindings_ref(cfg)

    # —— 派活：本次清单优先；缺省沿用项目里那一份；都没有 → 全量 9 步 ——
    triage_file = getattr(args, "triage", None)
    tri = _load_triage(vault, cfg, triage_file, None)
    if not (tri and tri.get("found")):
        tj = read_triage_json(vault, pid)
        if tj:
            tri = parse_triage_json(json.dumps(tj, ensure_ascii=False), set(bindings.keys()))
            tri["file"] = str(pdir / TRIAGE_FILE)
        else:
            tri = None
    if tri and tri.get("found") and tri.get("zero"):
        raise ValueError("派活清单判定本次零岗位 —— 零岗位不占版本号（没立项就没有版本）。")
    if tri and tri.get("found") and tri.get("roles"):
        roles = list(dict.fromkeys(tri["roles"]))
        docs = apply_version_prefix(_docs_for_roles(cfg, roles, tri.get("assets")), version)
        print("本次派活（pm 派活清单）：" + "、".join(roles))
    else:
        print("提示：没有派活清单 → 本次按**全量 9 步**推进。")
        roles = list(bindings.keys())
        docs = apply_version_prefix(_docs_for_roles(cfg, roles, None), version)

    # —— 版本目录：v2.19 只登记不预生成（ADR-0006 / ADR-0007）——
    # 建 `<YYYY-MM-DD>-V<N>/` 目录占位（版本=目录，日期 = 本次立项日），
    # **不生成任何空壳文档**。
    (pdir / (today + "-" + version)).mkdir(parents=True, exist_ok=True)

    # —— 产出索引：追加该版本的行 ——
    with VaultLock(vault):
        rows = read_index(vault, cfg)
        rows.extend(_index_rows_for(vault, cfg, pid, name, [], docs, "进行中",
                                    version=version, when=today[:7], dirname=pdir.name))
        write_index(vault, cfg, rows)

    # —— 版本台账：追加一行（产出份数按实际统计，立项时无产出 = 0；立项批准留痕）——
    vrows = read_versions(vault, pid)
    vrows.append({"版本": version, "时间": today, "需求": need, "状态": "进行中",
                  "产出份数": "0", "沉淀状态": "—", "沉淀时间": "",
                  "立项批准": "创始人同意：" + approved + " · " + today})
    write_versions(vault, pid, vrows, title=name)

    # —— 交接链：按本次实际生成（含版本前缀）的文档重建；起点 = 链上第一个被派岗位 ——
    chain = _dispatch_chain(cfg, docs) if (tri and tri.get("found") and tri.get("roles")) \
        else step_chain(cfg)
    if not chain:
        raise ValueError("本次版本没能构成任何交接阶段；请检查派活清单的岗位与产出资产列。")
    start_idx = 0
    if tri and tri.get("found") and tri.get("roles"):
        for i, (_a, _b, owner) in enumerate(chain):
            if owner in roles:
                start_idx = i
                break
    write_handoff(vault, cfg, {
        "project": pid, "step": start_idx, "stage": chain[start_idx][0],
        "role": chain[start_idx][2], "doc": chain[start_idx][1],
        "status": "进行中", "token": "", "next_role": "",
        "dispatch_roles": roles if (tri and tri.get("found")) else [],
        "dispatch_chain": [[a, b, c] for a, b, c in chain]
        if (tri and tri.get("found") and tri.get("roles")) else [],
        "round": version,
        "created": today,
    })
    cfg["active_project"] = pid
    save_config(vault, cfg)
    print("已追加立项：" + name + " · " + version + "（" + need + "）")
    print("立项批准留痕：创始人同意 · " + approved + " · " + today)
    print("登记产出：" + ("、".join(d[0] for d in docs) if docs else "（无）"))
    print("交接链起点：" + chain[start_idx][1] + " · " + chain[start_idx][2])
    print("产出文件不预生成（v2.18）：各岗位开工时用 opc scaffold --role <role> 生成自己的产出骨架。")
    print("下一步：各岗按 opc done --role <role> 报完成；沉淀用 opc distill --project " + name)
    # v2.28（2026-11-20 实测修）：追加版本同样要**给第一棒发门铃**。
    # 否则 V2 迭代的第一棒又没人叫醒 —— 与 W1（opc new 不发门铃）同一个病。
    wake_next_role(vault, cfg, pid, chain[start_idx][2], chain[start_idx][1], chain[start_idx][0])


def cmd_distill(args):
    """沉淀：一个版本的产出**按岗拆开**，各职业 Agent 自己写自己的履历。

    ① `opc distill` → 生成**派发清单**（谁有实际产出 / 素材是什么 / 写到哪），
       版本状态 → 待沉淀；**不生成中间层文件**（D3/D5）。
    ② 各职业 Agent 自己概括、自己写：`opc link --version V<N> --role <role>`（D8）。
    ③ `opc distill --status` → 汇总：哪些岗已写、哪些没写 → 版本状态 → 已沉淀。
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    project = getattr(args, "project", None) or cfg.get("active_project") or ""
    if not project:
        raise ValueError("请用 --project 指定项目；或先 opc new 建项。")
    pid = resolve_project(vault, cfg, project)
    ensure_version_ledger(vault, pid)
    version = resolve_version(vault, pid, getattr(args, "version", None))
    name = project_name_of(vault, pid)
    when = project_time_of(vault, pid) or date.today().isoformat()[:7]
    assets = _version_assets(vault, cfg, pid, version)

    if getattr(args, "status", False):
        return _distill_status(vault, cfg, pid, name, when, version, assets)

    print("沉淀派发清单 · " + name + " · " + version)
    print("（只发清单，不写中间层文件 —— 各职业 Agent 自己概括、自己写自己的履历卡）")
    if not assets:
        print("该版本没有实际落盘的产出 —— 无可沉淀素材。")
        update_version_row(vault, pid, version, 沉淀状态="已沉淀", 沉淀时间=date.today().isoformat())
        print("版本沉淀状态 → 已沉淀（无素材）。")
        return
    print("")
    for role in sorted(assets.keys()):
        print("  岗位：" + role)
        for a in assets[role]:
            print("    素材：" + a["uri"])
        print("    写到：99_Agent配置/履历/" + role + ".md 的 C 节")
        print("    命令：opc link --project " + name + " --version " + version
              + " --role " + role)
    empty = [r for r in _version_roles(vault, cfg, pid, version) if r not in assets]
    if empty:
        print("")
        print("以下岗位本版本没有实际产出 → 不写履历条目：" + "、".join(empty))
    update_version_row(vault, pid, version, 沉淀状态="待沉淀",
                       产出份数=str(sum(len(v) for v in assets.values())))
    print("")
    print("版本沉淀状态 → 待沉淀。各岗写完后执行：opc distill --status --project " + name)


def _version_roles(vault, cfg, pid, version):
    """该版本**派过活**的岗位（按索引里该版本的行判定）。"""
    out = []
    for r in read_index(vault, cfg):
        if project_key(r.get("project")) != project_key(pid):
            continue
        if (r.get("版本") or "").strip() != version:
            continue
        role = (r.get("role") or "").strip()
        if role and role != "—" and role not in out:
            out.append(role)
    return out


def _distill_status(vault, cfg, pid, name, when, version, assets):
    """沉淀汇总：哪些岗写了、哪些没写；齐了就把版本标成已沉淀。"""
    print("沉淀状态 · " + name + " · " + version)
    wrote, pending = [], []
    for role in sorted(assets.keys()):
        fp = resume_path(vault, role)
        title = "### " + name + " · " + when + " · " + version
        hit = False
        if fp.exists():
            hit = title in read_text(fp)
        if hit:
            wrote.append(role)
        else:
            pending.append(role)
    empty = [r for r in _version_roles(vault, cfg, pid, version) if r not in assets]
    print("  已写履历：" + ("、".join(wrote) if wrote else "（无）"))
    print("  待写履历：" + ("、".join(pending) if pending else "（无）"))
    print("  无产出（不写履历）：" + ("、".join(empty) if empty else "（无）"))
    if pending:
        print("")
        print("还差这些岗（各岗自己写自己的，不代写）：")
        for role in pending:
            print("  opc link --project " + name + " --version " + version + " --role " + role)
        return
    update_version_row(vault, pid, version, 沉淀状态="已沉淀",
                       沉淀时间=date.today().isoformat())
    print("")
    print("版本沉淀状态 → 已沉淀。")


def _scope_roles(scope):
    """--scope 只作标签；这里返回常规参考范围，仅用于提示差异，不参与路由决策。"""
    return {
        "画面": ["design"],
        "后端": ["architect", "engineer"],
        "前端": ["design", "architect", "engineer"],
        "文案": ["pm"],
        "数据": ["pm", "architect", "engineer"],
        "全量": [],
        "迭代": [],
        "其他": [],
    }.get(scope, [])


def cmd_triage(args):
    """**pm 起草派活清单（v2.11）**：派活有固定落点、结构化、解析失败不静默。

    落点：项目目录下的 `_派活清单.json`（机器可读，路由的唯一真相源）。
    同时回写 `4_产品基线.md` 的 `## 本次涉及岗位与产出资产清单` 小节（人可读）。

    用法：
      opc triage --project P001 --role design --assets 5_设计交付.md,5_原型.html --reason "改首页三处视觉"
      opc triage --project P001 --role architect --involved no --reason "接口不动"
      opc triage --project P001 --zero --reason "本次只修文档"
      opc triage --project P001 --show
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    bindings = role_bindings_ref(cfg)
    project = getattr(args, "project", None) or cfg.get("active_project") or ""
    if not project:
        raise ValueError("请用 --project 指定项目号（如 P001）；或先 opc new 建项。")
    pdir = Path(vault) / "30_项目" / project_dir_name(vault, project)
    exists = pdir.is_dir()
    if not exists:
        # 项目还没建起来 → 清单先落待建区，等 opc new --triage 时搬进项目目录。
        print("提示：项目 " + project + " 尚未建项 → 派活清单先落待建区，"
              "opc new --triage 时会自动搬进项目目录。")

    data = read_triage_json(vault, project) or {
        "project": project, "zero": False, "reason": "", "entries": []}
    data.setdefault("entries", [])

    if getattr(args, "show", False):
        print("派活清单（" + str(triage_json_path(vault, project)) + "）：")
        if data.get("zero"):
            print("  零岗位 —— " + str(data.get("reason") or "（未写理由）"))
        else:
            for e in data.get("entries") or []:
                mark = "涉及" if e.get("involved") else "不涉及"
                assets = "、".join(e.get("assets") or []) or "—"
                print("  " + str(e.get("role")) + " | " + mark + " | " + assets
                      + " | " + str(e.get("reason") or ""))
        return

    if getattr(args, "zero", False):
        reason = str(getattr(args, "reason", "") or "")
        if not reason:
            raise ValueError("判定零岗位必须给理由：--reason \"...\"")
        data["zero"] = True
        data["reason"] = reason
        data["entries"] = []
    else:
        role = getattr(args, "role", None)
        if not role:
            raise ValueError("请用 --role 指定岗位；零岗位请加 --zero。")
        if role not in bindings:
            raise ValueError("未登记的岗位：" + role + "；可用：" + "、".join(sorted(bindings.keys())))
        verdict = getattr(args, "involved", None)
        involved = True
        if verdict is not None:
            low = str(verdict).strip().lower()
            if low in ("no", "false", "n", "否", "不涉及"):
                involved = False
            elif low in ("yes", "true", "y", "是", "涉及"):
                involved = True
            else:
                raise ValueError("--involved 只认 是/否（yes/no）；你写的是：" + str(verdict))
        raw_assets = getattr(args, "assets", "") or ""
        assets = [a.strip() for a in re.split(r"[、,，;；]+", raw_assets) if a.strip()]
        reason = str(getattr(args, "reason", "") or "")
        if involved and not reason:
            raise ValueError("派活必须写分诊理由：--reason \"...\"（事后复盘的依据）")
        data["zero"] = False
        data["reason"] = ""
        entries = [e for e in data["entries"] if e.get("role") != role]
        entries.append({"role": role, "involved": involved, "assets": assets, "reason": reason})
        data["entries"] = entries

    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    p = write_triage_json(vault, project, data)
    fp = sync_triage_markdown(vault, project, data)

    print("已写派活清单：" + str(p))
    if fp:
        print("已写人可读形态：" + str(fp))
        if fp.name == TRIAGE_FILE.replace(".json", ".md"):
            print("（4_产品基线.md 存在时，同名小节也一并回写）")
    involved_roles = [e["role"] for e in data.get("entries") or [] if e.get("involved")]
    if data.get("zero"):
        print("本次判定：零岗位 —— " + str(data.get("reason") or ""))
    else:
        print("本次派活：" + ("、".join(involved_roles) if involved_roles else "（暂无「涉及」的岗位）"))
    if exists:
        # v2.23：已建项时，**派活清单不自动改交接链**。
        # 交接链（dispatch_chain）是在建项那一刻按当时的清单生成的，
        # 之后改清单不会改链 —— 路由实际按链走（project_roles 从产出索引反推）。
        # 过去这里只印一句「下一步：opc advance」，让人以为改清单 = 改派活，
        # 结果实测：改完清单、索引与交接链仍是旧的，岗位照旧被派。
        # 现在**如实说明**并给出两条真能生效的路。
        st = read_handoff(vault, cfg, project)
        chain = project_chain(vault, cfg, project)
        chained = [owner for _i, _d, owner in chain]
        newly_involved = [r for r in involved_roles if r not in chained]
        print("注意：本项目**已建项**，交接链不会因本次清单变更而改变。")
        print("  当前交接链：" + ("、".join(chained) if chained else "（空）"))
        if newly_involved:
            print("  清单新增了「" + "、".join(newly_involved) + "」，但它们**不在当前链上** → "
                  "这些岗位不会被派活。")
        print("  要让新清单生效，两条路：")
        print("    ① 重建项目（链与索引都按新清单生成）：删除项目目录后 "
              "opc new \"<项目名>\" --triage " + str(p))
        print("    ② 已产出的棒不动、只补后续：手动改 "
              + str(handoff_state_path(vault, cfg, project))
              + " 的 dispatch_chain / dispatch_roles（属绕过，需人工确认）")
        print("下一步：opc advance --project " + project + "（按**当前链**推进，非按本次清单）")
    else:
        print("下一步：opc new \"<项目名>\" --triage " + str(p)
              + "（按这份清单建项，不再走全量 9 步）")


def match_similar(rows, tags, exclude_project=None, role=None):
    out = []
    for r in rows:
        if exclude_project and r.get("project") == exclude_project:
            continue
        if r.get("状态") == "进行中":
            continue
        if role and r.get("role") != role:
            continue
        row_tags = set(parse_tags(r.get("类型标签", "")))
        overlap = row_tags & set(tags)
        if overlap:
            out.append((len(overlap), r))
    out.sort(key=lambda x: (-x[0], x[1].get("project", "")))
    return out


def print_similar(matches, tags):
    if not matches:
        print("相似项目：未命中（记录为「首次实践」，收尾时在简历 B 节新增能力项）")
        return
    print("相似项目（按标签重合度排序；落地物地址来自产出索引）：")
    last = None
    for score, r in matches:
        pid = r.get("project", "")
        if pid != last:
            print("  " + pid + "  " + _index_row_name(r) + "  [" + r.get("类型标签", "") + "]")
            last = pid
        print("     " + r.get("role", "") + " / " + r.get("逻辑键", "") + " -> " + r.get("物理地址 URI", ""))
    print("  提醒：仅作参考与溯源；是否适用按规则层 applies_when / counter_example 判断（§15.7）。")


def cmd_similar(args):
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    tags = parse_tags(args.tags)
    if not tags:
        raise ValueError("请用 --tags 提供至少一个标签，如 --tags '#订阅制 #高频工具'")
    matches = match_similar(read_index(vault, cfg), tags, role=args.role)
    print_similar(matches, tags)


def _project_roles(vault, cfg, project, rows):
    """本项目实际涉及的岗位集合 —— 从索引行反推，不受当前 profile 影响。"""
    roles = []
    for r in rows:
        if project_key(r.get("project")) != project_key(project):
            continue
        role = r.get("role", "")
        if role and role != "—" and role not in roles:
            roles.append(role)
    return roles


def cmd_close(args):
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    rows = read_index(vault, cfg)
    # v2.20 修（实测踩坑）：read_index 的 project 字段存的是**短 ID**（P004），
    # 而创始人传的是**项目名**——按名精确匹配永远空，
    # close 假装「全部通过/已归档」实际一行都没改。两侧 key 归一后匹配。
    _prow = None
    try:
        _prow = resolve_project(vault, cfg, args.project)
    except Exception:
        _prow = None
    pkeys = {project_key(args.project)}
    if _prow:
        pkeys.add(project_key(_prow))
        pkeys.add(project_key(project_dir_name(vault, _prow)))
    mine = [r for r in rows if project_key(r.get("project")) in pkeys]
    if not mine:
        print("警告：产出索引中没有", args.project, "的行。")
    print("收尾检查:", args.project)
    todo = []
    # 零岗位项目：只核对索引与分诊理由，不要求简历
    zero = any(r.get("role") == "—" for r in mine)
    for r in mine:
        if r.get("role") == "—":
            continue
        if not r.get("物理地址 URI") or r.get("物理地址 URI", "").startswith("解析失败"):
            todo.append("索引缺 URI: " + r.get("role", "") + "/" + r.get("逻辑键", ""))
        if r.get("状态") != "已归档":
            todo.append("索引状态未置为已归档: " + r.get("role", ""))
    resume_dir = vault / "99_Agent配置/履历"
    # 只要求本项目真正参与的岗位补简历（governance / architect 等不在交接链上的，
    # 只有真的产出了资产才需要）—— 不再拿当前 profile 的职业集硬套
    for role in _project_roles(vault, cfg, args.project, rows):
        f = resume_dir / (role + ".md")
        if not f.exists():
            todo.append("缺少职业简历: " + role)
            continue
        text = read_text(f)
        if ("### " + args.project) not in text and ("关联项目：" + args.project) not in text:
            todo.append("职业简历未追加 C 节: " + role)
        if "project_refs: []" in text:
            todo.append("职业简历 frontmatter 的 project_refs 仍为空: " + role)
    if todo:
        print("待办（" + str(len(todo)) + " 项，内容由各职业 Agent 补写，本命令不代写）：")
        for t in todo:
            print("  -", t)
    else:
        if zero:
            print("全部通过：零岗位项目，索引已归档并留痕分诊理由。")
        else:
            print("全部通过：简历、索引、project_refs 均已补齐。")
    if getattr(args, "link", False):
        args.role = None
        cmd_link(args)
    else:
        print("提示：加 --link 可自动把项目出处回填到各职业简历。")
    # 归档：把本项目索引行的状态置为「已归档」（收尾检查要求的终态）
    if getattr(args, "archive", False) or getattr(args, "link", False):
        # v2.13：索引是共享单文件 → 锁内**重新读一遍**再改再写，
        # 否则并发归档会把别的会话刚写的行覆盖掉。
        with VaultLock(vault):
            rows = read_index(vault, cfg)
            changed = 0
            for r in rows:
                if project_key(r.get("project")) in pkeys and r.get("状态") != "已归档":
                    r["状态"] = "已归档"
                    changed += 1
            if changed:
                write_index(vault, cfg, rows)
        if changed:
            print("已归档：" + str(changed) + " 条索引行状态置为「已归档」。")
        elif mine:
            print("索引状态已是「已归档」，无需改动。")
    final_doc = (profile_view(cfg).get("step_docs") or ["8_复盘.md"])[-1]
    print("下一步：按 §9 在 " + final_doc + " 登记可沉淀项，由创始人确认升格。")


def resume_path(vault, role):
    return vault / "99_Agent配置/履历" / (role + ".md")


def update_project_refs(text, project):
    m = re.search(r"^project_refs:\s*\[(.*?)\]", text, re.M)
    if not m:
        return text.replace("---\n", "---\nproject_refs: [" + project + "]\n", 1)
    items = [x.strip() for x in m.group(1).split(",") if x.strip()]
    if project in items:
        return text
    items.append(project)
    return text[:m.start()] + "project_refs: [" + ", ".join(items) + "]" + text[m.end():]


def build_version_resume_entry(project_name, when, version, role, uris):
    """履历条目（ADR-0005 D7）：一句话摘要 + 三小节 + 出处。

    标题 `### 项目名 · YYYY-MM · V<N>` —— 人读处不带 P00X。
    三小段留「待补」由本职业自己补齐 —— **不代写**（D8）。
    """
    ev = "、".join("[[" + (u.split("://", 1)[-1] if "://" in u else u) + "]]" for u in uris)
    return (
        "### " + project_name + " · " + when + " · " + version + "\n"
        "> 一句话摘要：（" + role + " 在 " + version + " 做了什么 —— 一句话）\n"
        "- 怎么落地的：待补\n"
        "- 坑与解法：待补\n"
        "- 结果：待补\n"
        "- 证据：" + (ev or "（待补）") + "\n"
        "- 关联项目：" + project_name + " → [[30_项目/_产出索引#" + project_name + "]]\n"
        "- 沉淀状态：已沉淀（" + version + "）\n\n"
    )


def _link_version(vault, cfg, args, project):
    """版本粒度履历条目：各职业 Agent **自己**写自己的（ADR-0005 D8）。"""
    pid = resolve_project(vault, cfg, project)
    name = project_name_of(vault, pid)
    ensure_version_ledger(vault, pid)
    version = resolve_version(vault, pid, args.version)
    role = getattr(args, "role", None)
    if not role:
        raise ValueError("写版本履历必须指定 --role（各职业只写自己的条目，不代写）。")
    when = project_time_of(vault, pid) or date.today().isoformat()[:7]
    assets = _version_assets(vault, cfg, pid, version)
    if role not in assets:
        print("本版本没有 " + role + " 的实际产出 —— 不写履历条目。")
        return
    uris = [a["uri"] for a in assets[role]]
    fp = resume_path(vault, role)
    if not fp.exists():
        print("缺少职业简历：" + str(fp))
        return
    text = read_text(fp)
    title = "### " + name + " · " + when + " · " + version
    note = (getattr(args, "modify", "") or "").strip()
    if title in text:
        if not note:
            print("本版本履历条目已存在（跳过）。要补充请加 --修改 \"<说明>\"。")
            return
        text = text.replace(title, title + "\n- 修改于 " + date.today().isoformat()
                            + "：" + note, 1)
        write_text(fp, text)
        print("已在既有条目上留痕修改：" + role + " · " + version)
        return
    entry = build_version_resume_entry(name, when, version, role, uris)
    marker_d = "## D 相似经验速查"
    if marker_d in text:
        text = text.replace(marker_d, entry + marker_d, 1)
    else:
        text = text.rstrip() + "\n\n" + entry
    text = update_project_refs(text, pid)
    write_text(fp, text)
    with VaultLock(vault):
        rows = read_index(vault, cfg)
        changed = False
        for r in rows:
            if project_key(r.get("project")) != project_key(pid):
                continue
            if (r.get("版本") or "").strip() != version:
                continue
            if (r.get("role") or "").strip() != role:
                continue
            r["简历条目"] = title
            changed = True
        if changed:
            write_index(vault, cfg, rows)
    print("已写入履历：" + str(fp) + " → " + title)
    print("提醒：「怎么落地 / 坑与解法 / 结果」三段请本职业自己补齐（工具不代写）。")
    # v2.20（创始人指出的缺口）：B 节能力矩阵随沉淀自动填充
    try:
        update_resume_b_section(fp, role)
    except Exception as exc:
        print("B 节能力矩阵更新失败（不阻塞）：" + str(exc)[:200])
    # v2.20（创始人拍板）：沉淀写履历 → 团队档案自动同步飞书（OPC/团队档案/履历/）
    try:
        sync_team_archives(vault, cfg, args)
    except Exception as exc:
        print("团队档案同步失败（不阻塞沉淀）：" + str(exc)[:200])


def build_resume_entry(row, today, final_doc="8_复盘"):
    uri = row.get("物理地址 URI", "") or "（待登记）"
    return (
        "### " + row["project"] + " · " + _index_row_name(row) + " · " + today + "\n"
        "- 关联项目：" + row["project"] + " → [[30_项目/_产出索引#" + row["project"] + "]]\n"
        "- 本职业产出地址：" + uri + "\n"
        "- 类型标签：" + row.get("类型标签", "") + "\n"
        "- 我负责：阶段 " + row.get("阶段", "") + "（" + row.get("role", "") + "）\n"
        "- 当时约束：待补\n"
        "- 怎么落地的：待补\n"
        "- 坑与解法：待补\n"
        "- 结果：待补\n"
        "- 证据：[[" + row["project"] + "/" + final_doc + "]]、" + uri + "\n"
        "- 沉淀状态：待评估\n\n"
    )


def rebuild_speed_table(text, rows):
    marker = "## D 相似经验速查"
    if marker not in text:
        return text
    start = text.find(marker)
    nxt = text.find("\n## ", start + len(marker))
    end = nxt if nxt != -1 else len(text)
    grouped = {}
    for r in rows:
        for t in parse_tags(r.get("类型标签", "")):
            grouped.setdefault("#" + t, set()).add(r.get("project", ""))
    lines = [marker, "", "| 类型标签 | 相关项目 | 推荐先看 |", "|---|---|---|"]
    if grouped:
        for tag in sorted(grouped):
            pids = sorted(p for p in grouped[tag] if p)
            lines.append("| " + tag + " | " + ", ".join(pids) + " | [[" + pids[-1] + "]] |")
    else:
        lines.append("| （暂无） | — | — |")
    return text[:start] + "\n".join(lines) + "\n" + text[end:]


def update_resume_b_section(fp, role):
    """按 C 节条目重建 B 节能力矩阵（v2.20：沉淀时自动填充，创始人指出的缺口）。

    口径：能力项 = 条目「类型标签」（词表由 governance 维护）；首次实践 = 首个带该
    标签的项目；累计项目数 = C 节中带该标签的条目数；熟练度 = 入门(1)/熟练(2-3)/精通(≥4)；
    关联规则 = 未升格留 —。幂等：整段重算重写。
    """
    text = read_text(fp)
    entries, cur, in_c = [], None, False
    for line in text.splitlines():
        if line.startswith("## "):
            in_c = line.strip().startswith("## C")
            cur = None
            continue
        if not in_c:
            continue
        if line.startswith("### "):
            if cur:
                entries.append(cur)
            cur = {"title": line[4:].strip(), "tags": []}
        elif cur is not None and line.strip().startswith("- 类型标签："):
            cur["tags"] = [t.strip() for t in line.split("：", 1)[1].split() if t.strip()]
    if cur:
        entries.append(cur)
    stats = {}
    for e in entries:
        pn = e["title"].split(" · ")[0]
        for t in e["tags"]:
            stats.setdefault(t, [])
            if pn not in stats[t]:
                stats[t].append(pn)
    rows = []
    for tag, projs in sorted(stats.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lvl = "精通" if len(projs) >= 4 else ("熟练" if len(projs) >= 2 else "入门")
        rows.append("| %s | %s | %d | %s | — |" % (tag, projs[0], len(projs), lvl))
    m = re.search(r"(## B[^\n]*\n)(.*?)(?=\n## |\Z)", text, re.S)
    if not m:
        return
    table = ("| 能力项 | 首次实践 | 累计项目数 | 熟练度 | 关联规则 |\n"
             "|---|---|---|---|---|\n"
             + ("\n".join(rows) + "\n" if rows
                else "| （尚无项目经历） | — | 0 | — | — |\n"))
    text = text[:m.start()] + m.group(1) + table + "\n" + text[m.end():]
    write_text(fp, text)


def cmd_link(args):
    """把项目出处回填进职业简历：C 节条目 + project_refs + D 节速查表。

    v2.17：`--version V<N>` → 写**版本粒度**条目（ADR-0005 D7/D8）——
    标题 `### 项目名 · YYYY-MM · V<N>`，只写**自己**岗位的条目。
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    project = getattr(args, "project", None) or getattr(args, "project_opt", None)
    if not project:
        raise ValueError("请指定项目：opc link <项目> 或 opc link --project <项目>。")
    # v2.22（创始人规则，2026-11-21 修订）：
    # 旧规则是「项目需求未完成（交接状态 ≠ 已完成）一律不能沉淀履历」——
    # 结果是各岗产出早早就交付了，履历卡却一直空着，创始人看到的与「什么都没干」无异。
    # 新规则：**本岗在该版本有实际产出即可写自己的条目**（粒度仍由 --version 圈定）。
    # 「禁止代写」不变：--role 只能写自己，且 _link_version 内部仍校验本岗有无产出。
    _pid = resolve_project(vault, cfg, project)
    _state = read_handoff(vault, cfg, _pid)
    _st = _state.get("status") or ""
    if getattr(args, "version", None):
        return _link_version(vault, cfg, args, project)
    if _st != "已完成":
        # 非版本粒度（整项目粒度）仍要求项目收尾态——那条路没有版本边界，
        # 放行会一次写进全项目历史，不符合「一次沉淀只覆盖一个版本的增量」。
        raise ValueError(
            "项目「%s」需求未完成（当前状态：%s；阶段 %s · %s）——整项目粒度沉淀需等项目收尾。"
            "若只想沉淀本岗某个版本的产出，请加 --version <V<N>>（本岗在该版本有实际产出即可写）。"
            % (project, _st, _state.get("stage", "?"), _state.get("role", "?")))
    args.project = project
    rows = read_index(vault, cfg)
    mine = [r for r in rows if r.get("project") == args.project]
    if not mine:
        print("产出索引中没有", args.project, "；先执行 new 或检查索引。")
        return
    today = date.today().isoformat()
    final_doc = ((profile_view(cfg).get("step_docs") or ["8_复盘.md"])[-1])[:-3]
    added = 0
    for row in mine:
        role = row.get("role", "")
        if getattr(args, "role", None) and role != args.role:
            continue
        fp = resume_path(vault, role)
        if not fp.exists():
            print("  跳过（缺简历）:", role)
            continue
        text = read_text(fp)
        if re.search(r"^### " + re.escape(args.project) + r"\b", text, re.M):
            continue
        entry = build_resume_entry(row, today, final_doc)
        marker_d = "## D 相似经验速查"
        if marker_d in text:
            text = text.replace(marker_d, entry + marker_d, 1)
        else:
            text = text.rstrip() + "\n\n" + entry
        text = update_project_refs(text, args.project)
        text = rebuild_speed_table(text, rows)
        write_text(fp, text)
        added += 1
    print("出处回填完成：新增", added, "条简历条目（已存在的跳过）")
    print("核验: python scripts/opc.py resume <role> --vault <vault>  /  trace", args.project, "--vault <vault>")


def cmd_resume(args):
    """列出某职业简历里的全部项目经验及其出处。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    rows = read_index(vault, cfg)
    fp = resume_path(vault, args.role)
    if not fp.exists():
        print("缺少职业简历:", args.role)
        return
    text = read_text(fp)
    # v2.17：条目标题两种形态都能认 ——
    #   旧：`### P001 · 项目名 · 日期`；新：`### 项目名 · YYYY-MM · V<N>`（人读不带 P00X）
    entries = re.findall(r"^### ([^\n]+)\n((?:> [^\n]*\n)?(?:- [^\n]*\n)*)", text, re.M)
    print("职业简历:", args.role, "|", len(entries), "份项目经验")
    by_key = {}
    by_name = {}
    for r in rows:
        by_key.setdefault((r.get("project", ""), r.get("role", "")), []).append(r)
        by_name.setdefault(_index_row_name(r), []).append(r)
    for title, body in entries:
        m_pid = re.match(r"^(P\d{3})\b", title)
        pid = m_pid.group(1) if m_pid else ""
        if not pid:
            for r in by_name.get(title, []):
                pid = r.get("project", "")
                break
        m_uri = re.search(r"^- 本职业产出地址：(.+)$", body, re.M)
        if not m_uri:
            m_uri = re.search(r"^- 证据：(.+)$", body, re.M)
        m_tag = re.search(r"^- 类型标签：(.+)$", body, re.M)
        print("  " + title + "  " + (m_tag.group(1).strip() if m_tag else ""))
        print("     产出地址:", m_uri.group(1).strip() if m_uri else "（未登记）")
        for r in by_key.get((pid, args.role), []):
            print("     索引:", r.get("逻辑键", ""), "|", r.get("状态", ""), "| 简历条目", r.get("简历条目", ""))


def cmd_trace(args):
    """按项目反查所有职业的产出地址与简历条目。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    try:
        pid = resolve_project(vault, cfg, args.project)
    except Exception:
        pid = args.project
    mine = [r for r in read_index(vault, cfg)
            if project_key(r.get("project")) == project_key(pid)]
    print("项目", project_name_of(vault, pid) or args.project, "（" + str(pid) + "）共",
          len(mine), "条职业产出：")
    for r in mine:
        print("  " + r.get("role", "") + " / " + r.get("逻辑键", "")
              + (" / " + r.get("版本", "") if r.get("版本") else ""))
        print("     URI:", r.get("物理地址 URI", ""))
        print("     简历:", r.get("简历条目", ""), "| 状态:", r.get("状态", ""))


def load_jobcard(vault, role):
    fp = vault / "99_Agent配置/岗位卡" / (role + ".md")
    if fp.exists():
        return read_text(fp)
    fp2 = ASSETS / "岗位卡" / (role + ".md")
    return read_text(fp2) if fp2.exists() else ""


def jobcard_scope(card):
    m1 = re.search(r"^- 只负责：(.+)$", card, re.M)
    m2 = re.search(r"^- 明确不负责：(.+)$", card, re.M)
    return (m1.group(1).strip() if m1 else "（见岗位卡 §2）"), (m2.group(1).strip() if m2 else "（见岗位卡 §2）")


def bot_system_prompt(role, version):
    return (
        "你是 OPC 一人公司的 " + role + " Agent。开工前必须：\n"
        "1. 读取 /OPC-架构规范.md（" + version + "）全文并报告版本号。\n"
        "2. 读取 /99_Agent配置/岗位卡/" + role + ".md；本次会话只绑定这一个 role。\n"
        "3. 读取 /99_Agent配置/履历/" + role + ".md 与 /30_项目/_产出索引.md，按 §15.7 检索相似经历（只作参考与溯源）。\n"
        "4. 开工前报告：role / 所属阶段 / 当前 project / 规范版本 / 本次输入来自哪个文件。\n"
        "5. 写文件前按 §3 定唯一落点、按 §14 自检；产出地址用 opc resolve 解析，禁止写死外部路径。\n"
        "6. 收到不属于本岗位的任务 → 拒绝执行并指明应由哪个 role 处理；禁止一次会话兼任两个职业。\n"
        "7. 收尾按 §15.6 追加本岗职业简历，并登记 /30_项目/_产出索引.md。\n"
        "8. 产出完成后执行 opc done --role " + role + " 报完成并通知创始人；附带资料用 --file 登记，不限文件格式。禁止自行通知、@ 或唤醒下游 Agent（§15.9）。\n"
        "9. 宪法要点以本提示词为投影，需要裁决时按章节号回查 /OPC-架构规范.md，不必全文常驻上下文；只读自己的职业简历且仅作参考，禁止读其他 Agent 简历、禁止与其他 Agent 对话内传递。\n"
        "10. 不确定就停下来问创始人，禁止猜测；执行只认规则层 status=生效。"
    )


def cmd_bots(args):
    """导出各职业 Bot 配置卡（按当前 profile 的职业集合）；不创建任何 Bot 实例。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    bindings = role_bindings_ref(cfg)
    version = schema_version(cfg)
    out = Path(getattr(args, "out", None) or "99_Agent配置/Bot配置卡")
    if not out.is_absolute():
        out = vault / out
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for role, binding in bindings.items():
        card = load_jobcard(vault, role)
        arts = list((binding.get("artifacts") or {}).keys())
        try:
            uri = resolve_uri(cfg, vault, role, arts[0], "PXXX")[1] if arts else "（未配置产出键）"
        except Exception:
            uri = "（由 opc.config.json 解析）"
        content = (
            "# Bot 配置卡 · " + role + "\n\n"
            "> 由 `opc bots` 生成。**本卡不创建 Bot**；实例、模型、工具权限、知识库挂载方式由宿主产品决定。\n"
            "> 把「一、系统提示词」复制到宿主的提示词 / 人设栏，再按「二」挂载文件即可。\n\n"
            "## 一、系统提示词（可直接粘贴）\n\n```\n" + bot_system_prompt(role, version) + "\n```\n\n"
            "## 二、需要挂载的文件 / 目录\n\n"
            "- `/OPC-架构规范.md`\n"
            "- `/99_Agent配置/岗位卡/" + role + ".md`（完整职业定义，见下方附录）\n"
            "- `/99_Agent配置/履历/" + role + ".md`\n"
            "- `/20_知识资产/<domain>/`（本岗专属知识库）\n"
            "- `/30_项目/_产出索引.md`\n"
            "- `/30_项目/PXXX/`（仅当前项目）\n\n"
            "## 三、主产出地址（默认后端）\n\n- `" + uri + "`\n\n"
            "## 四、岗位卡全文（附录）\n\n" + (card or "（未找到岗位卡，请先执行 init）") + "\n\n"
            "## 五、Profile 宿主适配（独立身份 · Hermes / openclaw 类）\n\n"
            "> 适用运行模式 `multi-profile`：一职业 = 一个独立持久身份（Profile）。\n"
            "> 三个硬适配点（手册 §21）+ 两条纪律：\n\n"
            "1. **SOUL.md** ← 把「一、系统提示词」整段贴进该 Profile 的 SOUL.md（不裁剪）。\n"
            "2. **workspace 必须指向同一个 Vault**（硬前提，否则交接链断）：\n\n"
            "   ```\n   " + str(vault) + "\n   ```\n\n"
            "   Profile 的 `workspace/` 通常完全隔离 —— 用挂载 / 软链 / 共享路径指向**同一份** Vault；\n"
            "   否则各 Profile 各写各的目录，谁也看不到谁的产出，§15.3 的文件交接直接断掉。\n"
            "3. **别造第二份真相**：Profile 自带的 `MEMORY.md` 是它自己的；职业履历仍以\n"
            "   `99_Agent配置/履历/" + role + ".md` 为准（宪法 §1 原则 4），避免两处记忆漂移。\n"
            "4. **看板 ≠ 闸门**：宿主 Kanban 可做任务分派与状态可视化，但阶段 0 / 2 / 7 的拍板\n"
            "   仍归创始人；交接完成判定仍以文件落盘 + `opc done` 回执为准。\n"
            "5. **领活靠 pull，不靠唤醒**：Agent 空闲时自己查 `opc queue --role <role>` 领活，\n"
            "   **禁止**互相 @ 唤醒（宪法 §15.3）。\n"
        )
        write_text(out / ("Bot配置卡_" + role + ".md"), content)
        n += 1
    print("已导出", n, "份 Bot 配置卡 ->", out)
    print("下一步：在宿主产品中手动创建 Bot，各贴一份「系统提示词」；宿主不支持多 Bot 时改用 `opc role <role>` 单 Bot 模式。")
    print("独立身份宿主（Hermes / openclaw 类 Profile）：每份卡第五节给出 SOUL.md / 共享 workspace / 记忆归属的适配指引。")


# ---------------------------------------------------------------------------
# §15.9 交接通知与人工步进：一阶段一通知、下游由 step_docs 推导、禁止跨步
# ---------------------------------------------------------------------------

def notify_conf(cfg):
    return cfg.get("notify") or {
        "enabled": False, "channel": "local", "channels": {},
        "state_path": "99_Agent配置/_交接状态.json", "gates": [0, 2, 7],
    }


def project_key(project):
    """项目键归一化：`P001` 与 `P001_项目a` 指向**同一份**交接状态。

    v2.12 修正：状态文件按**项目号**存（`_交接状态/P001.json`），而项目**目录名**
    带后缀（`P001_项目a`）。此前 `opc pm` / `opc queue` 用目录名去读状态 → 读不到
    → 闸门标记、待确认状态、已推送标记**全部读成空**（实测真实 Vault：
    `99_Agent配置/_交接状态/P001.json` 存在，但台账读的是 `P001_项目a.json`）。

    v2.17：目录名不再带 `P001_` 前缀；v2.19 起目录名 = **项目名**（不带年月），
    **从名字反推不出项目号** —— 因此一切标识先经 `resolve_project()` 归一成短 ID，
    本函数只负责「已经是短 ID 或带前缀的老全名」这两种可解析的形态。
    """
    m = re.match(r"(P\d{3})", str(project or ""))
    return m.group(1) if m else str(project or "")


def resolve_project(vault, cfg, ident):
    """把任何项目标识归一成**短 ID**（`P001` 形式）。

    接受：短 ID（`P001`）、老全名（`P001_项目a`）、新目录名（`项目名` / `项目名-2`）、
    项目显示名（`项目名`）。短 ID 是状态文件名与逻辑键的唯一主键。
    """
    s = str(ident or "").strip()
    if not s:
        return s
    m = re.match(r"^(P\d{3})", s)
    if m:
        return m.group(1)
    try:
        rows = read_index(vault, cfg)
    except Exception:
        return s
    if not rows:
        return s
    for r in rows:
        if r.get("project") == s:
            return project_key(s)
    for r in rows:
        try:
            if project_dir_name(vault, r.get("project")) == s:
                return project_key(r.get("project"))
        except Exception:
            continue
    hit = None
    for r in rows:
        if _index_row_name(r) == s:
            if hit is None or (r.get("状态") or "") == "进行中":
                hit = r.get("project")
    return project_key(hit) if hit else s


def handoff_state_path(vault, cfg, project=None):
    legacy = Path(vault) / notify_conf(cfg).get("state_path", "99_Agent配置/_交接状态.json")
    if not project:
        return legacy
    return legacy.parent / "_交接状态" / (project_key(project) + ".json")


def read_handoff(vault, cfg, project=None):
    """按项目读状态；project 为空时读「最近活跃」镜像（兼容旧 Vault）。"""
    if project:
        pid = resolve_project(vault, cfg, project)
        p = handoff_state_path(vault, cfg, pid)
        if p.exists():
            try:
                return json.loads(read_text(p))
            except (ValueError, OSError):
                return {}
        legacy = handoff_state_path(vault, cfg)
        if legacy.exists():
            try:
                d = json.loads(read_text(legacy))
                return d if project_key(d.get("project")) == pid else {}
            except (ValueError, OSError):
                return {}
        return {}
    p = handoff_state_path(vault, cfg)
    if not p.exists():
        return {}
    try:
        return json.loads(read_text(p))
    except (ValueError, OSError):
        return {}


def archive_decision(state):
    """把「本阶段」的拍板结论**归档进历史，并清空顶层字段**（v2.14）。

    为什么要清：拍板字段（`decision` / `decided_by` / `decided_at` /
    `decided_via` / `decision_basis`）是**某一个闸门**的结论。跨阶段残留会让
    下一个闸门看着像「已经拍过板」—— 自动化一旦只判「`decision` 在不在」，
    就会绕过「拍板权始终在创始人」（§23.4）。历史不丢：进 `decisions` 列表。
    """
    if not state.get("decision"):
        return
    hist = state.get("decisions")
    if not isinstance(hist, list):
        hist = []
    hist.append({
        "stage": state.get("stage"),
        "role": state.get("role"),
        "decision": state.get("decision"),
        "decided_by": state.get("decided_by"),
        "decided_at": state.get("decided_at"),
        "decided_via": state.get("decided_via"),
        "decision_basis": state.get("decision_basis"),
    })
    state["decisions"] = hist
    for k in ("decision", "decided_by", "decided_at", "decided_via", "decision_basis"):
        state.pop(k, None)


def write_handoff(vault, cfg, state):
    """每项目一份状态（互不覆盖），并同步一份「最近活跃」镜像供展示。

    v2.13 修正：状态文件里的 `project` 统一存**项目全名**（`P001_甲项目`）。
    此前有的路径写项目号、有的写全名，同一项目出现两种形态 → 比较点
    （如 done 的「项目不一致」校验）会误判。现在**写入即归一**，比较侧一律
    用 `project_key()`，项目号与全名互认。

    v2.19（ADR-0007）：目录名 = 项目名（不带 P00X、不带年月），从目录名反推不出
    项目号 → 状态里的 `project` 与状态**文件名**统一存**短 ID**（`P001`），
    目录名由 `project_dir_name()` 按索引还原。比较侧仍用 `project_key()`。
    """
    proj = state.get("project")
    if proj:
        state["project"] = resolve_project(vault, cfg, proj)
        proj = state["project"]
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    if proj:
        write_text(handoff_state_path(vault, cfg, proj), payload)
    write_text(handoff_state_path(vault, cfg), payload)


def active_project_of(args, cfg, state=None, vault=None):
    """取当前项目标识 → **短 ID**（`P001`）。

    历史问题：--project 传项目号（P001）与状态里记的全名（P001_xxx）不一致，
    导致 done 报「项目不一致」；而骨架 frontmatter 写的是项目号，
    done 校验却要求全名 —— 照骨架填完反而报不了完成。两处一并修。

    v2.17：目录名不再带项目号前缀 → 任何形态的标识先归一成短 ID，
    需要目录名时再由 `project_dir_name()` 还原。

    **v2.26（多项目防错闸）**：原来只取 `cfg.active_project`（**全局单值**）。
    多 Bot 私聊场景下这是**危险**的：创始人在 A 项目的私聊里回「继续」，
    若全局 active_project 指向 B，就会推进错项目（实测：P001 私聊里的「继续」
    推进了 P002，还顺手给创始人发了错误项目的派活私聊）。
    现在：**Vault 里存在多个项目且未显式指定 `--project` 时直接报错**，
    宁可停下来问，也不猜。
    """
    explicit = getattr(args, "project", None)
    p = explicit or cfg.get("active_project") or (state or {}).get("project") or ""
    if not p:
        raise ValueError("未确定当前项目：先执行 opc new，或 opc handoff --project PXXX；之后无需再声明项目")
    if vault:
        p = resolve_project(vault, cfg, p)
    if not explicit and vault:
        # v2.26：多项目时不得靠全局单值猜 —— 必须显式指定。
        try:
            cand = _pending_projects(vault, cfg)
        except Exception:
            cand = []
        if len(cand) > 1:
            raise ValueError(
                "Vault 里有多个项目处于待确认（" + "、".join(cand) + "）；"
                "为避免推进错项目，请显式指定：opc go --project <项目号>。"
                "（私聊场景：角色应带自己那一棒的项目号执行）")
    return p


def _pending_projects(vault, cfg):
    """列出所有「待确认」的项目短 ID（多项目防错闸用）。"""
    out = []
    try:
        base = Path(vault) / "99_Agent配置" / "_交接状态"
        if base.exists():
            for f in sorted(base.glob("*.json")):
                try:
                    d = json.loads(read_text(f))
                except (ValueError, OSError):
                    continue
                if d.get("status") == "待确认":
                    pid = project_key(d.get("project") or f.stem)
                    if pid and pid not in out:
                        out.append(pid)
    except Exception:
        pass
    return out


def step_chain(cfg):
    """[(index, doc, owner)]，顺序即唯一合法的交接顺序。"""
    prof = profile_view(cfg)
    owners = step_owner_map(prof)
    return [(i, d, owners.get(d, ("", ""))[0]) for i, d in enumerate(prof.get("step_docs") or [])]


def project_roles(vault, cfg, project):
    """本项目本次派活的岗位集合。

    优先读交接状态里记的 dispatch_roles（建项时按 pm 派活清单写入）；
    没有就回退到产出索引里出现过的 role。空集合表示「未限定」——
    此时交接链按 step_docs 完整走。
    """
    state = read_handoff(vault, cfg, project)
    roles = state.get("dispatch_roles") or []
    if roles:
        return list(roles)
    out = []
    for r in read_index(vault, cfg):
        if project_key(r.get("project")) != project_key(project):
            continue
        role = r.get("role", "")
        if role and role != "—" and role not in out:
            out.append(role)
    return out


def next_dispatch_index(chain, roles, cur):
    """从 cur 往后找下一个「本次要干的岗位」。

    roles 为空 → 不限定，按 step_docs 顺序走。
    roles 非空 → 跳过本次没派活的岗位（§15.14 需求不涉及就不产出、不执行）。
    """
    if not roles:
        return cur + 1 if cur + 1 < len(chain) else -1
    allowed = set(roles)
    for j in range(cur + 1, len(chain)):
        if chain[j][2] in allowed:
            return j
    return -1


def short_commit(vault):
    try:
        r = subprocess.run(["git", "-C", str(vault), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10,
                           encoding="utf-8", errors="replace")
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def push_notice(vault, cfg, channel_name, notice, project, role):
    """local 落盘永远执行；exec 通道把通知文本交给用户填写的 CLI / webhook。"""
    conf = notify_conf(cfg)
    local_file = Path(vault) / (((conf.get("channels") or {}).get("local") or {}).get("file")
                                or "99_Agent配置/_交接通知.md")
    old = read_text(local_file) if local_file.exists() else "# OPC 交接通知\n"
    write_text(local_file, old.rstrip() + "\n\n" + notice + "\n")
    rel = str(local_file.relative_to(vault))
    if channel_name == "local" or not conf.get("enabled", True):
        return "local", "已写入 " + rel
    ch = (conf.get("channels") or {}).get(channel_name) or {}
    if ch.get("type") != "exec":
        return "local", "未登记的通道 " + channel_name + "，已降级为 local"
    tmp = Path(vault) / "99_Agent配置/_交接通知_待发.md"
    write_text(tmp, notice)
    uri, msg = ExecBackend("notify", ch, vault).push(
        str(tmp), "handoff/" + project, project=project, role=role, key="handoff")
    if uri:
        return channel_name, "已推送：" + msg
    return "local", str(msg) + "；已降级写入 " + rel


# ---------------------------------------------------------------------------
# §15.10 资产快照：一次交接 = 一批资产的一个版本；快照记 commit，代码不强制随动
# ---------------------------------------------------------------------------

ASSET_SNAPSHOT_HEAD = """# %s 资产快照

> 唯一台账，由 `opc done` 自动生成，**禁止**手工编辑（§1 原则 4 单一真相源）。
> 每次交接生成一个批次：项目 / 阶段 / 职业 / 时间 / 资产清单 / 版本 / SHA256 / 当时的 commit。
> 资产版本独立自增，**不要求**代码随之迭代；commit message 建议带批次号形成双向指针。
> 二进制与附件只登记指纹与路径，实体留在 Vault 或已绑定的外部后端，不进 Git 索引。
> 按职业的视图用 `opc assets --role <role>` 查询，不另建台账。

"""


def asset_snapshot_path(vault, project):
    return Path(vault) / "30_项目" / project_dir_name(vault, project) / "_资产快照.md"


def read_asset_batches(vault, project):
    p = asset_snapshot_path(vault, project)
    if not p.exists():
        return []
    out = []
    for block in re.findall(r"```json\s*(.*?)```", read_text(p), re.S):
        try:
            out.append(json.loads(block))
        except ValueError:
            continue
    return out


def file_fingerprint(fp):
    h = hashlib.sha256()
    with open(str(fp), "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest(), fp.stat().st_size


def declared_version(fp):
    try:
        with io.open(str(fp), encoding="utf-8") as f:
            head = f.read(2048)
        if head.startswith("---"):
            m = re.search(r"^version:\s*(\S+)\s*$", head, re.M)
            if m:
                return m.group(1)
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def build_snapshot(vault, project, stage, role, main_doc, extras, commit, upstream):
    """生成资产快照。`main_doc` 是主产出的**绝对路径**（v2.18 起可能带版本目录）。"""
    batches = read_asset_batches(vault, project)
    batch_id = "A-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2).upper()
    assets = []
    for raw in [str(main_doc)] + [str(x) for x in extras]:
        fp = Path(raw)
        if not fp.is_absolute():
            fp = Path(vault) / raw
        if not fp.exists():
            continue
        try:
            rel = str(fp.relative_to(vault)).replace("\\", "/")
        except ValueError:
            rel = str(fp)
        sha, size = file_fingerprint(fp)
        prior = sum(1 for b in batches for a in b.get("assets", []) if a.get("path") == rel)
        assets.append({
            "path": rel,
            "format": fp.suffix.lstrip(".").lower() or "—",
            "version": declared_version(fp) or ("v1." + str(prior)),
            "time": datetime.fromtimestamp(fp.stat().st_mtime).isoformat(timespec="seconds"),
            "sha256": sha,
            "bytes": size,
        })
    return {
        "batch": batch_id, "project": project, "stage": stage, "role": role,
        "time": datetime.now().isoformat(timespec="seconds"),
        "commit": commit or "（未启用 Git）", "upstream": upstream, "assets": assets,
    }


def append_snapshot(vault, project, batch):
    p = asset_snapshot_path(vault, project)
    head = read_text(p) if p.exists() else ASSET_SNAPSHOT_HEAD % project
    names = "、".join(a["path"].split("/")[-1] for a in batch["assets"]) or "（无）"
    body = "\n".join([
        "## " + batch["batch"] + " · 阶段 " + str(batch["stage"]) + " · " + batch["role"],
        "",
        "- 时间：" + batch["time"] + " ｜ commit：`" + batch["commit"] + "` ｜ 上游：" + str(batch["upstream"]),
        "- 资产 " + str(len(batch["assets"])) + " 份：" + names,
        "",
        "```json",
        json.dumps(batch, ensure_ascii=False, indent=2),
        "```",
        "",
    ])
    write_text(p, head.rstrip() + "\n\n" + body)


# ---------------------------------------------------------------------------
# §15.11 工具治理：注册表全局只读、项目层每项目一份、升格须创始人批准
# ---------------------------------------------------------------------------

def tools_conf(cfg):
    return cfg.get("tools") or {
        "registry_path": "99_Agent配置/工具注册表.md",
        "project_config_name": "_工具配置.md",
        "registry_version": "v1.7",
        "require_approval_for_registry": True,
    }


def registry_path(vault, cfg):
    return Path(vault) / tools_conf(cfg).get("registry_path", "99_Agent配置/工具注册表.md")


def read_registry(vault, cfg):
    p = registry_path(vault, cfg)
    empty = {"registry_version": tools_conf(cfg).get("registry_version", "v1.7"), "tools": []}
    if not p.exists():
        return empty
    m = re.search(r"```json\s*(.*?)```", read_text(p), re.S)
    if not m:
        return empty
    try:
        return json.loads(m.group(1))
    except ValueError:
        return empty


def write_registry(vault, cfg, reg):
    p = registry_path(vault, cfg)
    block = "```json\n" + json.dumps(reg, ensure_ascii=False, indent=2) + "\n```"
    if p.exists():
        t = read_text(p)
        if re.search(r"```json\s*.*?```", t, re.S):
            t = re.sub(r"```json\s*.*?```", lambda _m: block, t, count=1, flags=re.S)
        else:
            t = t.rstrip() + "\n\n## 工具条目（机器可读，唯一真相源）\n\n" + block + "\n"
    else:
        t = "# 工具注册表\n\n" + block + "\n"
    write_text(p, t)


def project_tools_path(vault, cfg, project):
    return (Path(vault) / "30_项目" / project_dir_name(vault, project)
            / tools_conf(cfg).get("project_config_name", "_工具配置.md"))


def init_project_tools(vault, cfg, project):
    """建项时把注册表预置快照进项目层，并钉扎注册表版本。"""
    p = project_tools_path(vault, cfg, project)
    if p.exists():
        return
    reg = read_registry(vault, cfg)
    version = reg.get("registry_version", tools_conf(cfg).get("registry_version", "v1.7"))
    presets = [t for t in reg.get("tools", []) if t.get("preset") and t.get("status") == "生效"]
    rows = "\n".join(
        "| " + " | ".join([
            str(t.get("id", "")), str(t.get("name", "")), str(t.get("type", "")),
            str(t.get("capability", "")), "/".join(t.get("roles", [])),
            ", ".join(t.get("credential_env", [])) or "—",
        ]) + " |" for t in presets
    ) or "| — | 暂无已核实预置 | — | — | — | — |"
    text = "\n".join([
        "# " + project + " 工具配置",
        "",
        "> 项目级覆盖清单（§15.11）。**每项目一份，只增不改**，随项目一起进 `50_归档/`。",
        "> 注册表快照：`" + str(version) + "`，已写入 `0_约束卡.md` 的 `toolset_snapshot` / `pinned_refs`；",
        "> 在跑项目不受全局注册表升级影响，解钉须先做 §11 变更影响评估。",
        "> Agent 只能往下面「本项目临时工具」「登记提议」两区追加，",
        "> **禁止**修改总规范 / 岗位卡 / 工具注册表（§8 scope 只读）。",
        "> 凭证只写环境变量名；API Key 值放宿主环境变量或密钥管理，MCP server 配置在宿主，不进 Vault。",
        "",
        "## 已启用的预置工具",
        "",
        "| tool-id | 名称 | 类型 | 能力槽位 | 适用职业 | 凭证环境变量名 |",
        "|---|---|---|---|---|---|",
        rows,
        "",
        "## 本项目临时工具（Agent 可追加，只增不改）",
        "",
        "| tool-id | 名称 | 类型 | 能力槽位 | 使用职业 | 阶段 | 凭证环境变量名 | 状态 | 证据 |",
        "|---|---|---|---|---|---|---|---|---|",
        "",
        "## 登记提议（想进全局注册表时追加）",
        "",
        "| tool-id | 理由 | 印证项目 | 提请人 | 状态 |",
        "|---|---|---|---|---|",
        "",
    ])
    write_text(p, text + "\n")
    # 把注册表快照与规则钉扎写进 0_约束卡.md（宪法 §10：pinned_refs 必填）。
    # v2.18：约束卡不预生成（ADR-0006），这里文件不存在时跳过；
    # 由 `opc scaffold --role strategist` 生成约束卡后调用 `_ensure_rule_pinning` 补写。
    _ensure_rule_pinning(vault, cfg, project, version)


def _ensure_rule_pinning(vault, cfg, project, toolset_version=None):
    """幂等补写 `0_约束卡.md` 的 `toolset_snapshot` / `pinned_refs`（宪法 §10）。

    v2.18：约束卡由 scaffold 按需生成，生成后立即补钉扎（ADR-0006）。
    v2.20：约束卡落 `<版本目录>/strategist/`；多版本各有约束卡时逐份补齐，
    存量项目根的平铺副本也一并兼容（find_project_docs 兜底）。
    """
    pdir = Path(vault) / "30_项目" / project_dir_name(vault, project)
    pins = find_project_docs(pdir, "0_约束卡.md")
    if not pins:
        return
    if toolset_version is None:
        reg = read_registry(vault, cfg)
        toolset_version = reg.get("registry_version",
                                  tools_conf(cfg).get("registry_version", "v1.7"))
    for pin in pins:
        pt = read_text(pin)
        if "toolset_snapshot:" not in pt:
            pt = pt.replace("project_refs: []",
                            "project_refs: []\ntoolset_snapshot: " + str(toolset_version), 1)
        if "pinned_refs:" not in pt:
            pt = pt.replace("project_refs: []",
                            "project_refs: []\npinned_refs: [[OPC-架构规范@"
                            + str(constitution_version(vault, cfg)) + "]]", 1)
        write_text(pin, pt)


def append_project_tool(vault, cfg, project, row):
    p = project_tools_path(vault, cfg, project)
    t = read_text(p) if p.exists() else ""
    line = "| " + " | ".join(row) + " |"
    marker = "## 登记提议"
    if marker in t:
        idx = t.index(marker)
        t = t[:idx].rstrip() + "\n" + line + "\n\n" + t[idx:]
    else:
        t = t.rstrip() + "\n\n" + line + "\n"
    write_text(p, t)


def cmd_tools(args):
    """查看 / 项目内登记 / 提议 / 创始人批准后写入注册表。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    state = read_handoff(vault, cfg)
    project = active_project_of(args, cfg, state, vault)
    action = getattr(args, "action", "view") or "view"
    tconf = tools_conf(cfg)
    reg = read_registry(vault, cfg)
    ptp = project_tools_path(vault, cfg, project)
    if not ptp.exists():
        init_project_tools(vault, cfg, project)
    if action == "view":
        role = getattr(args, "role", None)
        print("项目 " + project + " · 注册表 " + str(reg.get("registry_version"))
              + " · 已登记工具 " + str(len(reg.get("tools", []))) + " 项")
        print("项目层：" + str(ptp.relative_to(vault)) + "（只增不改，随项目归档）")
        presets = [t for t in reg.get("tools", []) if t.get("preset") and t.get("status") == "生效"]
        if role:
            presets = [t for t in presets if role in t.get("roles", [])]
        print("\n已生效预置：" + ("、".join(str(t.get("id")) for t in presets) if presets else "（暂无，待联网核实后填入）"))
        # —— 可用性提示：按工具类型分组（mcp / oss / api / skill / office / saas）——
        if presets:
            print("\n可用性检测（按类型粗判，仅作提示；未命中 ≠ 未装）:")
            for t in presets:
                tid = str(t.get("id"))
                ttype = str(t.get("type") or "").lower()
                creds = t.get("credential_env") or t.get("env") or []
                if isinstance(creds, str):
                    creds = [creds]
                creds = [c for c in creds if c]
                env_ok = all(os.environ.get(c) for c in creds) if creds else True
                if t.get("bundled"):
                    mark = "✅ 随包预置（无需联网/无需 GitHub）"
                    hint = "→ 包内 " + str(t.get("bundled_path") or "skills/") + "；放到当前 Agent 约定的 Skill 目录（<skills 根>/<包名>/SKILL.md）即可加载，未加载按 §15.12 降级，不阻塞"
                elif ttype == "mcp":
                    mark = "ℹ️ MCP（需宿主连接器接入）"
                    hint = "→ 已接入直接用；未接入按 §15.12 降级，不阻塞"
                elif ttype == "skill":
                    mark = "ℹ️ Skill（需宿主已安装）"
                    hint = "→ 已安装直接用；未安装降级为手动，不阻塞"
                elif ttype == "office":
                    mark = "ℹ️ Office/WPS（需本机安装）"
                    hint = "→ 本机可用则直接用；不可用降级为 Markdown，不阻塞"
                elif ttype == "saas":
                    mark = "ℹ️ 在线服务（需账号登录）"
                    hint = "→ 已登录直接用；未登录降级为截图/人工，不阻塞"
                elif creds and not env_ok:
                    mark = "⚠️ 凭证未设置"
                    hint = "（环境变量 " + ", ".join(creds) + " 未设置 → 按 §15.12 降级，不阻塞）"
                else:
                    mark = "✅ 本地可用"
                    hint = ""
                print("  " + mark + " " + tid + " " + hint)
            print("提示：MCP / Skill / Office / 在线服务的接入状态以宿主为准，无法在脚本内判定；未命中的一律按 §15.12 降级为最低可用等价方式，不阻塞流程。当前会话可直接用自然语言问『我这个岗位有哪些工具可用』，Agent 会现场判断各类型可用性。")
        body = read_text(ptp) if ptp.exists() else ""
        seg = body.split("## 本项目临时工具", 1)[-1].split("## 登记提议", 1)[0]
        temp = [ln.strip() for ln in seg.split("\n")
                if ln.strip().startswith("|")
                and not ln.strip().startswith("|---")
                and not ln.strip().startswith("| tool-id")]
        print("本项目临时工具：" + (str(len(temp)) + " 项" if temp else "（无）"))
        for ln in temp:
            print("   " + ln)
        print("\n提示：项目内临时工具不进注册表；想复用请 opc tools propose，经创始人 opc tools promote --approve 才登记。")
        return
    if action == "add":
        tid = getattr(args, "id", None)
        if not tid:
            raise ValueError("登记临时工具需要 --id")
        envs = getattr(args, "env", None) or []
        row = [tid, getattr(args, "name", None) or tid, getattr(args, "type", None) or "mcp",
               getattr(args, "slot", None) or "—", getattr(args, "role", None) or "—",
               getattr(args, "stage", None) or "—", ", ".join(envs) or "—", "临时", project]
        append_project_tool(vault, cfg, project, row)
        print("已登记到 " + str(ptp.relative_to(vault)) + "（仅本项目生效）。")
        print("注册表未被修改；凭证环境变量名：" + (", ".join(envs) or "无") + "（值不写入任何文件）。")
        return
    if action == "propose":
        tid = getattr(args, "id", None)
        if not tid:
            raise ValueError("提议需要 --id")
        out = Path(vault) / "00_收件箱/agent-output/tools"
        out.mkdir(parents=True, exist_ok=True)
        body = "\n".join([
            "# 工具登记提议 · " + tid, "",
            "- 项目：" + project,
            "- 名称：" + str(getattr(args, "name", None) or tid),
            "- 类型：" + str(getattr(args, "type", None) or "mcp"),
            "- 能力槽位：" + str(getattr(args, "slot", None) or "—"),
            "- 适用职业：" + str(getattr(args, "role", None) or "—"),
            "- 凭证环境变量名：" + (", ".join(getattr(args, "env", None) or []) or "无"),
            "- 理由：" + str(getattr(args, "reason", None) or "（待补）"),
            "- 印证项目：" + project + "（升格为预置需 ≥2 项目印证 + §9 四判据）",
            "- 状态：待创始人确认", "",
            "> 创始人执行 `opc tools promote --id " + tid + " --approve` 后写入全局注册表，",
            "> 默认 `status: 候选`、`preset: false`；凭证值一律不进 Vault。", "",
        ])
        fp = out / (re.sub(r"[^\w.-]", "_", tid) + ".md")
        write_text(fp, body)
        print("提议已生成：" + str(fp.relative_to(vault)) + "；注册表未被修改。")
        return
    if action == "promote":
        if not getattr(args, "approve", False) and tconf.get("require_approval_for_registry", True):
            raise ValueError("写入全局注册表必须创始人显式 --approve；Agent 无此权限（§15.11）")
        tid = getattr(args, "id", None)
        if not tid:
            raise ValueError("需要 --id")
        if any(t.get("id") == tid for t in reg.get("tools", [])):
            raise ValueError(tid + " 已在注册表中；变更请走版本与废弃流程，禁止覆盖")
        reg.setdefault("tools", []).append({
            "id": tid,
            "name": getattr(args, "name", None) or tid,
            "type": getattr(args, "type", None) or "mcp",
            "capability": getattr(args, "slot", None) or "—",
            "roles": [getattr(args, "role")] if getattr(args, "role", None) else [],
            "status": "候选",
            "preset": False,
            "version": "v1.0",
            "credential_env": getattr(args, "env", None) or [],
            "network": True,
            "writes_external": False,
            "scope": "Propose",
            "evidence": [project],
            "approved_by": "创始人",
            "approved_at": datetime.now().isoformat(timespec="seconds"),
        })
        write_registry(vault, cfg, reg)
        print("已写入注册表：" + str(registry_path(vault, cfg).relative_to(vault)))
        print("状态 候选 / preset false（只登记不预置）；成为默认预置还需 §9 升格 + 版本 +0.1。")
        return
    raise ValueError("未知动作：" + action + "；可用 view / add / propose / promote")


def cmd_listen(args):
    """启动飞书长连接收确认（可选常驻组件）；发通知通道保持不变。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    # 配置写了要生效（D4-4）：notify.confirm_listener.enabled = false 时**不启动**长连接。
    # （以前这个键在整个 scripts/ 里 0 命中——写了等于没写。）
    cl = notify_conf(cfg).get("confirm_listener") or {}
    if "enabled" in cl and not cl.get("enabled"):
        print("收确认长连接**未启用**（notify.confirm_listener.enabled = false）。")
        print("要启用：把 opc.config.json 里 notify.confirm_listener.enabled 改成 true，再跑 opc listen。")
        print("未启用不影响流程：在当前会话回一句「继续 / 通过 / 驳回 / 需补充」，由当前会话代为执行 opc go。")
        return 0
    ws = SKILL_DIR / "scripts" / "notify_feishu_ws.py"
    if not ws.exists():
        raise ValueError("缺少长连接桥脚本：" + str(ws))
    cmd = [sys.executable, str(ws), "--vault", str(vault),
           "--opc", str(Path(__file__).resolve())]
    print("启动飞书长连接「收确认」；发通知仍走 notify.channel，两者互不替代。")
    print("停止：Ctrl+C\n")
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print("\n已停止长连接。")


def cmd_assets(args):
    """按职业查询资产快照视图；唯一真相源是 _资产快照.md，不另建台账。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    state = read_handoff(vault, cfg)
    project = active_project_of(args, cfg, state, vault)
    batches = read_asset_batches(vault, project)
    if not batches:
        print(project + " 暂无资产快照；opc done 会自动生成。")
        return
    role = getattr(args, "role", None)
    if role:
        batches = [b for b in batches if b.get("role") == role]
        if not batches:
            print(role + " 在 " + project + " 下暂无资产快照。")
            return
    p = asset_snapshot_path(vault, project)
    print("项目 " + project + " · 资产快照 " + str(len(batches)) + " 批 · 台账 "
          + str(p.relative_to(vault)) + "\n")
    for b in batches:
        print("[" + str(b.get("batch")) + "] 阶段 " + str(b.get("stage")) + " · "
              + str(b.get("role")) + " · " + str(b.get("time")) + " · commit " + str(b.get("commit")))
        for a in b.get("assets", []):
            print("   - " + a["path"] + " ｜ " + a["version"] + " ｜ " + a["format"]
                  + " ｜ " + str(a["bytes"]) + " B ｜ sha256 " + a["sha256"][:12])
        print("")


def cmd_scaffold(args):
    """**按需生成产出骨架（v2.18 / ADR-0006）**：岗位真正开工时才创建文件。

    立项（opc new / version new）只登记台账与索引，**不预生成任何空壳文档**。
    岗位干完活要落盘产出时，执行本命令生成当前岗位、当前版本的产出骨架，
    填内容、按岗位卡 §8 验收、改 status 后 opc done 报完成。
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    role = getattr(args, "role", "") or ""
    if not role:
        raise ValueError("请用 --role 指定岗位（如 pm / design / engineer）。")
    project = getattr(args, "project", None) or cfg.get("active_project") or ""
    if not project:
        raise ValueError("请用 --project 指定项目；或先 opc new 建项。")
    pid = resolve_project(vault, cfg, project)
    pdir = Path(vault) / "30_项目" / project_dir_name(vault, pid)
    if not pdir.is_dir():
        raise ValueError("项目目录不存在：" + str(pdir))
    state = read_handoff(vault, cfg, pid)
    version = (state.get("round") or "V1") if not getattr(args, "version", None) else args.version
    # 该岗位本版本登记的产出
    bindings = role_bindings_ref(cfg)
    if role not in bindings:
        raise ValueError("未登记的 role：" + role + "；可选：" + ", ".join(bindings.keys()))
    docs = _docs_for_roles(cfg, [role], None)
    if not docs:
        raise ValueError("岗位 " + role + " 没有登记的产出资产。")
    # v2.18：只生成本项目该版本**索引里登记**的该岗资产
    # （派活项目 pm 只登记了清单点名的资产，就只出那几份，不越权多出）
    reg = [x for x in _version_role_assets(vault, cfg, pid, version) if x[1] == role]
    if reg:
        reg_keys = {x[0] for x in reg}
        docs = [d for d in docs if d[2] in reg_keys]
        if not docs:
            raise ValueError("岗位 " + role + " 在 " + version
                             + " 没有登记的产出资产（索引里查不到）。")
    # 交接链里是否派活该岗位（v2.20 修：链外协作岗如 architect——§15.12 不在 done/go
    # 交接链、但派活清单「涉及」——允许 scaffold 自己登记的资产；原校验把它误拒，
    # 实测 P002 复盘暴露「architect 0/4 落盘无校验拦截」即此根因）
    chain = project_chain(vault, cfg, pid)
    if not any(owner == role for _i, _d, owner in chain) and role not in project_roles(vault, cfg, pid):
        raise ValueError("岗位 " + role + " 不在本项目交接链上（本次未派活）。")
    _gen_project_docs(vault, cfg, pid, docs, pdir, date.today().isoformat(), version=version)
    # 约束卡生成后补钉扎（v2.18：init_project_tools 不再提前补，因文件不预生成）
    if role == "strategist":
        _ensure_rule_pinning(vault, cfg, pid)
    # v2.18：pm 产出（4_产品基线.md）由 scaffold 生成后，补回写派活小节
    # （建项时文件不存在 → sync_triage_markdown 只写了 _派活清单.md；生成后再回写）
    if role == "pm":
        tj = read_triage_json(vault, pid)
        if tj:
            try:
                sync_triage_markdown(vault, pid, tj)
            except Exception:
                pass
    for fname, _r, key in docs:
        try:
            _b, uri = resolve_uri(cfg, vault, role, key, pid, version, pdir.name)
            print("已生成：" + uri)
        except Exception:
            print("已生成：" + key)
    print("下一步：填内容 → 按岗位卡 §8 验收 → status 改为完成 → opc done --role " + role)


def base_progress_push(vault, cfg, project, updates, version="V1"):
    """进度线：把状态更新写入多维表格台账（v2.20，创始人拍板「两码事」）。

    - 进度线（本函数）：推进/报完成/拍板 → **每次状态变化即同步**②版本开发计划；
      同时维护①需求池的层级结构（顶层=版本一行，阶段任务=子记录）；
    - 资产线（opc sync --flush）：文件产出等**整个需求版本结束后**统一推云盘。
    未配置 base_token → 静默跳过；任何失败只提示、**不阻塞流程**（§15.12）。
    写入权归 pm（§15.17）；本函数只在 pm 的推进/报完成路径上被调用。
    """
    conf = (cfg.get("backends") or {}).get("feishu") or {}
    if not str(conf.get("base_token") or "").strip():
        return
    try:
        import base_progress
        status, msg = base_progress.sync_progress(Path(vault), project, updates, version=version)
        print("进度台账（多维表格）：" + status + " · " + msg)
        titles = [u.get("title") for u in updates if u.get("title")]
        r_status, r_msg = base_progress.sync_requirements(Path(vault), project,
                                                          version=version, sub_records=updates)
        print("需求台账（①需求池）：" + r_status + " · " + r_msg)
    except Exception as exc:  # 台账同步失败绝不阻塞推进（§15.12）
        print("进度台账（多维表格）同步失败（不阻塞推进）：" + str(exc))


def _done_off_chain(vault, cfg, project, state, args):
    """链外协作岗报完成（v2.20 修）：architect 等岗位 §15.12 定为不在 done/go 交接链、
    但派活清单「涉及」——原逻辑被「跨步被拒绝」误拒，产出 0 落盘、无快照无校验
    （P002 复盘实测暴露）。现允许其报完成：登记资产快照 + 通知创始人 + 进度台账
    「已交付」，**不动交接状态游标**（游标推进只属于链上 owner 的 done/go，§15.9）。
    """
    role = args.role
    version = state.get("round") or "V1"
    reg = [x for x in _version_role_assets(vault, cfg, project, version) if x[1] == role]
    if not reg:
        raise ValueError("岗位 " + role + " 在 " + version + " 没有登记的产出资产（索引里查不到）。")
    docs_all = _docs_for_roles(cfg, [role], None)
    reg_keys = {x[0] for x in reg}
    docs = [d for d in docs_all if d[2] in reg_keys]
    if not docs:
        raise ValueError("岗位 " + role + " 在 " + version + " 没有登记的产出资产（索引里查不到）。")
    pdir = Path(vault) / "30_项目" / project_dir_name(vault, project)
    files = []
    for fname, _r, _k in docs:
        fp = _main_doc_path(pdir, role, fname, version)
        if fp.exists():
            files.append(fp)
    if not files:
        raise ValueError("主产出未落盘：请先执行 opc scaffold --role " + role
                         + " 生成产出骨架并填写（frontmatter status 改为完成）。")
    main_doc = files[0]
    for fp in files:
        if fp.suffix.lower() not in (".md", ".markdown"):
            continue  # 非 Markdown 资产（yaml/html 等）无 frontmatter，跳过结构校验
        body = read_text(fp)
        fm = body.split("---", 2)[1] if body.startswith("---") else ""
        if not re.search(r"^project:\s*" + re.escape(project_key(project)) + r"(?![0-9])", fm, re.M):
            raise ValueError("frontmatter 的 project 与当前项目不符：" + str(fp.relative_to(vault)))
        if role not in fm:
            raise ValueError("frontmatter 的 roles 不含 " + role + "：" + str(fp.relative_to(vault)))
        if re.search(r"^status:\s*草稿\s*$", fm, re.M):
            raise ValueError("status 仍为『草稿』：" + str(fp.relative_to(vault))
                             + " —— 请按岗位卡 §8 验收后改为完成状态")
    stage_no = None
    for r in read_index(vault, cfg):
        if project_key(r.get("project")) == project_key(project) and r.get("role") == role:
            try:
                stage_no = int(str(r.get("阶段") or "").strip() or 0)
            except ValueError:
                stage_no = None
            break
    if stage_no is None:
        stage_no = int(state.get("stage", 0))
    extras = [str(f.relative_to(vault)) for f in files[1:]]
    commit = short_commit(vault)
    token = secrets.token_hex(3).upper()
    snap = build_snapshot(vault, project, stage_no, role, main_doc, extras, commit, "")
    append_snapshot(vault, project, snap)
    conf = notify_conf(cfg)
    channel = getattr(args, "channel", None) or conf.get("channel", "local")
    notice = "\n".join([
        "【OPC 交接通知】项目 " + project_dir_name(vault, project) + " · 链外协作岗报完成",
        "完成职业：" + role + "（不在 done/go 交接链上，§15.9 协作岗——游标不推进）",
        "主产出：" + str(main_doc.relative_to(vault)),
        "落盘凭据：git " + commit,
        "资产批次：" + snap["batch"] + "（" + str(len(snap["assets"])) + " 份资产已登记版本 / 时间 / SHA256）",
        "回执：" + token,
    ])
    used, msg = push_notice(vault, cfg, channel, notice, project, role)
    print(notice)
    print("\n通知通道：" + used + " · " + msg)
    print("资产快照：" + str(asset_snapshot_path(vault, project).relative_to(vault))
          + "（" + str(len(snap["assets"])) + " 份）")
    base_progress_push(vault, cfg, project,
                       [{"stage": stage_no, "role": role, "progress": "已交付",
                         "doc": str(main_doc.relative_to(vault)),
                         "title": project_dir_name(vault, project)
                         + " 阶段" + str(stage_no) + " · " + role}])
    return 0


def cmd_done(args):
    """本岗产出完成后报完成并通知创始人；下游不接受指定，由 step_docs 顺序推导。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    hint = read_handoff(vault, cfg)
    project = active_project_of(args, cfg, hint, vault)
    # v2.20 修（实测踩坑，通用缺陷）：不带 --project 时落到 active_project，
    # 多项目 Vault 里极易对错项目操作（回执、错误提示还都不说是哪个项目）。
    # 这里显式点名，让操作者第一眼就能发现打错了靶子。
    print("操作项目：" + str(project)
          + ("" if getattr(args, "project", None) else "（未显式指定 --project，取自 active_project；"
             "多项目 Vault 建议所有命令显式带 --project）"))
    state = read_handoff(vault, cfg, project)
    if state.get("project") and project_key(state["project"]) != project_key(project):
        raise ValueError("项目不一致：当前交接属于 " + state["project"] + "，收到 " + project)
    chain = project_chain(vault, cfg, project)
    if not chain:
        raise ValueError("当前 profile 未定义 step_docs")
    i = chain_pos(chain, state)
    if i >= len(chain):
        raise ValueError("流程已走完；用 opc new 开新项目")
    idx, doc, owner = chain[i]
    dispatch = project_roles(vault, cfg, project)
    if args.role != owner:
        if args.role in dispatch:
            # v2.20 修（实测 P002 复盘暴露）：链外协作岗（architect 等）在派活清单
            # 「涉及」却不在 done/go 交接链——原逻辑直接拒绝，导致其产出 0 落盘、
            # 无快照无校验。现允许其报完成：登记资产快照 + 通知 + 进度台账，
            # **不动交接状态游标**（推进只属于链上 owner 的 done/go，§15.9）。
            return _done_off_chain(vault, cfg, project, state, args)
        # 本次没被派活的岗位不产生交接，但也不能越权报别人的阶段
        raise ValueError("跨步被拒绝：当前是阶段 " + str(idx) + "（" + owner + "），"
                         + args.role + " 不是本阶段 owner。下游由 step_docs 顺序推导，禁止指定、禁止跳步。")
    if state.get("status") == "待确认":
        raise ValueError("项目 " + str(state.get("project") or project)
                         + " 阶段 " + str(idx) + " 已报完成，正在等创始人确认；不能重复提交"
                         + "（多项目 Vault 请显式 --project 核对是否打错了靶子）")
    if state.get("status") == "已完成":
        raise ValueError("本项目本次派活的岗位已全部完成；请执行 opc close 收尾，或 opc new 开新项目")
    if state.get("zero_role"):
        raise ValueError("本项目是零岗位项目（pm 分诊判定无需岗位投入），已归档；请执行 opc close 收尾")
    pdir = Path(vault) / "30_项目" / project_dir_name(vault, project)
    # v2.19（ADR-0007）：主产出按版本目录解析 —— 版本级在 <版本目录>/<role>/，项目级在项目根
    main_doc = _main_doc_path(pdir, owner, doc, state.get("round") or "")
    if not main_doc.exists():
        raise ValueError("主产出未落盘：" + str(main_doc.relative_to(vault))
                         + " —— 请先执行 opc scaffold --role " + owner + " 生成产出骨架")
    body = read_text(main_doc)
    fm = body.split("---", 2)[1] if body.startswith("---") else ""
    # 骨架写的是项目号（project: P001），全名（project: P001_xxx）同样合法（v2.13）
    if not re.search(r"^project:\s*" + re.escape(project_key(project)) + r"(?![0-9])", fm, re.M):
        raise ValueError("frontmatter 的 project 与当前项目不符（应为 "
                         + project_key(project) + " 或 " + project + "）")
    if args.role not in fm:
        raise ValueError("frontmatter 的 roles 不含 " + args.role)
    if re.search(r"^status:\s*草稿\s*$", fm, re.M):
        raise ValueError("status 仍为『草稿』；请先按岗位卡 §8 验收并改为完成状态")
    extras = []
    for raw in (getattr(args, "file", None) or []):
        fp = Path(raw)
        if not fp.is_absolute():
            fp = Path(vault) / raw
        if not fp.exists():
            raise ValueError("附带产出不存在：" + str(fp))
        try:
            extras.append(str(fp.relative_to(vault)))
        except ValueError:
            extras.append(str(fp))
    commit = short_commit(vault)
    token = secrets.token_hex(3).upper()
    upstream = chain[i - 1][1] if i > 0 else "（流程起点）"
    snap = build_snapshot(vault, project, idx, owner, main_doc, extras, commit, upstream)
    append_snapshot(vault, project, snap)
    # 下一棒：跳过本次没派活的岗位（§15.14）
    n_i = next_dispatch_index(chain, dispatch, i)
    if n_i >= 0:
        n_idx, next_doc, next_role = chain[n_i]
        skipped = skipped_roles_between(cfg, idx, n_idx, dispatch)
        nxt = "阶段 " + str(n_idx) + " · " + next_role + " · 产出 " + next_doc
        if skipped:
            nxt += "（本次未派活，自动跳过：" + "、".join(skipped) + "）"
    else:
        n_idx, next_doc, next_role = -1, "", ""
        nxt = "（最后一步，确认后进入项目收尾）"
    gate = idx in (notify_conf(cfg).get("gates") or [0, 2, 7])
    notice = "\n".join([
        "【OPC 交接通知】项目 " + project + " · 阶段 " + str(idx) + " 已完成",
        "完成职业：" + owner,
        "主产出：" + str(main_doc.relative_to(vault)),
        "附带资料：" + ("、".join(extras) if extras else "（无）"),
        "落盘凭据：" + ("git " + commit if commit else "未启用 Git 或暂无提交"),
        "下一棒：" + nxt,
        "上游输入：" + upstream,
        "资产批次：" + snap["batch"] + "（" + str(len(snap["assets"])) + " 份资产已登记版本 / 时间 / SHA256，台账 _资产快照.md）",
        "人工闸门：" + ("是，需给结论 通过 / 驳回 / 需补充" if gate else "否，回『继续』即可"),
        "工具降级：如本阶段有工具未配置/需付费，已在交付物『待确认项』标注『用 X 降级替代』（§15.12 不阻塞流程）；可在宿主连接器面板补配后重跑以启用更优解。",
        "下一步（产品经理推进进度）：" + ("阶段 " + str(idx) + " 是人工闸门，需创始人先给结论（通过 / 驳回 / 需补充），执行 opc go --decision 通过"
                                          if gate else
                                          "创始人确认本阶段完成后，由产品经理执行 opc advance 推进到下一棒；"
                                          "下一棒：" + nxt),
        "确认方式：在工作台用自然语言回『继续 / 下一步 / 通过』；当前会话代为执行 opc go，项目与回执已记住，无需输入命令。",
        "回执：" + token,
    ])
    conf = notify_conf(cfg)
    channel = getattr(args, "channel", None) or conf.get("channel", "local")
    used, msg = push_notice(vault, cfg, channel, notice, project, args.role)
    # v2.24（创始人拍板 2026-11-19）：**干活的角色主动私聊创始人汇报完成**——
    # 创始人不需要 @ 它；「推进」则只回 pm（唯一调度中枢，§15.15）。
    # 用**本角色自己的**应用身份发，创始人看到的就是「这个角色来汇报了」。
    dispatch_done_notice(vault, cfg, project, owner, idx, main_doc, extras, nxt, gate, commit)
    state.update({
        "project": project, "step": i, "stage": idx, "role": owner, "doc": doc,
        "status": "待确认", "token": token, "next_step": n_idx,
        "next_role": next_role, "next_doc": next_doc,
        "artifacts": [str(main_doc.relative_to(vault))] + extras,
        "commit": commit, "gate": gate, "channel": used,
        "asset_batch": snap["batch"], "asset_count": len(snap["assets"]),
        "done_at": datetime.now().isoformat(timespec="seconds"),
    })
    write_handoff(vault, cfg, state)
    if project_key(cfg.get("active_project") or "") != project_key(project):
        cfg["active_project"] = project
        save_config(vault, cfg)
    print(notice)
    print("\n通知通道：" + used + " · " + msg)
    print("资产快照：" + str(asset_snapshot_path(vault, project).relative_to(vault))
          + "（" + str(len(snap["assets"])) + " 份）")
    print("建议本次 Git 提交信息：assets(" + project + "): " + snap["batch"]
          + " stage " + str(idx) + " " + owner)
    # v2.10 **增量同步**：各岗干完自己这步，**本地入队**（不连远程）；
    # 项目状态推进、远程写入统一由 pm 负责（§15.16）。
    if getattr(args, "sync_self", False):
        # 只入队**本次刚产出**的资产（增量）：主产出 + 附带资料。
        # 联调确认（v2.10 修）：职业简历/岗位卡是**工作区级团队档案**，
        # 不混进项目版本目录（用户口径：「项目里不要扯 Agent 配置」）。
        fresh = [main_doc] + [Path(vault) / e for e in extras]
        sync_self_scope(vault, cfg, project, owner, args, files=fresh)
    else:
        print("同步：本地已落盘。要把本岗这次产出登记为待同步，加 --sync-self"
              "（只在本地入队，带批次号/时间戳/版本号；不直接写远程仓库）。")
        print("      远程推送与项目状态推进统一由产品经理执行"
              "（opc advance / opc sync --flush）。")
    # v2.20 进度线（两码事之一）：本棒已报完成 → 台账「待确认」，设计师/工程师
    # 还在干的时候，多维表格里就能看到每根棒的状态。
    base_progress_push(vault, cfg, project,
                       [{"stage": int(idx), "role": owner, "progress": "待确认",
                         "doc": str(doc), "title": project_dir_name(vault, project)
                         + " 阶段" + str(idx) + " · " + owner}],
                       version=str(state.get("round") or "V1"))


def maybe_sync_to_feishu(vault, cfg, project, role, stage, args=None):
    """**产品经理**做**项目级全量上送**（v2.7 → v2.9 → **v2.10**）。

    v2.6：每次问一次，答「同步」才推。
    v2.7：初始化时配了云端仓库 → 之后自动同步，不再逐次问。
    v2.9：项目级上送归 pm——创始人明确「推进、上送、整个项目的管理都是 PM 做的」。
    **v2.10**：与**增量队列**并存、互不冲突——
      - **增量**（默认路径）：各岗 `opc done --sync-self` 本地入队，
        pm 用 `opc advance` / `opc sync --flush` 推送，每次产出落一个**版本目录**。
      - **全量**（本函数）：把整个项目目录写一份到云端，沉淀项目全貌。手动触发。

    本函数**只由 pm 路径调用**；其他岗位**不直接写远程仓库**（§15.16）。

    模式由 `backends.feishu.sync_mode` 决定：
      auto —— 直接上送（失败只提示，不阻塞流程推进）
      ask  —— 问一次（默认）
      off  —— 不碰云端

    本地照常写入、工作流不变；这一步只是在本地写完之后**额外**写一份到云端。
    本地 Vault 始终是唯一真相源（宪法 §15.8）。
    """
    conf = sync_conf(cfg)
    if not conf or not (conf.get("enabled", True)):
        return
    mode = sync_mode_of(cfg)
    if mode == "off":
        return
    cli = (conf.get("cli") or "lark-cli").strip()
    folder_token = (conf.get("folder_token") or "").strip()
    if not folder_token:
        print()
        print("── 项目级全量上送 ──")
        print("云端仓库：未配置 folder_token，跳过（不影响流程推进）。")
        print("        启用：opc.config.json 的 backends.feishu 加 \"folder_token\"，"
              "或用 opc sync init --folder-token <token> 建目录拿 token。")
        return
    if shutil.which(cli) is None:
        print()
        print("── 项目级全量上送 ──")
        print("云端仓库：未检测到 " + cli + "，跳过（不影响流程推进）。")
        return
    ns = argparse.Namespace(vault=str(vault), project=str(project), role=None,
                            all=False, action="push", folder_token=None,
                            prefix=None, allow_restore=False, dry_run=False,
                            query=None, limit=0, queue=False, flush=False)
    if mode == "auto":
        print()
        print("── 项目级全量上送（pm） ──")
        print("产品经理把整个项目目录写一份到云端，沉淀项目全貌（已配置，不再逐次询问）。")
        rc = cmd_sync(ns)
        if rc != 0:
            print("（上送未成功，不影响流程推进；本地产出已安全落盘）")
        return
    # ask：问一次
    print()
    print("── 项目级全量上送（可选，pm） ──")
    print("本阶段产出已落本地。是否由产品经理把整个项目目录再写一份到云端仓库？")
    print("  上送：opc sync --project " + str(project))
    print("  跳过：什么都不做即可（本地已写完）")
    if args is not None and not interactive_enabled(args):
        print("（非交互模式：不自动上送，需你显式执行上面那条命令）")
        return
    answer = prompt("输入「同步」推送到云端，其它键跳过：", "").strip()
    if answer in ("同步", "sync", "y", "Y", "yes"):
        rc = cmd_sync(ns)
        if rc != 0:
            print("（上送未成功，不影响流程推进；本地产出已安全落盘）")
    else:
        print("已跳过项目级上送。本地产出已安全落盘。")


def cmd_go(args):
    """创始人确认后只推进到紧邻的下一棒；token 可省略，自动取当前回执。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    hint = read_handoff(vault, cfg)
    project = active_project_of(args, cfg, hint, vault)
    state = read_handoff(vault, cfg, project)
    if not state or state.get("status") != "待确认":
        raise ValueError("项目 " + project + " 没有待确认的交接；请等本阶段 Agent 报完成后再回『继续』")
    token = getattr(args, "token", None) or state.get("token")
    if token != state.get("token"):
        raise ValueError("回执不匹配，拒绝推进（防止跨步或重复确认）")
    chain = project_chain(vault, cfg, project)
    i = chain_pos(chain, state)
    # v2.23：`--decision` **只对人工闸门有效**。
    # 过去这里不校验当前棒是不是闸门，`opc go --decision 通过` 打在非闸门棒上
    # 也会照走（实测把 P003 阶段 2 的闸门结论记到了 P002 阶段 5 上）。
    # 非闸门棒应当走 `opc advance`（pm 代推），语义不同：go 是创始人拍板。
    gates = notify_conf(cfg).get("gates") or [0, 2, 7]
    _cur_stage = int(state.get("stage", chain[i][0] if i < len(chain) else i))
    _is_gate = bool(state.get("gate")) or (_cur_stage in gates)
    d = getattr(args, "decision", None)
    basis = getattr(args, "basis", None)
    if _is_gate:
        if d not in ("通过", "驳回", "需补充"):
            raise ValueError("阶段 " + str(i) + " 是人工闸门，请给出结论：通过 / 驳回 / 需补充")
    else:
        # v2.25：**非闸门棒也能「不同意」**。
        # v2.23 只为闸门放开 --decision，理由是「--decision 通过」打在非闸门棒上会
        # 照走（实测把 P003 阶段 2 的闸门结论记到了 P002 阶段 5 上）——那是**「通过」**的错位。
        # 但「驳回 / 需补充」在非闸门棒上语义完全成立：**创始人不满意 → 退回本阶段重做**。
        # 过去非闸门棒没有这条通道 → 创始人只能说「不行」，状态却卡在「待确认」，
        # 而 `opc done` 又拒收重复提交（status=待确认）→ **流程死锁**（本轮实测缺口）。
        # 规则：**非闸门棒只接受「驳回 / 需补充」**；「通过」仍然拒绝（等价于不带 --decision 推进）。
        if d == "通过":
            raise ValueError(
                "阶段 " + str(i) + "（" + str(state.get("role", "")) + "）不是人工闸门"
                "（闸门＝阶段 " + " / ".join(str(g) for g in gates) + "），"
                "「--decision 通过」只对人工闸门有效。非闸门棒直接推进即可（不加 --decision）；"
                "若要退回重做用 --decision 驳回 / 需补充。")
        if d and d not in ("驳回", "需补充"):
            raise ValueError("--decision 只能是：通过 / 驳回 / 需补充")
    # 拍板审计（v2.11）：**谁拍的、什么结论、依据是什么、什么时候**——一律落盘。
    # 过去「驳回 / 需补充」记 decision + decided_at，**「通过」什么都不记**（只有 confirmed_at），
    # 事后查不出某个闸门是不是创始人放行的（D4-2）。
    if d:
        state.update({
            "decision": d,
            "decided_by": "创始人",
            "decided_at": datetime.now().isoformat(timespec="seconds"),
            "decided_via": "opc go",
            "decision_basis": str(basis) if basis else "",
        })
    if d in ("驳回", "需补充"):
        # 驳回 / 需补充：**退回本阶段、游标不推进**，由当前 role 补充后重报。
        # 闸门与非闸门同一条路（v2.25 把非闸门也纳入本分支）。
        state.update({"status": "进行中", "token": ""})
        write_handoff(vault, cfg, state)
        print("已登记『" + d + "』，退回阶段 " + str(i) + " 由 " + str(state.get("role")) + " 补充；工作流不推进。")
        print("拍板留痕：decided_by=创始人 · decided_at=" + str(state.get("decided_at"))
              + ((" · 依据：" + str(state.get("decision_basis"))) if state.get("decision_basis") else ""))
        if not _is_gate:
            print("（非闸门棒也可退回：--decision 驳回 / 需补充；「通过」只对闸门有效）")
        return
    # v2.14：推进到下一棒 = 本阶段的拍板结论**已成历史** → 归档并清空顶层，
    # 免得下一个闸门看着像「已经拍过板」（自动化据此判断就会绕过创始人）。
    archive_decision(state)
    next_role = state.get("next_role") or ""
    prev_stage = int(state.get("stage", i))
    prev_role = str(state.get("role", ""))
    print("已确认：" + str(state.get("project")) + " 阶段 " + str(state.get("stage", i))
          + "（" + str(state.get("role", "")) + "）")
    if next_role:
        chain2 = project_chain(vault, cfg, project)
        dispatch = project_roles(vault, cfg, project)
        n_i = next_dispatch_index(chain2, dispatch, i)
        if n_i < 0:
            n_i = i + 1
        n_idx2, next_doc2, next_role2 = chain2[n_i]
        skipped = skipped_roles_between(cfg, state.get("stage", i), n_idx2, dispatch)
        print("下一棒：阶段 " + str(n_idx2) + " · " + next_role2
              + " · 必读 " + str(next_doc2) + "\n")
        if skipped:
            print("（本次未派活，自动跳过：" + "、".join(skipped) + "）")
        cmd_role(argparse.Namespace(vault=args.vault, role=next_role2))
        dispatch_next_role_notice(vault, cfg, project, next_role2, next_doc2, n_idx2, skipped)
        # v2.28（2026-11-20 实测修）：通知创始人之外，**必须给下一棒发门铃**把它叫醒。
        # 过去只通知创始人 → 通知发出去了，下一棒 Agent 仍在睡觉 → 链断在半路。
        wake_next_role(vault, cfg, project, next_role2, next_doc2, n_idx2, skipped)
        state.update({"step": n_i, "stage": n_idx2, "role": next_role2,
                      "doc": next_doc2, "status": "进行中", "token": "",
                      "next_role": "", "next_step": -1, "next_doc": "", "gate": False,
                      "confirmed_at": datetime.now().isoformat(timespec="seconds")})
    else:
        state.update({"status": "已完成", "token": "",
                      "confirmed_at": datetime.now().isoformat(timespec="seconds")})
        print("本次派活的岗位已全部完成，按 §15.6 追加各职业简历并登记复盘。")
    write_handoff(vault, cfg, state)
    # v2.20 进度线：多维表格台账——上一棒已交付（含拍板结论），下一棒可开工
    d = getattr(args, "decision", None)
    updates = [{"stage": prev_stage, "role": prev_role, "progress": "已交付",
                "decision": d if d else "通过",
                "title": project_dir_name(vault, project) + " 阶段" + str(prev_stage) + " · " + prev_role}]
    if state.get("status") != "已完成":
        updates.append({"stage": int(state.get("stage", 0)), "role": str(state.get("role", "")),
                        "progress": "可开工", "doc": str(state.get("doc", "")), "claim": True,
                        "title": project_dir_name(vault, project) + " 阶段" + str(state.get("stage", 0))
                        + " · " + str(state.get("role", ""))})
    base_progress_push(vault, cfg, project, updates, version=str(state.get("round") or "V1"))


def cmd_handoff(args):
    """查看当前交接状态；可用 --project 设定当前项目（之后所有命令省略项目号）。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    p = getattr(args, "project", None)
    # v2.17：`--project` 接受任意形态（项目名 / 项目号 / 目录名）→ 先归一成短 ID。
    # 否则拿「显示名」去比状态里的「短 ID」，永远不相等 → 状态被当成「没有」重建，
    # 刚追加的版本（V2）会被悄悄打回第 0 阶段（实测：version new 后 handoff 显示
    # 「0 · strategist · 0_约束卡.md」，且把正确状态覆盖掉了）。
    pid = resolve_project(vault, cfg, p) if p else None
    state = read_handoff(vault, cfg, pid) if pid else read_handoff(vault, cfg)
    if pid:
        cfg["active_project"] = pid
        if not state or project_key(state.get("project") or "") != project_key(pid):
            chain0 = step_chain(cfg)
            state = {"project": pid, "step": 0, "stage": 0,
                     "role": chain0[0][2] if chain0 else "", "status": "进行中", "token": ""}
        save_config(vault, cfg)
        write_handoff(vault, cfg, state)
    if not state:
        print("尚无交接状态。执行 opc new 建项，或 opc handoff --project PXXX 指定当前项目。")
        return
    chain = project_chain(vault, cfg, state.get("project") or cfg.get("active_project") or "")
    i = chain_pos(chain, state)
    if state.get("zero_role"):
        print("当前项目：" + str(project_label_map(vault, cfg).get(state.get("project"))
                                   or state.get("project") or "（未设定）"))
        print("当前阶段：（零岗位项目 · pm 分诊判定无需岗位投入）")
        print("状态：" + str(state.get("status", "—")))
        print("分诊理由：" + str(state.get("reason") or "—"))
        print("回执：—")
        print("下一棒：—（无交接链，不发通知）")
        return
    cur = chain[i] if i < len(chain) else None
    _plabel = project_label_map(vault, cfg).get(state.get("project")) or state.get("project")
    print("当前项目：" + str(_plabel or "（未设定）"))
    print("当前阶段：" + (str(cur[0]) + " · " + cur[2] + " · " + cur[1] if cur else "（已走完）"))
    print("状态：" + str(state.get("status", "—")) + "（待确认 = 通知已发，等创始人回话）")
    print("回执：" + str(state.get("token") or "—"))
    print("下一棒：" + str(state.get("next_role") or "—"))
    print("通知通道：" + str(state.get("channel") or "—"))


def dispatch_next_role_notice(vault, cfg, project, next_role, next_doc, next_stage,
                              skipped=None):
    """v2.24（创始人拍板 2026-11-19）：**下一棒角色主动私聊创始人**告知轮到他开工。

    交互模型（创始人拍板）：
      · 干活的角色**主动私聊**创始人报告工作 —— 创始人不需要 @ 它；
      · 创始人说「推进」时**只 @ pm**（pm 是唯一调度中枢，§15.15）；
      · pm 推进一棒后，由下一棒角色用自己的身份私聊创始人 —— 闭环。

    本函数**不阻塞流程**：私聊发不出去只打印提示，流程照常推进（§15.12 降级）。
    凭证只走环境变量（FEISHU_FOUNDER_UNION_ID + 各角色 profile 的 .env），不入库。
    """
    conf = notify_conf(cfg)
    disp = (conf.get("dispatch") or {})
    if disp.get("enabled") is False:
        print("（派活私聊通知未启用：notify.dispatch.enabled=false）")
        return {"ok": False, "detail": "未启用"}
    founder_id = dispatch_founder_id(disp, next_role)
    if not founder_id:
        print("（未配置创始人用户 ID，跳过派活私聊通知；设 FEISHU_FOUNDER_UNION_ID 或 notify.dispatch.founder_id）")
        return {"ok": False, "detail": "未配置创始人 ID"}
    try:
        import dispatch as dispatch_mod
    except Exception as exc:  # noqa: BLE001
        print("（派活通知模块不可用：" + str(exc) + "）")
        return {"ok": False, "detail": str(exc)}
    proj_name = _index_row_name({"project": project}) if project else str(project)
    # v2.26：**必须带项目号**。多项目时角色在私聊里回「继续」要执行
    # `opc go --project <项目号>` —— 没有项目号就会推进错项目。
    proj_label = str(project) + ("（" + str(proj_name) + "）" if proj_name and proj_name != str(project) else "")
    title = ("【OPC 派活】项目 " + proj_label + " · 阶段 " + str(next_stage)
             + " · " + str(next_role) + " 轮到你开工")
    lines = [
        "我是 " + str(next_role) + "，本棒由我负责：" + str(next_doc),
        "项目：" + proj_label + "（项目号 " + str(project) + "）",
        "必读文件：" + str(next_doc),
        "Vault：" + str(vault),
        "开工前先读本项目的 _待确认.md（如有）与上游交接文件；产出用 opc done --role "
        + str(next_role) + " --project " + str(project) + " 报完成。",
    ]
    if skipped:
        lines.append("（本次未派活已跳过：" + "、".join(skipped) + "）")
    if _dispatch_group_mode(disp):
        lines.append("我这棒做完会**在这个群里** @ 你汇报；要交接下一棒，"
                     "**在这个群里 @ 我**回一句就行（其它角色不会被吵醒）。"
                     "有需要拍板的问题，也 @ 我。")
    else:
        lines.append("我这棒做完会**在这个私聊里**向你汇报；要交接下一棒回「继续」即可"
                     "（不用去群里 @ 任何人）。有需要拍板的问题，直接在这里问我。")
    lines.append("（本棒项目号是 " + str(project) + "；若要执行 opc go，请带 --project "
                 + str(project) + "，避免多项目时推进错。）")
    res = dispatch_mod.notify_next_role(
        str(next_role), founder_id, title, lines,
        profile_root=Path(disp.get("profile_root") or (Path.home() / ".hermes" / "profiles")),
        app_id_env=str(disp.get("app_id_env") or "FEISHU_APP_ID"),
        app_secret_env=str(disp.get("app_secret_env") or "FEISHU_APP_SECRET"),
        chat_id=str(disp.get("chat_id") or ""),
        mode=str(disp.get("mode") or "dm"))
    if res.get("ok"):
        print("派活通知：" + str(res.get("detail", "")))
    else:
        print("派活通知未送达：" + str(res.get("detail", "")))
        print("  流程照常推进；创始人也可在群里 @ 该角色手动开工。")
    return res


ORCH_STATE_REL = "99_Agent配置/_编排状态.json"


def _record_doorbell(vault, project, stage, role, ok, detail=""):
    """v2.29：门铃台账 —— 写进编排器的 `_编排状态.json`，供「重敲门铃」判重。

    只在**真正尝试叫醒**后记录（成功或失败都记，失败也计数）；
    配置性跳过（dispatch 未启用 / 非群模式 / 缺 chat_id）**不记** ——
    那种情况下重敲也不会成功，由编排器的停滞提醒兜底。
    写失败不阻塞门铃与流程（§15.12 降级）。
    """
    try:
        p = Path(vault) / ORCH_STATE_REL
        with VaultLock(vault):
            st = {}
            if p.exists():
                try:
                    st = json.loads(read_text(p))
                except (ValueError, OSError):
                    st = {}
            if not isinstance(st, dict):
                st = {}
            st.setdefault("version", 1)
            rec = st.setdefault("projects", {}).setdefault(project_key(project), {})
            bells = rec.setdefault("doorbells", {})
            key = str(stage) + "/" + str(role)
            prev = bells.get(key) or {}
            bells[key] = {
                "project": project_key(project), "stage": stage, "role": str(role),
                "last_at": datetime.now().isoformat(timespec="seconds"),
                "count": int(prev.get("count") or 0) + 1,
                "ok": bool(ok), "detail": str(detail)[:200],
            }
            write_text(p, json.dumps(st, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        pass


def wake_next_role(vault, cfg, project, next_role, next_doc, next_stage, skipped=None):
    """**门铃**：在群里 @ 下一棒岗位的 bot，让它自己醒来领活。

    为什么需要这一下（2026-11-20 实测踩坑）：
      · 原实现只在 `opc done` / `opc go` / `opc advance` 里通知**创始人**，
        **从来没叫醒过下一棒 Agent** —— 通知发出去了，链还是停着。
      · `opc new` 建项后更是**一条通知都不发** → 第一棒永远没人接。
    本函数只发「门铃」：@ 下一棒 + 告诉它去 `opc claim` 领活，
    **不替它做判断、不给它写产出**（§15.3 交接只走文件，§15.9 通知对象是创始人）。

    @ 的标识必须是**下一棒自己视角的 open_id**（见 dispatch.bot_open_id 的注释）：
    用 app_id 发，飞书会解析成空 user_id，门铃静默失效。
    """
    conf = notify_conf(cfg)
    disp = (conf.get("dispatch") or {})
    if disp.get("enabled") is False:
        print("（门铃未启用：notify.dispatch.enabled=false）")
        return {"ok": False, "detail": "未启用"}
    if not _dispatch_group_mode(disp):
        print("（门铃只在群模式发：notify.dispatch.mode=group）")
        return {"ok": False, "detail": "非群模式"}
    chat_id = str(disp.get("chat_id") or "").strip()
    if not chat_id:
        print("（门铃未发：notify.dispatch.chat_id 未配置）")
        return {"ok": False, "detail": "缺 chat_id"}
    try:
        import dispatch as dispatch_mod
    except Exception as exc:  # noqa: BLE001
        print("（门铃模块不可用：" + str(exc) + "）")
        return {"ok": False, "detail": str(exc)}
    root = Path(disp.get("profile_root") or (Path.home() / ".hermes" / "profiles"))
    # @ 目标 bot：用**它自己视角**的 open_id，否则飞书解析成空 user_id（实测失效）。
    at_open_id = dispatch_mod.bot_open_id(str(next_role), root)
    if not at_open_id:
        print("（门铃未发：" + str(next_role) + " 没有可用身份（缺 ~/.hermes/profiles/"
              + str(next_role) + "/.env 或应用已失效））")
        _record_doorbell(vault, project, next_stage, next_role, False, "目标角色无身份")
        return {"ok": False, "detail": "目标角色无身份"}
    # 发门铃用 pm 自己的应用身份（pm 是调度中枢，§15.15）。
    sender_env = dispatch_mod._profile_env("pm", root)
    app_id = (sender_env.get(str(disp.get("app_id_env") or "FEISHU_APP_ID")) or "").strip()
    app_secret = (sender_env.get(str(disp.get("app_secret_env") or "FEISHU_APP_SECRET")) or "").strip()
    if not app_id or not app_secret:
        print("（门铃未发：pm 没有飞书应用凭证）")
        _record_doorbell(vault, project, next_stage, next_role, False, "发送方无凭证")
        return {"ok": False, "detail": "发送方无凭证"}
    proj_name = _index_row_name({"project": project}) if project else str(project)
    proj_label = str(project) + ("（" + str(proj_name) + "）" if proj_name and proj_name != str(project) else "")
    title = "【OPC 门铃】项目 " + proj_label + " · 阶段 " + str(next_stage) + " · " + str(next_role) + " 轮到你"
    lines = [
        "我是 pm（产品经理），现在派给你这一棒：" + str(next_role) + " → " + str(next_doc),
        "项目：" + proj_label + "（项目号 " + str(project) + "）",
        "必读文件：" + str(next_doc),
        "Vault：" + str(vault),
        "**自己去领活**（不用等人派）：opc claim --role " + str(next_role) + " --project " + str(project),
        "领完按岗位卡干活；需要交互就在这个群里 @ 我（pm 是调度中枢）。",
    ]
    if skipped:
        lines.append("（本次未派活已跳过：" + "、".join(skipped) + "）")
    try:
        token = dispatch_mod._token(app_id, app_secret)
        sender_open_id = dispatch_mod.bot_open_id("pm", root)
        mid = dispatch_mod.send_group_at_open_id(token, chat_id, at_open_id, title, lines,
                                                  sender_open_id=sender_open_id)
    except Exception as exc:  # noqa: BLE001
        print("（门铃未送达：" + str(exc) + "）")
        print("  流程照常推进；创始人也可在群里 @ 该角色手动开工。")
        _record_doorbell(vault, project, next_stage, next_role, False, str(exc))
        return {"ok": False, "detail": str(exc)}
    print("门铃已发：已在群里 @" + str(next_role) + "（message_id=" + str(mid) + "）")
    _record_doorbell(vault, project, next_stage, next_role, True, "message_id=" + str(mid))
    return {"ok": True, "message_id": str(mid)}


def dispatch_done_notice(vault, cfg, project, owner, idx, main_doc, extras, nxt, gate, commit):
    """v2.24：角色用**自己的**应用身份私聊创始人，汇报本棒完成。

    「完成汇报」与「下一棒派活」是同一件事的两面：创始人看到哪个角色来汇报，
    就知道现在轮到谁；确认「推进」时仍然只回 pm（§15.15）。
    失败不阻塞：只打印提示，`opc done` 照常完成（§15.12 降级）。
    """
    conf = notify_conf(cfg)
    disp = (conf.get("dispatch") or {})
    if disp.get("enabled") is False:
        return {"ok": False, "detail": "未启用"}
    founder_id = dispatch_founder_id(disp, owner)
    if not founder_id:
        return {"ok": False, "detail": "未配置创始人 ID"}
    try:
        import dispatch as dispatch_mod
    except Exception as exc:  # noqa: BLE001
        print("（完成私聊通知模块不可用：" + str(exc) + "）")
        return {"ok": False, "detail": str(exc)}
    try:
        rel = str(main_doc.relative_to(vault))
    except Exception:
        rel = str(main_doc)
    # v2.26：完成汇报同样**必须带项目号**（多项目时角色据此执行 opc go --project）。
    proj_name = _index_row_name({"project": project}) if project else str(project)
    proj_label = str(project) + ("（" + str(proj_name) + "）" if proj_name and proj_name != str(project) else "")
    title = ("【OPC 完成汇报】项目 " + proj_label + " · 阶段 " + str(idx)
             + " · " + str(owner) + " 这一棒做完了")
    lines = [
        "我是 " + str(owner) + "，阶段 " + str(idx) + " 已完成。",
        "项目：" + proj_label + "（项目号 " + str(project) + "）",
        "主产出：" + rel,
        "附带资料：" + ("、".join(extras) if extras else "（无）"),
        "落盘凭据：" + ("git " + commit if commit else "未启用 Git 或暂无提交"),
        "下一棒：" + str(nxt),
        ("本阶段是人工闸门，需要你给结论（通过 / 驳回 / 需补充）；"
         if gate else "要交接下一棒，回「继续」即可。"),
        (("（v2.27 · §15.9：流程推进靠**创始人确认**——你**在这个群里 @ 我**回「继续 / 通过」"
          "我就地代你执行 opc go，游标前移、下一棒自己来 @ 你；不用再 @ 别的角色。"
          "不满意就 @ 我说「驳回」或「需补充」，我退回本阶段重做，游标不动。）")
         if _dispatch_group_mode(disp) else
         ("（v2.25 · §15.9：流程推进靠**创始人确认**——你**在这个私聊里**回「继续 / 通过」"
          "我就地代你执行 opc go，游标前移、下一棒自己来找你；不用切到群里 @ 任何人。"
          "不满意就回「驳回」或「需补充」，我退回本阶段重做，游标不动。）")),
        "（本棒项目号是 " + str(project) + "；执行 opc go 时请带 --project "
        + str(project) + "，避免多项目时推进错。）",
        "有要补充的，直接在这里回我。",
    ]
    res = dispatch_mod.notify_next_role(
        str(owner), founder_id, title, lines,
        profile_root=Path(disp.get("profile_root") or (Path.home() / ".hermes" / "profiles")),
        app_id_env=str(disp.get("app_id_env") or "FEISHU_APP_ID"),
        app_secret_env=str(disp.get("app_secret_env") or "FEISHU_APP_SECRET"),
        chat_id=str(disp.get("chat_id") or ""),
        mode=str(disp.get("mode") or "dm"))
    if res.get("ok"):
        print("完成汇报私聊：" + str(res.get("detail", "")))
    else:
        print("完成汇报私聊未送达：" + str(res.get("detail", "")))
    return res


def _dispatch_group_mode(disp):
    """派活通知走群（@ 创始人）还是私聊。"""
    return str((disp or {}).get("mode") or "dm").lower() == "group"


def dispatch_founder_id(disp, role=None):
    """创始人用户 ID：环境变量 → 该角色 profile 的 .env。

    union_id 跨应用统一（open_id 按应用隔离，跨 bot 发私聊会 99991 open_id cross app），
    所以一个 union_id 配给所有角色即可。
    凭证/ID 只走环境变量与 .env，**不入库**（§15.11）。
    """
    var = str(disp.get("founder_id_env") or "FEISHU_FOUNDER_UNION_ID")
    val = (os.environ.get(var) or "").strip()
    if val:
        return val
    if role:
        try:
            import dispatch as dispatch_mod
            root = Path(disp.get("profile_root") or (Path.home() / ".hermes" / "profiles"))
            # 同 W4：配置里写 "~/.hermes/profiles"，Path() 不展开波浪号 → 读不到 .env。
            root = root.expanduser()
            return dispatch_mod.resolve_founder_id(str(role), var, root)
        except Exception:
            pass
    return str(disp.get("founder_id") or "").strip()


def _profile_env_value(role, key, profile_root=None):
    """读某个角色 profile 的 .env 里的某个键（只读，不回显凭证值以外的内容）。

    profile_root 解析顺序：配置里的 notify.dispatch.profile_root → ~/.hermes/profiles。
    """
    root = profile_root or (Path.home() / ".hermes" / "profiles")
    try:
        root = Path(str(root)).expanduser()
    except Exception:
        return ""
    env_path = root / str(role) / ".env"
    if not env_path.is_file():
        return ""
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _sep, value = line.partition("=")
            if name.strip() == key:
                return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def print_multibot_checklist(vault):
    """init 收尾：多 Bot 群内唤醒的配置自检（只读检查 + 明确的下一步）。

    为什么要有：v2.27 的群内唤醒依赖三处配置 + 一个群 ID + 每个角色的 allowlist。
    新用户装完若没配，`opc done` 照常跑（不阻塞），但**群里 @ 角色不会有任何反应**——
    表现为「派活发出去了，对面不吭声」，极难自查。这里一次性把缺口列出来。

    v2.28 新增 allowlist 检查（实测踩坑）：`FEISHU_ALLOWED_USERS` 匹配的是
    **该应用视角下的 open_id**（open_id 按应用隔离，每个职业应用各不相同），
    **不是 union_id**（union_id 跨应用统一，是派活私聊的收件人）。
    填错 → 网关日志出现 `Unauthorized user: ou_xxx ... on feishu`，
    群里 @ 角色永远不醒，且**没有任何界面提示**。故这里逐角色核对。
    """
    try:
        cfg = load_config(Path(vault))
    except Exception:
        return
    disp = (notify_conf(cfg).get("dispatch") or {})
    if not disp.get("enabled"):
        return
    mode = str(disp.get("mode") or "dm").lower()
    need = []
    if mode == "group" and not str(disp.get("chat_id") or "").strip():
        need.append("notify.dispatch.chat_id（群 ID：从飞书群链接取 oc_ 开头那段，"
                     "填进 Vault 的 opc.config.json）")
    if not str(disp.get("founder_id_env") or "").strip():
        need.append("notify.dispatch.founder_id_env（创始人 union_id 的环境变量名）")
    # v2.28：逐角色核对 allowlist（open_id 按应用隔离，填 union_id 无效）
    profile_root = disp.get("profile_root")
    missing_allow = []
    for role in role_bindings_ref(cfg):
        allow = _profile_env_value(role, "FEISHU_ALLOWED_USERS", profile_root)
        if not allow:
            missing_allow.append(role)
        elif allow.startswith("on_"):
            missing_allow.append(role + "（填的是 union_id，应为该应用视角的 open_id）")
    if missing_allow:
        need.append("各角色 profile 的 .env 里 FEISHU_ALLOWED_USERS 未配或填错："
                    + "、".join(missing_allow)
                    + "（每个职业应用各填各自的 open_id；Hermes 向导选「Only allow listed user IDs」会自动填对）")
    print()
    print("── 多 Bot 群内唤醒自检 ──")
    if mode != "group":
        print("  当前模式：私聊（mode=dm）。要改成群里 @ 唤醒，把 notify.dispatch.mode 设为 group。")
    if not need:
        print("  ✓ 配置齐了：角色会在群里 @ 创始人唤醒，交互时创始人 @ 该角色。")
    else:
        print("  还差：")
        for item in need:
            print("    · " + item)
        print("  另外每个角色的 .env 需要这三个开关（provision 会自动写）：")
        print("    FEISHU_ALLOW_BOTS=mentions / FEISHU_REQUIRE_MENTION=true / FEISHU_GROUP_POLICY=open")
    print("  改完 .env 会自动生效（约 30 秒）；适配器若已断连则需重启网关。")
    print("  验证：某个角色收到群消息 → 查该 profile 的 state.db，sessions 出现 chat_type=group。")
    print("  门铃（v2.21）：配好后，`opc new` / `opc go` / `opc advance` 会由**脚本自动在群里 @ 下一棒**")
    print("    把人叫醒（下一棒仍须自己 `opc claim` 领活）。chat_id 没配也不影响推进 ——")
    print("    只是你得自己在群里 @ 下一棒。**不需要创始人手动 @ 每一个岗位。**")
    print("  排障：若群里 @ 角色毫无反应且网关日志有 `Unauthorized user: ou_xxx ... on feishu`，")
    print("        就是 allowlist 没配对（ou_xxx 是该应用视角的你的 open_id，填进去即可）。")
    print()


def cmd_role(args):
    """单 Bot 模式：输出本次会话的身份声明提示词。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    role = args.role
    bindings = role_bindings_ref(cfg)
    if role not in bindings:
        raise ValueError("未登记的 role：" + role + "；当前 profile 可选：" + ", ".join(bindings.keys()))
    card = load_jobcard(vault, role)
    only, never = jobcard_scope(card)
    arts = list((bindings[role].get("artifacts") or {}).keys())
    try:
        uri = resolve_uri(cfg, vault, role, arts[0], "PXXX")[1] if arts else "（由配置解析）"
    except Exception:
        uri = "（由配置解析）"
    print("# 本次身份声明 · " + role + "（单 Bot 模式）")
    print("把下面这段粘贴到当前 Bot 的对话开头或系统提示词：\n")
    print("```")
    print("本次会话以 " + role + " 身份工作。")
    print("只负责：" + only)
    print("明确不负责：" + never)
    print("开工前必读：/OPC-架构规范.md、/99_Agent配置/岗位卡/" + role + ".md、"
          "/99_Agent配置/履历/" + role + ".md、/30_项目/_产出索引.md。")
    print("本次工作区主产出：" + uri + "；写文件前用 opc resolve 解析实际地址。")
    print("按 §3 定落点、按 §14 自检；不属于本岗位的任务一律拒绝并指明归属 role。")
    print("产出由 opc done 自动登记资产快照（版本 / 时间 / SHA256 / commit），禁止手工编辑 _资产快照.md（§15.10）。")
    print("禁止一次会话兼任两个职业；换角色请开新会话重新声明。")
    print("产出完成后执行 opc done --role " + role + " 报完成并通知创始人（附带资料用 --file 登记，不限文件格式）。")
    print("禁止自行通知、@ 或唤醒任何下游 Agent；下一棒由创始人确认后启动（§15.9）。")
    print("干完自己这一步后，可以把本岗这次产出登记为**待同步**（只写本地队列，"
          "不碰远程仓库）：opc done --sync-self；批次号与时间戳自动生成，作为增量版本号。")
    if role == "pm":
        print("你同时负责**推进项目进度**：创始人口头告知某阶段完成后，你执行 opc advance 推进到下一棒（只推进一棒，不跳步）。")
        print("人工闸门阶段的拍板权始终在创始人手上，闸门未拍板时 opc advance 会拒绝推进并提示创始人确认。")
        print("你同时负责**远程仓库写入（v2.10）**：其他职业**不直接写远程**——他们只把产出"
              "本地入队；**远程推送与项目状态推进统一由你执行**（opc advance 会顺带推送队列，"
              "或单独跑 opc sync --flush）。增量语义：每次产出推到远端一个**版本目录**，"
              "历史版本保留、互不覆盖。")
        print("你是**统管全部项目的统筹者**（创始人本人是「主理人」）：开工先跑 opc pm 看统筹台账（所有项目的进度 / 谁在等你推进 / 待推送队列 / 岗位是否冲突）。")
    print("```")
    print("\nagent_mode =", agent_mode_of(cfg), "（single 为默认，不要求宿主支持多 Bot；切换：opc mode <值>）")


def sync_conf(cfg):
    """云端仓库（飞书）同步配置（v2.6 / v2.7）。

    v2.7 口径：飞书 = **远程仓库**（可带项目管理能力：知识库沉淀、产品原型、各项目任务）。
    配置里的 `sync_mode` 决定同步行为：
      auto —— 初始化时配了云端仓库，之后每次产出落盘**直接同步，不再逐次问**
      ask  —— 每次问一次（默认，保守）
      off  —— 完全不碰云端
    """
    return ((cfg.get("backends") or {}).get("feishu") or {})


def sync_mode_of(cfg):
    """取同步模式，归一化为 auto / ask / off。"""
    conf = sync_conf(cfg)
    if not conf or not conf.get("enabled", True):
        return "off"
    mode = (conf.get("sync_mode") or "ask").strip().lower()
    if mode not in ("auto", "ask", "off"):
        return "ask"
    # auto 必须真的配了 token 才有意义，否则退回 ask（否则等于自动失败）
    if mode == "auto" and not (conf.get("folder_token") or "").strip():
        return "ask"
    return mode


def _strip_cli_prefix(blob):
    """剥离 lark-cli 输出的非 JSON 前缀行（如 `[page 1] fetching...`），返回纯 JSON 文本。

    联调实测（v2.10）：`drive files list --page-all` 会先打一行 `[page N] fetching...`，
    直接 json.loads 会失败 → 查已有工作区永远失败 → 每次都误建同名文件夹。
    """
    blob = (blob or "").strip()
    i = blob.find("{")
    if i >= 0:
        return blob[i:]
    return blob


def setup_cloud_repo(vault, cfg, args):
    """初始化时配置云端仓库（v2.7 · 交互按创始人口径修正 2026-11-15）。

    创始人明确（2026-11-14）：
      - **初始化时问一次**要不要配云端仓库。配了 → `sync_mode=auto`，之后每次产出
        落盘**直接同步，不再逐次问**；没配 → 保持 ask / off。
      - **工作区 = 飞书上的一个文件夹**（根目录）：「我指定一个工作区，可以直接说
        创建，或者在飞书上创建然后指定它；存在就用，不存在就创建一个」。

    交互修正（2026-11-15 创始人拍板）：
      - **不再让用户手填 token**（普通用户不知道 token 是什么）。
      - 改为：用户给**工作区名称**（或粘贴飞书文件夹链接），系统自动查/建并拿 token，
        token 全程无感。

    这一步完全可选：不配也完全能用（§15.12 降级，不阻塞）。
    """
    conf = sync_conf(cfg)
    if not conf:
        return cfg
    cli = (conf.get("cli") or "lark-cli").strip()
    cloud_repo = (getattr(args, "cloud_repo", None) or "ask").strip()
    workspace_arg = (getattr(args, "workspace", None) or "").strip()
    print()
    print("── 云端仓库（可选） ──")
    print("用途：把项目托管到一个远程仓库（如飞书），并可沉淀知识库、产品原型、各项目任务。")
    print("      本地照常写入、工作流不变；配了之后产出落盘会自动同步一份到云端。")
    if cloud_repo == "skip":
        print("已跳过云端仓库配置（--cloud-repo skip）。之后可执行 `opc sync` 手工同步。")
        return cfg
    if not interactive_enabled(args):
        # 非交互：只有显式 --cloud-repo auto 才继续（workspace 由 --workspace 提供）
        if cloud_repo != "auto":
            print("（非交互模式：未指定 --cloud-repo auto，跳过配置；之后可随时执行 opc sync 手工同步）")
            return cfg
        workspace = workspace_arg
    else:
        # 交互：问一次是否配置（--workspace 可预填，跳过后续询问）
        answer = prompt("是否配置云端仓库（当前后端：" + str(conf.get("label") or "飞书") + "）？输入 y 配置，其它键跳过：", "").strip()
        if answer.lower() not in ("y", "yes", "是", "配置"):
            print("已跳过云端仓库配置。之后可执行 `opc sync` 手工同步，或改 opc.config.json 的 backends.feishu。")
            return cfg
        workspace = workspace_arg
    print()
    print("开始配置云端仓库。")
    # 1) CLI 是否就绪
    if shutil.which(cli) is None:
        print("未检测到 " + cli + "，无法自动完成认证与建目录。")
        print("  请先安装并登录该 CLI；安装后重新执行 `opc sync init` 与认证命令即可。")
        print("  同步模式保持 ask（不会自动推），不影响使用。")
        return cfg
    # 2) 认证状态
    rc, out, err = run_external([cli, "auth", "status"])
    blob = (out or "") + (err or "")
    if rc != 0:
        print("云端仓库 CLI 尚未认证，暂时无法自动同步。")
        print("  请执行：" + cli + " config init --new（会输出验证链接，用浏览器打开完成授权）")
        print("  同步模式保持 ask（不会自动推），不影响使用。")
        return cfg
    # 3) 工作区：用户给名称（或链接），系统自动查/建（2026-11-15 修正，不再手填 token）
    #    判断「是否已配置」看 folder_token（模板里的 project_prefix 默认值 OPC 不算数）
    existing_token = (conf.get("folder_token") or "").strip()
    # --workspace 参数优先；其次回落到配置里的 project_prefix（默认 OPC 不算数）
    if not workspace:
        workspace = (conf.get("project_prefix") or "").strip()
    if existing_token and workspace and workspace != "OPC":
        print("  已配置工作区：「" + workspace + "」（folder_token 已有值），跳过重新指定。")
        print("  想换工作区：改 opc.config.json 的 backends.feishu.folder_token / project_prefix。")
        return cfg
    if not workspace or workspace == "OPC":
        # 非交互 + auto 但没给 --workspace：无法继续，跳过
        if not interactive_enabled(args):
            print("  非交互模式未提供 --workspace，跳过配置。")
            return cfg
        print()
        print("请指定云端工作区（飞书上的一个文件夹，作为工作区根目录）：")
        print("  · 直接输入工作区名称（不存在会自动创建）")
        print("  · 或粘贴飞书文件夹链接（如 https://xxx.feishu.cn/drive/folder/<token>）")
        workspace = prompt("工作区名称 / 链接：", "").strip()
    if not workspace or workspace == "OPC":
        print("未提供工作区，同步模式保持 ask（不会自动推），不影响使用。")
        return cfg
    # 解析：链接 → 提取 token 与名称
    folder_token = ""
    m = re.search(r"(?:drive/folder/|folder/)([A-Za-z0-9]+)", workspace)
    if m:
        # 用户粘贴的是链接：用链接里的 token 作为工作区（父文件夹即工作区本身）
        folder_token = m.group(1)
        workspace = ""
        print("  已解析飞书文件夹链接 ✓")
    # 4) 在工作区根（无 folder_token 时是飞书根目录）查/建同名文件夹
    ident = ["--as", (conf.get("identity") or "user").strip()] if (conf.get("identity") or "").strip() else []
    root = folder_token  # 链接模式：工作区 = 链接指向的文件夹本身
    if not root:
        # 名称模式：在飞书根目录查/建「工作区名」文件夹
        # 注意：**不要用 --page-all**——联调实测它会输出多个 JSON 拼接，
        # json.loads 报 Extra data → 查已有工作区永远失败 → 每次误建同名文件夹。
        rc, out, err = run_external([cli, "drive", "files", "list",
                                     "--format", "json"] + ident)
        if rc != 0:
            print("  查询飞书目录失败：" + ((err or out).strip()[:200]))
            print("  同步模式保持 ask（不会自动推），不影响使用。")
            return cfg
        found = ""
        try:
            data = json.loads(_strip_cli_prefix((out or "") + (err or "")))
        except Exception:
            data = {}
        files = (data.get("data") or {}).get("files") or data.get("files") or []
        for it in files:
            if it.get("name") == workspace and it.get("token") and it.get("type") == "folder":
                found = it.get("token")
                break
        if found:
            folder_token = found
            print("  ✓ 已找到现有工作区「" + workspace + "」，使用它")
        else:
            rc, out, err = run_external([cli, "drive", "+create-folder", "--name", workspace,
                                         "--format", "json"] + ident)
            if rc != 0:
                print("  创建工作区失败：" + ((err or out).strip()[:200]))
                print("  同步模式保持 ask（不会自动推），不影响使用。")
                return cfg
            try:
                data = json.loads(_strip_cli_prefix((out or "") + (err or "")))
            except Exception:
                data = {}
            node = data.get("data") or data
            folder_token = node.get("token") or node.get("folder_token") or ""
            if not folder_token:
                print("  已创建但未取到 token，请手动在飞书确认工作区：「" + workspace + "」")
                print("  同步模式保持 ask（不会自动推），不影响使用。")
                return cfg
            print("  ✓ 已创建新工作区「" + workspace + "」")
    if not folder_token:
        print("  未能定位工作区，同步模式保持 ask（不会自动推），不影响使用。")
        return cfg
    conf["folder_token"] = folder_token
    conf["project_prefix"] = workspace
    conf["sync_mode"] = "auto"
    conf["enabled"] = True
    # 写回配置
    try:
        cfg.setdefault("backends", {})["feishu"] = conf
        save_config(vault, cfg)
    except Exception as exc:
        print("写配置失败（不影响本地使用）：" + str(exc))
        return cfg
    print("云端仓库已配置 ✓")
    print("  工作区：" + (workspace or "（链接指向的文件夹）"))
    print("  之后每次产出落盘会**自动同步一份到云端**，不再逐次询问。")
    print("  想改行为：opc.config.json 的 backends.feishu.sync_mode（auto / ask / off）。")
    return cfg


def run_external(argv, timeout=120):
    """执行外部命令，返回 (rc, stdout, stderr)。

    Windows 注意：.CMD / .bat 批处理文件不能被 subprocess 直接 CreateProcess，
    必须经由 shell 才会跑起来（否则报 WinError 2）。这里统一 shell=True + 引号包裹。
    """
    quoted = []
    for a in argv:
        a = str(a)
        if os.name == "nt":
            quoted.append('"' + a.replace('"', '\\"') + '"')
        else:
            quoted.append("'" + a.replace("'", "'\\''") + "'")
    try:
        proc = subprocess.run(" ".join(quoted), shell=True, capture_output=True,
                              text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时"
    except OSError as exc:
        return 126, "", str(exc)
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def cmd_advance(args):
    """pm 推进项目进度（v2.8）。

    创始人明确：**产品经理就是干「推进进度」这活的**。
    现实流程是：创始人写完代码、跑通测试 → 口头告诉产品经理「这个完成了」→
    产品经理去项目管理里把进度推进、启动下一棒。

    本命令把这个动作固化成一条命令：
    - 检查当前交接状态是否为「待确认」；
    - **只推进一棒**，不跳步（§15.9 一阶段一通知、禁止跨步）；
    - 人工闸门阶段（gates）**必须由创始人给结论**，pm 不能自己拍板；
    - 推进后打印下一棒的角色与必读文件。

    与 `opc go` 的关系：`opc go` 是创始人直接确认；本命令是**产品经理**在收到
    创始人口头通知后代为执行同一动作（现实里 PM 推进度就是这么干的）。
    两者推进语义完全一致，不新增机制、不改状态机。

    **v2.10：远程写入权归 pm 独有**——创始人明确
    「他呢，他就不能去改远程飞书的东西的这些状态的同步，统一交给 PM 去做……
      凡是涉及到这种远程仓库的更新和写入，包括推进项目状态的话，都由 pm 去做。
      ……就做一个增量同步吧。」
    因此本命令在推进的同时，**统一把待推送队列增量推到远端**
    （各岗已本地入队；每次产出落一个版本目录，历史版本保留）。
    各岗**不直接写远程仓库**（§15.16）。
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    hint = read_handoff(vault, cfg)
    project = active_project_of(args, cfg, hint, vault)
    state = read_handoff(vault, cfg, project)
    if not state or state.get("status") != "待确认":
        raise ValueError("项目 " + project + " 没有待确认的交接；"
                         "请等本阶段 Agent 报完成（opc done）后再推进。")
    token = getattr(args, "token", None) or state.get("token")
    if token != state.get("token"):
        raise ValueError("回执不匹配，拒绝推进（防止跨步或重复确认）")
    i = chain_pos(project_chain(vault, cfg, project), state)
    gate = state.get("gate")
    if gate:
        # 闸门是创始人的拍板权，产品经理不能代拍。
        # 「等创始人拍板」必须**落盘 + 推送**，不能只打印一句就算数（D4-3）：
        # 否则错过这一次通知就再没人提醒；而 pm 台账靠 state.gate 判定，
        # 落盘后「待拍板」才是真状态，才进得了台账的「待创始人拍闸门」清单。
        proj = str(state.get("project") or project)
        conf = notify_conf(cfg)
        channel = getattr(args, "channel", None) or conf.get("channel", "local")
        notice = "\n".join([
            "【OPC 人工闸门待拍板】项目 " + proj + " · 阶段 " + str(i)
            + "（" + str(state.get("role", "")) + "）",
            "该阶段是人工闸门，需创始人给结论：通过 / 驳回 / 需补充。",
            "拍板命令：opc go --project " + proj + " --decision 通过",
            "（拍板权始终在创始人手上，§7；pm 不得代拍）",
        ])
        used, msg = push_notice(vault, cfg, channel, notice, proj, state.get("role"))
        state.update({"gate": True, "gate_stage": i,
                      "gate_wait_at": datetime.now().isoformat(timespec="seconds"),
                      "gate_notified_channel": used})
        write_handoff(vault, cfg, state)
        print("阶段 " + str(i) + " 是**人工闸门**，需创始人给结论：通过 / 驳回 / 需补充。")
        print("  创始人确认后执行：opc go --decision 通过")
        print("  （产品经理推进的是「进度」，闸门拍板权始终在创始人手上，§7）")
        print("  已落盘「待拍板」（gate_stage=" + str(i) + "）并推送通知：" + used + " · " + msg)
        return 2
    print("产品经理推进进度：" + str(state.get("project")) + " 阶段 "
          + str(state.get("stage", i)) + "（" + str(state.get("role", "")) + "）已确认完成。")
    # v2.20（创始人拍板「两码事」）：推进时**不再自动推文件队列**——
    #   进度线 → 多维表格台账（base_progress），每次推进即同步，设计师/工程师
    #   干活中也能在表里跟进；
    #   资产线 → 文件队列留在本地，**整个需求版本结束后**统一 `opc sync --flush`。
    flush_result = 0
    print("资产线：文件队列留在本地（本次不推送）；整个需求版本结束后统一执行 opc sync --flush。")
    # v2.14：同上 —— 推进前把上一阶段的拍板结论归档进 `decisions` 历史并清空顶层。
    archive_decision(state)
    next_role = state.get("next_role") or ""
    if next_role:
        chain2 = project_chain(vault, cfg, project)
        dispatch = project_roles(vault, cfg, project)
        n_i = next_dispatch_index(chain2, dispatch, i)
        if n_i < 0:
            n_i = i + 1
        n_idx2, next_doc2, next_role2 = chain2[n_i]
        skipped = skipped_roles_between(cfg, state.get("stage", i), n_idx2, dispatch)
        print("下一棒：阶段 " + str(n_idx2) + " · " + next_role2
              + " · 必读 " + str(next_doc2) + "\n")
        if skipped:
            print("（本次未派活，自动跳过：" + "、".join(skipped) + "）")
        cmd_role(argparse.Namespace(vault=args.vault, role=next_role2))
        dispatch_next_role_notice(vault, cfg, project, next_role2, next_doc2, n_idx2, skipped)
        # v2.28（2026-11-20 实测修）：同上 —— 推进后必须给下一棒发门铃把它叫醒。
        wake_next_role(vault, cfg, project, next_role2, next_doc2, n_idx2, skipped)
        state.update({"step": n_i, "stage": n_idx2, "role": next_role2,
                      "doc": next_doc2, "status": "进行中", "token": "",
                      "next_role": "", "next_step": -1, "next_doc": "", "gate": False,
                      "confirmed_at": datetime.now().isoformat(timespec="seconds"),
                      "advanced_by": "pm",
                      "pushed_at": datetime.now().isoformat(timespec="seconds"),
                      "pushed_by": "pm",
                      "pushed_scope": "queue",
                      "pushed_count": flush_result,
                      "pushed_stage": state.get("stage", i)})
    else:
        state.update({"status": "已完成", "token": "",
                      "confirmed_at": datetime.now().isoformat(timespec="seconds"),
                      "advanced_by": "pm",
                      "pushed_at": datetime.now().isoformat(timespec="seconds"),
                      "pushed_by": "pm",
                      "pushed_scope": "queue",
                      "pushed_count": flush_result,
                      "pushed_stage": state.get("stage", i)})
        print("本次派活的岗位已全部完成，按 §15.6 追加各职业简历并登记复盘。")
    write_handoff(vault, cfg, state)
    # v2.20 进度线：多维表格台账——上一棒已交付，下一棒可开工（或项目已完成）
    updates = [{"stage": i, "role": str(state.get("role", "")), "progress": "已交付",
                "decision": "通过",
                "title": project_dir_name(vault, project) + " 阶段" + str(i)
                + " · " + str(state.get("role", ""))}]
    if state.get("status") != "已完成":
        updates.append({"stage": int(state.get("stage", 0)), "role": str(state.get("role", "")),
                        "progress": "可开工", "doc": str(state.get("doc", "")), "claim": True,
                        "title": project_dir_name(vault, project) + " 阶段" + str(state.get("stage", 0))
                        + " · " + str(state.get("role", ""))})
    base_progress_push(vault, cfg, project, updates, version=str(state.get("round") or "V1"))
    return 0


def role_scope_files(vault, cfg, project, role):
    """算某角色**自己该同步**的文件清单（v2.9 范围隔离 → v2.10 增量入队）。

    创始人口径（v2.10 修正）：「他呢，他就不能去改远程飞书的东西的这些状态的同步，
    统一交给 PM 去做……后续的这些其他职业的话，它只是每一次产出产出的新的一些资料，
    就是做一个同步就好了……就做一个增量同步吧。」

    因此本函数只**算范围**（谁产出谁负责），**不接触远程**：
      ① 自己 owner 的阶段主产出（该阶段文档）＋ 自己产出过的配套资产

    联调确认（v2.10 修）：**不再混入职业简历与知识资产**——它们是工作区级
    团队档案（「Agent 配置是公司团队档案，不属于任何单个项目」），
    项目版本目录只放本项目自己的资料。

    远端落哪、什么时候推，由 pm 统一执行（§15.16）。
    """
    out = []
    pdir = vault / "30_项目" / project_dir_name(vault, project)
    if not pdir.is_dir():
        return out
    # ① 该角色在产出索引里登记过的资产（最准：谁产出谁负责）
    for row in read_index(vault, cfg):
        if project_key(row.get("project")) != project_key(project):
            continue
        if (row.get("role") or "").strip() != role:
            continue
        uri = (row.get("物理地址 URI") or "").strip()
        if not uri:
            continue
        # 索引里的 URI 形如 `local://30_项目/<项目>/<版本目录>/<role>/0_约束卡.md`，
        # 要剥掉协议头再当路径用（§15.8 地址绑定）
        for pref in ("local://", "file://"):
            if uri.startswith(pref):
                uri = uri[len(pref):]
                break
        try:
            fp = Path(uri)
            if not fp.is_absolute():
                fp = Path(vault) / uri
            if fp.exists() and fp.is_file():
                out.append(fp)
        except (OSError, ValueError):
            continue
    # 去重 + 保序
    seen = set()
    uniq = []
    for fp in out:
        try:
            key = str(fp.resolve())
        except OSError:
            key = str(fp)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(fp)
    return uniq


def _role_owns_domain(role, domain_name):
    """判断某岗位是否拥有某个知识资产 domain（按目录名含岗位名或岗位码）。"""
    return role in domain_name


# ---------------------------------------------------------------------------
# §15.16 增量同步队列：各岗本地入队，PM 统一推送（v2.10）
# ---------------------------------------------------------------------------

QUEUE_DIR_NAME = "_同步队列"


def queue_dir(vault):
    """同步队列目录：本地 Vault 内，是「待推送」的唯一台账。"""
    return Path(vault) / "30_项目" / QUEUE_DIR_NAME


def queue_file(vault, item_or_batch, project="", role=""):
    """队列文件路径：`<批次>-<项目>-<岗位>.json`。

    v2.20 修（撞号 bug）：批次号 `YYYY-MM-DD-V<n>` 是**项目内**计数，跨项目必撞
    ——只按批次命名会让两个项目的入队互相覆盖、状态回写错位（实测：
    P002 design 与 P003 pm 同为 `2026-11-18-V1`，后者入队覆盖前者）。
    文件名因此带上项目号与岗位；`read_queue` 仍 glob 全部 `*.json`，
    老命名文件继续可读（存量不迁移也能用）。
    """
    if isinstance(item_or_batch, dict):
        batch = item_or_batch.get("batch") or ""
        project = item_or_batch.get("project") or ""
        role = item_or_batch.get("role") or ""
    else:
        batch = item_or_batch

    def _seg(s):
        return re.sub(r"[^0-9A-Za-z_-]+", "_", str(s)).strip("_")

    name = batch
    if project:
        name += "-" + _seg(project)
    if role:
        name += "-" + _seg(role)
    return queue_dir(vault) / (name + ".json")


def read_queue(vault):
    """读全部待推送队列条目（按入队时间排序）。"""
    d = queue_dir(vault)
    if not d.is_dir():
        return []
    out = []
    for fp in sorted(d.glob("*.json")):
        try:
            item = json.loads(read_text(fp))
        except (ValueError, OSError):
            continue
        if item.get("batch"):
            out.append(item)
    out.sort(key=lambda x: (x.get("queued_at") or "", x.get("batch") or ""))
    return out


def write_queue(vault, item):
    d = queue_dir(vault)
    d.mkdir(parents=True, exist_ok=True)
    write_text(queue_file(vault, item),
               json.dumps(item, ensure_ascii=False, indent=2) + "\n")


def drop_queue(vault, item_or_batch, project="", role=""):
    try:
        queue_file(vault, item_or_batch, project, role).unlink()
    except OSError:
        pass


def next_version_no(vault, project):
    """算下一个版本号：`YYYY-MM-DD-V{n}`，项目内同天 V1→V2→V3 递增。

    联调确认（v2.10 修）：不要 `S-时间戳-随机码`，用户要「今天某年某月某日
    第 V1 版本、V2 版本、V3 版本」这样的日期版本号。
    跨天后第一个版本从 V1 重新计（新的一天新起点）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    mx = 0
    d = queue_dir(vault)
    if d.is_dir():
        for fp in d.glob("*.json"):
            try:
                item = json.loads(read_text(fp))
            except (ValueError, OSError):
                continue
            b = item.get("batch") or ""
            if project_key(item.get("project")) == project_key(project) and b.startswith(today + "-V"):
                m = re.match(r"^\d{4}-\d{2}-\d{2}-V(\d+)$", b)
                if m:
                    mx = max(mx, int(m.group(1)))
    return today + "-V" + str(mx + 1)


def enqueue_sync(vault, cfg, project, role, files, batch=None):
    """**本地入队**（v2.10）：登记本岗本次产出，等 pm 统一推送。

    这是各岗唯一能碰「同步」的动作——**只写本地队列，不连远程**。
    创始人口径：「他就不能去改远程飞书的东西的这些状态的同步，统一交给 PM 去做。」
    「就做一个增量同步吧」——每次产出入队一条，带**版本号（日期-Vn）+ 时间戳**，
    远端按版本目录落，历史版本全部保留。

    返回队列条目 dict。
    """
    batch = batch or next_version_no(vault, project)
    items = []
    for fp in files:
        try:
            rel = str(fp.relative_to(Path(vault))).replace("\\", "/")
        except ValueError:
            rel = fp.name
        try:
            sha, size = file_fingerprint(fp)
        except OSError:
            sha, size = "", 0
        items.append({
            "path": rel,
            "sha256": sha,
            "bytes": size,
            "version": declared_version(fp) or "",
            "time": datetime.fromtimestamp(fp.stat().st_mtime).isoformat(timespec="seconds"),
        })
    if not items:
        return None
    item = {
        "batch": batch,
        "project": project,
        "role": role,
        "project_dir": project_dir_name(vault, project),  # 远端项目文件夹名（联调 v2.10 修）
        "queued_at": datetime.now().isoformat(timespec="seconds"),
        "status": "待推送",
        "remote_version": batch,   # 远端版本目录名 = 版本号（YYYY-MM-DD-Vn）
        "assets": items,
        "pushed_at": "",
        "pushed_by": "",
    }
    write_queue(vault, item)
    return item


def stage_queue_item(vault, item):
    """把队列条目暂存成目录，结构 = `<项目文件夹名>/<版本目录>/<相对路径>`。

    远端结构 = `工作区(prefix)/<项目文件夹名>/<版本目录>/<相对路径>`。
    版本目录名 = `YYYY-MM-DD-V{n}`（日期版本号，同天 V1→V2→V3 递增），
    历史版本目录并列保留 → **增量**：不覆盖、能看到每次产出的演进。

    联调确认（v2.10 修）：不要岗位层、不要 S-随机码批次。
    用户口径：「工作区下面有很多项目，每个项目就是一个文件夹……
    一次迭代就写一个版本，按日期版本就好，某年某月某日第 V1/V2/V3 版本」。
    Agent 配置（岗位卡/简历）是工作区级团队档案，不进项目目录。

    **项目文件夹 = 本地 `30_项目/<项目目录>` 那一层**：资产路径带 `30_项目/…` 前缀的，
    在这里剥掉，让版本目录里保留项目自己的层级（v2.19 起为
    `YYYY-MM-DD-V<N>/<role>/…`，存量老项目仍是 `V<N>/<role>/…`；
    v2.20 起约束卡 / MRD 也落 `<版本目录>/<owner>/`，存量项目根平铺副本照常能推）。

    ⚠️ **批次号与本地版本号的关系（v2.20 撤销批次层）**：批次号只留在本地队列
    台账（文件名 + batch 字段）作登记用；远端路径 = `工作区/<项目>/<本地版本目录>/<role>/<文件>`，
    **与本地目录结构一致**（一次需求 = 一个版本 = 云端一个版本目录，创始人拍板）。
    非项目文件（如简历）不入队（见 role_scope_files / cmd_done 的 fresh 范围）。

    **为什么把两层直接铺进暂存目录**：底层 `drive +push` 是「本地目录 → Drive 文件夹」的
    目录结构镜像（会自动 create_folder 补出嵌套层级），所以只要本地这边层级对，
    远端层级就一定对。
    """
    import shutil as _sh
    import tempfile as _tf
    tmp = Path(_tf.mkdtemp(prefix="opc_sync_"))
    project = item.get("project") or ""
    batch = item.get("batch") or ""
    # 项目文件夹名 = 本地 `30_项目/` 下的目录名（如 `P001_联调项目`），远端与本地一致。
    pdir = item.get("project_dir") or project_dir_name(vault, project)
    # 剥掉资产路径里的 `30_项目/<项目目录>/` 前缀（联调 v2.10 修）
    strip = "30_项目/" + project_dir_name(vault, project) + "/"
    try:
        for a in item.get("assets", []):
            src = Path(vault) / a["path"]
            if not src.exists() or not src.is_file():
                continue
            rel = a["path"]
            if rel.startswith(strip):
                rel = rel[len(strip):]
            dst = tmp / pdir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _sh.copy2(str(src), str(dst))
    except OSError as exc:
        _sh.rmtree(tmp, ignore_errors=True)
        print("暂存队列条目失败：" + str(exc))
        return None
    return tmp


def queue_remote_prefix(prefix, item):
    """远端版本目录：`<工作区>/<项目文件夹名>/<本地版本目录>`。

    v2.20 修（创始人拍板，撤销 ADR-0007 的批次层）：一次需求 = 一个版本，
    云端目录 = 本地目录镜像——远端不再套「推送批次」层。
    批次号仍保留在队列台账里（文件名 + batch 字段），只用于本地登记，不进远端路径。
    """
    pdir = item.get("project_dir") or item.get("project") or ""
    seg = pdir
    return (prefix + "/" + seg) if prefix else seg


def push_queue_item(vault, cfg, item, args=None, dry_run=False):
    """把一条队列条目推到远端（**只有 pm 走这条路径**）。

    增量语义：远端落 `<工作区>/<项目文件夹名>/<版本目录>/<相对路径>`，
    新版本目录 = `YYYY-MM-DD-V{n}`，**不覆盖历史版本**。
    """
    conf = sync_conf(cfg)
    cli = (conf.get("cli") or "lark-cli").strip()
    prefix = (conf.get("project_prefix") or "").strip()
    folder_token = (conf.get("folder_token") or "").strip()
    if args is not None:
        if getattr(args, "folder_token", None):
            folder_token = args.folder_token
        if getattr(args, "prefix", None):
            prefix = args.prefix
    if not folder_token:
        print("错误：未配置云端根文件夹 token（见 opc sync init）。")
        return 2
    batch = item.get("batch") or ""
    role = item.get("role") or ""
    project = item.get("project") or ""
    tmp = stage_queue_item(vault, item)
    if tmp is None:
        return 2
    remote_prefix = queue_remote_prefix(prefix, item)
    print("增量推送：" + str(batch) + "（" + str(role) + " · " + str(project) + "）")
    print("  本岗资产：" + str(len(item.get("assets", []))) + " 份")
    print("  远端版本目录：" + remote_prefix + "/")
    print("  增量语义：新版本目录 = 历史版本保留，不覆盖。")
    ns = argparse.Namespace(vault=str(vault), action="push",
                            folder_token=folder_token, prefix=None,
                            allow_restore=False, dry_run=dry_run,
                            query=None, limit=0, queue=False, flush=False)
    # folder_token 本身就是工作区根；项目文件夹/版本目录两层由暂存目录结构带上去（+push 是目录镜像）
    argv_prefix = prefix
    try:
        rc = sync_bridge("push", vault, cli, argv_prefix, folder_token, ns, local_dir=tmp)
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)
    if rc == 0:
        item = dict(item)
        item["status"] = "已推送"
        item["pushed_at"] = datetime.now().isoformat(timespec="seconds")
        item["pushed_by"] = "pm"
        item["remote_path"] = remote_prefix
        write_queue(vault, item)
    return rc


def stage_role_files(vault, files, rel_root):
    """把某角色的文件**按原相对路径**暂存到临时目录，供按角色推送到远端。

    远端结构 = `<rel_root>/<项目号>/<role>/<相对路径>`，
    每个角色一个目录 → 多职业并行同步不会互相覆盖（v2.10 增量版本目录）。
    """
    import shutil as _sh
    import tempfile as _tf
    tmp = Path(_tf.mkdtemp(prefix="opc_sync_"))
    try:
        for fp in files:
            try:
                rel = fp.relative_to(Path(vault))
            except ValueError:
                rel = Path(fp.name)
            dst = tmp / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _sh.copy2(str(fp), str(dst))
    except OSError as exc:
        _sh.rmtree(tmp, ignore_errors=True)
        print("暂存本岗文件失败：" + str(exc))
        return None
    return tmp


def sync_self_scope(vault, cfg, project, role, args, files=None):
    """本岗**本地入队**（v2.10）——不连远程，只登记待推送。

    创始人口径（v2.10 修正）：
    「他呢，他就不能去改远程飞书的东西的这些状态的同步，统一交给 PM 去做。
      ……后续的这些其他职业的话，它只是每一次产出产出的新的一些资料，
      就是做一个同步就好了……每一次需求给到来都是一个时间戳，或者说一个版本号吧，
      这样子的话，我只要更新到新的版本号就行了呀，就推到新的一个时间戳或者说版本号
      同步上去就好了呀，就做一个增量同步吧。」

    所以本岗的「同步」= **写一条本地队列**（批次号 + 时间戳 + SHA256 + 版本号），
    **不碰远程仓库**。远程推送由 pm 统一执行（`opc advance` / `opc sync --flush`）。

    **增量语义**：`files` 传本次刚产出的资产（主产出 + 附带资料 + 本岗简历），
    不重推本岗历史产出——每次产出只推一个新的版本目录。

    入队失败**只提示、不阻塞**流程推进（§15.12）。
    """
    conf = sync_conf(cfg)
    mode = sync_mode_of(cfg)
    if files is None:
        files = role_scope_files(vault, cfg, project, role)
    if not files:
        print()
        print("── 增量同步入队 ──")
        print("本岗暂无需要同步的文件（本次产出未登记）。")
        print("  本地产出已安全落盘；不影响流程推进。")
        return
    item = enqueue_sync(vault, cfg, project, role, files)
    if item is None:
        print()
        print("── 增量同步入队 ──")
        print("入队失败（本岗无文件）；本地产出已安全落盘。")
        return
    print()
    print("── 增量同步入队（本地） ──")
    print("版本：" + item["batch"] + "（版本号 / 时间戳：" + item["queued_at"] + "）")
    print("本岗资产：" + str(len(item["assets"])) + " 份 → 待推送")
    for fp in files[:12]:
        try:
            print("  · " + str(fp.relative_to(Path(vault))))
        except ValueError:
            print("  · " + str(fp))
    if len(files) > 12:
        print("  · …… 另有 " + str(len(files) - 12) + " 份")
    item["project_dir"] = project_dir_name(vault, project)
    print("  远端版本目录（由 pm 推送时创建）："
          + queue_remote_prefix((conf.get("project_prefix") or ""), item) + "/")
    print("  说明：本岗**不直接写远程仓库**；等 pm 执行 opc advance / opc sync --flush 时推送。")
    if mode == "off":
        print("  提示：sync_mode=off，远程仓库未启用——本条**仅本地留存**，"
              "启用远程后由 pm 推送（不影响流程推进）。")
    print("  本地 Vault 始终是唯一真相源；队列在 30_项目/_同步队列/ 下，可随时查看。")


def project_of_queue(vault, items=None):
    """队列里若有且只有一个项目号，返回它；否则返回空串。"""
    items = items if items is not None else read_queue(vault)
    ps = sorted({i.get("project", "") for i in items if i.get("project")})
    return ps[0] if len(ps) == 1 else ""


def cmd_sync_queue(vault, args):
    """查看增量同步队列（**只读**，不碰远程）。"""
    items = read_queue(vault)
    print("增量同步队列（本地台账）——共 " + str(len(items)) + " 条")
    print("")
    print("  " + "批次".ljust(26) + "项目".ljust(8) + "岗位".ljust(12)
          + "状态".ljust(10) + "资产")
    for it in items:
        print("  " + str(it.get("batch", "")).ljust(16)
              + str(it.get("project", "")).ljust(8)
              + str(it.get("role", "")).ljust(12)
              + str(it.get("status", "")).ljust(10)
              + str(len(it.get("assets", []))) + " 份")
    print("")
    print("推送命令（pm 专用）：opc sync --flush")
    print("  增量语义：每个版本目录（YYYY-MM-DD-Vn）推到远端，历史版本保留、互不覆盖。")
    print("  本地 Vault 始终是唯一真相源（§15.8）。")
    return 0


def cmd_sync_enqueue_role(vault, cfg, args, prefix, role):
    """`opc sync --role <本岗>`：**只入队，不推远程**（v2.10）。

    各岗不直接写远程仓库；本命令等价于「登记本岗待同步」。
    纯本地记账，**不需要 folder_token**，也不调用任何外部 CLI。
    实际推送由 pm 统一执行（opc advance / opc sync --flush）。
    """
    project = active_project_of(args, cfg, read_handoff(vault, cfg), vault)
    files = role_scope_files(vault, cfg, project, role)
    if not files:
        print("本岗暂无需要同步的文件（产出索引里没有本岗资产、简历尚未建立）。")
        print("  本地产出已安全落盘；不影响流程推进。")
        return 0
    item = enqueue_sync(vault, cfg, project, role, files)
    if item is None:
        print("入队失败（本岗无文件）；本地产出已安全落盘。")
        return 0
    print("增量同步入队（本地，不直接写远程仓库）")
    print("  版本：" + item["batch"] + "（版本号 / 时间戳：" + item["queued_at"] + "）")
    print("  本岗资产：" + str(len(item["assets"])) + " 份 → 待推送")
    print("  远端版本目录（由 pm 推送时创建）："
          + queue_remote_prefix(prefix, item) + "/")
    print("  推送命令（pm）：opc sync --flush")
    print("  说明：本岗**不直接写远程仓库**；等 pm 执行 opc advance / opc sync --flush 时推送。")
    print("  本地 Vault 始终是唯一真相源；队列在 30_项目/_同步队列/ 下，可随时查看。")
    return 0


def cmd_sync_flush(vault, cfg, args):
    """**pm 统一推送队列**（v2.10 增量同步）。

    创始人口径：「凡是涉及到这种远程仓库的更新和写入，包括推进项目状态的话，
    都由 pm 去做……每一次需求给到来都是一个时间戳，或者说一个版本号吧，
    这样子的话，我只要更新到新的版本号就行了呀，就推到新的一个时间戳或者说版本号
    同步上去就好了呀，就做一个增量同步吧。」

    本命令把**待推送队列**逐条推到远端：
      远端路径 = `<prefix>/<项目号>/<role>/<批次号>/<相对路径>`
      每个批次 = 一个**版本目录** → 增量，历史版本不覆盖，能看到演进轨迹。

    推送失败只提示、不阻塞（§15.12）；失败的条目留在队列里等下次重试。
    """
    conf = sync_conf(cfg)
    mode = sync_mode_of(cfg)
    if mode == "off":
        print("sync_mode=off，不碰远程仓库。")
        return 0
    items = read_queue(vault)
    proj = (getattr(args, "project", None) or "").strip()
    if proj:
        items = [i for i in items if project_key(i.get("project")) == project_key(proj)]
    pending = [i for i in items if i.get("status") != "已推送"]
    if not pending:
        print("待推送队列为空" + ("（项目 " + proj + "）" if proj else "") + "，无需推送。")
        return 0
    print("增量推送（pm 统一执行）——待推 " + str(len(pending)) + " 条")
    ok = 0
    failed = []
    for it in pending:
        print("")
        rc = push_queue_item(vault, cfg, it, args=args,
                             dry_run=bool(getattr(args, "dry_run", False)))
        if rc == 0:
            ok += 1
        else:
            failed.append(it.get("batch"))
    print("")
    print("推送完成：成功 " + str(ok) + " 条"
          + ("，失败 " + str(len(failed)) + " 条（留在队列里，下次重试）" if failed else ""))
    print("增量语义：远端按版本目录（YYYY-MM-DD-Vn）落，历史版本保留、互不覆盖。")
    return 0 if not failed else 1


def flush_sync_queue(vault, cfg, project, args=None):
    """pm 推进时统一推送队列（v2.10）。

    与 `cmd_sync_flush` 的区别：这里**只推当前项目**的待推送条目，
    且**失败不阻塞推进**（§15.12），只提示。返回推送条数。
    """
    mode = sync_mode_of(cfg)
    if mode == "off":
        return 0
    items = [i for i in read_queue(vault)
             if project_key(i.get("project")) == project_key(project) and i.get("status") != "已推送"]
    if not items:
        print("增量同步：本项目无待推送条目（各岗产出已本地入队，等有产出时自动带上）。")
        return 0
    print("")
    print("── 增量同步（pm 统一推送） ──")
    print("待推 " + str(len(items)) + " 条 → 远端按版本目录（YYYY-MM-DD-Vn）落，历史版本保留。")
    ok = 0
    failed = []
    for it in items:
        rc = push_queue_item(vault, cfg, it, args=args)
        if rc == 0:
            ok += 1
        else:
            failed.append(it.get("batch"))
    print("")
    print("增量推送完成：成功 " + str(ok) + " 条"
          + ("，失败 " + str(len(failed)) + " 条（留在队列里，不阻塞推进）" if failed else ""))
    return ok


def cmd_sync(args):
    """云端仓库（远程仓库）读写（v2.7 → v2.9 → **v2.10 增量同步**）。

    本地照常写入、工作流不变；本命令是在本地写完之后，
    额外把同一份产出同步一份到云端，并支持从云端**读**：
      push   —— 本地 → 云端（默认）
      status —— 比对本地与云端差异
      init   —— 建远端目录，回显 token
      pull   —— 云端 → 本地（**需显式 --allow-restore**）
      tasks  —— 拉云端各项目的未完成任务 / 需求（只读，不改本地）
      search —— 搜云端沉淀的知识库 / 产品原型（只读，不改本地）

    **v2.10 增量同步 + 远程写入权归 pm**：
      --queue —— 只看待推送队列（只读，不碰远程）
      --flush —— **pm 专用**：把待推送队列逐条推到远端，每次产出一个**版本目录**
                  （`OPC/<项目号>/<role>/<批次号>/`），历史版本全部保留、互不覆盖。
      不带这两个开关 → 维持 v2.9 行为（整目录镜像）。

    覆盖本地一律需要显式授权；本地 Vault 始终是唯一真相源（宪法 §15.8）。
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    conf = sync_conf(cfg)
    cli = (conf.get("cli") or "lark-cli").strip()
    # 联调 v2.10 修：folder_token 直接指向工作区根，project_prefix 只是展示用前缀
    # （默认空；不 fallback 到 "OPC" 以免误导为要在工作区里建 OPC 子目录）
    prefix = (conf.get("project_prefix") or "").strip()
    folder_token = (conf.get("folder_token") or "").strip()
    if getattr(args, "folder_token", None):
        folder_token = args.folder_token
    if getattr(args, "prefix", None):
        prefix = args.prefix
    action = getattr(args, "action", None) or "push"

    # —— 只读动作：不需要 folder_token（tasks / search）——
    if action in ("tasks", "search"):
        print("云端仓库读操作（不改本地任何文件）")
        return sync_bridge(action, vault, cli, prefix, folder_token, args)

    # v2.10：--role <本岗> 是**纯本地入队**，不碰远程，不需要 token
    # （修复：本分支原先被下面的 token 检查误挡，导致各岗无法入队）
    role = (getattr(args, "role", None) or "").strip()
    if role and not getattr(args, "flush", False):
        return cmd_sync_enqueue_role(vault, cfg, args, prefix, role)

    if action == "init":
        # —— init 自举（联调修的真问题：鸡生蛋）——
        # 原来的流程要求「先有 folder_token 才能跑 init」，
        # 但 token 恰恰要靠 init 建出来的目录才能拿到。
        # 实际上 `drive +create-folder` 省略 --folder-token 时会落在**根目录**，
        # 所以 init 无 token 也能建 OPC/ 根目录并回显它的 token。
        return sync_bridge("init", vault, cli, prefix, folder_token, args)

    if not folder_token:
        # v2.10：--queue 是**纯本地只读**，不碰远程，不需要 token
        if getattr(args, "queue", False):
            return cmd_sync_queue(vault, args)
        print("错误：未配置云端根文件夹 token。")
        print("      两种做法：")
        print("      ① 先跑 `opc sync init` 建远端根目录并拿 token（无需 token 也能跑）；")
        print("      ② 在 opc.config.json 的 backends.feishu 里加 \"folder_token\": \"<token>\"；")
        print("      ③ 命令行加 --folder-token <token>。")
        print("      拿 token：lark-cli drive files list 看根目录，或 "
              "lark-cli drive +create-folder --name " + prefix + " 拿返回的 token。")
        return 2

    # —— v2.10：--queue 只看队列（只读，不碰远程）——
    if getattr(args, "queue", False):
        return cmd_sync_queue(vault, args)

    # —— v2.10：--flush 由 pm 统一推送队列（增量，每次产出版本目录）——
    if getattr(args, "flush", False):
        return cmd_sync_flush(vault, cfg, args)

    role = (getattr(args, "role", None) or "").strip()
    tmp_stage = None
    if role:
        return cmd_sync_enqueue_role(vault, cfg, args, prefix, role)
    elif getattr(args, "all", False):
        local_dir = vault / "30_项目"
        what = "整个 30_项目/ 目录"
    else:
        project = active_project_of(args, cfg, read_handoff(vault, cfg), vault)
        pdir = vault / "30_项目" / project_dir_name(vault, project)
        if not pdir.is_dir():
            print("错误：项目目录不存在：" + str(pdir))
            return 2
        # v2.20 修（实测踩坑）：全量推送也要带「项目文件夹」层，与队列推送口径一致——
        # 否则项目内容会平铺到远端根，多项目推送时互相合并（两个项目的
        # _派活清单.json / _版本台账.md 同名覆盖）。
        import shutil as _sh
        import tempfile as _tf
        tmp_stage = Path(_tf.mkdtemp(prefix="opc_sync_proj_"))
        # v2.20 修（实测踩坑）：**--local-dir 指向的是「要被镜像的根」**。
        # 旧写法把 tmp_stage 传给 +push，而 tmp_stage 里装的是「项目名/」这一层
        # → 镜像后远端得到的是项目目录的**内容**（_素材/、2026-11-21-V1/ 平铺到根），
        # 多项目推送会互相合并（同名 _版本台账.md 覆盖）。
        # 正确：--local-dir 指向 tmp_stage（内含「项目名/」一层），
        # 且 cwd 切到 tmp_stage 的父目录 → 远端得到 <项目名>/ 这一层。
        _sh.copytree(str(pdir), str(tmp_stage / project_dir_name(vault, project)))
        local_dir = tmp_stage
        what = "项目 " + project + " 的全部产出"
    if not local_dir.is_dir():
        print("错误：待同步目录不存在：" + str(local_dir))
        return 2
    # 远端目录（v2.10 增量语义）：**每个推送落一个版本目录**，历史版本保留、互不覆盖
    remote_prefix = prefix
    if role:
        remote_prefix = prefix + "/" + role
    print("云端同步（" + what + "）")
    print("  本地目录：" + str(local_dir))
    print("  远端目录：" + remote_prefix + "/（远端根由 folder-token 定位）")
    if role:
        print("  范围隔离：只同步本岗文件，远端落在本岗独立目录，不与他人互相覆盖。")
    print("  说明：本地已写完，这一步只是额外把同一份产出再写一份到云端。")
    print("  写入提醒：远程仓库的写入由产品经理统一执行（§15.16）；其他岗位请用"
          " --sync-self 本地入队。")
    try:
        rc = sync_bridge(action, vault, cli, remote_prefix, folder_token, args, local_dir=local_dir)
    finally:
        if tmp_stage is not None:
            import shutil as _sh
            _sh.rmtree(tmp_stage, ignore_errors=True)
    if rc == 0 and action == "push":
        print()
        print("同步完成 —— 本地与云端各有一份。")
    return rc


def sync_team_archives(vault, cfg, args=None):
    """团队档案线：把 99_Agent配置/履历/ 推到飞书 OPC/团队档案/履历/（v2.20 新增，创始人拍板）。

    触发时机 = 沉淀写履历（opc link）成功后自动执行——「我说要沉淀简历的时候
    就把这事干了并同步到飞书上」。与项目数据资产线相互独立（两码事口径）：
    - 履历是**工作区级团队档案**，不混进项目版本目录（v2.10 联调口径不变）；
    - 增量语义：目录镜像推送，内容未变的文件由云端去重跳过；
    - 未配置云端（folder_token 空）→ 静默跳过；失败只提示不阻塞（§15.12）。
    """
    conf = sync_conf(cfg)
    folder_token = (conf.get("folder_token") or "").strip()
    if not folder_token:
        print("团队档案同步：跳过（未配置云端仓库 folder_token）。")
        return 0
    resume_dir = Path(vault) / "99_Agent配置" / "履历"
    if not resume_dir.is_dir():
        print("团队档案同步：跳过（本地无履历目录）。")
        return 0
    import tempfile as _tf
    import shutil as _sh
    tmp = Path(_tf.mkdtemp(prefix="opc_team_"))
    dst = tmp / "团队档案" / "履历"
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for f in sorted(resume_dir.glob("*.md")):
        _sh.copy2(str(f), str(dst / f.name))
        copied += 1
    if not copied:
        _sh.rmtree(tmp, ignore_errors=True)
        print("团队档案同步：跳过（履历目录为空）。")
        return 0
    cli = (conf.get("cli") or "lark-cli").strip()
    prefix = (conf.get("project_prefix") or "").strip()
    ns = argparse.Namespace(vault=str(vault), action="push", allow_restore=False,
                            dry_run=bool(getattr(args, "dry_run", False) if args else False))
    print("团队档案同步：履历 %d 份 → 远端 OPC/团队档案/履历/" % copied)
    try:
        rc = sync_bridge("push", vault, cli, prefix, folder_token, ns, local_dir=tmp)
    finally:
        _sh.rmtree(tmp, ignore_errors=True)
    return rc


def sync_bridge(action, vault, cli, prefix, folder_token, args, local_dir=None):
    """调用 sync_feishu.py 桥脚本。优先用 Vault 内那份，回退到 Skill 包内。"""
    candidates = [
        Path(vault) / "99_Agent配置/通知桥/sync_feishu.py",
        Path(__file__).resolve().parent / "sync_feishu.py",
    ]
    bridge = None
    for c in candidates:
        if c.exists():
            bridge = c
            break
    if bridge is None:
        print("错误：找不到同步桥（sync_feishu.py）。")
        print("      可执行 opc init 补齐，或手动放到 " + str(candidates[0]))
        return 2
    argv = [sys.executable, str(bridge), action, "--vault", str(vault),
            "--prefix", prefix, "--folder-token", folder_token]
    if local_dir is not None:
        argv += ["--local-dir", str(local_dir)]
    if getattr(args, "allow_restore", False):
        argv.append("--allow-restore")
    if getattr(args, "dry_run", False):
        argv.append("--dry-run")
    query = (getattr(args, "query", None) or "").strip()
    if query:
        argv += ["--query", query]
    limit = getattr(args, "limit", None)
    if limit:
        argv += ["--limit", str(int(limit))]
    try:
        # 全链统一 UTF-8：子进程显式指定编码 + 强制其 stdout 也用 UTF-8，
        # 否则 Windows 下 locale(GBK) 解码子进程的 UTF-8 字节会抛 UnicodeDecodeError，
        # 读取线程崩溃会让输出被静默截断（v2.13 修复）
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=900,
                              encoding="utf-8", errors="replace", env=env)
    except OSError as exc:
        print("调用同步桥失败：" + str(exc))
        return 2
    out = (proc.stdout or "") + (proc.stderr or "")
    if out.strip():
        print(out.rstrip())
    return proc.returncode


def pinned_versions(vault):
    """扫描各项目 `0_约束卡.md` 的 `pinned_refs` → [(项目目录名, 钉扎的规范版本)]。

    §10 钉扎是「项目启动时锁定的规则版本」；宪法升版后老项目仍钉旧版本是
    **设计使然**，但必须**看得见**（status 一眼可查），否则漂移就变成暗坑。
    """
    out = []
    base = Path(vault) / "30_项目"
    if not base.exists():
        return out
    for pdir in sorted(base.iterdir()):
        if not pdir.is_dir():
            continue
        pins = find_project_docs(pdir, "0_约束卡.md")
        if not pins:
            continue
        f = pins[-1]  # 多版本各有约束卡时取最新一份（最新立项的钉扎即当前状态）
        try:
            # 容忍多项钉扎：取该行里 OPC-架构规范 的版本（单项写法是常态，
            # 但不能因为多了一项就整条漏报 —— 漏报会让漂移变暗坑）。
            m = re.search(r"^pinned_refs:.*?OPC-架构规范@([^\],\s]+)", read_text(f), re.M)
        except OSError:
            continue
        if m:
            out.append((pdir.name, m.group(1)))
    return out


def cmd_repin(args):
    """重钉：把项目的规则钉扎迁到新的规范版本（宪法 §10 解钉 + §11 评估后执行）。

    §10：「解钉**必须**先做 §11 变更影响评估」→ 本命令据此设了三道闸：
      1. `--reason` **必填** —— §11 评估结论写进项目 frontmatter 留痕（`repin_reason`）；
      2. 默认**只处理显式点名的项目**（`--project`，可多次）—— 要动全部得显式 `--all`；
      3. 每次重钉都留迁移轨迹（`repinned_from` / `repinned_at`）并更新 `updated`（§11 步骤 4）。
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    cur = constitution_version(vault, cfg)
    to = (getattr(args, "to", None) or cur).strip()
    reason = (getattr(args, "reason", None) or "").strip()
    if not reason:
        raise ValueError("重钉必须带 --reason（把 §11 变更影响评估结论写进留痕）")
    targets = []
    if getattr(args, "all", False):
        targets = [d for d, _ in pinned_versions(vault)]
    else:
        for p in (getattr(args, "project", None) or []):
            targets.append(project_dir_name(vault, p))
    if not targets:
        print("未指定项目：用 --project PXXX（可多次）或 --all。")
        print("当前宪法实际版本：" + cur)
        return
    today = datetime.now().strftime("%Y-%m-%d")
    changed = []
    for dname in targets:
        pins = find_project_docs(Path(vault) / "30_项目" / dname, "0_约束卡.md")
        if not pins:
            print("跳过（无 0_约束卡）：" + dname)
            continue
        for f in pins:  # v2.20：多版本各有约束卡时逐份重钉；存量项目根平铺副本一并兼容
            text = read_text(f)
            m = re.search(r"^pinned_refs:.*?OPC-架构规范@([^\],\s]+)", text, re.M)
            if not m:
                print("跳过（未找到 pinned_refs）：" + dname)
                continue
            old = m.group(1)
            if old == to:
                print("跳过（已钉在 " + to + "）：" + dname)
                continue
            new_text = re.sub(r"^pinned_refs:.*$",
                               lambda mm: mm.group(0).replace("@" + old, "@" + to),
                               text, count=1, flags=re.M)
            # 留痕：每次重钉都**重写** from/at/reason。
            # （此前只在没有留痕时才写 → 二次重钉后 pinned_refs 已是新版本，
            #   留痕却还写着上一次迁移的 from/日期/结论 —— 留痕与事实矛盾。）
            # 同时把本次迁移追加进 repin_history，历次轨迹不丢。
            hist = []
            mh = re.search(r"^repin_history:\s*(\[.*\])\s*$", new_text, re.M)
            if mh:
                try:
                    hist = json.loads(mh.group(1))
                    if not isinstance(hist, list):
                        hist = []
                except (ValueError, TypeError):
                    hist = []
            hist.append({"from": old, "to": to, "at": today, "reason": reason})
            # 先删掉旧的留痕行（重写而非叠加，避免出现两套 from/at）
            new_text = "\n".join(
                ln for ln in new_text.split("\n")
                if not re.match(r"^(repinned_from|repinned_at|repin_reason|repin_history):", ln))
            trail = ("repinned_from: " + old
                     + "\nrepinned_at: " + today
                     + "\nrepin_reason: " + json.dumps(reason, ensure_ascii=False)
                     + "\nrepin_history: " + json.dumps(hist, ensure_ascii=False))
            new_text = re.sub(
                r"^(pinned_refs:.*)$",
                lambda mm: (mm.group(1) + "\n" + trail),
                new_text, count=1, flags=re.M)
            new_text = re.sub(r"^updated:.*$", "updated: " + today, new_text, count=1, flags=re.M)
            write_text(f, new_text)
            changed.append((dname, old, to))
    if not changed:
        print("没有需要重钉的项目。")
        return
    print("已重钉 " + str(len(changed)) + " 个项目（宪法 §10 解钉，§11 评估后执行）：")
    for dname, old, new in changed:
        print("  - " + dname + "：" + old + " -> " + new)
    print("留痕已写入各项目 0_约束卡.md（repinned_from / repinned_at / repin_reason）。")
    print("提示：重钉只改「依据哪个版本」，不改已产出的内容 —— 产出本身仍按原依据保留。")


def cmd_status(args):
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    prof = profile_view(cfg)
    bindings = role_bindings_ref(cfg)
    print("Vault:", vault, "存在" if vault.exists() else "不存在")
    print("配置:", "已生成" if (vault / "opc.config.json").exists() else "缺失（使用 Skill 默认模板）")
    print("profile:", prof["label"] + "（active_profile=" + prof["name"] + "）")
    print("  步骤文档:", len(prof["step_docs"]), "份 |", " → ".join(d[:-3] for d in prof["step_docs"]))
    print("  职业集合:", len(bindings), "个：", ", ".join(bindings.keys()))
    const_v = constitution_version(vault, cfg)
    cfg_v = schema_version(cfg)
    print("规范:", "已就位" if (vault / "OPC-架构规范.md").exists() else "缺失",
          "| 宪法实际 " + const_v + " | 配置声明 " + cfg_v)
    if const_v != cfg_v:
        print("  版本漂移：opc.config.json 仍声明 " + cfg_v + "，与 Vault 内宪法实际版本 "
              + const_v + " 不一致 —— 请把配置里的 schema 改成 opc-team/" + const_v
              + "（新项目的规则钉扎按宪法实际版本写，不受此项影响）")
    pins = pinned_versions(vault)
    if pins:
        old = [p for p in pins if p[1] != const_v]
        print("规则钉扎:", str(len(pins)) + " 个项目 | 钉在旧规范版本："
              + (", ".join(p[0] + "→" + p[1] for p in old) if old else "无")
              + "（§10：升级不自动改变在跑项目依据）")
    print("岗位卡:", len(list((vault / "99_Agent配置/岗位卡").glob("*.md"))), "份")
    print("职业简历:", len(list((vault / "99_Agent配置/履历").glob("*.md"))), "份")
    rows = read_index(vault, cfg)
    print("产出索引:", len(rows), "行", "已就位" if index_file(vault, cfg).exists() else "缺失")
    used = {b.get("backend") for b in bindings.values()}
    for name in sorted(used):
        try:
            backend = get_backend(name, cfg, vault)
            ok, msg = backend.check()
            print("后端", name + ":", "OK" if ok else "注意", "-", msg)
        except ValueError as exc:
            print("后端", name + ":", "错误 -", str(exc))
    print("安全:", "凭证仅环境变量" if cfg.get("security", {}).get("credentials_in_config") is False else "请检查 security 配置")
    print("后端选择: local（默认，规范 §2 目录） / feishu（飞书 CLI 或自定义外部 CLI）")
    print("运行模式:", agent_mode_of(cfg), "-", AGENT_MODES[agent_mode_of(cfg)])
    others = [p for p in available_profiles(cfg) if p != prof["name"]]
    if others:
        print("可切换 profile:", ", ".join(others), "（opc profile <名称>；换行业不换架构）")


def cmd_profile(args):
    """查看 / 切换行业 profile。"""
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    profiles = cfg.get("profiles") or {}
    prof = profile_view(cfg)
    if not getattr(args, "name", None):
        print("当前 profile:", prof["name"], "-", prof["label"])
        print("  步骤文档:", " → ".join(d[:-3] for d in prof["step_docs"]))
        print("  职业集合:", ", ".join(role_bindings_ref(cfg).keys()))
        print("  知识域:", ", ".join(prof.get("knowledge_domains", [])))
        if profiles:
            print("可选 profile：")
            for n, p in profiles.items():
                mark = "（当前）" if n == prof["name"] else ""
                print("  - " + n + " - " + p.get("label", n) + mark)
        else:
            print("本配置为旧版（无 profiles 段），升级方式：用新模板重新 init 或手工迁移。")
        return
    name = args.name
    if not profiles:
        raise ValueError("本配置为旧版（无 profiles 段），无法切换")
    if name not in profiles:
        raise ValueError("未登记的 profile：" + name + "；可选：" + ", ".join(profiles))
    if name != prof["name"]:
        cfg["active_profile"] = name
        save_config(vault, cfg)
        print("已切换 active_profile ->", name, "（" + profiles[name].get("label", name) + "）")
        print("注意：既有项目的索引行保留为历史记录；新项目按新 profile 的步骤文档与职业集合创建。")
        print("建议：为新 profile 缺卡的职业执行 init（幂等，自动生成骨架卡），并检查 20_知识资产 目录。")
    else:
        print("当前已是", name)


def cmd_mode(args):
    """查看 / 切换运行模式（agent_mode）—— OPC 里团队形态的**唯一写入入口**（v2.13）。

    三种取值对应 GLOSSARY 第五节「多 Agent 协同」的三种落地形态：
    single（默认）/ multi（每职业一个独立 Agent）/ multi-profile（独立身份 + 独立记忆）。
    """
    vault = Path(args.vault).resolve()
    cfg = load_config(vault)
    mode = agent_mode_of(cfg)
    name = getattr(args, "name", None)
    if not name:
        print("当前运行模式:", mode, "-", AGENT_MODES[mode])
        print("可选：")
        for k, v in AGENT_MODES.items():
            print("  - " + k + ("（当前）" if k == mode else "") + " —— " + v)
        print("切换：opc mode <值>")
        print("说明：独立身份 ≠ 流水线并行；并行靠不同项目波次（手册 §22.2 / §23.4）。")
        return
    old = set_agent_mode(vault, name)
    if old == name:
        print("当前已是", name)
        return
    print("已切换运行模式:", old, "->", name, "-", AGENT_MODES[name])
    if name == "single":
        print("  一个 Agent 按阶段切换身份：每次开工开新会话 → opc role <role> 取身份声明。")
    elif name == "multi":
        print("  每职业一个独立 Agent：执行 opc bots 导出配置卡，逐个在宿主创建并各贴一份提示词。")
    else:
        print("  一职业 = 一个独立持久身份（Profile）：opc bots 导出卡含「Profile 宿主适配」段"
              "（SOUL.md / 共享 workspace / 记忆归属）。")
        print("  硬前提：所有 Profile 的 workspace 必须指向同一个 Vault，否则交接链断（宪法 §15.3）。")
    print("  下一步：opc onboard 会按新模式输出对应引导。")


def extract_global_flags(argv):
    """允许 --vault 与 --non-interactive 出现在子命令之前或之后。"""
    vault, rest, i = None, [], 0
    non_interactive = False
    while i < len(argv):
        if argv[i] == "--vault" and i + 1 < len(argv):
            vault = argv[i + 1]
            i += 2
        elif argv[i].startswith("--vault="):
            vault = argv[i].split("=", 1)[1]
            i += 1
        elif argv[i] == "--non-interactive":
            non_interactive = True
            i += 1
        else:
            rest.append(argv[i])
            i += 1
    return vault, non_interactive, rest


def _is_output_channel_error(exc: BaseException) -> bool:
    """宿主把输出管道提前关了（`| head` 式截断、重定向到已关闭的流）→ 只是显示问题。

    Windows 写已关闭的管道抛 EINVAL(22)，POSIX 抛 EPIPE(32)。
    """
    return isinstance(exc, OSError) and exc.errno in (errno.EINVAL, errno.EPIPE)


def _silence_stdout() -> None:
    """把 stdout 指向空设备 —— 之后任何 print 都不再抛异常。"""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="opc-team：一人公司虚拟团队工作流")
    ap.add_argument("--vault", default=None, help="Vault 根目录；省略时交互式询问")
    ap.add_argument("--non-interactive", action="store_true", help="关闭交互式引导")
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("init", help="幂等初始化 Vault（不带 --vault 时引导选择目录）")
    p.add_argument("--choose", action="store_true", help="尝试弹出系统目录选择框")
    p.add_argument("--profile", default=None, help="行业 profile（如 app / content）；省略时交互式选择，默认 app")
    p.add_argument("--workspace-root", default=None, help="宿主加载入口文件的工作区根；默认自动推断")
    p.add_argument("--cloud-repo", default=None, choices=["auto", "ask", "skip"],
                   help="云端仓库配置：auto=非交互自动配置（需 --workspace）/ ask=交互询问 / skip=跳过")
    p.add_argument("--workspace", default=None,
                   help="云端工作区：名称（不存在会自动创建）/ 飞书文件夹链接 / folder token")
    p.add_argument("--agent-mode", default=None, choices=["single", "multi", "multi-profile"],
                   help="运行模式：single 单 Bot 切换身份（默认）/ multi 每职业一个 Bot / "
                        "multi-profile 独立身份 + 独立记忆（Profile 型宿主类）")
    p.add_argument("--no-attach", action="store_true", dest="no_attach",
                   help="跳过宿主入口（不写 AGENTS.md/CLAUDE.md 受控区块）；之后可随时 opc attach --apply")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("roster", help="职业状态仓库：各 role 空闲 / 认领中 / 可领任务数")
    p.set_defaults(func=cmd_roster)

    p = sub.add_parser("claim", help="职业领任务：从任务池领一条可开工的活")
    p.add_argument("--role", required=True)
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release", help="释放某职业当前认领")
    p.add_argument("--role", required=True)
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("queue", help="流水线视图：各项目当前阶段与接手 role（只读）")
    p.add_argument("--role", default=None)
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("onboard", help="安装期引导：幂等检测工作流状态并输出下一步清单（只读）")
    p.add_argument("--workspace-root", default=None)
    p.set_defaults(func=cmd_onboard)

    p = sub.add_parser("attach", help="已有项目接入：受控区块合并到宿主入口，不覆盖原文件")
    p.add_argument("--workspace-root", default=None)
    p.add_argument("--entry", default=None, help="指定入口名，逗号分隔；默认检测常见入口")
    p.add_argument("--apply", action="store_true", help="真正写入；省略时只读诊断")
    p.add_argument("--remove", action="store_true", help="移除受控区块（卸载）")
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("bind", help="绑定某职业某产出到后端与地址")
    p.add_argument("role")
    p.add_argument("backend", nargs="?", default=None, help="local 或任一 exec 后端名（yunxiao / feishu / dingtalk / 自定义）；省略时引导选择")
    p.add_argument("--key", required=True)
    p.add_argument("--ref", required=True)
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("bind-cli", help="绑定外部系统 CLI 与命令模板")
    p.add_argument("backend")
    p.add_argument("--cli", required=True)
    p.add_argument("--push-cmd", required=True)
    p.add_argument("--url-prefix", default="")
    p.set_defaults(func=cmd_bind_cli)

    p = sub.add_parser("resolve", help="解析逻辑键为 URI")
    p.add_argument("role")
    p.add_argument("key")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("ask", help="本岗主动问创始人（交互点专用：用本岗身份在群里 @ 创始人）")
    p.add_argument("--role", required=True, help="本岗 role（如 strategist / design / engineer）")
    p.add_argument("--text", required=True, help="要问什么 / 要拍板什么")
    p.add_argument("--project", default=None, help="项目号（可选，带上更清楚）")
    p.add_argument("--vault", default=None)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("new", help="新建项目并检索相似项目")
    p.add_argument("name", nargs="?", default=None, help="项目名；省略时引导输入")
    p.add_argument("--tags", default="")
    p.add_argument("--light", default=None, help="变更性质标签（兼容别名；不再决定派活范围）")
    p.add_argument("--scope", default=None, help="变更性质标签：画面/后端/前端/文案/数据/迭代/其他/全量（仅标签，不决定谁干活）")
    p.add_argument("--triage", default=None,
                   help="pm 派活清单文件（_派活清单.json 或 4_产品基线.md 等）；路由的唯一真相源。"
                        "解析失败会报错中止，不会静默降级为全量 9 步")
    p.add_argument("--from", dest="from_project", default=None, help="继承上一版项目（含其 pm 分诊结论），如 P001")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("triage", help="pm 起草派活清单：固定落点 + 结构化 + 解析失败显式报错")
    p.add_argument("--project", default=None, help="项目号（如 P001）；省略时用当前项目")
    p.add_argument("--role", default=None, help="本次是否涉及的岗位（如 design / engineer）")
    p.add_argument("--involved", default=None, help="涉及 / 不涉及（yes/no）；省略按「涉及」处理")
    p.add_argument("--assets", default="", help="产出资产，逗号或顿号分隔（如 5_设计交付.md,5_原型.html）")
    p.add_argument("--reason", default="", help="分诊理由（必填）")
    p.add_argument("--zero", action="store_true", help="判定本次零岗位（需配 --reason）")
    p.add_argument("--show", action="store_true", help="只读查看当前派活清单")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("similar", help="按标签检索相似项目落地物")
    p.add_argument("--tags", required=True)
    p.add_argument("--role", default=None)
    p.set_defaults(func=cmd_similar)

    p = sub.add_parser("migrate", help="存量 Vault 迁移：目录名去 P00X 前缀 + 补版本台账 V1")
    p.add_argument("--dry-run", action="store_true", help="只报要改什么，不动文件")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("scaffold", help="按需生成岗位产出骨架（v2.18：立项不预生成，岗位开工时创建）")
    p.add_argument("--role", required=True, help="岗位（如 pm / design / engineer）")
    p.add_argument("--project", default=None, help="项目名 / 项目号 / 目录名都行；省略用当前项目")
    p.add_argument("--version", default=None, help="版本号（如 V2）；省略用交接状态里的当前版本")
    p.set_defaults(func=cmd_scaffold)

    p = sub.add_parser("version", help="版本链：项目内追加一次立项 = 新版本号（V2、V3…）")
    p.add_argument("--project", default=None, help="项目名 / 项目号 / 目录名都行")
    vsub = p.add_subparsers(dest="version_cmd")
    pv = vsub.add_parser("list", help="列出版本台账")
    pv.add_argument("--project", default=None)
    pv.set_defaults(func=cmd_version_list)
    pv = vsub.add_parser("new", help="追加一次立项 = 新版本号（v2.29.5：须创始人同意）")
    pv.add_argument("--project", default=None)
    pv.add_argument("--需求", "--need", dest="need", default="", help="这次要做什么（一句话，必填）")
    pv.add_argument("--triage", default=None, help="本次 pm 派活清单（缺省沿用项目上一版）")
    pv.add_argument("--founder-approved", dest="founder_approved", default="",
                    help="创始人同意依据（必填：先取得创始人同意再立项；留痕进版本台账）")
    pv.set_defaults(func=cmd_version_new)
    p.set_defaults(func=cmd_version_list)

    p = sub.add_parser("distill", help="沉淀：一个版本的产出按岗拆开，各职业自己写自己的履历")
    p.add_argument("--project", default=None, help="项目名 / 项目号 / 目录名都行")
    p.add_argument("--version", default=None, help="版本号（默认当前最新版本）")
    p.add_argument("--status", action="store_true", help="只读汇总：哪些岗写了、哪些没写")
    p.set_defaults(func=cmd_distill)

    p = sub.add_parser("link", help="把项目出处回填进职业简历")
    p.add_argument("project", nargs="?", default=None, help="项目名 / 项目号 / 目录名")
    p.add_argument("--project", dest="project_opt", default=None, help="同上（与位置参数二选一）")
    p.add_argument("--role", default=None)
    p.add_argument("--version", default=None, help="版本号（给了就写版本粒度条目，只写自己岗位）")
    p.add_argument("--修改", "--modify", dest="modify", default="", help="改自己旧条目时写留痕说明")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("resume", help="列出某职业全部项目经验及出处")
    p.add_argument("role")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("trace", help="按项目反查所有职业产出")
    p.add_argument("project")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("close", help="项目收尾检查")
    p.add_argument("project")
    p.add_argument("--link", action="store_true", help="自动回填简历出处，并把索引状态置为已归档")
    p.add_argument("--archive", action="store_true", help="只把索引状态置为已归档")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("bots", help="导出 8 份 Bot 配置卡（不创建 Bot 实例）")
    p.add_argument("--out", default=None, help="输出目录，默认 Vault 内 99_Agent配置/Bot配置卡/")
    p.set_defaults(func=cmd_bots)

    p = sub.add_parser("role", help="输出单 Bot 模式的本次身份声明")
    p.add_argument("role")
    p.set_defaults(func=cmd_role)

    p = sub.add_parser("done", help="本岗产出完成后报完成并通知创始人（下游由配置推导，禁止跨步）")
    p.add_argument("--role", required=True)
    p.add_argument("--project", default=None, help="省略时取 opc.config.json 的 active_project")
    p.add_argument("--file", action="append", default=[], help="附带产出，任意文件格式；可重复")
    p.add_argument("--channel", default=None, help="覆盖通知通道：local / feishu / dingtalk / yunxiao / 自定义（见 notify.channels）")
    p.add_argument("--sync-self", action="store_true",
                   help="同步本岗自己那部分到云端（只推本岗文件，远端按角色分目录，不碰别人的）")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("go", help="创始人确认后推进到紧邻的下一棒（项目与回执自动带出，无需输入）")
    p.add_argument("--project", default=None)
    p.add_argument("--token", default=None, help="省略时取当前挂起回执")
    p.add_argument("--decision", default=None, help="闸门阶段必填：通过 / 驳回 / 需补充")
    p.add_argument("--basis", default=None, help="拍板依据（可选）：写清为什么这么判，随拍板一起落盘可审计")
    p.set_defaults(func=cmd_go)

    p = sub.add_parser("advance", help="产品经理推进进度：收到创始人口头通知后代为推进到下一棒（并做项目级上送）")
    p.add_argument("--project", default=None)
    p.add_argument("--token", default=None, help="省略时取当前挂起回执")
    p.set_defaults(func=cmd_advance)

    p = sub.add_parser("pm", help="产品经理统筹台账：统管全部项目的进度 / 待推进 / 待上送 / 岗位冲突（只读）")
    p.set_defaults(func=cmd_pm)

    p = sub.add_parser("handoff", help="查看当前交接状态；可 --project 设定当前项目")
    p.add_argument("--project", default=None)
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("tools", help="查看 / 项目内登记 / 提议 / 创始人批准后写注册表")
    p.add_argument("action", nargs="?", default="view", help="view | add | propose | promote")
    p.add_argument("--project", default=None)
    p.add_argument("--role", default=None)
    p.add_argument("--id", default=None)
    p.add_argument("--name", default=None)
    p.add_argument("--type", default=None, help="mcp | skill | cli | oss | api")
    p.add_argument("--slot", default=None, help="能力槽位")
    p.add_argument("--stage", default=None)
    p.add_argument("--env", action="append", default=[], help="凭证环境变量名，可重复；只存名不存值")
    p.add_argument("--reason", default=None)
    p.add_argument("--approve", action="store_true", help="创始人批准写入全局注册表")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("listen", help="启动飞书长连接，实时接收创始人的自然语言确认（可选）")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("assets", help="按职业查询资产快照视图（版本 / 时间 / SHA256 / commit）")
    p.add_argument("--project", default=None)
    p.add_argument("--role", default=None)
    p.set_defaults(func=cmd_assets)

    p = sub.add_parser("status", help="配置与完整性自检")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sync", help="云端仓库（远程仓库）读写：增量推送产出 / 查知识 / 拉未完成任务")
    p.add_argument("action", nargs="?", default="push",
                   help="push=本地→云端（默认）｜status=比对差异｜init=建远端目录"
                        "｜tasks=拉云端未完成任务｜search=搜云端知识/原型"
                        "｜pull=云端→本地（需 --allow-restore）")
    p.add_argument("--role", default=None, help="只同步某岗位的产出（配合 --project）")
    p.add_argument("--project", default=None, help="项目号，如 P001")
    p.add_argument("--all", action="store_true", help="同步整个 30_项目/ 目录（默认行为）")
    p.add_argument("--flush", action="store_true",
                   help="pm 专用：把待推送队列增量推到远端（每次产出落一个版本目录，历史版本保留）")
    p.add_argument("--queue", action="store_true",
                   help="只看待推送队列（只读，不碰远程）")
    p.add_argument("--folder-token", default=None, help="云端根文件夹 token（覆盖配置）")
    p.add_argument("--prefix", default=None, help="远端项目目录前缀，默认 OPC")
    p.add_argument("--allow-restore", action="store_true", help="pull 时必须显式加：允许云端覆盖本地")
    p.add_argument("--dry-run", action="store_true", help="只预览不执行")
    p.add_argument("--query", default=None, help="tasks / search 的关键词")
    p.add_argument("--limit", type=int, default=0, help="tasks / search 最多显示多少条（0=全部）")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("profile", help="查看 / 切换行业 profile（换行业不换架构）")
    p.add_argument("name", nargs="?", default=None, help="要切换到的 profile 名；省略时仅查看")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("mode", help="查看 / 切换运行模式（single / multi / multi-profile）")
    p.add_argument("name", nargs="?", default=None, help="要切换到的运行模式；省略时仅查看")
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser("repin", help="重钉：把项目的规则钉扎迁到新规范版本（§10 解钉 / §11 评估后执行）")
    p.add_argument("--project", action="append", default=None,
                   help="项目号或项目全名，可多次；省略则需 --all")
    p.add_argument("--all", action="store_true", help="处理全部已钉扎项目（默认只动点名项目）")
    p.add_argument("--to", default=None, help="目标版本；默认 = Vault 内宪法实际版本")
    p.add_argument("--reason", default=None, help="§11 变更影响评估结论（必填，写进项目留痕）")
    p.set_defaults(func=cmd_repin)

    cli_vault, non_interactive, rest = extract_global_flags(sys.argv[1:])
    args = ap.parse_args(rest)
    args.vault = cli_vault or DEFAULT_VAULT
    args.vault_explicit = bool(cli_vault)
    args.non_interactive = non_interactive
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    try:
        rc = args.func(args)
        # 子命令可以返回非零表示「该动作失败但流程不阻塞」（如同步未成功），
        # 这个码要透传，否则调用方会把失败误判成成功。
        if isinstance(rc, int) and rc != 0:
            return rc
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        if _is_output_channel_error(exc):
            # 宿主把输出管道提前关了（`| head` 式截断 / 重定向到已关闭的流）。
            # **动作本身已完成**（文件已落盘）—— 这只是显示问题，不能算失败：
            # 否则 Agent 会把「已经装好了」误判成「安装失败」而重试。
            _silence_stdout()
            return 0
        print("错误:", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
