---
name: OPC: Scaffold
description: "按需生成岗位产出骨架：立项只登记不产文件，岗位开工时才生成自己那一份"
argument-hint: "--role <岗位> [--project <项目名>] [--version V<N>]"
---

生成某个岗位在某版本的**产出骨架**（空模板 + frontmatter）。

**脚本**：`<skill包目录>/scripts/opc.py scaffold --role <role> [--project <项目>] [--version <V<N>>]`（`<skill包目录>` = 本 Skill 包所在目录，与宿主无关）。定位顺序：① 环境变量 `OPC_SKILL_DIR`；② 宿主注入的 skill 目录变量（指向包目录本身或其父目录）；③ 目录结构约定：`<本命令文件所在目录>/../../skills/opc-team/`（多数宿主把 `commands/` 与 `skills/` 放在同一配置根下）；④ 在工作区与用户主目录搜 `opc-team/scripts/opc.py`。用宿主可用的 Python 解释器以绝对路径调用。

**何时用**

- 岗位（pm / design / engineer / …）**开工写产出之前** —— 先有骨架，再填内容。
- 用户说「准备做设计了」「开工吧，先出交付模板」→ 本命令。
- **`opc done` 报「主产出未落盘」时** → 本命令是解法（先 scaffold 再填）。

**步骤**

1. **确定岗位与项目**：`--role` 必填（如 `pm` / `design` / `engineer`）；`--project` 省略则用当前项目；`--version` 省略则用交接状态里的当前版本。
2. **执行**：`scaffold --role <role> [--project <项目>] [--version V<N>]`。
3. **汇报**：生成了哪些文件（脚本会逐条打印 URI）、下一步要做什么。

**要点（v2.18 · 规范 §15.19）**

- **立项不预生成文件**：`opc new` / `opc version new` 只写版本台账、产出索引与交接链，**一个产出文件都不生成**。
- **只生成本项目该版本索引里登记的该岗资产**：派活项目里 pm 只点名了 `4_产品基线`，就只出那一份，不越权多出。
- **落点**：`V<N>/<role>/<资产>`（版本目录 → 职业目录）；阶段 0 / 1（`0_约束卡.md` / `1_MRD.md`）也随版本落职业目录（v2.20：strategist / market 子目录）。
- **已存在则跳过**，不覆盖已有内容。
- `scaffold --role strategist` 生成约束卡时**立即补写钉扎**（`pinned_refs` / `toolset_snapshot`，宪法 §10）。
- 标准动作序列：**scaffold → 填内容 → 改 `status` → `opc done`**。

**完成标准**：脚本打印出本岗产出文件的 URI，且该文件真实存在。

**深入**：手册 §43；规范 §15.19。
