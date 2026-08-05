# -*- coding: utf-8 -*-
"""阶段 0 验收：后端协议、默认路径零派发、Fake 后端 RRF 融合。"""
from datetime import datetime

from dnamemory import MemorySystem, RecallQuery
from dnamemory.backends.base import GraphBackend, TimeBackend

START = datetime(2026, 3, 1)


class CountingTimeBackend:
    def __init__(self):
        self.calls = 0
        self.closed = False

    def time_window(self, t0, tol_days):
        self.calls += 1
        return {}

    def close(self):
        self.closed = True


class CountingGraphBackend:
    def __init__(self):
        self.calls = 0
        self.closed = False

    def traverse(self, seed_ids, max_hops, rel_filter=None):
        self.calls += 1
        return {}

    def close(self):
        self.closed = True


class FakeTimeBackend:
    def __init__(self, scores):
        self.scores = scores

    def time_window(self, t0, tol_days):
        return self.scores

    def close(self):
        pass


class FakeGraphBackend:
    def __init__(self, scores):
        self.scores = scores

    def traverse(self, seed_ids, max_hops, rel_filter=None):
        return self.scores

    def close(self):
        pass


def test_backend_protocols_runtime_checkable():
    assert isinstance(CountingTimeBackend(), TimeBackend)
    assert isinstance(CountingGraphBackend(), GraphBackend)


def test_default_path_does_not_dispatch_backends():
    tb = CountingTimeBackend()
    gb = CountingGraphBackend()
    mem = MemorySystem(time_backend=tb, graph_backend=gb)
    mem.add_event("与张总讨论预算", START, kind="meeting", value_score=0.6)
    mem.add_entity("项目A", "project")
    # 语义/词面路径不触发时间与图谱后端
    mem.recall(RecallQuery(text="预算"), k=5, mode="semantic")
    assert tb.calls == 0 and gb.calls == 0
    # 时间路触发时间后端，不触发图谱后端
    mem.recall(RecallQuery(time=(START, 2)), k=5, mode="time")
    assert tb.calls == 1 and gb.calls == 0
    # 图谱路触发图谱后端，不触发时间后端
    mem.recall(RecallQuery(topic=["项目A"]), k=5, mode="graph")
    assert tb.calls == 1 and gb.calls == 1
    mem.close()
    assert tb.closed and gb.closed


def test_fake_backends_fuse_into_rrf():
    tb = FakeTimeBackend({1: 0.9})
    gb = FakeGraphBackend({1: 0.5, 2: 0.8})
    mem = MemorySystem(time_backend=tb, graph_backend=gb)
    e1 = mem.add_event("事件一", START, kind="meeting", value_score=0.6)
    e2 = mem.add_event("事件二", START, kind="meeting", value_score=0.6)
    mem.add_entity("项目A", "project")
    hits = mem.recall(RecallQuery(time=(START, 2), topic=["项目A"]),
                      k=5, mode="dual", node_types=("event",))
    assert [h.node_id for h in hits] == [e1, e2]
    mem.close()


def test_plain_memorysystem_unchanged(tmp_path):
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    eid = mem.add_event("与张总讨论预算", START, kind="meeting",
                        value_score=0.6)
    mem.add_entity("项目A", "project")
    hits = mem.recall(RecallQuery(time=(START, 2), topic=["项目A"]),
                      k=5, mode="dual", node_types=("event",))
    assert hits and hits[0].node_id == eid
    mem.close()
