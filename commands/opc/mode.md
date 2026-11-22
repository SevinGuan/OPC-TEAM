---
name: OPC: Mode
description: "查看 / 切换运行模式：single 单 Bot / multi 多 Bot / multi-profile 多 Profile 增强"
argument-hint: "[single|multi|multi-profile]"
---

查看或切换 OPC 的**运行模式**（配置字段 `agent_mode`）——团队形态的唯一写入入口。

**脚本**：`<skill包目录>/scripts/opc.py <子命令>`（`<skill包目录>` = 本 Skill 包所在目录，与宿主无关）。定位顺序：① 环境变量 `OPC_SKILL_DIR`；② 宿主注入的 skill 目录变量（指向包目录本身或其父目录）；③ 目录结构约定：`<本命令文件所在目录>/../../skills/opc-team/`（多数宿主把 `commands/` 与 `skills/` 放在同一配置根下）；④ 在工作区与用户主目录搜 `opc-team/scripts/opc.py`。用宿主可用的 Python 解释器以绝对路径调用。

**步骤**

1. 不带参数时执行 `mode`，向用户复述当前模式与三档说明。
2. 切换时执行 `mode <single|multi|multi-profile>`；值不合法时脚本会直接报错并列出合法值。
3. 切换后向用户说明该档的**前提**与**代价**（见下），不要只说「已切换」。
4. `multi` / `multi-profile` 档补一句：`opc bots` 导出配置卡；`multi-profile` 还须满足「所有 Profile 指向同一个 Vault」。

**三档取值**

| 值 | 含义 | 前提 | 代价 / 注意 |
|---|---|---|---|
| `single`（默认） | 单 Bot 按阶段切换身份 | 任何支持自定义提示词 / 知识库的 Agent | 每次开工开新会话；边界按**会话**成立（规范 §15.1） |
| `multi` | 每职业一个独立 Agent 实例 | 宿主支持创建多个 Bot | 8 个 Agent 存在 ≠ 8 条流水线并行 |
| `multi-profile` | 独立身份 + 独立记忆 | 宿主支持独立持久身份（Profile 类机制） | **workspace 必须指向同一个 Vault**，否则交接链断（规范 §15.3） |

**完成标准**：目标模式已写入 `opc.config.json` 的 `agent_mode`，且已向用户说明该档的前提与代价。

**注意**：OPC **不采用**「主 Agent 调度子 Agent、互相 @ 唤醒」这类**对话式团队** —— 宪法 §15.3 禁止 Agent 间对话内传递，交接一律走文件落盘 + `opc done` 回执。`multi` / `multi-profile` 提供的是**独立身份**，流水线并行靠**不同项目波次**（手册 §22.2 / §23.4）。

**深入**：运行模式 → 手册 §8.2 / §8.5；宿主能力矩阵 → §21；术语 → 工作区根 `GLOSSARY.md` 第五节、`docs/adr/0001-多agent协同形态.md`。
