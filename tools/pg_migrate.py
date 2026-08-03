# -*- coding: utf-8 -*-
"""Postgres DDL 迁移脚本（P3 可选交付）。

默认打印 SQL；传 --dsn 时直接执行（需 pip install dnamemory[pg]）：
  py tools/pg_migrate.py
  py tools/pg_migrate.py --dsn postgresql://user:pass@host/db
"""
from __future__ import annotations

import argparse

DDL = """
CREATE TABLE IF NOT EXISTS nodes (
  id BIGSERIAL PRIMARY KEY,
  node_type TEXT NOT NULL CHECK (node_type IN ('event','entity')),
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  ts TIMESTAMPTZ,
  value_score DOUBLE PRECISION NOT NULL,
  protected BOOLEAN NOT NULL DEFAULT FALSE,
  access_label TEXT NOT NULL DEFAULT 'public',
  lifecycle TEXT NOT NULL DEFAULT 'active',
  life DOUBLE PRECISION NOT NULL,
  decay_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
  last_access TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL,
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_nodes_ts ON nodes(ts);
CREATE INDEX IF NOT EXISTS idx_nodes_life ON nodes(lifecycle);

CREATE TABLE IF NOT EXISTS edges (
  id BIGSERIAL PRIMARY KEY,
  from_id BIGINT NOT NULL REFERENCES nodes(id),
  to_id BIGINT NOT NULL REFERENCES nodes(id),
  relation_type TEXT NOT NULL,
  weight DOUBLE PRECISION NOT NULL,
  confidence DOUBLE PRECISION NOT NULL,
  valid_at TIMESTAMPTZ NOT NULL,
  invalid_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL,
  last_access TIMESTAMPTZ,
  access_count INTEGER NOT NULL DEFAULT 0,
  lifecycle TEXT NOT NULL DEFAULT 'active',
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(relation_type);

CREATE TABLE IF NOT EXISTS facts (
  id BIGSERIAL PRIMARY KEY,
  node_id BIGINT NOT NULL REFERENCES nodes(id),
  fact_key TEXT NOT NULL,
  fact_value TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence DOUBLE PRECISION NOT NULL,
  valid_at TIMESTAMPTZ NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL,
  invalid_at TIMESTAMPTZ,
  superseded_by BIGINT,
  tombstoned BOOLEAN NOT NULL DEFAULT FALSE,
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(node_id, fact_key);

CREATE TABLE IF NOT EXISTS versions (
  id BIGSERIAL PRIMARY KEY,
  node_id BIGINT NOT NULL REFERENCES nodes(id),
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS tombstones (
  id BIGSERIAL PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id BIGINT NOT NULL,
  reason TEXT NOT NULL,
  tombstoned_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  op TEXT NOT NULL,
  target_type TEXT,
  target_id BIGINT,
  reason TEXT,
  meta TEXT,
  at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS node_vectors (
  node_id BIGINT PRIMARY KEY REFERENCES nodes(id),
  dense TEXT NOT NULL,
  sparse TEXT,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS vector_index_meta (
  model TEXT PRIMARY KEY,
  dim INTEGER NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=None,
                        help="提供则直接执行 DDL，否则仅打印")
    args = parser.parse_args()
    if args.dsn:
        import psycopg
        with psycopg.connect(args.dsn) as conn:
            conn.execute(DDL)
            conn.commit()
        print("DDL 执行完成")
    else:
        print(DDL)


if __name__ == "__main__":
    main()
