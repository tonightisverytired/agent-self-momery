# -*- coding: utf-8 -*-
"""Postgres 时间链后端（阶段 1）。

- 查询只读：数据由 sync_memory_to_postgres 预置；
- 分语义与 retrieval._time_path 一致（Python timedelta.days 语义）。
"""
from __future__ import annotations

from datetime import datetime

from ..errors import StorageError

DDL = """
CREATE TABLE IF NOT EXISTS nodes (
  id BIGINT PRIMARY KEY,
  node_type TEXT NOT NULL,
  name TEXT NOT NULL,
  ts TIMESTAMPTZ,
  value_score DOUBLE PRECISION NOT NULL DEFAULT 0.5
);
CREATE TABLE IF NOT EXISTS edges (
  from_id BIGINT NOT NULL,
  to_id BIGINT NOT NULL,
  rel TEXT NOT NULL,
  weight DOUBLE PRECISION NOT NULL,
  confidence DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_ts ON nodes(ts);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);
"""


class PostgresTimeBackend:
    """时间链后端：`time_window(t0, tol_days)` 返回事件 node_id -> 时间分。"""

    def __init__(self, dsn: str):
        try:
            import psycopg
        except ImportError as e:  # pragma: no cover
            raise StorageError(
                "E009 需要 psycopg：pip install dnamemory[pg]") from e
        try:
            self._conn = psycopg.connect(dsn)
        except Exception as e:
            raise StorageError(f"E009 Postgres 连接失败: {e}") from e

    def time_window(self, t0: datetime, tol_days: int) -> dict[int, float]:
        try:
            rows = self._conn.execute(
                """
                SELECT id,
                       1.0/(1.0 + ABS(FLOOR(
                           EXTRACT(EPOCH FROM (ts - %s))/86400))) AS score
                FROM nodes
                WHERE node_type='event' AND ts IS NOT NULL
                  AND ABS(FLOOR(EXTRACT(EPOCH FROM (ts - %s))/86400)) <= %s
                ORDER BY id
                """, (t0, t0, int(tol_days))).fetchall()
        except Exception as e:
            raise StorageError(f"E009 Postgres 时间查询失败: {e}") from e
        return {int(r[0]): float(r[1]) for r in rows}

    def close(self):
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


def _assert_not_primary_store(conn):
    """守卫：目标库若是 PGStore 主存储（完整 schema），拒绝 DROP。

    本函数的 DDL 是**精简版**（nodes 只有 5 列、edges 无生命周期），与主存储
    schema 不可共存；误指主存储库会在 DROP 时删掉全部数据。
    """
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='nodes' AND column_name='idempotency_key'").fetchone()
    if row:
        raise StorageError(
            "E009 目标库是 dnamemory 主存储 schema（nodes 含 idempotency_key），"
            "拒绝执行 DROP。时间链后端请用独立的库（如 dnamemory_legacy）。")


def sync_memory_to_postgres(mem, dsn: str):
    """把 MemorySystem 的 nodes/edges 预置到 Postgres（查询只读用）。"""
    import psycopg
    conn = psycopg.connect(dsn)
    try:
        _assert_not_primary_store(conn)
        conn.execute("DROP TABLE IF EXISTS edges")
        conn.execute("DROP TABLE IF EXISTS nodes")
        conn.execute(DDL)
        nodes = mem.store.fetch_nodes()
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO nodes(id,node_type,name,ts,value_score) "
                "VALUES(%s,%s,%s,%s,%s)",
                [(n.nid, n.node_type, n.name, n.ts, n.value_score)
                 for n in nodes])
            cur.executemany(
                "INSERT INTO edges(from_id,to_id,rel,weight,confidence) "
                "VALUES(%s,%s,%s,%s,%s)",
                [(e.from_id, e.to_id, e.rel, e.weight, e.confidence)
                 for e in mem.store.fetch_edges()])
        conn.commit()
    finally:
        conn.close()
