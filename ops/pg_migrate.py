# -*- coding: utf-8 -*-
"""PostgreSQL 建表脚本（薄壳，DDL 单一真理源）。

DDL 直接来自 ``dnamemory.pg_schema.DDL_STATEMENTS``，与运行时建表（PGStore）
和迁移脚本共用同一份——本文件原先内嵌的那套 8 张表 DDL 已删除，它缺
beliefs/intents/impacts/memory_links/patterns/triggers 六张表，且类型与主
schema 不一致，是漂移源。

用法：
  py ops/pg_migrate.py                                  # 打印 DDL
  py ops/pg_migrate.py --dsn postgresql://user:pass@host/db   # 直接执行
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dnamemory.pg_schema import DDL_STATEMENTS, ddl_text  # noqa: E402


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main():
    _force_utf8()
    parser = argparse.ArgumentParser(description="PostgreSQL 建表（DDL 单一来源）")
    parser.add_argument("--dsn", default=None,
                        help="提供则直接执行 DDL，否则仅打印")
    args = parser.parse_args()
    if args.dsn:
        import psycopg
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            for stmt in DDL_STATEMENTS:
                conn.execute(stmt)
        print(f"DDL 执行完成（{len(DDL_STATEMENTS)} 条语句，含 vector 扩展与 HNSW 索引）")
    else:
        print(ddl_text())


if __name__ == "__main__":
    main()
