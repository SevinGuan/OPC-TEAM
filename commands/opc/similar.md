---
name: OPC: Similar
description: "按类型标签检索相似项目及全部历史落地物 URI"
argument-hint: "--tags \"#标签1 #标签2\" [--role <role>]"
---

按标签检索历史经验。

**脚本**：`<skill包目录>/scripts/opc.py <子命令>`（`<skill包目录>` = 本 Skill 包所在目录，与宿主无关）。定位顺序：① 环境变量 `OPC_SKILL_DIR`；② 宿主注入的 skill 目录变量（指向包目录本身或其父目录）；③ 目录结构约定：`<本命令文件所在目录>/../../skills/opc-team/`（多数宿主把 `commands/` 与 `skills/` 放在同一配置根下）；④ 在工作区与用户主目录搜 `opc-team/scripts/opc.py`。用宿主可用的 Python 解释器以绝对路径调用。

**步骤**

1. 标签缺失时先从当前 profile 的 `tags_vocab` 让用户选，拼成 `--tags "#A #B"`。
2. 执行 `similar`；可加 `--role` 只看某职业的落地物。
3. 汇报每个命中项目的落地物 URI，并按规范 §15.7 判断**哪些适用本次**（简历只作参考，适用与否由规则层 `applies_when` 决定）。

**完成标准**：命中项目的落地物 URI 全部列出，且逐项给出「适用 / 不适用 + 理由」，不直接照抄历史产出。

**深入**：相似检索判据 → 规范 §15.7；简历自进化 → §15.6。
