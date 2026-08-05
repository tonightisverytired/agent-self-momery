# -*- coding: utf-8 -*-
"""阶段 1 验收：Postgres 时间链后端与默认路径结果一致。

需要 DNAMEMORY_PG_DSN 环境变量（CI 默认跳过）。
"""
import os
import random
from datetime import datetime, timedelta

import pytest

from dnamemory import MemorySystem, RecallQuery
from dnamemory.backends.postgres import (PostgresTimeBackend,
                                         sync_memory_to_postgres)

PG_DSN = os.environ.get("DNAMEMORY_PG_DSN")
pytestmark = pytest.mark.skipif(
    not PG_DSN, reason="需要 DNAMEMORY_PG_DSN 环境变量")

START = datetime(2026, 3, 1)


def build_memory(path, seed=7):
    rng = random.Random(seed)
    mem = MemorySystem(path=path)
    entities = [("张总", "person"), ("李工", "person"), ("项目A", "project"),
                ("微服务", "concept"), ("预算", "concept")]
    for name, kind in entities:
        mem.add_entity(name, kind)
    for i in range(60):
        ts = START + timedelta(days=rng.randint(0, 60),
                               hours=rng.randint(0, 23))
        eid = mem.add_event(f"事件{i}", ts, kind="meeting",
                            value_score=0.5 + 0.1 * (i % 5))
        mem.add_edge(eid, rng.choice(entities)[0], "discusses",
                     0.7, 0.8, valid_at=ts)
    return mem


def test_postgres_time_backend_matches_default(tmp_path):
    path = str(tmp_path / "m.db")
    mem = build_memory(path)
    sync_memory_to_postgres(mem, PG_DSN)
    default = MemorySystem(path=path)
    pg_mem = MemorySystem(path=path,
                          time_backend=PostgresTimeBackend(PG_DSN))
    events = [n for n in mem.store.fetch_nodes()
              if n.node_type == "event" and n.ts]
    rng = random.Random(1)
    for n in rng.sample(events, 10):
        q = RecallQuery(time=(n.ts, 2))
        a = [h.node_id for h in default.recall(
            q, k=20, mode="time", node_types=("event",))]
        b = [h.node_id for h in pg_mem.recall(
            q, k=20, mode="time", node_types=("event",))]
        assert a == b, f"时间路不一致: t0={n.ts}"
    default.close()
    pg_mem.close()
    mem.close()
