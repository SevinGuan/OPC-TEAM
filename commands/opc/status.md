---
name: OPC: Status
description: "OPC 自检：profile、后端绑定、凭证变量是否就位、产出索引漏写"
argument-hint: "[--vault <目录>]"
---

对当前 Vault 做一次只读自检。

**脚本**：`<skill包目录>/scripts/opc.py <子命令>`（`<skill包目录>` = 本 Skill 包所在目录，与宿主无关）。定位顺序：① 环境变量 `OPC_SKILL_DIR`；② 宿主注入的 skill 目录变量（指向包目录本身或其父目录）；③ 目录结构约定：`<本命令文件所在目录>/../../skills/opc-team/`（多数宿主把 `commands/` 与 `skills/` 放在同一配置根下）；④ 在工作区与用户主目录搜 `opc-team/scripts/opc.py`。用宿主可用的 Python 解释器以绝对路径调用。

**步骤**

1. 确定 Vault（默认 `./OPC-Vault`，以 `opc.config.json` 的 `vault_root` 为准）。
2. 执行 `status`。
3. 用人话汇总：profile、已绑定后端、缺失的凭证**变量名**（只报名字，绝不打印值）、索引漏写项。

**完成标准**：每条异常都给出下一步具体命令（如 `bind` / `bind-cli` / 补登记索引）。

**深入**：配置字段 → `references/命令与配置手册.md` §2；故障排查 → §7。
