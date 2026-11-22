---
name: OPC: Role
description: "单 Bot 模式：为指定职业生成本次会话的身份声明，并列出该岗必读文件"
argument-hint: "<role：strategist|market|pm|design|architect|engineer|growth|governance>"
---

取得某职业的本次身份声明。

**脚本**：`<skill包目录>/scripts/opc.py <子命令>`（`<skill包目录>` = 本 Skill 包所在目录，与宿主无关）。定位顺序：① 环境变量 `OPC_SKILL_DIR`；② 宿主注入的 skill 目录变量（指向包目录本身或其父目录）；③ 目录结构约定：`<本命令文件所在目录>/../../skills/opc-team/`（多数宿主把 `commands/` 与 `skills/` 放在同一配置根下）；④ 在工作区与用户主目录搜 `opc-team/scripts/opc.py`。用宿主可用的 Python 解释器以绝对路径调用。

**步骤**

1. role 缺失时询问；只接受当前 profile 的 `role_bindings` 中的键。
2. 执行 `role <role>`。
3. 向用户复述：本次身份 = 该 role，阶段 / 项目 / 必读文件清单。
4. 声明**一次会话只绑定一个 role**；换角色须新开会话。

**完成标准**：身份声明已输出，且已明确本次只按该岗位卡 scope 写入。

**深入**：一 Agent 一岗位 → 规范 §15.1；职业工作模式 → 手册 §14。
