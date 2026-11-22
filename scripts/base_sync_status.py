#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地真相源 → 飞书多维表格台账 · 状态同步器（pm 专用，§15.17.4 写入权归 pm）。

**数据来源只有本地 Vault**（§15.8：本地是唯一真相源，台账是副本与入口）。
本脚本**不猜任何状态**：全部从下列本地文件读出，再写进台账：

| 台账内容 | 本地来源 |
|---|---|
| 版本键 / 版本需求名 | `30_项目/<项目>/_版本台账.md` 的「版本」「需求」两列（作废版本跳过）|
| 阶段棒（②表子记录） | `99_Agent配置/_交接状态/<短ID>.json` 的 `dispatch_chain` + 当前 `stage/role/status` |
| 需求行（①表子记录） | 同上（阶段棒即需求下的任务）|
| 项目汇总 | `_版本台账.md` + 交接状态 + `30_项目/_产出索引.md` |

**层级口径（v2.20 创始人拍板）**：
- ①需求池：**顶层 = 一个需求版本一行**（任务名称 = 版本台账「需求」列；需求编号 = `<短ID>-<版本>`），
  **子记录 = 该版本下的任务**（挂「关联父任务」）。
- ②版本开发计划：**顶层 = 一个需求版本一行**（版本键 = `<短ID>-<版本>`），
  **子记录 = 阶段棒**（挂「关联父任务」）。

**用法**：
```bash
python scripts/base_sync_status.py --vault <Vault绝对路径> [--project P001] [--dry-run]
```
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import base_progress as bp
import base_schema as bs

STAGE_TO_REQ_STATUS = getattr(bp, "_STAGE_TO_REQ_STATUS", {})


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _projects(vault: Path):
    """[(项目目录名, 短ID, 版本行列表)] —— 版本行只取未作废的。"""
    out = []
    for d in sorted((vault / "30_项目").iterdir()):
        if not d.is_dir():
            continue
        led = d / "_版本台账.md"
        if not led.is_file():
            continue
        vs = []
        for line in led.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s.startswith("|") or s.startswith("|---"):
                continue
            c = [x.strip() for x in s.strip("|").split("|")]
            if len(c) >= 4 and c[0].startswith("V") and c[0][1:].isdigit():
                vs.append({"version": c[0], "req": c[2], "status": c[3]})
        if vs:
            out.append((d.name, vs))
    return out


def _short_id(vault: Path, dirname: str) -> str:
    """从产出索引首列/末列取短ID；取不到退回目录名。"""
    idx = vault / "30_项目" / "_产出索引.md"
    if idx.is_file():
        for line in idx.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("|") and dirname in line:
                c = [x.strip() for x in line.strip("|").split("|")]
                if len(c) >= 12 and c[11].startswith("P"):
                    return c[11]
    return dirname.split("_")[0]


def _doc_done(vault: Path, dirname: str, doc: str) -> bool:
    """该阶段产出是否**真的落盘且非草稿** —— 比索引状态可靠（索引可能还没标状态）。

    找遍项目下所有版本目录里同名文件，读 frontmatter 的 status：
    `status: 完成 / 生效` → 已交付；`status: 草稿` → 未交付。
    """
    for vdir in sorted((vault / "30_项目" / dirname).glob("*")):
        if not vdir.is_dir():
            continue
        for f in vdir.rglob(doc):
            try:
                head = f.read_text(encoding="utf-8", errors="replace")[:600]
            except Exception:
                continue
            for line in head.splitlines():
                if line.startswith("status:"):
                    val = line.split(":", 1)[1].strip()
                    return val not in ("草稿", "", "无")
    return False


def _stage_bars(vault: Path, dirname: str, short: str, chain, state):
    """由 dispatch_chain 推阶段棒。

    进展判定优先级：
      ① 当前棒（stage+role 对上交接状态）→ 按交接状态（进行中 / 待确认）
      ② 产出文件已落盘且 status≠草稿 → 已交付
      ③ 其余 → 未开始
    """
    bars = []
    for item in chain or []:
        stage, doc, role = item[0], item[1], item[2]
        cur_stage = state.get("stage")
        cur_role = state.get("role")
        if stage == cur_stage and role == cur_role:
            prog = {"进行中": "进行中", "待确认": "待确认"}.get(state.get("status"), "进行中")
        elif _doc_done(vault, dirname, doc):
            prog = "已交付"
        else:
            prog = "未开始"
        bars.append({
            "stage": stage, "role": role, "progress": prog,
            "title": "阶段%s %s（%s）" % (stage, doc, role),
            "doc": doc,
            "upstream": "",
        })
    return bars


def _full_chain(vault: Path):
    """无派活清单链时（全量 9 步流程）→ 从 profile 配置取 step_owner 还原 9 步链。"""
    try:
        cfg = json.loads((vault / "opc.config.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    prof = (cfg.get("profiles") or {}).get(cfg.get("active_profile", "app"), {})
    chain = []
    for doc_name, own in (prof.get("step_owner") or {}).items():
        chain.append([int(own.get("stage", 0)), doc_name, own.get("role", "")])
    chain.sort(key=lambda x: x[0])
    return chain


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--project", default="", help="只同步指定项目（目录名或短ID）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    vault = Path(a.vault).resolve()
    token, missing, _ = bs.resolve_base(vault_hint=str(vault))
    if not token:
        print("[skip] 多维表格未配置（base_token 为空）——不阻塞流程（§15.12）")
        return 0
    if missing:
        print("[warn] 缺表登记：%s —— 先跑 base_schema.py --apply" % "、".join(missing))

    projects = _projects(vault)
    if a.project:
        projects = [p for p in projects if a.project in p[0] or a.project == _short_id(vault, p[0])]

    done = 0
    for dirname, versions in projects:
        short = _short_id(vault, dirname)
        state = _read_json(vault / "99_Agent配置" / "_交接状态" / (short + ".json"))
        chain = state.get("dispatch_chain") or []
        for v in versions:
            if "作废" in v["status"]:
                print("  [%s] %s 跳过（已作废版本）" % (short, v["version"]))
                continue
            if not chain:
                # 无派活清单链（全量 9 步流程）→ 回退到 profile 的 step_docs/step_owner
                chain = _full_chain(vault)
            bars = _stage_bars(vault, dirname, short, chain, state)
            rows = []
            for b in bars:
                # **两张表必须用同一个 title**（v2.20 实测踩坑）：
                # ②表用「阶段N 文档名（角色）」，①表若用别的写法 → 幂等匹配不上，
                # 每次同步都新建一批重复子记录。统一取 bars 里的 title。
                r = {"stage": b["stage"], "role": b["role"], "progress": b["progress"],
                     "title": b["title"], "doc": b["doc"]}
                if b["upstream"]:
                    r["upstream"] = b["upstream"]
                rows.append(r)
            if a.dry_run:
                print("  [%s] %s 待同步：%d 根棒" % (short, v["version"], len(rows)))
                for r in rows:
                    print("      阶段%s %s %s" % (r["stage"], r["role"], r["progress"]))
                continue

            st, msg = bp.sync_progress(vault, short, rows, project_name=dirname, version=v["version"])
            print("  [%s] %s ②表：%s" % (short, v["version"], msg))
            st2, msg2 = bp.sync_requirements(
                vault, short, version=v["version"], project_name=dirname,
                sub_titles=[b["title"] for b in bars],
                sub_records=[{"title": b["title"], "progress": b["progress"]} for b in bars])
            print("  [%s] %s ①表：%s" % (short, v["version"], msg2))
            done += 1

    print("[ok] 已同步 %d 个项目版本（层级：顶层=需求版本行，子记录=任务/阶段棒）" % done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
