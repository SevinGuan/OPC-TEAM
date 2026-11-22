---
name: OPC: Handoff
description: "查看当前交接状态（项目 / 阶段 / 回执 / 下一棒 / 通知通道），或切换当前项目"
argument-hint: "[--project PXXX]"
---

查看交接状态。

**脚本**：`<skill包目录>/scripts/opc.py <子命令>`（`<skill包目录>` = 本 Skill 包所在目录，与宿主无关）。定位顺序：① 环境变量 `OPC_SKILL_DIR`；② 宿主注入的 skill 目录变量（指向包目录本身或其父目录）；③ 目录结构约定：`<本命令文件所在目录>/../../skills/opc-team/`（多数宿主把 `commands/` 与 `skills/` 放在同一配置根下）；④ 在工作区与用户主目录搜 `opc-team/scripts/opc.py`。用宿主可用的 Python 解释器以绝对路径调用。

**步骤**

1. 不带参数：读取 `opc.config.json` 的 `active_project`。
2. 带 `--project PXXX`：切换当前项目后再展示。
3. 汇报：当前项目、阶段、上一棒回执、下一棒 role 与必读文件、通知通道是否可用（不可用是否已降级）。

**完成标准**：用户能一句话说出「现在停在谁手上、下一步该谁干」。

**深入**：交接协议 → 规范 §15.3；通知降级 → §15.9。
