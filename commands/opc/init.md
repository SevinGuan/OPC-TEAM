---
name: OPC: Init
description: "初始化 OPC Vault：目录骨架、岗位卡、职业简历、产出索引、opc.config.json"
argument-hint: "[--profile app|content] [--agent-mode single|multi|multi-profile] [--vault <目录>]"
---

初始化 OPC 工作台（幂等：重复运行不覆盖已有文件）。

**脚本**：`<skill包目录>/scripts/opc.py <子命令>`（`<skill包目录>` = 本 Skill 包所在目录，与宿主无关）。定位顺序：① 环境变量 `OPC_SKILL_DIR`；② 宿主注入的 skill 目录变量（指向包目录本身或其父目录）；③ 目录结构约定：`<本命令文件所在目录>/../../skills/opc-team/`（多数宿主把 `commands/` 与 `skills/` 放在同一配置根下）；④ 在工作区与用户主目录搜 `opc-team/scripts/opc.py`。用宿主可用的 Python 解释器以绝对路径调用。

**步骤**

1. Vault 路径：用户未给时询问，**推荐项排第一 = 当前工作目录下的 `./OPC-Vault`**，其次才列独立目录（如 `~/OPC-Vault`）；不擅自选盘符。
2. profile：用户未给时确认 `app`（默认，APP / 互联网产品）或 `content`（内容创作）。
3. **运行模式（团队形态）**：用户未给时确认 `single`（默认，单 Bot 切换身份）/ `multi`（每职业一个独立 Agent）/ `multi-profile`（一职业 = 一个独立持久身份 + 独立记忆，需宿主支持 Profile 类机制）。说清代价：`multi` / `multi-profile` 提供的是**独立身份**，不等于流水线自动并行（并行度取决于不同项目波次）；OPC **不采用**「主 Agent 调度子 Agent、互相 @ 唤醒」这类对话式团队（宪法 §15.3 禁止 Agent 间对话内传递）。之后可随时 `opc mode <值>` 切换。
4. **是否现在创建项目（每次初始化必问）**：主动问一次「是否现在创建一个项目？」。选项固定为 **否，先只搭骨架（推荐/默认）→ 是，现在创建**。选「是」时先完成 Vault 骨架，再收集 1 个项目名 + 2–4 个类型标签（从当前 profile 的 `tags_vocab` 选），然后执行独立的 `opc new`；不要塞进 `init` 命令。用户不表态按「否」处理。
5. **宿主入口**：`init` 默认在工作区根写一段受控区块（没有入口文件时创建 `AGENTS.md`，有则不动）。用户不想让 Vault 同级出现入口文件时，跑 `init --no-attach`；之后要接回可用 `attach --apply`，撤销用 `attach --remove --apply`。
6. 云端仓库（产出副本）：默认纯本地。用户要飞书时先让其挑已有文件夹或新建，再 `--cloud-repo auto --workspace <名称|链接|token>`。**本步只配「产出副本」，与「职业消息机器人」（各职业收发消息）是两件事**，不要混为一谈。
7. **职业范围（`multi` / `multi-profile` 时必问）**：询问要建哪些职业身份，选项顺序固定：**前五岗（推荐/默认）** `strategist, market, pm, design, architect` → 完整 8 岗 → 自定义。用户不表态按前五岗。建身份走 `/opc:provision`（**必须显式传 `--roles`**，不要依赖脚本无参数时建全量）。
8. 执行 `init --vault <vault> --profile <profile> --agent-mode <mode> --non-interactive [--no-attach] [--cloud-repo auto --workspace ...]`。
9. 执行 `status --vault <vault>`。
10. `multi-profile` 下：先跑一次代建 dry-run 确认计划，再由用户决定是否 `--apply`；**职业消息机器人**（一职业 = 一个独立飞书应用 + 一套独立凭证，**凭证不复用**，N 个职业身份 = N 个应用）默认「稍后配置」，要现在配就显式选「现在配置」。选「现在配置」只表示逐身份配置该职业自己的消息应用 —— **应用创建、发布审批与扫码都在飞书侧由创始人完成，Skill 不代建、不代发布、不代扫码**；⚠️ **无 TTY 环境（Agent 会话）下官方向导会假成功**，改走浏览器代建：登录码用 `lark-cli auth qrcode` 直接渲染 PNG（**禁止浏览器截图**）→ 建应用 → 写该身份 `.env` → 重跑 provision（幂等）；⚠️ **建应用只能走控制台 UI**：应用管理接口只收 tenant token，user token 报 `99991668 user access token not support` —— 这**不代表路不通**，浏览器代建照常有效，别误判成「没路了」回头走截图；未接通（`missing/retrying`）一律如实报未连通。见 `/opc:provision`。

**完成标准**：`status` 无报错，且 Vault 内 `opc.config.json`、`99_Agent配置/岗位卡/`、`99_Agent配置/履历/`、`30_项目/_产出索引.md` 都存在。

**深入**：配置字段 → `references/命令与配置手册.md` §2；Bot 部署 → §8；运行模式 → §8.2 / §8.5；profile → §9；代建扩展 → `EXTENSIONS.md`。
