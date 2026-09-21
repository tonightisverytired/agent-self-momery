# -*- coding: utf-8 -*-
"""0.7.0 P1-05-T ImpactResolver 裁决测试卡。"""
from datetime import datetime, timedelta

from dnamemory import MemorySystem, RecallFilters
from dnamemory.resolve import ImpactResolver, MemoryStateResolver

START = datetime(2026, 3, 1)


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


def _mem_with_impacts():
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    return mem, u


# ---------------- P1-05-T ImpactResolver ----------------
class TestImpactResolver:
    def test_current_by_subject_dimension(self):
        mem, u = _mem_with_impacts()
        t1, t2 = datetime(2026, 1, 1), datetime(2026, 2, 1)
        mem.store.insert_impact(u, "income", "increase", "positive", 0.7,
                                valid_at=t1, created_at=t1)
        mem.store.insert_impact(u, "income", "decrease", "negative", 0.6,
                                valid_at=t2, created_at=t2)
        out = ImpactResolver(mem.config).resolve(mem.store, START)
        assert len(out["current"]) == 1
        assert out["current"][0].direction == "decrease"
        assert [i.direction for i in out["history"]] == ["increase"]
        mem.close()

    def test_coexist_dimensions(self):
        mem, u = _mem_with_impacts()
        t0 = datetime(2026, 1, 1)
        mem.store.insert_impact(u, "income", "increase", "positive", 0.7,
                                valid_at=t0, created_at=t0)
        mem.store.insert_impact(u, "stress", "increase", "negative", 0.8,
                                valid_at=t0, created_at=t0)
        out = ImpactResolver(mem.config).resolve(mem.store, START)
        dims = {i.dimension for i in out["current"]}
        assert dims == {"income", "stress"}, "不同维度并行 current 不互斥"
        mem.close()

    def test_disappear_stays_current(self):
        mem, u = _mem_with_impacts()
        t0 = datetime(2026, 1, 1)
        mem.store.insert_impact(u, "hobby", "disappear", "neutral", 0.5,
                                valid_at=t0, created_at=t0)
        out = ImpactResolver(mem.config).resolve(mem.store, START)
        assert len(out["current"]) == 1
        assert out["current"][0].direction == "disappear"
        mem.close()

    def test_expired_to_history(self):
        mem, u = _mem_with_impacts()
        t0 = datetime(2026, 1, 1)
        mem.store.insert_impact(u, "income", "increase", "positive", 0.7,
                                valid_at=t0, invalid_at=datetime(2026, 2, 1),
                                created_at=t0)
        out = ImpactResolver(mem.config).resolve(mem.store, START)
        assert not out["current"]
        assert len(out["history"]) == 1
        mem.close()

    def test_superseded_to_history(self):
        mem, u = _mem_with_impacts()
        t0 = datetime(2026, 1, 1)
        mem.store.insert_impact(u, "income", "increase", "positive", 0.7,
                                valid_at=t0, created_at=t0)
        mem.store.insert_impact(u, "income", "stable", "neutral", 0.5,
                                valid_at=datetime(2026, 2, 1),
                                superseded_by=999,
                                created_at=datetime(2026, 2, 1))
        out = ImpactResolver(mem.config).resolve(mem.store, START)
        assert len(out["current"]) == 1
        assert out["current"][0].direction == "increase"
        mem.close()

    def test_resolver_failure_isolated(self, monkeypatch):
        """impact 子 resolver 异常不影响其他维度解析。"""
        mem, u = _mem_with_impacts()
        t0 = datetime(2026, 1, 1)
        mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                     valid_at=t0)

        def boom(store, query_time=None, filters=None):
            raise RuntimeError("impact 解析爆炸")

        monkeypatch.setattr("dnamemory.resolve.ImpactResolver.resolve", boom)
        resolver = MemoryStateResolver(mem.store, mem.config)
        state = resolver.resolve(mem.store.fetch_nodes(), START,
                                 query_type="current_state")
        assert state.current_facts, "fact 维度不受 impact 异常影响"
        mem.close()
