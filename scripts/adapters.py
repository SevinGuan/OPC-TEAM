"""OPC 后端适配器：把逻辑引用解析成可打开的 URI。

仅依赖 Python 标准库。外部系统的 CLI 名称与命令模板由用户在配置中填写，
本模块不预设、不臆造任何厂商命令。
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path


class Backend:
    def __init__(self, name, conf, vault_root):
        self.name = name
        self.conf = conf or {}
        self.vault_root = Path(vault_root)

    def resolve(self, ref):
        raise NotImplementedError

    def push(self, local_path, ref):
        raise NotImplementedError

    def check(self):
        raise NotImplementedError


class LocalBackend(Backend):
    def resolve(self, ref):
        return "local://" + ref.replace("\\", "/")

    def push(self, local_path, ref):
        src = Path(local_path)
        dst = self.vault_root / ref
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists() and src.resolve() != dst.resolve():
            shutil.copy2(str(src), str(dst))
        return self.resolve(ref)

    def check(self):
        return True, "local 后端就绪"


class ExecBackend(Backend):
    """通用外部系统适配器（飞书 / Jira 类等）。

    push_cmd 模板可用占位符：{file} {project} {role} {key} {ref} {vault}。
    凭证只从环境变量读取，绝不写入配置文件。
    """

    def resolve(self, ref):
        if "://" in ref[:12]:
            return ref
        prefix = (self.conf.get("url_prefix") or "").rstrip("/")
        if prefix:
            return prefix + "/" + ref.lstrip("/")
        return self.name + "://" + ref

    def check(self):
        cli = (self.conf.get("cli") or "").strip()
        tpl = (self.conf.get("push_cmd") or "").strip()
        if not cli or not tpl:
            return False, "未绑定 CLI 或 push_cmd（产出将降级为本地待同步）"
        missing = [v for v in self.conf.get("credential_env", []) if not self.credential(v)]
        if missing:
            return False, "缺少环境变量：" + ", ".join(missing) + "（仅检查是否设置，不读取值）"
        return True, "外部后端配置就绪"

    def credential(self, name: str) -> str:
        """取一个凭证值。

        v2.23：**先看进程环境变量，再看当前 profile 的 .env**。
        过去只看 os.environ —— 但 Hermes 的 terminal 工具**只透传 TERMINAL_* 前缀**
        给子进程（tools/terminal_scope.py），其余变量一律过滤，于是 agent 内部
        执行 `opc done` 时读不到 FEISHU_WEBHOOK_URL，通知静默降级为「写本地文件」，
        创始人在群里收不到，而 opc done 本身不报错。

        HERMES_HOME 在 agent 的 terminal 里是有的（指向当前 profile 目录），
        所以可以从 `<HERMES_HOME>/.env` 读到该身份自己的凭证。
        **只读值、不打印值**；不写回任何文件。
        """
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
        home = (os.environ.get("HERMES_HOME") or "").strip()
        if not home:
            return ""
        env_file = Path(home) / ".env"
        try:
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, sep, val = line.partition("=")
                if not sep or key.strip() != name:
                    continue
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if val:
                    return val
        except OSError:
            return ""
        return ""


    def push(self, local_path, ref, **ctx):
        ok, msg = self.check()
        if not ok:
            return None, msg
        tpl = self.conf["push_cmd"]
        values = {
            "file": str(local_path).replace("\\", "/"),
            "ref": ref.replace("\\", "/"),
            "project": ctx.get("project", ""),
            "role": ctx.get("role", ""),
            "key": ctx.get("key", ""),
            "vault": str(self.vault_root).replace("\\", "/"),
        }
        try:
            rendered = tpl.format(**values)
        except (KeyError, ValueError) as exc:
            return None, "push_cmd 模板占位符错误：" + str(exc)
        try:
            argv = shlex.split(rendered)
        except ValueError as exc:
            return None, "push_cmd 无法解析：" + str(exc)
        if not argv:
            return None, "push_cmd 为空"
        try:
            # 强制 UTF-8：外部 CLI 输出含中文时，locale(GBK) 解码会抛 UnicodeDecodeError
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            # v2.23：把凭证注入子进程环境（桥脚本自己也要读这些变量）
            for _ck in self.conf.get("credential_env", []):
                _cv = self.credential(_ck)
                if _cv:
                    env[_ck] = _cv
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                                  encoding="utf-8", errors="replace", env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, "外部命令执行失败：" + str(exc)
        if proc.returncode != 0:
            return None, "外部命令返回 " + str(proc.returncode) + "：" + (proc.stderr or "").strip()[:200]
        out = (proc.stdout or "").strip()
        return (out if out else self.resolve(ref)), "已推送"


def get_backend(name, cfg, vault_root):
    conf = (cfg.get("backends") or {}).get(name)
    if conf is None:
        raise ValueError("未注册的后端：" + name + "；请先在 opc.config.json 的 backends 中登记")
    kind = conf.get("type", "local")
    if kind == "local":
        return LocalBackend(name, conf, vault_root)
    if kind == "exec":
        return ExecBackend(name, conf, vault_root)
    raise ValueError("未知后端类型：" + kind)
