# -*- coding: utf-8 -*-
"""阶段 2 验收：Neo4j 空间链后端与默认路径 top-k 完全一致。

需要 DNAMEMORY_NEO4J_URI 环境变量（CI 默认跳过）。
"""
import os
import random
from datetime import datetime, timedelta

import pytest

from dnamemory import MemorySystem, RecallQuery
from dnamemory.backends.neo4j import Neo4jGraphBackend, sync_memory_to_neo4j

NEO4J_URI = os.environ.get("DNAMEMORY_NEO4J_URI")
NEO4J_AUTH = tuple(
    os.environ.get("DNAMEMORY_NEO4J_AUTH", "neo4j:dnatest2026").split(":", 1))
pytestmark = pytest.mark.skipif(
    not NEO4J_URI, reason="需要 DNAMEMORY_NEO4J_URI 环境变量")

START = datetime(2026, 3, 1)


def build_memory(path, seed=9):
    rng = random.Random(seed)
    mem = MemorySystem(path=path)
    entities = [("张总", "person"), ("李工", "person"), ("项目A", "project"),
                ("微服务", "concept"), ("预算", "concept"),
                ("供应商B", "vendor")]
    for name, kind in entities:
        mem.add_entity(name, kind)
    for i in range(50):
        ts = START + timedelta(days=rng.randint(0, 40))
        eid = mem.add_event(f"事件{i}", ts, kind="meeting",
                            value_score=0.5 + 0.1 * (i % 5))
        for _ in range(2):
            ent = rng.choice(entities)[0]
            mem.add_edge(eid, ent, "discusses",
                         round(rng.uniform(0.5, 0.9), 3), 0.8,
                         valid_at=ts)
    return mem


def test_neo4j_graph_backend_matches_default(tmp_path):
    path = str(tmp_path / "m.db")
    mem = build_memory(path)
    sync_memory_to_neo4j(mem, NEO4J_URI, auth=NEO4J_AUTH)
    default = MemorySystem(path=path)
    neo_mem = MemorySystem(
        path=path, graph_backend=Neo4jGraphBackend(NEO4J_URI, auth=NEO4J_AUTH))
    for topic in [["项目A"], ["张总"], ["微服务"], ["预算"]]:
        q = RecallQuery(topic=topic)
        a = [h.node_id for h in default.recall(
            q, k=20, mode="graph", node_types=("event",))]
        b = [h.node_id for h in neo_mem.recall(
            q, k=20, mode="graph", node_types=("event",))]
        assert a == b, f"图谱路 top-k 不一致: {topic}"
    default.close()
    neo_mem.close()
    mem.close()
