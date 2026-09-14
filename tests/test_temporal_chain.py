# -*- coding: utf-8 -*-
"""0.5.0 阶段 E：TemporalChainBuilder 测试卡（E-01-T/E-02-T）。"""
from datetime import datetime, timedelta

from dnamemory import MemorySystem
from dnamemory.temporal import TemporalChainBuilder, stable_sort


class Clock:
    def __init__(self, t=datetime(2026, 3, 1)):
        self.now = t

    def __call__(self):
        return self.now


# ---------------- E-01-T 稳定时间排序 ----------------
def test_stable_sort():
    mem = MemorySystem()
    u = mem.add_entity("用户", "person")
    t0 = datetime(2026, 1, 1)
    # 三个同 valid_at 不同 id 的 fact：多次排序逐位一致
    f1 = mem.add_fact(u, "k1", "v1", source="chat", confidence=0.7,
                      valid_at=t0)
    f2 = mem.add_fact(u, "k2", "v2", source="chat", confidence=0.7,
                      valid_at=t0)
    f3 = mem.add_fact(u, "k3", "v3", source="chat", confidence=0.7,
                      valid_at=t0)
    facts = mem.store.fetch_facts()
    r1 = stable_sort(facts)
    r2 = stable_sort(facts)
    assert [f.fid for f in r1] == [f1, f2, f3]
    assert [f.fid for f in r1] == [f.fid for f in r2]
    # event/fact 混排：occurred_at(ts) 与 valid_at 正确先后
    ev = mem.add_event("后来的事件", t0 + timedelta(days=10), kind="life")
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    mixed = stable_sort(facts + [nodes[ev]])
    assert isinstance(mixed[0], facts[0].__class__) or mixed[0].fid == f1
    assert mixed[-1].nid == ev
    # 缺失 valid_at 的 belief 降级用 created_at
    b = mem.store.insert_belief(u, "无时间观点", "neutral", 0.7, "chat",
                                None, None, None, "active", "public", [],
                                t0 + timedelta(days=20))
    beliefs = mem.store.fetch_beliefs()
    r3 = stable_sort(beliefs)
    assert r3[0].id == b
    mem.close()


# ---------------- E-02-T 链拼接 ----------------
def test_chain_build():
    mem = MemorySystem()
    u = mem.add_entity("用户", "person")
    t = [datetime(2026, 1, 1) + timedelta(days=10 * i) for i in range(4)]
    eA = mem.add_event("事件A", t[0], kind="life")
    eB = mem.add_event("事件B", t[1], kind="life")
    fC = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                      valid_at=t[2])
    bD = mem.store.insert_belief(u, "观点D", "positive", 0.8, "chat",
                                 t[3], None, None, "active", "public", [],
                                 t[3])
    mem.store.insert_memory_link(eB, fC, "fact", "changes", 0.9, t[2], None)
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    factC = next(f for f in mem.store.fetch_facts() if f.fid == fC)
    beliefD = next(b for b in mem.store.fetch_beliefs() if b.id == bD)
    builder = TemporalChainBuilder(mem.config)
    chains = builder.build([nodes[eA], nodes[eB], factC, beliefD],
                           links=mem.store.fetch_memory_links())
    assert len(chains) == 1
    ns = chains[0].nodes
    assert [n.dimension for n in ns] == ["event", "event", "fact", "belief"]
    assert [n.memory.nid if n.dimension == "event" else None
            for n in ns[:2]] == [eA, eB]
    assert ns[1].relation == "before"
    assert ns[2].relation == "changes" and ns[2].source_id == eB
    assert ns[3].relation == "before"
    mem.close()


def test_chain_two_events_time_order():
    mem = MemorySystem()
    t0 = datetime(2026, 1, 1)
    e1 = mem.add_event("早事件", t0, kind="life")
    e2 = mem.add_event("晚事件", t0 + timedelta(days=5), kind="life")
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    builder = TemporalChainBuilder(mem.config)
    chains = builder.build([nodes[e1], nodes[e2]], links=[])
    assert len(chains) == 1 and len(chains[0].nodes) == 2
    assert chains[0].nodes[0].memory.nid == e1
    assert chains[0].nodes[1].memory.nid == e2
    assert chains[0].nodes[1].relation == "before"
    mem.close()


def test_chain_superseded_facts_changes():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    u = mem.add_entity("用户", "person")
    t0 = datetime(2026, 1, 1)
    f_old = mem.add_fact(u, "city", "南京", source="chat", confidence=0.6,
                         valid_at=t0)
    f_new = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                         valid_at=t0)
    mem.store.supersede_fact(f_old, f_new, t0)
    facts = mem.store.fetch_facts()
    builder = TemporalChainBuilder(mem.config)
    chains = builder.build(facts, links=[])
    assert len(chains) == 1 and len(chains[0].nodes) == 2
    assert chains[0].nodes[0].memory.fid == f_old
    assert chains[0].nodes[1].memory.fid == f_new
    assert chains[0].nodes[1].relation == "changes"
    assert chains[0].nodes[1].source_id == f_old
    mem.close()
