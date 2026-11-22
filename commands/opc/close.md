---
name: OPC: Close
description: "项目收尾：完整性检查、回填各职业简历出处、索引置为已归档"
argument-hint: "<PXXX> [--link] [--archive]"
---

收尾一个项目。

**脚本**：`<skill包目录>/scripts/opc.py <子命令>`（`<skill包目录>` = 本 Skill 包所在目录，与宿主无关）。定位顺序：① 环境变量 `OPC_SKILL_DIR`；② 宿主注入的 skill 目录变量（指向包目录本身或其父目录）；③ 目录结构约定：`<本命令文件所在目录>/../../skills/opc-team/`（多数宿主把 `commands/` 与 `skills/` 放在同一配置根下）；④ 在工作区与用户主目录搜 `opc-team/scripts/opc.py`。用宿主可用的 Python 解释器以绝对路径调用。

**步骤**

1. 缺项目号时询问（`PXXX`）。
2. 执行 `close <PXXX> --link`（`--archive` 同时归档）。
3. 汇报：回填了哪些职业简历的 C 节条目与 `project_refs`、索引状态变化、仍缺的产出项。
4. 提醒用户补写「怎么落地的 / 坑与解法 / 结果」，并按规范 §9 登记可沉淀项。

**完成标准**：索引状态为「已归档」，各职业简历 C 节能按 `/opc:similar` 反查到本项目 URI。

**深入**：升格与回流 → 规范 §9 / §15.6；产出索引 → §15.8。
