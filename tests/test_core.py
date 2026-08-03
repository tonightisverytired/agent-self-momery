# -*- coding: utf-8 -*-
"""M1 核心单元测试：配置、存储、事务、幂等、生命周期、矛盾、TTL/墓碑、
访问控制、反射压缩、持久化。"""
from datetime import datetime, timedelta

import pytest

from dnamemory import (MemoryConfig, MemorySystem, RecallFilters, RecallQuery)
from dnamemory.errors import ValidationError

START = datetime(2026, 3, 1)


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


def test_config_validation():
    with pytest.raises(ValidationError):
        MemoryConfig(relations=frozenset())
    with pytest.raises(ValidationError):
        MemoryConfig(source_rank={"agent": 2})
    with pytest.raises(ValidationError):
        MemoryConfig(conflict_confirm_threshold=1.5)
    with pytest.raises(ValidationError):
        MemoryConfig(rrf_k=0)
    with pytest.raises(ValidationError):
        MemoryConfig(dense_min_sim=1.5)


def test_insert_and_idempotency(tmp_path):
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    a = mem.add_event("幂等事件", START, kind="meeting",
                      idempotency_key="evt:1")
    b = mem.add_event("幂等事件", START, kind="meeting",
                      idempotency_key="evt:1")
    assert a == b
    mem.add_entity("咖啡", "preference")
    f1 = mem.add_fact("咖啡", "drink", "拿铁", idempotency_key="fact:1")
    f2 = mem.add_fact("咖啡", "drink", "拿铁", idempotency_key="fact:1")
    assert f1 == f2
    mem.close()


def test_domain_and_bitemporal_validation():
    mem = MemorySystem()
    with pytest.raises(ValidationError):
        mem.add_edge("a", "b", "unknown_rel")
    mem.add_entity("x")
    with pytest.raises(ValidationError):
        mem.add_fact("x", "k", "v", valid_at=START,
                     invalid_at=START - timedelta(days=1))
    with pytest.raises(ValidationError):
        mem.add_edge("x", "x", "discusses", weight=1.5)


def test_lifecycle_decay_persists(tmp_path):
    clock = Clock()
    mem = MemorySystem(path=str(tmp_path / "m.db"), clock=clock)
    eid = mem.add_event("临时", START, kind="transient", life=1.0,
                        decay_rate=1.0)
    clock.now += timedelta(days=2)
    mem.step_day()
    node = next(n for n in mem.store.fetch_nodes() if n.nid == eid)
    assert node.lifecycle == "archived"
    mem.close()


def test_protected_no_decay(tmp_path):
    clock = Clock()
    mem = MemorySystem(path=str(tmp_path / "m.db"), clock=clock)
    mem.add_entity("咖啡", "preference", protected=True)
    mem.add_fact("咖啡", "drink", "拿铁", source="profile", confidence=0.95)
    for _ in range(300):
        clock.now += timedelta(days=1)
        mem.step_day()
    node = next(n for n in mem.store.fetch_nodes() if n.name == "咖啡")
    assert node.lifecycle == "active"
    assert mem.fact_lookup("咖啡", "drink")
    mem.close()


def test_conflict_source_trust():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    mem.add_entity("居住地", "preference", protected=True)
    mem.add_fact("居住地", "address", "上海", source="profile",
                 confidence=0.95, valid_at=clock())
    mem.add_fact("居住地", "address", "慕尼黑", source="chat",
                 confidence=0.6, valid_at=clock() + timedelta(days=30))
    ds = mem.resolve_conflicts()
    assert ds[0].kind == "resolved"
    assert ds[0].winner.value == "上海"
    assert [f.value for f in mem.fact_lookup("居住地", "address")] == ["上海"]


def test_conflict_same_source_confirm():
    mem = MemorySystem()
    mem.add_entity("口味", "preference", protected=True)
    mem.add_fact("口味", "taste", "辣的", source="chat", confidence=0.9)
    mem.add_fact("口味", "taste", "甜的", source="chat", confidence=0.9)
    ds = mem.resolve_conflicts()
    assert ds[0].kind == "confirm"


def test_ttl_and_tombstone_cascade(tmp_path):
    clock = Clock()
    mem = MemorySystem(path=str(tmp_path / "m.db"), clock=clock)
    mem.add_entity("促销活动", "concept")
    mem.add_fact("促销活动", "price", "85 折", source="agent",
                 confidence=0.8, valid_at=clock(),
                 invalid_at=clock() + timedelta(days=90))
    mem.add_entity("旧项目", "project")
    mem.add_fact("旧项目", "status", "进行中", source="chat", confidence=0.8)
    clock.now += timedelta(days=91)
    assert not mem.fact_lookup("促销活动", "price")
    mem.forget("旧项目", reason="retracted")
    assert not mem.fact_lookup("旧项目", "status")
    assert len(mem.store.fetch_tombstones()) == 1
    mem.close()


def test_access_control():
    mem = MemorySystem()
    mem.add_entity("健康档案", "preference", access_label="sensitive")
    mem.add_event("健康讨论", START, kind="chat", value_score=0.3,
                  access_label="sensitive", life=50.0, decay_rate=0.28)
    mem.add_edge("健康讨论", "健康档案", "discusses", 0.8, 0.9,
                 valid_at=START)
    hidden = mem.recall(RecallQuery(topic=["健康"]), k=5,
                        node_types=("event",))
    visible = mem.recall(RecallQuery(topic=["健康"]), k=5,
                         filters=RecallFilters(access_labels=("sensitive",)),
                         node_types=("event",))
    assert len(hidden) == 0
    assert len(visible) >= 1
    assert any(n.name == "健康档案" for n in mem.store.fetch_nodes())


def test_reflection_compression():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    mem.add_entity("项目A", "project")
    ids = []
    for d in range(5, 25, 5):
        eid = mem.add_event(f"讨论{d}", clock() + timedelta(days=d),
                            kind="meeting", value_score=0.6,
                            life=150.0, decay_rate=0.02)
        mem.add_edge(eid, "项目A", "discusses", 0.8, 0.9,
                     valid_at=clock() + timedelta(days=d))
        ids.append(eid)
    summary = mem.reflect_monthly(2026, 3)
    assert summary is not None
    assert len(mem.neighbors("2026-03 月度摘要", "summarizes")) == len(ids)
    assert len(mem.neighbors("2026-03 脉络", "part_of")) == 1


def test_persistence_reopen(tmp_path):
    path = str(tmp_path / "m.db")
    m1 = MemorySystem(path=path)
    m1.add_entity("项目A", "project")
    for d in range(1, 6):
        m1.add_event(f"事件{d}", START + timedelta(days=d), kind="meeting",
                     value_score=0.5, life=50.0, decay_rate=0.28)
        m1.add_edge(f"事件{d}", "项目A", "discusses", 0.6, 0.8,
                    valid_at=START + timedelta(days=d))
    n1 = len(m1.store.fetch_nodes())
    m1.close()
    m2 = MemorySystem(path=path)
    hits = m2.recall(RecallQuery(topic=["项目A"]), k=5,
                     node_types=("event",))
    assert len(m2.store.fetch_nodes()) == n1
    assert len(hits) >= 1
    m2.close()


def test_write_text_requires_extractor():
    mem = MemorySystem()
    with pytest.raises(ValidationError):
        mem.write_text("今天和张总讨论了预算")
