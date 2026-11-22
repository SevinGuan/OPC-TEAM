# opc-team — 一人公司（OPC）虚拟团队工作流 Skill 包

一个**宿主无关**的 AI Agent 技能包：把「一人公司」的完整产品工作流交给虚拟团队——
创始人只说人话，Agent 负责翻译成确定性命令，8 个职业按岗位卡分工走 9 步流程，
全部产出可溯源、可沉淀、可上云。

- **规范版本** v2.22（宪法，规则层唯一真相源）/ **包版本** v2.29.6（执行层）
- 只用 Python 标准库（`scripts/opc.py` 等），无第三方依赖
- 适用宿主：Hermes Agent（支持最深，含 multi-profile / 群门铃）及任意支持自定义提示词的宿主
  （Claude Code / CodeBuddy / Cursor / …，斜杠命令源文件开箱即装）

## 核心能力

| 能力 | 说明 |
|---|---|
| 9 步工作流 | 约束卡 → MRD → 立项 → BRD → 产品定义 → 设计 → 实现 → 上线 → 复盘 |
| 8 份岗位卡 | strategist / market / pm / design / architect / engineer / growth / governance，一文件一 owner |
| 派活权归 pm | 派活清单结构化落盘（JSON + MD），是路由唯一真相源；解析失败显式报错 |
| 立项是创始人决策点 | 追加立项（新版本号）必须创始人同意，`--founder-approved` 留痕进版本台账（v2.29.5） |
| 版本链与沉淀 | 版本 = 一次立项（V1、V2…只增不复用）；各职业自己写自己的履历条目，有产出即可写（v2.29.6） |
| 云端仓库 | 本地 Vault 是唯一真相源；飞书/云效/钉钉/自定义 CLI 共用 exec 入口，增量推送 |
| 群门铃 | `opc new` / `go` / `advance` 自动在群里 @ 下一棒；闸门拍板权永远在创始人 |

## 仓库结构（本仓库 = 包本体）

```
├── SKILL.md              # Agent 加载入口：路由表 + 人话入口 + 完整 Commands 签名
├── VERSION.md            # 近期版本说明（v2.20 起）
├── VERSION-HISTORY.md    # 更早版本完整归档
├── EXTENSIONS.md         # 本地扩展：provision 身份代建 / 链接代建 / 消息网关口径
├── assets/               # 岗位卡、职业简历、模板、人话入口细则、凭证回填表单模板
├── commands/opc/*.md     # 斜杠命令源文件（宿主无关，复制到任意宿主即成 /opc:init …）
├── references/           # OPC-架构规范（宪法）+ 命令与配置手册
├── scripts/              # opc.py 确定性命令 + provision/orchestrator/基线门禁等
└── skills/fantasy-web-design/   # design 岗随包设计子技能
```

## 安装

把整个仓库目录（或 clone 下来的目录）复制到宿主的技能目录即可，**目录名保持 `opc-team`**：

- **Hermes（全局）**：复制到 `~/.hermes/skills/opc-team/`
- **Hermes（项目级）**：复制到 `<git仓库>/.hermes/skills/opc-team/`，然后 `hermes skills trust <git仓库>`
- **其它宿主的斜杠命令**：

```bash
python scripts/install_commands.py --list        # 查看候选宿主（CodeBuddy / Claude Code / Cursor …）
python scripts/install_commands.py --target <宿主的 commands 目录>
```

## 快速开始

```bash
python scripts/opc.py init --vault ./OPC-Vault --profile app --non-interactive
python scripts/opc.py status --vault ./OPC-Vault
python scripts/opc.py new "项目名" --tags "#MVP #高频工具"
```

日常使用**不需要记命令**——对 Agent 说人话：「初始化 OPC 团队」「新建项目〈名字〉」
「继续推进」「现在到哪一步了」「该派活给谁」「可以沉淀知识了」「这个项目收尾」。
完整映射见 `assets/人话入口.md`；三条硬边界（闸门拍板权归创始人 / 一会话一角色 /
Agent 间禁止互相唤醒）见 SKILL.md。

## multi-profile（独立身份）

每个职业落成独立持久身份（Hermes Profile），消息应用走**链接代建**：Agent 发飞书
一键建应用链接 + 凭证回填表单，创始人在自己的浏览器建应用、填表回传，Agent 落盘。
**注意首响采集**：allowlist 未配时，群里首条 @ 机器人必然无响应（宿主安全设计，
预期行为）——首条消息是采集步骤，Agent 从网关日志抓各应用视角 open_id 与群 ID
后写入配置，第二条消息起机器人正常应答。完整规程见 `EXTENSIONS.md`。

## 安全边界

- 凭证只走环境变量（`.env`），配置文件只存变量名；本仓库不含任何租户凭证
- 应用创建 / 发布审批 / 扫码授权一律由创始人完成，Agent 只发链接、不代扫码
- 云端仓库只多写一份副本，本地 Vault 始终是唯一真相源

## 文档地图

| 要看什么 | 去哪 |
|---|---|
| Agent 的加载入口与完整命令签名 | `SKILL.md` |
| 架构规范（宪法 v2.22） | `references/OPC-架构规范.md` |
| 命令与配置手册（§1–§44） | `references/命令与配置手册.md` |
| 身份代建 / 消息网关 / 坑清单 | `EXTENSIONS.md` |
| 版本变更记录 | `VERSION.md`（近期）/ `VERSION-HISTORY.md`（早期） |

## 许可证

[MIT](LICENSE) © 2026 sevin
