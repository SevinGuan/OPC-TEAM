#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""飞书多维表格 · 演示数据清理器（OPC v2.15）

**为什么单独一个脚本**：base_schema.py 是*结构同步器*，它的安全承诺是「不删列、不删行」。
删行不可逆，所以必须与结构同步**分开、单独显式触发、单独备份、单独留痕**——
不能混在一次 `--apply` 里让人分不清哪一步会毁数据。

清理范围 = base_schema.TABLES 里的 5 张业务表。
模板自带的两张引导表（💡 模板使用说明 / 🎉 快速上手多维表格）不在 TABLES 里 → 天然不碰。

用法：
  python scripts/base_clean.py --list                          # 只读列出每表每行
  python scripts/base_clean.py --apply --dry-run               # 走一遍流程但不真删
  python scripts/base_clean.py --apply --yes                   # 真删（自动先备份）
  python scripts/base_clean.py --apply --yes --keep-table ①需求池（关联项目）

退出码：0 = 成功；2 = 参数错误（缺 --yes）；3 = 飞书接口调用失败。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from base_schema import (  # noqa: E402
    BACKUP_DIR,
    LarkError,
    TABLES,
    lark,
    resolve_base,
    _force_utf8_io,
)

# 每张表取哪一列当「这一行是什么」的人话标签
LABEL_FIELDS = {
    "项目汇总": "项目名称",
    "①需求池（关联项目）": "任务名称",
    "②版本开发计划": "任务描述",
    "③版本 Bug 记录": "问题名称",
    "发布版本": "版本编号",
}


def list_rows(base_token, table_id, limit=200):
    """取一表的行（record_id + 每格值）。返回 (列名, 行列表)。"""
    data = lark(["base", "+record-list", "--table-id", table_id, "--limit", str(limit)], base_token)
    fields = data.get("fields") or []
    ids = data.get("record_id_list") or []
    matrix = data.get("data") or []
    rows = []
    for i, rid in enumerate(ids):
        cells = matrix[i] if i < len(matrix) else []
        rows.append({"record_id": rid, "cells": cells})
    return fields, rows


def label_of(fields, row, table_name):
    """取该行的可读标签（优先用指定列，否则用第一个非空文本格）。"""
    want = LABEL_FIELDS.get(table_name)
    if want and want in fields:
        idx = fields.index(want)
        if idx < len(row["cells"]):
            v = row["cells"][idx]
            if isinstance(v, str) and v.strip():
                return v.strip()
            if v:
                return json.dumps(v, ensure_ascii=False)
    for v in row["cells"]:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def delete_rows(base_token, table_id, record_ids):
    """删一批行（单次调用传全部 id）。"""
    payload = json.dumps({"record_id_list": list(record_ids)}, ensure_ascii=False)
    lark(["base", "+record-delete", "--table-id", table_id, "--json", payload, "--yes"], base_token)


def main(argv=None):
    ap = argparse.ArgumentParser(description="飞书多维表格演示数据清理器")
    ap.add_argument("--list", action="store_true", help="只读列出每表每行")
    ap.add_argument("--apply", action="store_true", help="执行清理")
    ap.add_argument("--dry-run", action="store_true", help="配合 --apply：走流程但不真删")
    ap.add_argument("--yes", action="store_true", help="确认真删（高危动作，必须显式给）")
    ap.add_argument("--keep-table", action="append", default=[], help="排除某张表（可重复）")
    ap.add_argument("--base-token", default="", help="多维表格 token（默认取环境变量或配置文件）")
    ap.add_argument("--config", default="", help="opc.config.json 路径（默认自动查找）")
    ap.add_argument("--vault", default="", help="Vault 目录（取其中的 opc.config.json）")
    args = ap.parse_args(argv)

    if not (args.list or args.apply):
        ap.print_help()
        return 2
    if args.apply and not args.dry_run and not args.yes:
        print("删行是高危动作：真删必须显式加 --yes（试跑用 --dry-run）", file=sys.stderr)
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
        tables = {n: s for n, s in TABLES.items() if n not in args.keep_table}
        snapshot = {
            "base_token": token,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "tables": {},
        }
        total = 0
        for name, spec in tables.items():
            fields, rows = list_rows(token, spec["table_id"])
            snapshot["tables"][name] = {
                "table_id": spec["table_id"],
                "fields": fields,
                "records": [
                    {"record_id": r["record_id"], "label": label_of(fields, r, name), "cells": r["cells"]}
                    for r in rows
                ],
            }
            total += len(rows)
            print("△ %s：%d 行" % (name, len(rows)))
            for r in rows:
                print("    %s  %s" % (r["record_id"], label_of(fields, r, name)))

        if args.list:
            print("\n合计 %d 行（--list 不做任何删除）" % total)
            return 0

        if args.apply and not args.dry_run:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            bpath = BACKUP_DIR / ("records-%s.json" % stamp)
            bpath.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            print("\n删前备份 → %s" % bpath)

        for name, spec in tables.items():
            ids = [r["record_id"] for r in snapshot["tables"][name]["records"]]
            if not ids:
                continue
            if args.dry_run:
                print("    [试跑] %s：删 %d 行" % (name, len(ids)))
                continue
            delete_rows(token, spec["table_id"], ids)
            print("    → %s：已删 %d 行" % (name, len(ids)))

        if args.dry_run:
            print("\n试跑完成（未删任何行）")
            return 0

        print("\n回读校验：")
        for name, spec in tables.items():
            _, rows = list_rows(token, spec["table_id"])
            print("    %s：剩 %d 行" % (name, len(rows)))
        print("\n清理完成")
        return 0
    except LarkError as exc:
        print("飞书接口调用失败：%s" % exc, file=sys.stderr)
        return 3


if __name__ == "__main__":
    _force_utf8_io()
    sys.exit(main())
