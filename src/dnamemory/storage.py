# -*- coding: utf-8 -*-
"""存储后端解析（可选 PostgreSQL）。

「用 SQLite 还是 PostgreSQL」只在这一处判断：CLI / HTTP 服务 / MCP 都调
``build_store()``，避免三处各写一遍解析逻辑。

默认（不设 DSN）仍是 SQLite，构造路径与既有行为逐字节相同；提供 DSN 时切到
``PGStore``。回退方式：清掉 ``DNAMEMORY_DSN`` 并重启即可。
"""
from __future__ import annotations

import os
from typing import Optional

from .store import SQLiteStore

# 环境变量名（与 server/main.py、server/mcp.py 共用）
DSN_ENV = "DNAMEMORY_DSN"


def resolve_dsn(cli_dsn: Optional[str] = None) -> Optional[str]:
    """解析 DSN：命令行参数 > 环境变量 DNAMEMORY_DSN > None（表示用 SQLite）。"""
    return cli_dsn or os.environ.get(DSN_ENV) or None


def build_store(path: str = ":memory:", dsn: Optional[str] = None):
    """按 DSN 选择存储实现；dsn 为空时返回 SQLiteStore（默认路径，零行为变更）。"""
    if dsn:
        from .pg_store import PGStore  # 惰性 import：未装 psycopg 不影响 SQLite 路径
        return PGStore(dsn)
    return SQLiteStore(path)
