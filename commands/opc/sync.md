---
name: OPC: Sync
description: "云端仓库：各岗本地入队、pm 推送队列、拉云端任务与知识（只读）"
argument-hint: "[--queue | --flush | --role <role>] [tasks | search --query 词 | status]"
---

云端仓库操作。**远程写入权只归 pm**，其它岗位只能本地入队。

**脚本**：`<skill包目录>/scripts/opc.py <子命令>`（`<skill包目录>` = 本 Skill 包所在目录，与宿主无关）。定位顺序：① 环境变量 `OPC_SKILL_DIR`；② 宿主注入的 skill 目录变量（指向包目录本身或其父目录）；③ 目录结构约定：`<本命令文件所在目录>/../../skills/opc-team/`（多数宿主把 `commands/` 与 `skills/` 放在同一配置根下）；④ 在工作区与用户主目录搜 `opc-team/scripts/opc.py`。用宿主可用的 Python 解释器以绝对路径调用。

**步骤**

1. 各岗干完本步：`sync --role <role>` 或 `done --sync-self`，只写本地队列记录，**不碰远程**。
2. pm 推送：`sync --flush`（增量版本目录，历史保留不覆盖）；先 `--queue` 看待推积压。
3. 只读：`sync tasks` 拉云端未完成任务、`sync search --query 词` 搜云端知识。
4. `pull` 会用云端覆盖本地，**必须用户显式同意后才加 `--allow-restore`**。
5. 未配置 `folder_token`、未装 `lark-cli` 或未认证 → 提示一句并跳过，不阻塞任何流程。

**完成标准**：入队成功或推送成功；降级情形已明确说明「跳过」而非失败。

**深入**：云端仓库 → 手册 §32；增量同步队列 → §36；范围隔离 → 规范 §15.15 / §15.16。
