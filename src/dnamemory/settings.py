# -*- coding: utf-8 -*-
"""项目配置加载（``.env`` 风格，零依赖）。

查找顺序（前者优先）：

1. 显式路径（``--config`` 或 ``apply_config(path=...)``）
2. 环境变量 ``DNAMEMORY_CONFIG``
3. ``./dnamemory.env``（当前目录，随项目走）
4. ``~/.dnamemory.env``（用户级默认）

**优先级**：命令行参数 > 真实环境变量 > 配置文件 > 内置默认值。
已存在的环境变量**不会**被配置文件覆盖——systemd / docker / CI 注入的运行时值
始终优先，配置文件只负责补默认。

只注入白名单前缀（``DNAMEMORY_`` / ``DEEPSEEK_``）的键，防止配置文件误设
``PATH``、``PYTHONPATH`` 之类的进程关键变量。

不做变量插值、不执行命令：配置文件是纯数据。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: 指定配置文件路径的环境变量
CONFIG_ENV = "DNAMEMORY_CONFIG"

#: 允许由配置文件注入的键前缀（白名单）
ALLOWED_PREFIXES = ("DNAMEMORY_", "DEEPSEEK_")

#: 当前目录下的默认文件名
DEFAULT_FILENAME = "dnamemory.env"

#: 用户级默认路径
USER_CONFIG = Path.home() / ".dnamemory.env"

#: 配置模板文件（随仓库分发，供复制）
EXAMPLE_FILENAME = "dnamemory.env.example"


def parse_env_text(text: str) -> dict[str, str]:
    """解析 ``.env`` 文本 → ``{KEY: VALUE}``。

    规则：

    - 忽略空行与 ``#`` 开头的整行注释；
    - 支持可选的 ``export `` 前缀；
    - 值可用成对的单/双引号包裹（去引号；引号内一律保留原样，含 ``#``）；
    - 未加引号时剥离行尾的 `` #...`` 注释（`` #`` 前有空格才算注释）；
    - 键名非法（空、含空格）的行直接跳过，不抛异常。
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or any(c.isspace() for c in key):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            # 引号包裹：取配对引号之间的内容（内部的 # 一律保留），
            # 引号之后的部分按注释忽略
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def find_config_file(explicit: Optional[str] = None) -> Optional[Path]:
    """按查找顺序返回配置文件路径；都没有则 None（不报错，用内置默认）。"""
    if explicit:
        return Path(explicit)
    env_path = os.environ.get(CONFIG_ENV)
    if env_path:
        return Path(env_path)
    cwd_file = Path.cwd() / DEFAULT_FILENAME
    if cwd_file.is_file():
        return cwd_file
    if USER_CONFIG.is_file():
        return USER_CONFIG
    return None


def load_config(explicit: Optional[str] = None, *, strict: bool = False
                ) -> tuple[dict[str, str], Optional[Path]]:
    """读取配置文件，返回 ``(配置字典, 实际使用的路径)``。

    - 找不到文件且非 strict → 返回 ``({}, None)``（正常情况：没配就用内置默认）；
    - strict 下显式指定的路径不存在 → 抛 ``FileNotFoundError``（避免拼错路径被静默忽略）。
    """
    path = find_config_file(explicit)
    if path is None:
        return {}, None
    if not path.is_file():
        if strict and explicit:
            raise FileNotFoundError(f"配置文件不存在: {path}")
        return {}, None
    text = path.read_text(encoding="utf-8-sig")  # 容忍 Windows 记事本的 BOM
    return parse_env_text(text), path


def apply_config(explicit: Optional[str] = None, *, override: bool = False,
                 strict: bool = False) -> dict[str, str]:
    """把配置文件注入 ``os.environ``，返回实际生效的键值。

    ``override=False``（默认）时不覆盖已存在的环境变量——运行时注入优先。
    只注入白名单前缀的键；其余键在文件中保留但不生效（不报错）。
    """
    config, _path = load_config(explicit, strict=strict)
    applied: dict[str, str] = {}
    for key, value in config.items():
        if not key.startswith(ALLOWED_PREFIXES):
            continue
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


def preview(explicit: Optional[str] = None) -> dict:
    """注入**之前**预览：用哪个文件、哪些键会生效、哪些被环境变量压制。

    供启动日志使用；必须在 ``apply_config`` 之前调用（注入后 os.environ 里
    已有这些键，会被误判为「被压制」）。
    """
    config, path = load_config(explicit)
    usable = [k for k in config if k.startswith(ALLOWED_PREFIXES)]
    applied = [k for k in usable if k not in os.environ]
    shadowed = [k for k in usable if k in os.environ]
    ignored = [k for k in config if not k.startswith(ALLOWED_PREFIXES)]
    return {"file": str(path) if path else None,
            "applied": sorted(applied),
            "shadowed": sorted(shadowed),
            "ignored": sorted(ignored)}
