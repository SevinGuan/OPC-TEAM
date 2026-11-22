"""云端仓库同步桥：把 OPC 本地产出再写一份到云端（远程仓库）。

定位（v2.7，创始人明确）：
- 本地照常写入，**工作流一个字都不改**。
- 写完本地之后，**再写一份到云端**。
- 飞书 = **远程仓库**（v2.7 口径），并且是**带项目管理能力的远程仓库**：
    · 知识库沉淀（项目方法论、结论、复盘）
    · 产品原型沉淀（设计产出）
    · 各项目任务与需求（未完成任务清单）
  创始人可从飞书查知识、拿未完成任务，回来在本地用 Agent 干活。
- 因此推拉都是**读-写对称**的，但**覆盖本地必须显式授权**：
  `pull` 需 `--allow-restore` —— 正常用法是「本地写 → 同步到云端」，
  不是「从云端回灌本地」。
- 本地 Vault 始终是唯一真相源（宪法 §15.8）。云端是副本与入口，不是真相源。

实现：直接调用官方 `lark-cli` 的 `drive` / `task` 域，
不自己造轮子、不预设任何厂商私有接口。

命令约定（由 OPC 的 backends.feishu 配置提供，均可留空走默认值）：
  cli          —— lark-cli 可执行名，默认 "lark-cli"
  folder_token —— 飞书根文件夹 token（省略则用调用者的云空间根目录）
  project_prefix —— 远端项目目录前缀，默认 "OPC"

用法：
  python sync_feishu.py push    --local-dir <dir> [--folder-token T] [--prefix OPC]
  python sync_feishu.py status  --local-dir <dir> [--folder-token T] [--prefix OPC]
  python sync_feishu.py pull    --local-dir <dir> --folder-token T [--prefix OPC] --allow-restore
  python sync_feishu.py init    --folder-token T [--prefix OPC]      # 建根目录
  python sync_feishu.py tasks   [--query 关键词] [--limit N]        # 拉未完成任务
  python sync_feishu.py search  --query 关键词 [--limit N]          # 搜云端知识/原型

只依赖 Python 标准库 + 外部 lark-cli。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_CLI = "lark-cli"
DEFAULT_PREFIX = "OPC"


def _cli(conf, key, fallback=""):
    v = (conf or {}).get(key)
    if v is None or str(v).strip() == "":
        return fallback
    return str(v).strip()


def load_conf(vault):
    """读 Vault 里的 opc.config.json，拿 backends.feishu 的同步配置。"""
    p = Path(vault) / "opc.config.json"
    if not p.exists():
        return {}
    try:
        with io_open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def io_open(path):
    return open(str(path), "r", encoding="utf-8")


def which_cli(name):
    return shutil.which(name)


def run_cli(argv, dry_run=False, timeout=600, cwd=None):
    """执行 lark-cli，返回 (rc, stdout, stderr)。

    ``cwd``：工作目录。**必须提供**——`drive +push --local-dir` 会用
    「当前工作目录」把 local-dir 换算成相对路径（Go 的 filepath.Rel）。
    当暂存目录在别的盘符（如系统 Temp 在 C:、Vault 在 D:）时，
    跨盘符 Rel 必然失败，报：
    ``Rel: can't make <tmp>\\... relative to <cwd>``。
    所以推送时把 cwd 切到 local-dir 的父目录，让相对路径始终成立。

    Windows 注意：lark-cli 通常是 `lark-cli.CMD` 批处理文件，Python 的
    subprocess 在 Windows 上直接执行 .CMD 会报 WinError 2（找不到文件），
    必须经由 shell / cmd /c 才能跑起来。这里统一用 shell=True + 引号包裹，
    同时兼容 Windows / macOS / Linux。
    """
    cmd = list(argv)
    if dry_run:
        cmd.append("--dry-run")
    # 路径与参数里的空格、引号要做 shell 转义
    quoted = []
    for a in cmd:
        a = str(a)
        if os.name == "nt":
            quoted.append('"' + a.replace('"', '\\"') + '"')
        else:
            quoted.append("'" + a.replace("'", "'\\''") + "'")
    try:
        proc = subprocess.run(" ".join(quoted), shell=True, capture_output=True,
                              cwd=(str(cwd) if cwd else None),
                              text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时（" + str(timeout) + "s）：" + " ".join(cmd)
    except OSError as exc:
        return 126, "", "命令执行失败：" + str(exc)
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def push_cwd(local_dir):
    """返回 `drive +push` 应使用的工作目录：local-dir 的**父目录**。

    `+push` 内部用 Go 的 ``filepath.Rel(cwd, local-dir)`` 把 local-dir
    换算成相对路径——**跨盘符必然失败**（C: 的系统 Temp 相对 D: 的 Vault）。
    联调实测报错：
    ``Rel: can't make C:\\...\\opc_sync_xxx relative to D:\\...``。

    把 cwd 切到 local-dir 的父目录，相对路径就只剩一段目录名，永远成立。
    """
    try:
        p = Path(local_dir).resolve()
        return p.parent if p.parent and str(p.parent) else p
    except Exception:
        return None


def check_auth(cli, need="any"):
    """检查 lark-cli 是否已认证；未认证时给出可执行的指引。

    联调实测（v2.10）：**不要用 `'"ok": true'` 判成功**——
    新版 lark-cli（1.0.98+）的 `auth status` 成功输出里**没有 `ok` 字段**，
    结构是：``appId`` / ``identities.{bot,user}.{status,available}`` / ``identity``。
    旧写法会把「明明已认证」误判为「认证状态未知」，导致所有推送失败。

    正确判据：解析 JSON → ``identities`` 里存在 ``available: true`` 的身份。
    ``need`` 可取 ``any`` / ``user`` / ``bot``；``user`` 用于 task 域
    （该域只支持用户身份）。
    """
    rc, out, err = run_cli([cli, "auth", "status"])
    blob = (out or "") + (err or "")
    if "not_configured" in blob or "not configured" in blob:
        return False, ("lark-cli 未认证。请先执行 `lark-cli config init --new`，"
                       "它会输出一个验证链接，用浏览器打开完成授权。")
    try:
        data = json.loads(out or "{}")
    except Exception:
        data = {}
    ids = data.get("identities") or {}
    if isinstance(ids, dict):
        want = (need or "any").lower()
        if want == "any":
            ok = any(isinstance(v, dict) and v.get("available") for v in ids.values())
        else:
            v = ids.get(want) or {}
            ok = bool(isinstance(v, dict) and v.get("available"))
        if ok:
            return True, "已认证"
    if rc != 0:
        return False, "lark-cli 认证状态未知：" + blob.strip()[:200]
    # 兜底：老版本 CLI 的输出里有 ok 字段
    if '"ok": true' in blob.replace(" ", ""):
        return True, "已认证"
    return False, ("lark-cli 认证状态未知（未找到可用身份）：" + blob.strip()[:200])


def ensure_project_dir(cli, folder_token, prefix, dry_run=False, ident=None):
    """确保远端存在 OPC 根目录，返回它的 folder token。

    已存在则复用（+push 本身也会镜像目录结构，这里只保证根目录可寻址）。
    """
    ident = list(ident or [])
    rc, out, err = run_cli([cli, "drive", "files", "list", "--folder-token", folder_token,
                            "--page-all", "--format", "json"] + ident, dry_run=dry_run)
    if rc != 0:
        return None, "列远端目录失败：" + (err or out).strip()[:200]
    try:
        data = json.loads(out or "{}")
    except Exception:
        return None, "远端返回无法解析为 JSON"
    # 联调实测：`drive files list` 返回结构是 `data.files`（不是 `data.items`）。
    # 旧写法匹配不到任何已有目录 → 每次 push 都新建一个同名根目录壳（重复堆叠）。
    files = (data.get("data") or {}).get("files") or data.get("files") or []
    for it in files:
        name = it.get("name") or ""
        if name == prefix and it.get("token"):
            return it.get("token"), ""
    # 不存在则建
    rc, out, err = run_cli([cli, "drive", "+create-folder", "--name", prefix,
                            "--folder-token", folder_token, "--format", "json"] + ident,
                           dry_run=dry_run)
    if rc != 0:
        return None, "创建远端目录失败：" + (err or out).strip()[:200]
    try:
        data = json.loads(out or "{}")
    except Exception:
        return None, "创建远端目录返回无法解析"
    node = data.get("data") or data
    token = node.get("token") or node.get("folder_token") or (node.get("folder") or {}).get("token")
    if not token:
        return None, "创建远端目录成功但未取到 token，请手动确认目录：" + prefix
    return token, ""


def identity_args(conf):
    """返回 lark-cli 的身份参数（``--as user`` / ``--as bot``）。

    联调实测（v2.10）：
    - 企业自建应用的 **bot 身份默认没有云盘 scope**，``drive`` 域会报
      ``99991672 app_scope_not_applied``；
    - ``task`` 域**只支持 user 身份**（bot 报 "only supports: user"）。

    因此默认走 **user** 身份（由 `lark-cli auth login --domain drive,task,im`
    授权，用户态的云盘 / 任务权限由账号自身持有，无需逐个申请应用 scope）。
    可在 opc.config.json 的 backends.feishu.identity 显式指定 "user" / "bot"；
    留空则不传 --as，由 lark-cli 按 auto-detect 自行选择。
    """
    ident = (_cli(conf, "identity", "") or "").strip().lower()
    if ident in ("user", "bot"):
        return ["--as", ident]
    return []


def cmd_push(args, conf):
    cli = _cli(conf, "cli", DEFAULT_CLI)
    if not which_cli(cli):
        print("错误：找不到 " + cli + "。请安装飞书 CLI，或在 opc.config.json 的 backends.feishu.cli 里填正确的可执行名。")
        return 2
    ok, msg = check_auth(cli)
    if not ok:
        print("错误：" + msg)
        return 2
    local_dir = Path(args.local_dir).resolve()
    if not local_dir.is_dir():
        print("错误：本地目录不存在：" + str(local_dir))
        return 2
    folder_token = _cli(conf, "folder_token", "")
    prefix = _cli(conf, "project_prefix", DEFAULT_PREFIX)
    if args.folder_token:
        folder_token = args.folder_token
    if args.prefix:
        prefix = args.prefix
    if not folder_token:
        print("错误：未配置飞书根文件夹 token。")
        print("      两种做法：① 在 opc.config.json 的 backends.feishu.folder_token 填；")
        print("      ② 命令行加 --folder-token <token>。")
        print("      token 用 `lark-cli drive files list` 看根目录，或 `lark-cli drive +create-folder --name OPC` 拿。")
        return 2
    # 联调修复：**不再套 prefix 壳**。
    # folder_token 本身就是目标文件夹（如「卷卷项目3_联调测试」），
    # `+push` 会把 local-dir 的 `P001/<role>/<批次>/...` 结构直接镜像进去。
    # 旧写法在目标文件夹里再找/建同名 prefix 子目录，导致一层层套壳
    # （每推一次在根目录里多一个「卷卷项目3_联调测试」文件夹）。
    root = folder_token
    # 增量语义（v2.10）：每个批次一个版本目录，**历史版本不覆盖**。
    # 同批次重推时用 smart（比对 mtime，已是最新就跳过）——比 overwrite 更安全，
    # 也不会踩「upload_all 字段灰度未开导致 overwrite 直接报错」的坑。
    argv = [cli, "drive", "+push", "--local-dir", str(local_dir), "--folder-token", root,
            "--if-exists", "smart", "--format", "json"]
    argv += identity_args(conf)
    if args.dry_run:
        argv.append("--dry-run")
    # cwd 切到 local-dir 的父目录：+push 会用 cwd 把 local-dir 换算成相对路径，
    # 跨盘符（暂存目录在系统 Temp、Vault 在别盘）会报 Rel 失败。
    rc, out, err = run_cli(argv, dry_run=args.dry_run, cwd=push_cwd(local_dir))
    if rc != 0:
        print("推送失败：" + ((err or out).strip()[:400]))
        return rc
    print("已推送到飞书（远端目录 " + prefix + "）。")
    if out:
        try:
            data = json.loads(out)
            s = data.get("summary") or data.get("data", {}).get("summary") or {}
            if s:
                print("  上传 " + str(s.get("uploaded", "?")) + " · 覆盖 "
                      + str(s.get("overwritten", "?")) + " · 跳过 "
                      + str(s.get("skipped", "?")) + " · 失败 " + str(s.get("failed", "?")))
        except Exception:
            print("  " + out.strip()[:300])
    return 0


def cmd_status(args, conf):
    cli = _cli(conf, "cli", DEFAULT_CLI)
    if not which_cli(cli):
        print("错误：找不到 " + cli + "。")
        return 2
    ok, msg = check_auth(cli)
    if not ok:
        print("错误：" + msg)
        return 2
    local_dir = Path(args.local_dir).resolve()
    if not local_dir.is_dir():
        print("错误：本地目录不存在：" + str(local_dir))
        return 2
    folder_token = args.folder_token or _cli(conf, "folder_token", "")
    prefix = args.prefix or _cli(conf, "project_prefix", DEFAULT_PREFIX)
    if not folder_token:
        print("错误：未配置飞书根文件夹 token。")
        return 2
    root = folder_token  # 联调修复：不套 prefix 壳（见 cmd_push 注释）
    argv = [cli, "drive", "+status", "--local-dir", str(local_dir), "--folder-token", root,
            "--quick", "--format", "json"]
    argv += identity_args(conf)
    # cwd 切到 local-dir 的父目录：+push 会用 cwd 把 local-dir 换算成相对路径，
    # 跨盘符（暂存目录在系统 Temp、Vault 在别盘）会报 Rel 失败。
    rc, out, err = run_cli(argv, dry_run=args.dry_run, cwd=push_cwd(local_dir))
    if rc != 0:
        print("状态查询失败：" + ((err or out).strip()[:400]))
        return rc
    print(out.strip()[:4000])
    return 0


def cmd_pull(args, conf):
    """从飞书拉回本地。**默认禁用**——正常用法是本地写 → 同步到云端，不是反着来。"""
    if not args.allow_restore:
        print("拒绝执行：正常用法是「本地写 → 同步一份到飞书」，默认不从飞书覆盖本地。")
        print("      确实要用飞书版本覆盖本地时，显式加 --allow-restore。")
        return 2
    cli = _cli(conf, "cli", DEFAULT_CLI)
    if not which_cli(cli):
        print("错误：找不到 " + cli + "。")
        return 2
    ok, msg = check_auth(cli)
    if not ok:
        print("错误：" + msg)
        return 2
    local_dir = Path(args.local_dir).resolve()
    folder_token = args.folder_token or _cli(conf, "folder_token", "")
    prefix = args.prefix or _cli(conf, "project_prefix", DEFAULT_PREFIX)
    if not folder_token:
        print("错误：未配置飞书根文件夹 token。")
        return 2
    root = folder_token  # 联调修复：不套 prefix 壳（见 cmd_push 注释）
    argv = [cli, "drive", "+pull", "--local-dir", str(local_dir), "--folder-token", root,
            "--if-exists", "overwrite", "--format", "json"]
    argv += identity_args(conf)
    # cwd 切到 local-dir 的父目录：+push 会用 cwd 把 local-dir 换算成相对路径，
    # 跨盘符（暂存目录在系统 Temp、Vault 在别盘）会报 Rel 失败。
    rc, out, err = run_cli(argv, dry_run=args.dry_run, cwd=push_cwd(local_dir))
    if rc != 0:
        print("拉取失败：" + ((err or out).strip()[:400]))
        return rc
    print("已从飞书拉取到 " + str(local_dir))
    if out:
        print("  " + out.strip()[:600])
    return 0


def cmd_init(args, conf):
    """只建远端 OPC 根目录并回显 token。"""
    cli = _cli(conf, "cli", DEFAULT_CLI)
    if not which_cli(cli):
        print("错误：找不到 " + cli + "。")
        return 2
    ok, msg = check_auth(cli)
    if not ok:
        print("错误：" + msg)
        return 2
    folder_token = args.folder_token or _cli(conf, "folder_token", "")
    prefix = args.prefix or _cli(conf, "project_prefix", DEFAULT_PREFIX)
    # init **允许无 token**：`drive +create-folder` 省略 --folder-token 时落在根目录，
    # 这正好解掉「要 token 才能建目录、要目录才能拿 token」的鸡生蛋。
    argv = [cli, "drive", "+create-folder", "--name", prefix, "--format", "json"]
    if folder_token:
        argv += ["--folder-token", folder_token]
    argv += identity_args(conf)
    rc, out, err = run_cli(argv, dry_run=args.dry_run)
    if rc != 0:
        print("创建失败：" + ((err or out).strip()[:400]))
        return rc
    print("已创建（或已存在）远端目录：" + prefix)
    # v2.20 修（实测踩坑，通用缺陷）：目录若以**机器人身份**创建，机器人不主动共享
    # 时使用者的云文档侧边栏**看不到它**——表现为「同步回执成功，但用户找不到文件」。
    # 这里把提醒提升到 stdout 显式位置，并给出可复制的授权命令。
    try:
        start, end = out.find("{"), out.rfind("}")
        meta = json.loads(out[start:end + 1]) if start >= 0 and end > start else {}
        ft = ((meta.get("data") or {}).get("folder_token")) or ""
        created = bool((meta.get("data") or {}).get("created"))
    except Exception:
        ft, created = "", False
    if not folder_token and created:
        print("⚠️ 本目录由机器人身份新建，**尚未共享给使用者**：请把它共享给你自己"
              "（权限=可编辑），否则你在飞书云文档里看不到它。")
        if ft:
            print("   可执行：lark-cli drive +member-add --token " + ft
                  + " --type folder --member-type openid --member-id <使用者openid>"
                  + " --perm full_access --yes")
    print(out.strip()[:600])
    return 0


def cmd_tasks(args, conf):
    """拉云端各项目的**未完成任务 / 需求**清单（v2.7）。

    用途：创始人从飞书拿「各项目还没完成的任务与需求」，
    回来在本地用 Agent 干活。这是**读**动作，不改本地任何文件。
    """
    cli = _cli(conf, "cli", DEFAULT_CLI)
    if not which_cli(cli):
        print("错误：找不到 " + cli + "。")
        return 2
    ok, msg = check_auth(cli, need="user")
    if not ok:
        print("错误：" + msg)
        print("      task 域只支持**用户身份**。请执行：lark-cli auth login --domain drive,task,im")
        return 2
    # 注意：`--completed` 是布尔 flag，必须写成 `--completed=false`；
    # 写成 `--completed false` 会被 lark-cli 当作位置参数而报错
    # （联调实测："positional arguments are not supported"）。
    # task 域**只支持 user 身份**，这里强制 --as user。
    argv = [cli, "task", "+search", "--completed=false", "--as", "user", "--format", "json"]
    if args.query:
        argv += ["--query", args.query]
    rc, out, err = run_cli(argv, dry_run=args.dry_run)
    if rc != 0:
        print("拉取任务失败：" + ((err or out).strip()[:400]))
        return rc
    items = _extract_items(out)
    if not items:
        print("云端没有未完成任务" + ("（关键词：" + args.query + "）" if args.query else "") + "。")
        return 0
    limit = args.limit if args.limit and args.limit > 0 else len(items)
    print("云端未完成任务（" + str(len(items)) + " 条，显示前 " + str(min(limit, len(items))) + " 条）：")
    print("─" * 60)
    for it in items[:limit]:
        title = (it.get("summary") or it.get("title") or it.get("name") or "").strip()
        tid = it.get("task_id") or it.get("id") or ""
        done = it.get("completed")
        mark = "[x]" if done else "[ ]"
        due = it.get("due") or ""
        desc = (it.get("description") or "").replace("\n", " ").strip()
        line = mark + " " + (title or "(无标题)")
        meta = []
        if tid:
            meta.append("id=" + str(tid))
        if due:
            meta.append("due=" + str(due))
        if meta:
            line += "  (" + " · ".join(meta) + ")"
        print(line)
        if desc:
            print("      " + desc[:160])
    print("─" * 60)
    print("提示：这些是云端登记的任务/需求。确认要做哪几条后，在本地用 Agent 开工。")
    return 0


def cmd_search(args, conf):
    """搜云端沉淀的知识库 / 产品原型等（v2.7）。

    用途：创始人「无论想去查飞书里的哪些知识，都通过飞书去查」。
    这是**读**动作，不改本地任何文件。
    """
    cli = _cli(conf, "cli", DEFAULT_CLI)
    if not which_cli(cli):
        print("错误：找不到 " + cli + "。")
        return 2
    ok, msg = check_auth(cli)
    if not ok:
        print("错误：" + msg)
        return 2
    if not args.query:
        print("错误：需要 --query 关键词。")
        print("      例：opc sync search --query 冷启动")
        return 2
    argv = [cli, "drive", "+search", "--query", args.query, "--format", "json"]
    argv += identity_args(conf)
    # 限定在配置的根目录内检索（若已配置 folder_token）
    folder_token = args.folder_token or _cli(conf, "folder_token", "")
    if folder_token:
        argv += ["--folder-tokens", folder_token]
    rc, out, err = run_cli(argv, dry_run=args.dry_run)
    if rc != 0:
        print("检索失败：" + ((err or out).strip()[:400]))
        return rc
    items = _extract_items(out)
    if not items:
        print("云端未找到与「" + args.query + "」相关的内容。")
        return 0
    limit = args.limit if args.limit and args.limit > 0 else len(items)
    print("云端检索「" + args.query + "」（命中 " + str(len(items)) + " 条，显示前 "
          + str(min(limit, len(items))) + " 条）：")
    print("─" * 60)
    for it in items[:limit]:
        title = (it.get("title") or it.get("name") or "").strip()
        typ = it.get("type") or it.get("doc_type") or ""
        token = it.get("token") or it.get("doc_token") or ""
        url = it.get("url") or ""
        line = "• " + (title or "(无标题)")
        meta = []
        if typ:
            meta.append(str(typ))
        if token:
            meta.append("token=" + str(token))
        if meta:
            line += "  (" + " · ".join(meta) + ")"
        print(line)
        if url:
            print("      " + str(url))
    print("─" * 60)
    return 0


def _extract_items(out):
    """从 lark-cli 的 JSON 输出里尽量取出条目列表。"""
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("items", "tasks", "docs", "files", "records", "data"):
        v = data.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in ("items", "tasks", "docs", "files", "records"):
                v2 = v.get(k2)
                if isinstance(v2, list):
                    return v2
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(description="OPC 云端仓库同步桥（本地写完再同步一份到云端）")
    ap.add_argument("action", choices=["push", "status", "pull", "init", "tasks", "search"],
                    help="push=本地→云端（默认动作）｜status=比对差异｜pull=云端→本地（需 --allow-restore）"
                         "｜init=建远端目录｜tasks=拉云端未完成任务｜search=搜云端知识/原型")
    ap.add_argument("--vault", default=".", help="Vault 根（读 opc.config.json 取云端配置）")
    ap.add_argument("--local-dir", default="", help="要同步的本地目录")
    ap.add_argument("--folder-token", default="", help="云端根文件夹 token")
    ap.add_argument("--prefix", default="", help="远端项目目录前缀，默认 OPC")
    ap.add_argument("--allow-restore", action="store_true", help="pull 时必须显式加：允许云端覆盖本地")
    ap.add_argument("--dry-run", action="store_true", help="只预览不执行")
    ap.add_argument("--query", default="", help="tasks / search 的关键词")
    ap.add_argument("--limit", type=int, default=0, help="tasks / search 最多显示多少条（0=全部）")
    args = ap.parse_args(argv)
    conf = load_conf(args.vault)
    backends = conf.get("backends") or {}
    feishu = backends.get("feishu") or {}
    handlers = {"push": cmd_push, "status": cmd_status, "pull": cmd_pull,
                "init": cmd_init, "tasks": cmd_tasks, "search": cmd_search}
    return handlers[args.action](args, feishu)


if __name__ == "__main__":
    sys.exit(main())
