#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿主适配器（通用回退）：**不代建任何身份**，只产出「落地清单」。

为什么要有它
------------
`multi-profile` 的三条映射（description / 人设文件 / 工作目录）**任何宿主都能手工做**，
但大多数宿主没有「建身份」的命令行。所以本适配器：

- 不假设任何宿主的目录结构、命令或产品名；
- `--apply` 时把落地清单写进 Vault（`99_Agent配置/Profile落地清单.md`），
  照着清单在宿主里手工建身份即可；
- 永远探测通过 —— 它是**兜底**，不是首选。

换宿主若能提供命令行，在 `scripts/hosts/` 下另写一个同接口文件即可；
本文件永远是最后那个「都能用」的选项。
"""

from __future__ import annotations

from pathlib import Path

HOST_NAME = "manual"
HOST_LABEL = "通用回退（手工落地，不代建）"
CAN_CREATE = False

_STATE = {"vault": None}


def detect() -> dict:
    return {"ok": True, "home": "", "detail": "通用回退：不代建任何身份，只产出落地清单（任何宿主通用）"}


def profile_exists(role: str) -> bool:
    return False


def create_profile(role: str, description: str, clone: bool = True) -> dict:
    return {"ok": False, "status": "failed", "reason": "host_unsupported",
            "message": "通用回退不代建身份：请照落地清单手工创建"}


def write_identity(role: str, filename: str, content: str) -> dict:
    return {"ok": False, "status": "failed", "reason": "host_unsupported",
            "message": "通用回退不写宿主文件：请照落地清单手工粘贴人设文件"}


def set_workspace(role: str, path: str) -> dict:
    return {"ok": False, "status": "failed", "reason": "host_unsupported",
            "message": "通用回退不设工作目录：请照落地清单手工填 Vault 绝对路径"}


def env_path(role: str) -> Path | None:
    return None


def messaging_command(role: str) -> str:
    return "（手工）为 " + role + " 配置**独立**的消息应用（一身份一应用，凭证不复用）"


def setup_messaging(role: str) -> dict:
    return {"ok": False, "status": "failed", "reason": "host_unsupported",
            "message": "通用回退不代配消息应用：请照清单手工创建独立应用"}


def messaging_states(roles: list) -> dict:
    return {}


def setup(ctx: dict) -> None:
    _STATE["vault"] = ctx.get("vault")
    _STATE["apply"] = bool(ctx.get("apply", True))
