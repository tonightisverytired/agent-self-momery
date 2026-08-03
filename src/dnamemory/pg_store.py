# -*- coding: utf-8 -*-
"""PostgresStore（可选，设计占位）。

本期仅交付设计占位与 DDL（tools/pg_migrate.py），不接入 MemorySystem：
- 完整实现列为正式项目后续里程碑；
- 避免半成品后端进入主路径导致隐性行为差异。
"""


class PostgresStore:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "PostgresStore 为设计期占位：请先运行 tools/pg_migrate.py 建表，"
            "完整实现见 docs/系统设计与架构设计.md")
