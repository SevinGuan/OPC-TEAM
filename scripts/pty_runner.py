#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在伪终端（PTY）里跑交互式命令，让 curses / raw-mode 菜单能收按键。

为什么需要
----------
Hermes 的 `hermes gateway setup` 是 **curses 菜单**（`curses_single_select` +
prompt_toolkit raw mode），必须挂在真终端上才能收方向键 / 数字键。
用普通 `subprocess.run(...)` 调它 → 子进程没有 TTY → 读到空输入 →
菜单把空输入当成「跳过」，向导直接退出（实测 5 个身份全部
「[x] 向导结束但未检测到完整的 FEISHU_APP_ID / FEISHU_APP_SECRET」）。

本模块提供 `run_in_pty(cmd, input_script, timeout)`：
  - 用 `pty.openpty()` 给子进程一个真终端；
  - 按 `input_script`（行序列）逐条写入，模拟人工按键；
  - 收集全部输出，超时或退出码非 0 都如实返回。

只依赖 Python 标准库（POSIX）。Windows 上退化为普通 subprocess（无 PTY）。
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def run_in_pty(cmd: list[str], input_script: list[str] | None = None,
               timeout: float = 600.0, env: dict | None = None,
               cwd: str | None = None) -> tuple[int, str]:
    """在 PTY 里跑 cmd 并按脚本喂输入。

    返回 (退出码, 合并后的输出)。非 POSIX 或 PTY 不可用时退化为普通 subprocess。

    input_script 每项是一次「输入」：
      - 以 "\\n" 结尾 → 连字符 + 回车（即按数字键后回车）；
      - 以 "\\x1b" / "\\r" 之类转义序列 → 原样写入（方向键等）。
    """
    if _is_windows():
        return _run_plain(cmd, timeout=timeout, env=env, cwd=cwd)

    try:
        import pty
    except ImportError:
        return _run_plain(cmd, timeout=timeout, env=env, cwd=cwd)

    script = list(input_script or [])
    child_env = dict(env) if env is not None else dict(os.environ)

    try:
        master_fd, slave_fd = pty.openpty()
    except OSError:
        return _run_plain(cmd, timeout=timeout, env=env, cwd=cwd)

    try:
        proc = subprocess.Popen(
            cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            env=child_env, cwd=cwd, close_fds=True, start_new_session=True,
        )
    except OSError:
        os.close(master_fd)
        os.close(slave_fd)
        return _run_plain(cmd, timeout=timeout, env=env, cwd=cwd)

    os.close(slave_fd)
    chunks: list[str] = []
    pending = list(script)
    deadline = time.monotonic() + timeout
    next_write = time.monotonic() + 0.8   # 给向导一点渲染时间再喂第一个输入

    try:
        while True:
            if time.monotonic() > deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                chunks.append("\n[pty] 超时，已终止\n")
                break

            # 按脚本喂输入
            if pending and time.monotonic() >= next_write:
                item = pending.pop(0)
                try:
                    os.write(master_fd, item.encode("utf-8"))
                except OSError:
                    pass
                next_write = time.monotonic() + 0.8

            r, _, _ = select.select([master_fd], [], [], 0.2)
            if r:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                chunks.append(data.decode("utf-8", "replace"))
            elif proc.poll() is not None:
                # 进程已退出：再排一次读，把残余输出取完
                try:
                    while True:
                        data = os.read(master_fd, 65536)
                        if not data:
                            break
                        chunks.append(data.decode("utf-8", "replace"))
                except OSError:
                    pass
                break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.returncode or -1

    return rc, "".join(chunks)


def _run_plain(cmd: list[str], timeout: float = 600.0,
               env: dict | None = None, cwd: str | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env, cwd=cwd)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as exc:
        return 127, str(exc)


if __name__ == "__main__":
    # 自检：跑一个读 stdin 的小程序，验证 PTY 路径可用
    code, out = run_in_pty(
        [sys.executable, "-c", "import sys;print('got:', sys.stdin.readline().strip())"],
        ["hello\n"], timeout=20)
    print("exit:", code)
    print("out :", out.strip())
