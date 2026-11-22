#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多维表格台账 · 进度同步器（v2.20 新增，创始人拍板）。

**两条线，两码事**（创始人口径 2026-11-18）：
  - **进度线（本脚本）**：项目推进到哪根棒、谁接手、闸门拍板——**每次状态变化就同步**
    到飞书多维表格（②版本开发计划），版本没做完也要跟进。
  - **资产线（sync_feishu.py / opc sync）**：文件产出=数据资产，等**整个需求版本结束后**
    统一推云盘即可。

写入权归 pm（宪法 §15.17）；其它岗位只读。
未配置 base_token 时全部跳过、不报错（§15.12 降级，不阻塞流程）。

表结构真相源在 base_schema.py（TABLES）；本脚本只写行，不改结构。
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import base_schema as bs

# OPC 9 阶段：数字 → 台账「OPC 阶段」枚举名（唯一出处 base_schema.STAGES）
STAGE_NAMES = {i: name for i, name in enumerate(bs.STAGES)}


def _field_text(val):
    """把飞书记录字段值归一成文本（select/text/link 都可能返回 list/dict/str）。"""
    if val is None:
        return ""
    if isinstance(val, list):
        return "".join(_field_text(x) for x in val)
    if isinstance(val, dict):
        return str(val.get("text") or val.get("name") or "")
    return str(val)


def _list_records(token, table_id):
    """读全部记录（ndjson --output 落盘后逐行解析为 dict）。

    v2.20 修：`--format json` 的 stdout 是**位置数组**（无字段名），
    字典形态只在 ndjson 输出文件里有；manifest 打在 stdout，不能走 bs.lark 的
    stdout JSON 解析，这里直接调 lark-cli 落文件再解析。
    """
    import subprocess
    import tempfile
    recs = []
    # --output 有安全白名单（宿主可能把系统 TMPDIR 重定向到白名单外，实测）：候选目录依次回退；
    # lark-cli 会写 <output>.manifest.json 伴生文件，重名即拒——文件名加唯一戳 + --overwrite。
    out_dirs = [d for d in (tempfile.gettempdir(), "/tmp") if Path(d).is_dir()]
    for ident in (bs._identity(), "bot"):
        for out_dir in out_dirs:
            stamp = "%d_%d" % (os.getpid(), time.time_ns() % 10**9)
            out = Path(out_dir) / ("opc_rec_%s_%s.ndjson" % (re.sub(r"[^0-9A-Za-z]", "_", table_id), stamp))
            cmd = bs.lark_entry() + ["base", "+record-list", "--table-id", table_id,
                                     "--base-token", token, "--as", ident,
                                     "--format", "ndjson", "--output", str(out),
                                     "--overwrite", "--limit", "2000"]
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                               encoding="utf-8", errors="replace")
            except Exception:
                continue
            if not out.is_file():
                continue
            for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("record_id"):
                    if not isinstance(obj.get("fields"), dict):
                        # v2.20 修：ndjson 的字段是**顶层平铺**（无 fields 包裹）——
                        # 不归一化会让所有 rec.get("fields") 拿到 null，
                        # 匹配永远落空 → 每次同步都重复建行（实测 29 行重复的根因）。
                        obj = {"record_id": obj["record_id"],
                               "fields": {k: v for k, v in obj.items() if k != "record_id"}}
                    recs.append(obj)
            if recs:
                return recs
    return recs


def _record_id(rec):
    return rec.get("record_id") or rec.get("id") or ""


def _norm_row(upd, project_name, task_field):
    """把一条进度更新转成台账字段 dict（CellValue 口径见 +record-batch-update Tips）。

    text → 字符串；单选 → [值]；datetime → "YYYY-MM-DD HH:MM"；checkbox → true/false。
    task_field：模板文本列名自适应（「敏捷项目管理」模板 = 任务描述；老模板 = 任务名称）。
    """
    stage_no = int(upd.get("stage", -1))
    fields = {}
    title = str(upd.get("title") or ("%s · 阶段%d" % (project_name, stage_no)))
    if task_field:
        fields[task_field] = title
    if stage_no in STAGE_NAMES:
        fields["OPC 阶段"] = [STAGE_NAMES[stage_no]]
    if upd.get("role"):
        fields["负责角色"] = [upd["role"]]
    if upd.get("progress"):
        fields["进展"] = [upd["progress"]]
    if upd.get("doc"):
        fields["产出文档"] = str(upd["doc"])
    if upd.get("upstream"):
        fields["上游文档"] = str(upd["upstream"])
    if upd.get("decision") is not None:
        fields["拍板结论"] = str(upd["decision"])
        fields["闸门"] = True
    if upd.get("claim"):
        fields["认领时间"] = time.strftime("%Y-%m-%d %H:%M")
    return fields


def sync_progress(vault, project, updates, project_name="", version="V1"):
    """把进度更新写入台账②版本开发计划（v2.20 层级重构，与①需求池同构）。

    updates: [{"stage": 4, "role": "pm", "progress": "待确认", "doc": "...",
               "decision": "通过", "claim": True, "title": "..."}]
    结构：顶层 = 一个版本一行（任务描述 = 版本台账「需求」名，版本键 = "<短ID>-<版本>"）；
    阶段任务 = 子记录（关联父任务挂顶层行）。行匹配 = 版本键 + OPC 阶段精确命中。
    返回 (状态, 说明)：状态 ∈ ok / skip / fail。
    """
    token, missing, _ = bs.resolve_base(vault_hint=str(vault))
    if not token:
        return ("skip", "多维表格未配置（base_token 为空），进度不同步——配置方法见 opc init")
    tid = bs.TABLES.get("②版本开发计划", {}).get("table_id", "")
    if not tid:
        return ("skip", "②版本开发计划 表未登记 table_id——先跑 base_schema.py --apply 注入")
    try:
        project_name = project_name or project_dir_name_fallback(vault, project)
    except Exception:
        project_name = str(project)
    try:
        records = _list_records(token, tid)
    except bs.LarkError as exc:
        return ("fail", "读台账失败：" + str(exc))
    pkey = str(project).strip().lower()
    vkey = "%s-%s" % (project, version)
    try:
        flds = bs.list_fields(token, tid)
        items = flds if isinstance(flds, list) else (flds.get("fields") or flds.get("items") or [])
        names = set()
        for f in items:
            names.add(str(f.get("field_name") or f.get("name") or ""))
    except bs.LarkError as exc:
        return ("fail", "读字段失败：" + str(exc))
    task_field = ("任务名称" if "任务名称" in names
                  else "任务描述" if "任务描述" in names else "")
    # —— v2.20 层级：保障本版本顶层行存在（与①需求池同构）——
    top_id = ""
    if "版本键" in names:
        for rec in records:
            f = rec.get("fields") or {}
            par = f.get("关联父任务")
            no_parent = not ((isinstance(par, list) and par) or isinstance(par, dict))
            if no_parent and _field_text(f.get("版本键")) == vkey:
                top_id = _record_id(rec)
                break
        if not top_id:
            req_name = (_ledger_requirement_name(vault, project, version, project_name)
                        or ("%s %s" % (project_name, version)))
            try:
                payload = {"create_records": [{task_field: req_name, "版本键": vkey}]} if task_field \
                    else {"create_records": [{"版本键": vkey}]}
                data = bs.lark(["base", "+record-batch-create", "--table-id", tid,
                                "--json", json.dumps(payload, ensure_ascii=False)], token)
                ids = data.get("record_id_list") or []
                top_id = ids[0] if ids else ""
            except bs.LarkError as exc:
                return ("fail", "写版本顶层行失败：" + str(exc)[:160])
    results = []
    for upd in updates:
        stage_no = int(upd.get("stage", -1))
        if stage_no not in STAGE_NAMES:
            # v2.20 守卫（实测踩坑）：stage 无效的更新绝不落阶段行——
            # 否则按「版本键」宽匹配会误命中**顶层行**，把版本需求名覆盖成阶段标题。
            results.append("跳过无效阶段更新（stage=%s）" % upd.get("stage"))
            continue
        want_stage = STAGE_NAMES.get(stage_no, "")
        fields = _norm_row(upd, project_name, task_field)
        if "版本键" in names:
            fields["版本键"] = vkey
        if top_id and "关联父任务" in names:
            fields["关联父任务"] = [{"id": top_id}]
        hit = None
        # 精确匹配：版本键 + OPC 阶段（层级重构后的行都带版本键）
        for rec in records:
            f = rec.get("fields") or {}
            par = f.get("关联父任务")
            # v2.20 守卫：顶层行（版本行）无父链接，不参与阶段行匹配
            if not ((isinstance(par, list) and par) or isinstance(par, dict)):
                continue
            rec_stage = _field_text(f.get("OPC 阶段"))
            if want_stage and rec_stage and rec_stage != want_stage:
                continue
            if _field_text(f.get("版本键")) == vkey:
                hit = rec
                break
        if hit is None:
            # 存量兼容：无版本键的旧行按旧口径（pkey 在整行文本中 + 阶段）
            for rec in records:
                f = rec.get("fields") or {}
                if _field_text(f.get("版本键")):
                    continue
                rec_stage = _field_text(f.get("OPC 阶段"))
                if want_stage and rec_stage and rec_stage != want_stage:
                    continue
                joined = " ".join(_field_text(v) for v in f.values())
                if pkey and pkey in joined.lower():
                    hit = rec
                    break
        try:
            if hit and _record_id(hit):
                rid = _record_id(hit)
                payload = {"update_records": {rid: fields}}
                bs.lark(["base", "+record-batch-update", "--table-id", tid,
                         "--json", json.dumps(payload, ensure_ascii=False)], token)
                results.append("更新 %s（阶段%d）" % (want_stage or "?", stage_no))
            else:
                fields2 = dict(fields)
                if task_field:
                    fields2[task_field] = str(fields.get(task_field) or "") + " · " + str(project_name)
                payload = {"create_records": [fields2]}
                bs.lark(["base", "+record-batch-create", "--table-id", tid,
                         "--json", json.dumps(payload, ensure_ascii=False)], token)
                results.append("新建 %s（阶段%d）" % (want_stage or "?", stage_no))
        except bs.LarkError as exc:
            return ("fail", "写台账失败（阶段%d）：%s" % (stage_no, exc))
    return ("ok", "；".join(results) if results else "无更新项")


def project_dir_name_fallback(vault, project):
    """兜底拿项目目录名做台账行标识（读产出索引首列失败时用）。"""
    idx = Path(vault) / "30_项目" / "_产出索引.md"
    if idx.is_file():
        m = re.search(r"^\|\s*([^|\n]+?)\s*\|[^|\n]*\|\s*\w+\s*\|[^|\n]*\|.*\|\s*%s\s*\|" % re.escape(str(project)),
                      idx.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            return m.group(1).strip()
    return str(project)




# 阶段进展 → ①需求池「需求状态」枚举（REQ 生命周期）的映射口径（创始人确认的填充方案）
_STAGE_TO_REQ_STATUS = {"已交付": "已上线", "待确认": "开发中", "进行中": "开发中", "可开工": "方案中"}


def _top_prd_ref(vault, project):
    """顶层行的 PRD 指针 = 该项目 4_产品定义.md 的 Vault 相对路径（找不到返回空）。"""
    try:
        pdir = Path(vault) / "30_项目" / project_dir_name_fallback(vault, project)
        hits = sorted(pdir.glob("**/4_产品定义.md"))
        if hits:
            return str(hits[-1].relative_to(Path(vault)))
    except Exception:
        pass
    return ""


def lark_raw_api(method, path, token=None, body=None, retries=1):
    """raw OpenAPI 通道（lark-cli api <method> <path>）——用于 CLI 无子命令的端点。

    lark-cli api 的身份取 LARK_CLI_IDENTITY（默认 user），bot 严格模式宿主自动回退 bot。

    v2.20 修（实测踩坑）：**`lark-cli api` 不认 `--base-token`**（token 走该身份默认 app），
    传了会直接报 unknown flag "--base-token" → 所有视图属性读写静默失败。
    故此处**不传** token 参数，身份与租户由 `lark-cli api` 自己解析。
    """
    import subprocess as _sp
    for ident in (bs._identity(), "bot"):
        cmd = bs.lark_entry() + ["api", method.upper(), path,
                                 "--as", ident, "--format", "json"]
        if body is not None:
            cmd += ["--data", json.dumps(body, ensure_ascii=False)]
        proc = _sp.run(cmd, capture_output=True, text=True, timeout=120,
                       encoding="utf-8", errors="replace")
        raw = bs.ANSI_RE.sub("", (proc.stdout or "")).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            if ident == "bot":
                raise bs.LarkError((proc.stderr or proc.stdout or "")[:200])
            continue
        try:
            obj = json.loads(raw[start:end + 1])
        except ValueError:
            if ident == "bot":
                raise bs.LarkError(raw[:200])
            continue
        if obj.get("ok"):
            return obj.get("data") or {}
        err = obj.get("error") or {}
        # 404/405 = 方法或路径不对（PUT 会 404），换下一身份/重试无益
        if (err.get("code") in (404, 405) or err.get("subtype") == "not_found") and ident != "bot":
            continue
        raise bs.LarkError("接口报错：%s" % json.dumps(err, ensure_ascii=False)[:300])
    raise bs.LarkError("raw API 调用失败（身份均不可用）")


def ensure_hierarchy_config(vault, token, table_id, parent_field_name="关联父任务"):
    """确保②表的网格视图层级配置（hierarchy_config.field_id → 自关联字段）已设置。

    v2.20 实测定案：父子**数据**写好后，视图仍平铺，缺的是视图属性
    hierarchy_config.field_id（①需求池正常仅因模板预置）。OpenAPI 可设：
    PATCH /bitable/v1/apps/<app_token>/tables/<tid>/views/<view_id>
    （lark-cli 无 view-update 子命令，PUT 会 404，**必须 PATCH**）。
    幂等：视图已指向目标字段则跳过。失败只返回说明，不抛错（§15.12）。
    """
    import subprocess as _sp
    try:
        flds = bs.list_fields(token, table_id)
        items = flds if isinstance(flds, list) else flds.get("fields") or []
        fid = ""
        for f in items:
            if (f.get("field_name") or f.get("name")) == parent_field_name:
                fid = f.get("field_id") or f.get("id")
                break
        if not fid:
            return "未找到自关联字段（先建「%s」列）" % parent_field_name
        data = bs.lark(["base", "+view-list", "--table-id", table_id], token)
        views = data.get("views") or data.get("items") or []
        done, changed = 0, 0
        for v in views:
            vid = v.get("view_id") or v.get("id") or ""
            vtype = v.get("view_type") or v.get("type") or ""
            if not vid or vtype != "grid":
                continue
            cur = lark_raw_api("GET", "/open-apis/bitable/v1/apps/%s/tables/%s/views/%s"
                               % (token, table_id, vid))
            # v2.20 修（实测踩坑）：**lark_raw_api 已解包 data**（返回 {"view": {...}}），
            # 此处再取 cur["data"]["view"] 永远拿不到 → 每次都判「未配置」而重复 PATCH。
            view = cur.get("view") or ((cur.get("data") or {}).get("view") or {})
            prop = view.get("property") or {}
            hc = (prop.get("hierarchy_config") or {}).get("field_id")
            if hc == fid:
                done += 1
                continue
            lark_raw_api("PATCH", "/open-apis/bitable/v1/apps/%s/tables/%s/views/%s"
                         % (token, table_id, vid),
                         body={"property": {"hierarchy_config": {"field_id": fid}}})
            changed += 1
        return "网格视图层级配置：已配置 %d 个，本次新配 %d 个" % (done, changed)
    except Exception as exc:
        return "失败（不阻塞）：" + str(exc)[:160]


def _ledger_requirement_name(vault, project, version, project_name=""):
    """从 _版本台账.md 取该版本的「需求」列名（§15.18.2 版本链唯一真相源）。

    取不到回退 "<项目名> <版本>"。
    """
    pdir = Path(vault) / "30_项目" / project_dir_name_fallback(vault, project)
    ledger = pdir / "_版本台账.md"
    if ledger.is_file():
        in_tbl = False
        for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("|") and "版本" in s and "需求" in s and "立项时间" in s:
                in_tbl = True
                continue
            if in_tbl and s.startswith("|"):
                cells = [c.strip() for c in s.strip("|").split("|")]
                if len(cells) >= 3 and cells[0] == version:
                    return cells[2] or ("%s %s" % (project_name or project, version))
        return project_name or str(project)
    return None


def sync_requirements(vault, project, version="V1", project_name="", sub_titles=None, sub_records=None):
    """①需求池（关联项目）· 版本行 + 阶段子记录（v2.20 层级重构，创始人拍板）。

    - **顶层 = 一个版本一行**：任务名称 = 版本台账「需求」列；匹配键 = 需求编号
      字段存 "<短ID>-<版本>"（幂等，追加立项 V2 再加一行）。
    - **子记录 = 阶段任务**：挂在顶层行下（「关联父任务」link 字段），
      由进度钩子按棒传入 title 逐条登记（同一 title 幂等跳过）。
    - 未配置台账降级跳过。返回 (状态, 说明)。
    """
    token, _missing, _p = bs.resolve_base(vault_hint=str(vault))
    if not token:
        return ("skip", "多维表格未配置，需求池不同步")
    tid = bs.TABLES.get("①需求池（关联项目）", {}).get("table_id", "")
    if not tid:
        return ("skip", "①需求池 表未登记 table_id——先跑 base_schema.py --apply")
    try:
        records = _list_records(token, tid)
    except bs.LarkError as exc:
        return ("fail", "读台账失败：" + str(exc))
    key = "%s-%s" % (str(project).strip(), version)
    pname = project_name or project_dir_name_fallback(vault, project)
    req_name = _ledger_requirement_name(vault, project, version, pname) or ("%s %s" % (pname, version))

    top_id = ""
    dup_ids = []
    for rec in records:
        f = rec.get("fields") or {}
        if _field_text(f.get("需求编号")) == key:
            rid = _record_id(rec)
            if not top_id:
                top_id = rid
            else:
                dup_ids.append(rid)
    results = []
    if dup_ids:
        # 幂等去重：同键多行（历史失败运行残留）只留第一行
        try:
            bs.lark(["base", "+record-delete", "--table-id", tid]
                    + sum([["--record-id", x] for x in dup_ids], []) + ["--yes"], token)
            results.append("去重删除 %d 行" % len(dup_ids))
        except bs.LarkError:
            pass
    try:
        if not top_id:
            payload = {"create_records": [{"需求编号": key, "任务名称": req_name,
                                           "需求状态": ["已上线"], "优先级": ["P0"],
                                           "PRD": _top_prd_ref(vault, project),
                                           "验收标准": "MVP 边界（透传）：本次只验证 1 个核心假设——" + req_name}]}
            data = bs.lark(["base", "+record-batch-create", "--table-id", tid,
                            "--json", json.dumps(payload, ensure_ascii=False)], token)
            ids = data.get("record_id_list") or []
            top_id = ids[0] if ids else ""
            results.append("新建版本行 %s（%s）" % (version, req_name))
        else:
            results.append("版本行 %s 已存在" % version)
        if not top_id:
            # 建表接口没回 record_id 时重查一次
            for rec in _list_records(token, tid):
                if _field_text((rec.get("fields") or {}).get("需求编号")) == key:
                    top_id = _record_id(rec)
                    break
        if not top_id:
            return ("fail", "版本行建立后取不到 record_id")
    except bs.LarkError as exc:
        return ("fail", "写版本行失败：" + str(exc))

    if top_id:
        try:
            payload = {"update_records": {top_id: {
                "需求状态": ["已上线"], "优先级": ["P0"],
                "PRD": _top_prd_ref(vault, project),
                "验收标准": "MVP 边界（透传）：本次只验证 1 个核心假设——" + req_name}}}
            bs.lark(["base", "+record-batch-update", "--table-id", tid,
                     "--json", json.dumps(payload, ensure_ascii=False)], token)
        except bs.LarkError:
            pass

    # 子记录：阶段任务（同一 title 幂等）；列填充 = 需求来源/需求状态(阶段映射)/优先级
    sub_meta = {}
    for u in (sub_records or []):
        t = u.get("title")
        if t:
            sub_meta[t] = u
    # 既有子记录列回填（新增列口径对存量行生效）
    upd_backfill = []
    for rec in records:
        f = rec.get("fields") or {}
        t = _field_text(f.get("任务名称"))
        par = f.get("关联父任务")
        has_parent = bool((isinstance(par, list) and par) or isinstance(par, dict))
        if has_parent and t in sub_meta and not _field_text(f.get("需求来源")):
            u = sub_meta[t]
            upd_backfill.append({"record_id": _record_id(rec), "fields": {
                "需求来源": ["日常迭代"], "优先级": ["P0"],
                "需求状态": [_STAGE_TO_REQ_STATUS.get(u.get("progress") or "", "方案中")]}})
    if upd_backfill:
        try:
            payload = {"update_records": {d["record_id"]: d["fields"] for d in upd_backfill}}
            bs.lark(["base", "+record-batch-update", "--table-id", tid,
                     "--json", json.dumps(payload, ensure_ascii=False)], token)
            results.append("回填子记录列 %d 条" % len(upd_backfill))
        except bs.LarkError as exc:
            results.append("子记录列回填失败：" + str(exc)[:120])

    # 子记录：阶段任务（同一 title 幂等）
    subs = {}
    for rec in records:
        f = rec.get("fields") or {}
        parent = f.get("关联父任务")
        parent_ids = []
        if isinstance(parent, list):
            parent_ids = [str(x.get("record_id") or x.get("id") or "") for x in parent if isinstance(x, dict)]
        elif isinstance(parent, dict):
            parent_ids = [str(parent.get("record_id") or parent.get("id") or "")]
        if top_id and top_id in parent_ids:
            subs[_field_text(f.get("任务名称"))] = _record_id(rec)
    created_n = 0
    for title in (sub_titles or []):
        if not title or title in subs:
            continue
        try:
            meta = (sub_meta or {}).get(title) or {}
            sub_fields = {"任务名称": title, "关联父任务": [{"record_id": top_id}],
                          "需求来源": ["日常迭代"], "优先级": ["P0"],
                          "需求状态": [_STAGE_TO_REQ_STATUS.get(meta.get("progress") or "", "方案中")]}
            payload = {"create_records": [sub_fields]}
            try:
                # shape 探针只试一次（retries=1），避免确定性失败拖满退避重试
                bs.lark(["base", "+record-batch-create", "--table-id", tid,
                         "--json", json.dumps(payload, ensure_ascii=False)], token, retries=1)
            except bs.LarkError as exc1:
                if "shape" not in str(exc1):
                    raise
                # link 字段 CellValue 形态回退：lark-cli base 域提示用 {"id": ...}
                payload = {"create_records": [dict(sub_fields,
                            关联父任务=[{"id": top_id}])]}
                bs.lark(["base", "+record-batch-create", "--table-id", tid,
                         "--json", json.dumps(payload, ensure_ascii=False)], token)
            created_n += 1
        except bs.LarkError as exc:
            return ("fail", "写子记录失败（%s）：%s" % (title[:40], str(exc)[:160]))
    if created_n:
        results.append("新增子记录 %d 条" % created_n)
    else:
        results.append("子记录无新增")
    # v2.20：视图层级配置保障（数据就位后仍平铺 = 缺 hierarchy_config，PATCH 可设）
    if sub_meta:
        tid2 = bs.TABLES.get("②版本开发计划", {}).get("table_id", "")
        if tid2:
            hc = ensure_hierarchy_config(Path(vault), token, tid2)
        else:
            hc = "②表未登记 table_id，跳过"
        results.append("视图层级：" + hc)
    return ("ok", "；".join(results))


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    vault, project, json_file = ".", "", ""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--vault":
            vault = args[i + 1]; i += 2
        elif a == "--project":
            project = args[i + 1]; i += 2
        elif a == "--rows":
            json_file = args[i + 1]; i += 2
        else:
            i += 1
    if not project or not json_file:
        print("用法：base_progress.py --project <项目号> --rows <updates.json> [--vault <路径>]")
        return 2
    updates = json.loads(Path(json_file).read_text(encoding="utf-8"))
    status, msg = sync_progress(Path(vault), project, updates)
    print("[%s] %s" % (status, msg))
    return 0 if status == "ok" else (0 if status == "skip" else 1)


if __name__ == "__main__":
    sys.exit(main())
