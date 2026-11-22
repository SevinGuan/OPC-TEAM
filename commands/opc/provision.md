---
name: OPC: Provision
description: "把各职业落成独立身份（multi-profile）：描述 / 人设文件 / 共享同一 Vault；可选逐身份配独立消息应用"
argument-hint: "[--roles a,b,c] [--host auto|manual] [--apply] [--update] [--feishu-bots now|later]"
---

把 OPC 的职业在**宿主**里落成独立身份（一职业 = 一个持久身份 + 独立记忆）。这是 `multi-profile` 模式的落地动作；`single` 模式不需要它。

**脚本**：`<skill包目录>/scripts/provision_profiles.py`（定位同其它命令：`OPC_SKILL_DIR` → 宿主注入的 skill 变量 → 目录约定 → 文件搜索；见手册 §37.4）。核心宿主无关：**怎么建身份由宿主适配器决定**（`scripts/hosts/<适配器>.py`），`--host auto` 自动探测，`--host manual` 是通用回退（不代建，只产出落地清单，任何宿主通用）。

**步骤**

1. 先确认运行模式：`python <包>/scripts/opc.py mode --vault <Vault>`。不是 `multi-profile` 就先执行 `mode multi-profile`。
2. 跑一次 dry-run，把计划念给用户：`python <包>/scripts/provision_profiles.py --vault <Vault> [--roles …] [--host …] [--feishu-bots now|later]`。逐职业读出 description（岗位卡 §1）与将写入的人设文件路径。若初始化中用户选了「现在配置」消息应用，在同一次计划里带 `--feishu-bots now`，让用户看到每个身份都会各开一次宿主官方向导。
3. 用户确认后加 `--apply`。**不要**替用户决定建哪些职业；若初始化已确认职业范围，按确认结果显式传 `--roles`。职业范围选项顺序固定为：**前五岗（推荐）`strategist,market,pm,design,architect` → 完整 8 岗 → 自定义**；用户不表态就取前五岗，不要依赖脚本无参数时建全量。消息应用的选择必须沿用初始化中的答复：**现在** → `--feishu-bots now`；**稍后** → 默认 `later`，**不得擅自创建或发布平台应用**（应用创建、发布审批与扫码都在飞书侧由创始人完成）。
4. 消息应用阶段（仅 `now`）：脚本逐角色调用**宿主自己的**官方向导（命令由适配器提供，形如 `<宿主> -p <role> gateway setup`），每个角色由用户选择 Feishu / Lark 并扫码；**一个身份一个不同应用**，禁止复用凭证。脚本会查重并在配置完成后等待连接状态为 `connected`；`missing/retrying` 只能报未连通。
   ⚠️ **向导是 curses 菜单，无 TTY 会假成功**（秒退且看似完成，实际拿不到凭证）——此时别反复重跑向导，改走 `EXTENSIONS.md` 的 `needs_founder` 完成路径 b（**链接代建** + v2.29.4 **凭证回填表单**）：Agent 发 `https://open.feishu.cn/page/launcher?from=backend_oneclick`（一键 Agent 应用，机器人/事件/权限预配）+ 应用名清单（**名字随意**——绑定只认凭证不认名字，建议 `OPC·<role>` 只是为了对得上岗位），**同一入口地址建全部应用**（登录态持久，建完一个接着填下一个，页面跳走重开同一链接即可，不用重新扫码/登录）；同时把包内 `assets/凭证回填表单模板.html` 复制到 `<Vault>/99_Agent配置/凭证回填表单.html`、只改顶部 `ROLES` 数组为本轮 `--roles`，用宿主内嵌预览（Hermes 桌面 `::preview`）随链接一起交付 → 创始人**自己的浏览器**建应用、把各组 App ID / Secret **填进表单点「发送给 Agent」**（无内嵌预览的宿主，表单自动退化为生成文本让创始人复制粘贴）→ Agent 写各身份 `.env`（值只落 .env，不回显）→ 重跑 `provision_profiles.py --apply`（幂等）；**首响采集**（v2.29.4）：allowlist 未配时群里首条 @ 必然无响应（预期，安全设计）——创始人 @ 全部机器人发一条，Agent 从网关日志抓各应用视角 `ou_`（`Unauthorized user` 行）与 `chat_id`（`Inbound group message` 行），写 `FEISHU_ALLOWED_USERS` + `notify.dispatch.chat_id`（union_id 同场可取：lark-cli 用户身份读群成员），约 30 秒重扫后再发一条确认有回复。⚠️ **Agent 别用自己代管的浏览器登录控制台代建**：无人值守会话里浏览器随时被回收，扫码确认永远送不到（v2.29.3 实测复现 3 次）。⚠️ **建应用只能走控制台 UI**：应用管理接口只收 tenant token，user token 报 `99991668 user access token not support`。
5. 建完核验三条（命令按宿主官方文档）：身份列表里目标职业都在、工作目录都指向**同一个** Vault（绝对路径）、人设文件内容等于对应 Bot 配置卡 §一 的系统提示词；若配置了消息应用，再看该宿主的网关运行态。
6. 提醒边界：`--clone`（适配器支持时）只继承当前身份的配置与密钥；模型 / 工具权限仍归用户；扫码与飞书应用发布 / 审批归用户；`governance` 不在 0–8 交接链，不调 `opc done` / `opc go`。

**完成标准**：目标职业都有独立身份，工作目录指向**同一个** Vault，人设文件等于对应 Bot 配置卡 §一 的系统提示词；并已向用户交代「独立身份 ≠ 流水线并行」（手册 §8.2 / §21）。若选择 `now`，还要每个职业有独立飞书凭证且连接状态为 `connected` 才能说机器人已上线；`later` 则明确标注「消息机器人待配置」。

**注意**：默认 dry-run、`--feishu-bots later`、已存在的身份默认跳过；旧人设文件会备份为 `<名>.bak-<时间戳>`。飞书**云端仓库 ≠ 消息网关**，前者同步产出，后者用于收发消息。脚本不直接调用飞书注册 API、不复制或回显凭证、不碰模型与权限。用法与适配器接口见 `EXTENSIONS.md`。
