# 本地扩展（未上游）

本包在 **opc-team v2.15.1** 基础上加了「宿主适配能力」。上游 §8.1 的边界是「只给说明书与配置卡，实例由宿主侧建」，所以「检测宿主 + 代建」上游没有；这层扩展补的就是这一段，**只在本机生效，不改变规范、岗位卡与状态机**。

## 新增了什么

| 文件 | 作用 |
|---|---|
| `scripts/provision_profiles.py` | **宿主无关的代建核心**：检查 `agent_mode=multi-profile`，按岗位卡把各职业落成独立身份；可选逐身份配置独立飞书消息应用（`--feishu-bots now\|later`） |
| `scripts/hosts/hermes.py` | **宿主适配器（示例实现）**：把「建身份 / 写人设文件 / 设工作目录 / 开消息向导」落到某个具体宿主的命令行上 |
| `scripts/hosts/manual.py` | **通用回退适配器**：不代建任何身份，只产出「落地清单」，任何宿主（哪怕没有 Profile 概念）都能照着手工做 |
| `commands/opc/provision.md` | 斜杠命令源（`/opc:provision`），供有 commands 目录的宿主使用 |
| `EXTENSIONS.md` | 本文件 |

**为什么要拆成「核心 + 适配器」**：OPC 只规定「三条映射」，**不该规定某个宿主的命令行**。核心只管「计划 → 落地 → 核验」，宿主特有的部分（目录布局、命令名、状态文件格式、消息向导）全部收在适配器里。换宿主 = 往 `scripts/hosts/` 里再放一个同接口的文件，**核心与规则层一行都不用改**。

## 它做/不做

**做**（每个职业三条映射，全部有出处）：

1. 身份 `description` ← 岗位卡 §1「职业定位」
2. 人设文件（`SOUL.md`）← `Bot配置卡_<role>.md` §一「系统提示词」整段（不裁剪，见配置卡第五节）
3. 工作目录（workspace / cwd）← Vault 绝对路径（共享同一份 Vault，否则 §15.3 文件交接断）
4. 可选：`.env` 写 `OPC_SKILL_DIR=<包目录>`（命令文件四级定位的第 1 级）
5. 可选：`--feishu-bots now` 时逐身份调用**宿主自己的**官方向导，创建**每个职业自己的**飞书消息应用；`later` 只输出后续命令

**不做**：不指定模型、不授予工具权限、不直接调用飞书注册 API、不复制凭证、不代扫码、不写业务文档。建身份调用的是**宿主自己的**建身份命令（`--clone` 只是继承当前身份的配置/密钥/技能，等价于用户在界面里勾「clone from default」）。飞书**云端仓库 ≠ 消息网关**：前者同步产出，后者用于收发消息，两者互不阻塞。

## 用法

```bash
# 1) 看计划（什么都不写；Bot 配置卡会先导出，那是 Vault 内产物）
python scripts/provision_profiles.py --vault ~/OPC-Vault

# 2) 推荐默认：只建前段 5 个职业
python scripts/provision_profiles.py --vault ~/OPC-Vault \
  --roles strategist,market,pm,design,architect --apply

# 3) 后来要补 engineer / growth / governance
python scripts/provision_profiles.py --vault ~/OPC-Vault --roles engineer,growth,governance --apply

# 4) 规范/提示词改版后，把已存在身份的 SOUL 与 cwd 同步一遍
python scripts/provision_profiles.py --vault ~/OPC-Vault --update --apply

# 5) 初始化中选了「现在配置飞书机器人」：身份建好后配置消息网关
#    ⚠️ 无 TTY 环境（Agent 会话）下向导会假成功，改走**链接代建**（v2.29.3）：
#    Agent 发链接 + 应用名清单 → 创始人自己的浏览器建应用、发回凭证
#    → Agent 写各身份 .env → 重跑本命令（幂等）
python scripts/provision_profiles.py --vault ~/OPC-Vault --apply --feishu-bots now

# 6) 先看机器人配置计划但不执行（默认就是 later + dry-run）
python scripts/provision_profiles.py --vault ~/OPC-Vault --feishu-bots now

# 7) 宿主没有可用适配器时：产出「落地清单」，照着手工做
python scripts/provision_profiles.py --vault ~/OPC-Vault --host manual --apply
```

默认行为：身份已存在就跳过（不覆盖别人的人设文件），加 `--update` 才同步；旧人设文件备份为 `<名>.bak-<时间戳>`。`--json` 输出结构化结果。

消息应用阶段是**幂等**的：已有完整 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 的身份保持原配置，不重建应用；脚本会对默认身份与全部命名身份做凭证指纹查重，发现同一凭证属于多个身份就停止，避免 `duplicate_credential`。配置后等待该身份的连接状态为 `connected`（默认最多 45 秒，`--gateway-verify-timeout 0` 只查一次）；`missing/retrying` 会如实报未连通。扫码、飞书应用发布与审批仍由创始人在飞书完成。

## 检测逻辑

- `agent_mode` 读 `<Vault>/opc.config.json`；不是 `multi-profile` 直接退出并提示 `opc mode multi-profile`
- 宿主：`--host auto` 按序探测 `scripts/hosts/*.py`，第一个探测通过即采用；探测不到就提示换 `--host manual` 或 `opc mode single`
- 职业集合：读 `<Vault>/99_Agent配置/岗位卡/*.md`，不写死 8 个

## 适配器接口（写一个新宿主就照这个）

放在 `scripts/hosts/<名字>.py`，核心按约定调用：

| 成员 | 作用 |
|---|---|
| `HOST_NAME` / `HOST_LABEL` | 适配器名（`--host <HOST_NAME>`）与显示名 |
| `CAN_CREATE` | 是否代建身份（`False` = 只出落地清单） |
| `detect()` | `{"ok": bool, "home": str, "detail": str}` —— 当前环境是否可用 |
| `profile_exists(role)` | 该职业的身份是否已存在 |
| `create_profile(role, desc, clone)` | 建身份 → `{"ok", "status", "message"}` |
| `write_identity(role, file, text)` | 写入人设文件 |
| `set_workspace(role, path)` | 把工作目录指向 Vault |
| `env_path(role)` | 该身份 `.env` 的位置（`None` = 不提供） |
| `messaging_command(role)` | 打开消息向导的命令（用于「稍后配置」时逐条打印） |
| `setup_messaging(role)` | 执行消息向导 → `{"ok", "status", "message"}` |
| `messaging_states(roles)` | `{"connected" \| "missing" \| "retrying" \| …}` 连接状态 |

**约束**：适配器里不得假设宿主之外的目录结构；不得读写 Vault 业务文档；凭证只读键名与指纹，**绝不打印值**。

## 顺带修的两处上游小问题

1. `opc queue` 结尾原来硬写「当前 single 模式：…」，不管 `agent_mode` 是什么值——`multi` / `multi-profile` 下也照样这么印，容易误导。已改为按 `agent_mode` 分支输出（`single` / `multi` / `multi-profile` 三句），并补一句「并行度取决于项目数」。
2. `opc init` 现在支持 `--no-attach`：默认会在工作区根写一段受控区块（没有入口文件时创建 `AGENTS.md`），用户不想让 Vault 同级出现入口文件时可跳过；之后要接回用 `attach --apply`，撤销用 `attach --remove --apply`。

## 与宪法的一致性

宪法 §8.1 禁止 Skill 在宿主里注册 Bot 实例、指定模型、授予权限。本扩展：

- **不**直接调用平台 API 注册 Bot、**不**改模型/工具权限；
- 只在创始人**显式**选择（`--feishu-bots now`）并 `--apply` 时调用宿主的官方向导；扫码与发布仍归用户；
- 默认 dry-run + `--feishu-bots later`，与 `install_commands.py` 的「宿主适配脚本」定位一致；
- 绝不跨身份复制飞书凭证，并在配置前后做指纹查重；
- 规则层与执行层**不绑定任何单一宿主**：宿主名只出现在适配器实现与本说明里，核心脚本与斜杠命令只说「宿主」。

上游若接纳这段，把核心并入 `opc.py` 的 `provision` 子命令即可（适配器目录刻意独立，便于丢弃或替换）。

## v2.23 修复的五个实测缺口（2026-11-18 · Hermes 宿主 · 从零安装实测）

记录口径：每条都是**实测复现**并**已修复**；每条附回归自测。
回归自测：`scripts/selftest_v223.py`（5 个修复专项）+ `scripts/selftest_e2e.py`（端到端链路）。

### F1 · `provision_profiles.py --feishu-bots now` 在 Hermes 上必然失败 → 已修

**现象**：`scripts/hosts/hermes.py` 的 `setup_messaging()` 用
`subprocess.run([hermes, -p, role, gateway, setup])` 调向导，**没有分配 PTY**。
Hermes 的 `gateway setup` 是 curses 菜单（`curses_single_select` + prompt_toolkit raw mode），
读不到按键就把空输入当成选择 → 5 个身份全部「[x] 向导结束但未检测到完整的
FEISHU_APP_ID / FEISHU_APP_SECRET」。

**修法**：新增 `scripts/pty_runner.py`（纯标准库 `pty`，POSIX；Windows 退化为普通
subprocess），用 `pty.openpty()` 给子进程一个真终端，并按脚本喂「8 + 回车」
选菜单里的 Feishu / Lark。适配器改调 `pty_runner.run_in_pty(...)`。
**状态判定仍以 `.env` 里是否真拿到凭证为准，不信任退出码**（退出码非 0 时向导
可能已经写好了凭证）。

### F7 · `opc claim` 覆盖交接状态 → 已修

**现象**：`cmd_claim` 领活时**无条件重建**该项目的交接状态，只写 7 个字段，
把 `dispatch_chain` / `dispatch_roles` / `decisions`（闸门拍板留痕）/ `asset_batch` /
`gate` / `confirmed_at` 全部冲掉。实测把 P002 从「阶段 5 · design」改写成
「阶段 0 · strategist」，两次闸门拍板留痕归零。
配套第二个坑：**自动领活会自我强化** —— `infer_tasks()` 依据交接状态推算可开工棒
→ claim 改写交接状态 → 下一轮 tick 又按新状态推算 → 反复认领同一项目。

**修法**：领活改为**读旧状态 + 只更新游标字段**（project / step / stage / role /
doc / status / token / next_role / claimed_at），其余字段一律保留；
交接状态仍由 `opc done` / `opc advance` 维护，不由领活维护。

### F9 · `opc go` 不校验当前棒是不是闸门 → 已修

**现象**：`opc go --decision 通过` 打在**非闸门**棒上也会照走。
实测在多项目场景下，把 P003 阶段 2 的闸门结论记到了 P002 阶段 5 上。

**修法**：`cmd_go` 先算 `_is_gate = state.gate or (step in gates)`；
带了 `--decision` 但当前棒不是闸门 → 报错并提示改用 `opc advance`。
非闸门棒走 `advance`（pm 代推），闸门棒走 `go`（创始人拍板），两种语义不再混用。

### F10 · agent 内部拿不到通知通道的凭证 → 已修

**现象**：`ExecBackend.check()` 用 `os.environ.get(credential_env)` 判凭证，
但 Hermes 的 terminal 工具**只透传 `TERMINAL_*` 前缀**给子进程
（`tools/terminal_scope.py`），其余变量一律过滤 —— 即使给 agent 进程喂了环境变量
也没用（实测 agent 的 terminal 里 `env | grep FEISHU_WEBHOOK_URL` 返回 NOT_FOUND）。
于是 `opc done` 的飞书通知**静默降级**为「写本地文件」，创始人在群里收不到，
而 `opc done` 本身**不报错**（降级是有意设计）。

**修法**：`ExecBackend.credential(name)` 按 **① 进程环境变量 → ② `<HERMES_HOME>/.env`**
两级查找。`HERMES_HOME` 在 agent 的 terminal 里是有的（指向当前 profile 目录），
所以能读到该身份自己的凭证。`push()` 时把取到的凭证**注入子进程环境**再执行
`push_cmd`（桥脚本自己也要读这些变量）。**只读值、不打印值、不写回任何文件**。

### F2 · `opc triage` 对已建项无效 → 已修（改为如实说明）

**现象**：交接链（`dispatch_chain`）是**建项那一刻**按当时的清单生成的，
之后改清单不会改链；路由实际按链走。而 `cmd_triage` 只印一句「下一步：opc advance」，
让人以为改清单 = 改派活。实测：改完清单，索引与交接链仍是旧的，岗位照旧被派。

**修法**：已建项时，`cmd_triage` **如实说明**「本项目已建项，交接链不会因本次清单
变更而改变」，并列出当前链上的岗位；若清单新增了链上没有的岗位，明确指出
「这些岗位不会被派活」，并给出两条真能生效的路（重建项目 / 手动改
`dispatch_chain`（属绕过，需人工确认））。步骤提示改为「按**当前链**推进，非按本次清单」。

### F3 · 只写「不涉及」条目的派活清单，报错不说清用法 → 已修

**现象**：清单里若没有任何一行 `involved: true`，解析器报「结构化派活清单里
没有任何岗位判为『涉及』」——但没说清正确用法。

**修法**：报错信息改为说清 —— 派活清单是**全量声明**：要派活的岗位必须**逐条写**
`--role <岗位> --assets <资产> --reason "..."`；只写要排除的岗位不够；
若本次真的一个岗位都不派，用 `--zero --reason "..."`。并列出当前只写了哪些岗位。

### F5 · `architect` 被派活但状态机里没有槽位 → 已修

**现象**：`app` profile 的 `step_owner` 只登记 9 份步骤文档，**没有 architect**；
但 `role_bindings` 和索引会登记 architect 的 8 项产出（5_架构设计 / 5_API契约 /
5_时序与数据模型 / 5_任务档案）。结果 `opc done --role architect` 必然报
「跨步被拒绝：当前是阶段 5（design），architect 不是本阶段 owner」——
**architect 能写产出，却无法在交接链上报完成**，产出永远进不了 `_资产快照.md`。

**修法**：`_dispatch_chain()` 在固定 9 步链之外，为**同一阶段的并行岗位**
补一个槽位（阶段号相同、文档取该岗该阶段的主产出）。同阶段内按岗位名字典序，
保证确定性。

（2026-11-18，Hermes 宿主 · 从零安装实测）

记录口径：每条都是**实测复现**，不是推测；后续修的时候照这三条当验收用例。

## v2.29.1 · 无人值守不挂死 + Agent 代建应用流程（2026-11-21 · Hermes 宿主实测）

> **v2.29.3 更新（本节现状）**：本节的「Agent 浏览器代建」流程**已废弃**——
> 实测 Agent 代管的浏览器在对话轮次之间会被回收，创始人扫码确认发生在轮次间隙、
> 接收确认的登录页已经死了：手机端显示「成功」而浏览器端永远收不到，登录永远
> 落不了地（Hermes 宿主复现 3 次）。**完成路径 b 改为「链接代建」**，见下。
> 本节保留是因为其中的浏览器修复（步骤 1）与 lark-cli 登录规程（步骤 2）在
> 「Agent 确实代管一个持久化浏览器」的场景（自建脚本 / cron + 自有 Chromium）仍有用。

> **v2.29.2 更新**：本节「登录飞书开放平台」一步已改用 `lark-cli auth qrcode` 直接渲染 PNG，
> **禁止浏览器截图**（登录码是 JS 动态 `<canvas>`，截图必糊且秒过期）。见下方步骤 2 的 ⚠️。

### 挂死修复

`scripts/hosts/hermes.py` 的 `setup_messaging()` 在启动向导前检测父进程 `sys.stdin.isatty()`：
**非 TTY（Agent 会话 / cron / 管道）就不启动向导**，立即返回
`{"status": "needs_founder", "reason": "no_tty"}` + 两条完成路径（见下），不再干等到 900 秒超时。
真终端（人类在场）行为不变，仍走 v2.23 的 PTY 向导（F1）。验收：无人值守重跑
`provision --feishu-bots now`，N 个身份全部秒回，汇总表显示 `needs_founder`。

### 链接代建流程（v2.29.3 默认；v2.29.4 加凭证回填表单：账号侧动作全部在创始人浏览器里做）

`needs_founder` 的完成路径 b 的**完整操作规程**（Agent 不登录、不碰浏览器）：

1. **Agent 发链接 + 应用名清单**：把下面链接发给创始人，附各职业应用名
   （**名字随意**——绑定只认凭证不认名字，建议 `OPC·<role>` 只是为了对得上岗位；
   5 身份 = 5 个独立应用，凭证绝不复用）：
   `https://open.feishu.cn/page/launcher?from=backend_oneclick`
   这是飞书官方**一键 Agent 应用**入口：机器人能力、事件订阅（长连接）、权限
   全部预配好，填名字点 Create 即成——**替代旧的控制台 6 步清单**（v2.29.3 实测）。
   成功页直接显示 App ID / App Secret（Secret 打码，点显示/复制按钮取真值）。
   **链接只有一个**：5 个应用 = 同一入口地址重复 5 次「填名 → Create」；
   登录态持久，建完一个接着填下一个，页面跳走就重开同一链接，不用重新扫码/登录。
2. **Agent 同时交付凭证回填表单**（v2.29.4 必做——别让创始人在聊天里手工组织凭证格式）：
   把包内 `assets/凭证回填表单模板.html` 复制到 `<Vault>/99_Agent配置/凭证回填表单.html`，
   只改顶部 `ROLES` 数组为本轮 `--roles` 的岗位清单，用宿主内嵌预览交付
   （Hermes 桌面：`::preview{file="…"}`）。创始人建完应用把每组 App ID / App Secret
   填进表单点「发送给 Agent」，凭证按「`<role>: cli_xxx / <secret>`」结构化发回，
   不会发错岗位、不会漏一半。无法内嵌预览的宿主：表单自动退化为生成文本
   让创始人复制粘贴，流程不中断。
3. **创始人逐个建应用、填表回传**：每应用约 1 分钟。创始人浏览器登录态持久、
   扫码一次终身有效，没有轮次间隙问题。
4. **Agent 落盘收尾**：解析表单回执（或创始人手工发的凭证）→ 走下方步骤 4–6
   （写 .env → 重跑 provision → 验收）。值只落 .env，不回显、不入库。
5. **首响采集（v2.29.4 必经步骤，别跳）**：allowlist 未配时，群里 @ 机器人**必然无响应**
   ——这是 Hermes 宿主的安全设计（allowlist 空 = 拒收所有人），**是采集机会，不是故障**；
   且各应用视角的 `ou_` 与群 `chat_id` 只有这条消息能带出来（一键应用的预配权限不含读群成员 scope）。
   让创始人 @ **全部**机器人发一条消息 → Agent 从网关日志一次抓齐两类值：
   各应用视角下创始人的 `ou_`（`Unauthorized user: ou_xxx ... on feishu` 行，N 个应用 N 个值）
   与群 `chat_id`（`Inbound group message received: ... chat_id=oc_...` 行）→
   写各身份 `.env` 的 `FEISHU_ALLOWED_USERS`（各归各位，不复用）与 Vault `opc.config.json` 的
   `notify.dispatch.chat_id` → 约 30 秒 `.env` 重扫生效 → 创始人**再发一条**确认机器人有回复
   = 消息链路验收完成。创始人 union_id（门铃 @ 创始人用，全租户统一一份）同场可取：
   `lark-cli api GET /open-apis/im/v1/chats/<chat_id>/members --as user
   --params '{"member_id_type":"union_id"}'`（群成员里的人只有一个 = 创始人；
   需要 lark-cli 用户身份在线；写各身份 `.env` 的 `FEISHU_FOUNDER_UNION_ID`）。

⚠️ **为什么不再让 Agent 用浏览器代建**（v2.29.3 实测复现 3 次）：Agent 代管的
浏览器在对话轮次之间会被回收；创始人扫码确认发生在轮次间隙，接收确认的登录页
已经死了——手机端显示「成功」而浏览器端永远收不到，登录永远落不了地。教训：
**账号侧动作（登录/扫码/建应用）一律留在创始人手里，Agent 只发链接。**

### 附录：Agent 持久化浏览器代建（仅限特殊宿主；默认不走）

以下规程只在「Agent 自己启动并持有浏览器进程（进程不随对话轮次回收，登录
cookie 落磁盘 profile）」的场景使用（验证于 v2.29.1 安装，v2.29.2 修正二维码渲染方式）：

1. **修浏览器（如缺系统库）**：Chromium 起不来报 `libnspr4.so` 等缺失时，无需 root——
   `apt-get download <缺失库包名>` → `dpkg-deb -x` 解包 → 全部 `.so` 拷进
   Chromium 程序目录 → 用 patchelf 给**每个拷入的库**（含 chrome 本体）写
   `--set-rpath '$ORIGIN'`。⚠️ `$ORIGIN` 只对**直接依赖**生效，二级依赖
   （如 libcups→libavahi、libnss3→libplc4）必须给**库文件自身**也写 rpath，
   否则依旧 not found。验收：`chrome --version` 正常且 `ldd | grep 'not found'` 为 0。
2. **登录飞书开放平台**：Agent 打开 `https://open.feishu.cn/app` → **用 `lark-cli`
   直接渲染二维码 PNG 给创始人** → **创始人手机飞书 App 扫码确认**（唯一需要创始人的
   一步）→ Agent 轮询页面确认登录态。
   ⚠️ **禁止浏览器截图取二维码**（v2.29.2 实测踩坑并修正）：
   开放平台登录码是登录页 **JS 动态生成的 `<canvas>`**，页面上没有 `<img>`，
   截图必然糊/被视口截断/几十秒即过期，还要求用户反复重扫。
   正确做法 = lark-cli device flow，签发**带一次性 code 的 URL**，再转 PNG：
   ```bash
   lark-cli auth login --recommend --no-wait --json     # 取 verification_url + device_code
   lark-cli auth qrcode "<verification_url>" --output ~/files/xxx.png --size 320
   # 用 MEDIA: 交付 PNG；用户确认后：
   lark-cli auth login --device-code <device_code>
   ```
   `lark-cli auth qrcode` 的 `--output` 只接受 `cwd` / `/tmp` / `~/files`。
3. **逐身份建应用**（5 身份 = 5 个独立应用，凭证绝不复用）：每个职业建一个
   「企业自建应用」，取名建议 `<职业名>-opc`（strategist-opc、market-opc…）；
   开启**事件订阅-长连接模式**（websocket，无需公网回调地址）。
   ⚠️ **建应用必须走控制台 UI，不能用 lark-cli API 代建**：飞书应用管理接口
   (`/open-apis/application/v6/applications`) 只接受 tenant token，**user token
   会报 `99991668 user access token not support`**——不要因此以为整条路走不通，
   浏览器代建（步骤 2 已登录）仍然有效。
4. **取凭证写 .env**：在每个应用凭证页取 App ID / App Secret，写入该身份 Profile 的
   `.env`（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`）。**值只落 .env，不回显、不入库。**
5. **补齐协作开关**：重跑 `provision_profiles.py --apply --feishu-bots now`（幂等，
   已配置身份自动跳过）——适配器的 `ensure_collab_config()` 会写
   `FEISHU_ALLOW_BOTS=mentions` / `FEISHU_REQUIRE_MENTION=true` / `FEISHU_GROUP_POLICY=open`。
6. **验收**：`hermes -p <role> gateway status` 显示 feishu `connected`；
   群里 @ 该角色有反应。失败排查：网关日志 `Unauthorized user: ou_xxx` = 该身份
   `.env` 的 `FEISHU_ALLOWED_USERS` 没填对（ou_xxx 是该应用视角的创始人 open_id）。

**边界不变**：应用发布审批、扫码确认始终由创始人在飞书侧完成；Agent 不代扫码、
不跨身份复制凭证、不回显凭证值。
