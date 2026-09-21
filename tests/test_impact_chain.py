# -*- coding: utf-8 -*-
"""0.7.0 P2 影响链测试卡（P2-03-T / P2-04-T）。"""
from datetime import datetime, timedelta

from dnamemory import MemorySystem
from dnamemory.temporal import derive_impact_links

START = datetime(2026, 3, 1)


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


def _base_mem():
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    return mem, u


# ---------------- P2-03-T 规则影响链推导 ----------------
class TestDeriveImpactLinks:
    def test_impact_to_belief_link(self):
        mem, u = _base_mem()
        t0 = datetime(2026, 1, 1)
        iid = mem.store.insert_impact(
            u, "stress", "increase", "negative", 0.8,
            valid_at=t0, created_at=t0)
        bid = mem.store.insert_belief(
            u, "工作压力太大了", "negative", 0.8, "chat",
            t0 + timedelta(days=3), None, None, "active", "public", [],
            t0 + timedelta(days=3))
        n = derive_impact_links(mem.store)
        assert n == 1
        links = mem.store.fetch_memory_links()
        assert any(l.relation == "caused_by" and l.source_type == "belief"
                   and l.source_id == iid and l.target_id == bid
                   and l.confidence == 0.5 for l in links)
        # 幂等重跑不重复
        assert derive_impact_links(mem.store) == 0
        assert len(mem.store.fetch_memory_links()) == len(links)
        mem.close()

    def test_outside_window_no_link(self):
        mem, u = _base_mem()
        t0 = datetime(2026, 1, 1)
        mem.store.insert_impact(u, "stress", "increase", "negative", 0.8,
                                valid_at=t0, created_at=t0)
        mem.store.insert_belief(u, "工作压力太大了", "negative", 0.8, "chat",
                                t0 + timedelta(days=30), None, None,
                                "active", "public", [],
                                t0 + timedelta(days=30))
        assert derive_impact_links(mem.store) == 0
        mem.close()

    def test_belief_to_intent_link(self):
        mem, u = _base_mem()
        t0 = datetime(2026, 1, 1)
        bid = mem.store.insert_belief(
            u, "工作压力太大了", "negative", 0.8, "chat", t0, None, None,
            "active", "public", [], t0)
        iid = mem.store.insert_intent(
            u, "考虑离职", "active", 0.7, "chat", t0 + timedelta(days=2),
            None, "active", "public", [], t0 + timedelta(days=2))
        n = derive_impact_links(mem.store)
        assert n == 1
        links = mem.store.fetch_memory_links()
        assert any(l.source_type == "intent" and l.source_id == bid
                   and l.target_id == iid and l.relation == "caused_by"
                   for l in links)
        mem.close()

    def test_impact_to_impact_link(self):
        mem, u = _base_mem()
        t0 = datetime(2026, 1, 1)
        i1 = mem.store.insert_impact(u, "income", "increase", "positive",
                                     0.7, valid_at=t0, created_at=t0)
        i2 = mem.store.insert_impact(u, "consumption", "increase",
                                     "positive", 0.6,
                                     valid_at=t0 + timedelta(days=3),
                                     created_at=t0 + timedelta(days=3))
        n = derive_impact_links(mem.store)
        assert n == 1
        links = mem.store.fetch_memory_links()
        assert any(l.source_type == "impact" and l.source_id == i1
                   and l.target_id == i2 and l.relation == "caused_by"
                   for l in links)
        mem.close()

    def test_no_ground_truth_modified(self):
        mem, u = _base_mem()
        t0 = datetime(2026, 1, 1)
        mem.store.insert_impact(u, "stress", "increase", "negative", 0.8,
                                valid_at=t0, created_at=t0)
        mem.store.insert_belief(u, "工作压力太大了", "negative", 0.8, "chat",
                                t0 + timedelta(days=3), None, None,
                                "active", "public", [],
                                t0 + timedelta(days=3))
        n_impacts = len(mem.store.fetch_impacts())
        n_beliefs = len(mem.store.fetch_beliefs())
        derive_impact_links(mem.store)
        assert len(mem.store.fetch_impacts()) == n_impacts
        assert len(mem.store.fetch_beliefs()) == n_beliefs
        mem.close()


# ---------------- P2-04-T ImpactChainBuilder ----------------
class TestImpactChainBuilder:
    def _chain_mem(self):
        """换工作 → 收入↑ → 消费观 belief → 离职 intent 语料。"""
        mem, u = _base_mem()
        t0 = datetime(2026, 1, 1)
        ev = mem.add_event("换工作", t0, kind="life")
        i1 = mem.store.insert_impact(
            u, "income", "increase", "positive", 0.7,
            cause_event_id=ev, valid_at=t0 + timedelta(days=1),
            created_at=t0 + timedelta(days=1))
        b = mem.store.insert_belief(
            u, "收入提高了可以改善生活", "positive", 0.8, "chat",
            t0 + timedelta(days=5), None, None, "active", "public", [],
            t0 + timedelta(days=5))
        it = mem.store.insert_intent(
            u, "考虑换更好的工作", "active", 0.7, "chat",
            t0 + timedelta(days=8), None, "active", "public", [],
            t0 + timedelta(days=8))
        mem.store.insert_memory_link(i1, b, "belief", "caused_by", 0.5,
                                     t0 + timedelta(days=5), None,
                                     source_dim="impact")
        mem.store.insert_memory_link(b, it, "intent", "caused_by", 0.5,
                                     t0 + timedelta(days=8), None,
                                     source_dim="belief")
        return mem, u, ev, i1, b, it

    def test_event_impact_belief_intent_chain(self):
        from dnamemory.impact_chain import ImpactChainBuilder
        mem, u, ev, i1, b, it = self._chain_mem()
        nodes = {n.nid: n for n in mem.store.fetch_nodes()}
        impacts = {i.id: i for i in mem.store.fetch_impacts()}
        beliefs = {x.id: x for x in mem.store.fetch_beliefs()}
        intents = {x.id: x for x in mem.store.fetch_intents()}
        memories = [nodes[ev], impacts[i1], beliefs[b], intents[it]]
        builder = ImpactChainBuilder()
        chains = builder.build(
            memories, links=mem.store.fetch_memory_links())
        assert len(chains) == 1
        dims = [n.dimension for n in chains[0].nodes]
        assert dims == ["event", "impact", "belief", "intent"]
        mem.close()

    def test_secondary_impact_chain(self):
        from dnamemory.impact_chain import ImpactChainBuilder
        mem, u = _base_mem()
        t0 = datetime(2026, 1, 1)
        i1 = mem.store.insert_impact(u, "income", "increase", "positive",
                                     0.7, valid_at=t0, created_at=t0)
        i2 = mem.store.insert_impact(u, "consumption", "increase",
                                     "positive", 0.6,
                                     valid_at=t0 + timedelta(days=2),
                                     created_at=t0 + timedelta(days=2))
        mem.store.insert_memory_link(i1, i2, "impact", "caused_by", 0.5,
                                     t0 + timedelta(days=2), None,
                                     source_dim="impact")
        impacts = {i.id: i for i in mem.store.fetch_impacts()}
        builder = ImpactChainBuilder()
        chains = builder.build([impacts[i1], impacts[i2]],
                               links=mem.store.fetch_memory_links())
        assert len(chains) == 1
        assert [n.dimension for n in chains[0].nodes] == \
            ["impact", "impact"]
        mem.close()

    def test_cycle_guard(self):
        from dnamemory.impact_chain import ImpactChainBuilder
        mem, u = _base_mem()
        t0 = datetime(2026, 1, 1)
        b1 = mem.store.insert_belief(u, "观点一", "positive", 0.8, "chat",
                                     t0, None, None, "active", "public",
                                     [], t0)
        b2 = mem.store.insert_belief(u, "观点二", "negative", 0.8, "chat",
                                     t0 + timedelta(days=1), None, None,
                                     "active", "public", [],
                                     t0 + timedelta(days=1))
        mem.store.insert_memory_link(b1, b2, "belief", "caused_by", 0.5,
                                     t0 + timedelta(days=1), None,
                                     source_dim="belief")
        mem.store.insert_memory_link(b2, b1, "belief", "caused_by", 0.5,
                                     t0 + timedelta(days=2), None,
                                     source_dim="belief")
        beliefs = {x.id: x for x in mem.store.fetch_beliefs()}
        builder = ImpactChainBuilder()
        chains = builder.build([beliefs[b1], beliefs[b2]],
                               links=mem.store.fetch_memory_links())
        # 不死循环且每个链无重复节点
        for c in chains:
            ids = [n.memory.id for n in c.nodes]
            assert len(ids) == len(set(ids))
        mem.close()

    def test_empty_returns_empty(self):
        from dnamemory.impact_chain import ImpactChainBuilder
        assert ImpactChainBuilder().build([], links=[]) == []
