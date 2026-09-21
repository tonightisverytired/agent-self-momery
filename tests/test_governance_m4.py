# -*- coding: utf-8 -*-
"""M4 治理验收用例：生命周期状态机、合规删除级联、恢复语义、
反射压缩幂等/敏感级继承/主题簇、事实版本链。"""
from datetime import datetime, timedelta

import pytest

from dnamemory import MemorySystem, RecallQuery
from dnamemory.errors import ValidationError

START = datetime(2026, 3, 1)


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


def test_lifecycle_illegal_transition_rejected():
    mem = MemorySystem()
    eid = mem.add_event("事件", START, kind="transient", life=1.0,
                        decay_rate=1.0)
    mem.store.update_lifecycle(eid, "archived", mem.clock())
    mem.store.update_lifecycle(eid, "deleted", mem.clock())
    with pytest.raises(ValidationError):
        mem.store.update_lifecycle(eid, "active", mem.clock())
    with pytest.raises(ValidationError):
        mem.store.update_lifecycle(eid, "tombstoned", mem.clock())
    mem.close()


def test_compliance_delete_cascades_facts_and_edges(tmp_path):
    clock = Clock()
    mem = MemorySystem(path=str(tmp_path / "m.db"), clock=clock)
    ent = mem.add_entity("咖啡", "preference")
    mem.add_fact("咖啡", "drink", "拿铁", source="profile", confidence=0.95)
    ev = mem.add_event("喝咖啡", START, kind="life", value_score=0.3)
    mem.add_edge(ev, "咖啡", "mentions", 0.6, 0.7, valid_at=START)
    mem.forget("咖啡", reason="gdpr", force=True)
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    assert nodes[ent].lifecycle == "deleted"
    assert all(f.tombstoned for f in mem.store.fetch_facts())
    assert all(e.lifecycle == "tombstoned" for e in mem.store.fetch_edges())
    assert len(mem.store.fetch_tombstones()) == 1
    ops = [r[0] for r in mem.store.read(
        "SELECT op FROM audit_log ORDER BY id")]
    assert "edge_lifecycle" in ops
    assert ops.count("tombstone") >= 2  # node + fact
    mem.close()


def test_restore_rejects_deleted_and_recovers_tombstoned(tmp_path):
    clock = Clock()
    mem = MemorySystem(path=str(tmp_path / "m.db"), clock=clock)
    eid = mem.add_event("临时", START, kind="transient", life=1.0,
                        decay_rate=1.0)
    clock.now += timedelta(days=2)
    mem.step_day()
    mem.restore(eid)
    assert next(n for n in mem.store.fetch_nodes()
                if n.nid == eid).lifecycle == "active"
    mem.forget(eid, reason="retracted")
    mem.restore(eid)
    mem.forget(eid, reason="gdpr", force=True)
    with pytest.raises(ValidationError) as ei:
        mem.restore(eid)
    assert "E004" in str(ei.value)
    mem.close()


def test_reflect_monthly_idempotent():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    mem.add_entity("项目A", "project")
    for d in range(5, 25, 5):
        eid = mem.add_event(f"讨论{d}", clock() + timedelta(days=d),
                            kind="meeting", value_score=0.6,
                            life=150.0, decay_rate=0.02)
        mem.add_edge(eid, "项目A", "discusses", 0.8, 0.9,
                     valid_at=clock() + timedelta(days=d))
    first = mem.reflect_monthly(2026, 3)
    n1 = len(mem.store.fetch_nodes())
    second = mem.reflect_monthly(2026, 3)
    assert first == second
    assert len(mem.store.fetch_nodes()) == n1
    mem.close()


def test_reflect_inherits_highest_access_label():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    mem.add_event("公开事件", START, kind="life", value_score=0.3,
                  access_label="public")
    mem.add_event("敏感事件", START + timedelta(days=1), kind="health",
                  value_score=0.5, access_label="sensitive")
    mem.reflect_monthly(2026, 3)
    summary = next(n for n in mem.store.fetch_nodes()
                   if n.kind == "summary")
    cluster = next(n for n in mem.store.fetch_nodes()
                   if n.kind == "cluster" and "脉络" in n.name)
    assert summary.access_label == "sensitive"
    assert cluster.access_label == "sensitive"
    mem.close()


def test_fact_history_chain():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    mem.add_entity("居住地", "preference", protected=True)
    mem.add_fact("居住地", "address", "上海", source="profile",
                 confidence=0.95, valid_at=clock())
    mem.add_fact("居住地", "address", "慕尼黑", source="chat",
                 confidence=0.6, valid_at=clock() + timedelta(days=30))
    mem.resolve_conflicts()
    history = mem.fact_history("居住地", "address")
    assert len(history) == 2
    assert [f.value for f in history] == ["上海", "慕尼黑"]
    assert history[0].superseded_by is None
    assert history[1].superseded_by == history[0].fid
    mem.close()


def test_reflect_builds_theme_cluster():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    mem.add_entity("项目A", "project")
    ids = []
    for d in range(5, 20, 5):
        eid = mem.add_event(f"与项目A讨论{d}", clock() + timedelta(days=d),
                            kind="meeting", value_score=0.6,
                            life=150.0, decay_rate=0.02)
        mem.add_edge(eid, "项目A", "discusses", 0.8, 0.9,
                     valid_at=clock() + timedelta(days=d))
        ids.append(eid)
    summary = mem.reflect_monthly(2026, 3)
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    tcluster = next(n for n in nodes.values()
                    if n.kind == "cluster" and n.name == "2026-03 项目A")
    assert all(any(e.rel == "part_of" and e.from_id == eid
                   and e.to_id == tcluster.nid
                   for e in mem.store.fetch_edges())
               for eid in ids)
    assert any(e.rel == "part_of" and e.from_id == tcluster.nid
               and e.to_id == summary for e in mem.store.fetch_edges())
    mem.close()


# ---------------- P-01-T / P-02-T Reflection 2.0 ----------------
def test_reflect_covers_state():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    u = mem.add_entity("用户", "person")
    mem.add_event("事件", datetime(2026, 3, 5), kind="meeting",
                  life=150.0, decay_rate=0.02)
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 3, 10))
    mem.store.insert_belief(u, "观点", "positive", 0.8, "chat",
                            datetime(2026, 3, 15), None, None, "active",
                            "public", [], datetime(2026, 3, 15))
    mem.store.insert_intent(u, "计划", "active", 0.7, "chat",
                            datetime(2026, 3, 15), None, "active",
                            "public", [], datetime(2026, 3, 15))
    s1 = mem.reflect_monthly(2026, 3)
    assert s1 is not None
    links = mem.store.fetch_memory_links()
    assert any(l.relation == "summarizes" and l.source_type == "fact"
               for l in links)
    assert any(l.relation == "summarizes" and l.source_type == "belief"
               for l in links)
    assert any(l.relation == "summarizes" and l.source_type == "intent"
               for l in links)
    # 重复 reflect 幂等：不重复建链
    n1 = len(links)
    mem.reflect_monthly(2026, 3)
    assert len(mem.store.fetch_memory_links()) == n1
    mem.close()


def test_reflect_evidence_trace():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    u = mem.add_entity("用户", "person")
    mem.add_event("搬迁", datetime(2026, 3, 5), kind="meeting",
                  life=150.0, decay_rate=0.02)
    f = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                     valid_at=datetime(2026, 3, 10))
    evi = mem.store.insert_evidence("conversation", "c1", None, None,
                                    datetime(2026, 3, 5), None, 5, {},
                                    datetime(2026, 3, 5))
    with mem.store.transaction() as conn:
        conn.execute("UPDATE facts SET evidence_ids=? WHERE id=?",
                     (f"[{evi}]", f))
    s1 = mem.reflect_monthly(2026, 3)
    summary = next(n for n in mem.store.fetch_nodes() if n.nid == s1)
    # 摘要继承源记忆证据并集 → 反向追溯全链路
    assert evi in summary.evidence_ids
    ex = mem.explain(s1)
    assert ex["evidence"] and ex["evidence"][0].id == evi
    assert ex["related"]
    mem.close()
    # 无证据源 → explain(summary) 抛 E013
    from dnamemory.errors import EvidenceNotFoundError
    mem2 = MemorySystem(clock=Clock())
    u2 = mem2.add_entity("用户", "person")
    mem2.add_event("事件", datetime(2026, 3, 5), kind="meeting",
                   life=150.0, decay_rate=0.02)
    mem2.add_fact(u2, "city", "上海", source="profile", confidence=0.9,
                  valid_at=datetime(2026, 3, 10))
    s2 = mem2.reflect_monthly(2026, 3)
    with pytest.raises(EvidenceNotFoundError):
        mem2.explain(s2)
    mem2.close()


# ---------------- R-01-T Episode 构建 ----------------
def test_episode_clusters():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    mem.add_entity("项目A", "project")
    ev1 = mem.add_event("项目A会议一", datetime(2026, 3, 5), kind="meeting",
                        life=150.0, decay_rate=0.02)
    ev2 = mem.add_event("项目A会议二", datetime(2026, 3, 10), kind="meeting",
                        life=150.0, decay_rate=0.02)
    ev3 = mem.add_event("孤立事件", datetime(2026, 3, 15), kind="life",
                        life=150.0, decay_rate=0.02)
    mem.add_edge(ev1, "项目A", "discusses", 0.8, 0.9,
                 valid_at=datetime(2026, 3, 5))
    mem.add_edge(ev2, "项目A", "discusses", 0.8, 0.9,
                 valid_at=datetime(2026, 3, 10))
    mem.reflect_monthly(2026, 3)
    episodes = [n for n in mem.store.fetch_nodes() if n.kind == "episode"]
    assert len(episodes) == 2  # 共享实体一簇 + 孤立一簇
    ep_ids = {e.nid for e in episodes}
    part_edges = [e for e in mem.store.fetch_edges()
                  if e.rel == "part_of" and e.to_id in ep_ids]
    members = {e.from_id for e in part_edges}
    assert {ev1, ev2} <= members and ev3 in members
    # 共享实体的两事件同簇
    cl1 = {e.to_id for e in part_edges if e.from_id == ev1}
    cl2 = {e.to_id for e in part_edges if e.from_id == ev2}
    assert cl1 == cl2 and cl1
    # 重复 reflect 幂等
    n_ep = len(episodes)
    mem.reflect_monthly(2026, 3)
    assert len([n for n in mem.store.fetch_nodes() if n.kind == "episode"]) \
        == n_ep
    mem.close()


def test_episode_inherits_label():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    mem.add_entity("项目B", "project")
    mem.add_event("敏感会议", datetime(2026, 3, 5), kind="meeting",
                  access_label="sensitive", life=150.0, decay_rate=0.02)
    mem.reflect_monthly(2026, 3)
    eps = [n for n in mem.store.fetch_nodes() if n.kind == "episode"]
    assert eps and eps[0].access_label == "sensitive"
    mem.close()


# ---------------- R-02-T Stable Memory ----------------
def test_stable_memory():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    u = mem.add_entity("用户", "person")
    # 两个月的版本演化：3 月南京 → 4 月上海
    mem.add_event("三月事件", datetime(2026, 3, 5), kind="meeting",
                  life=150.0, decay_rate=0.02)
    mem.add_fact(u, "city", "南京", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 3, 1),
                 invalid_at=datetime(2026, 4, 1))
    mem.add_event("四月事件", datetime(2026, 4, 5), kind="meeting",
                  life=150.0, decay_rate=0.02)
    f_sh = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                        valid_at=datetime(2026, 4, 1))
    mem.reflect_monthly(2026, 3)
    mem.reflect_monthly(2026, 4)
    n = mem.compress_stable()
    assert n == 1
    stable = [x for x in mem.store.fetch_nodes() if x.kind == "stable"]
    assert len(stable) == 1 and "上海" in stable[0].name
    links = mem.store.fetch_memory_links()
    assert any(l.relation == "summarizes" and l.source_type == "fact"
               and l.target_id == stable[0].nid for l in links)
    # 幂等重跑不重复
    assert mem.compress_stable() == 0
    assert len([x for x in mem.store.fetch_nodes() if x.kind == "stable"]) \
        == 1
    mem.close()


def test_stable_memory_single_month_skips():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    u = mem.add_entity("用户", "person")
    mem.add_event("三月事件", datetime(2026, 3, 5), kind="meeting",
                  life=150.0, decay_rate=0.02)
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 3, 1))
    mem.reflect_monthly(2026, 3)
    assert mem.compress_stable() == 0  # 单月出现不生成
    mem.close()


# ---------------- R-03-T 压缩完整性 ----------------
def test_compression_keeps_raw():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    u = mem.add_entity("用户", "person")
    mem.add_event("三月事件", datetime(2026, 3, 5), kind="meeting",
                  life=150.0, decay_rate=0.02)
    f1 = mem.add_fact(u, "city", "南京", source="profile", confidence=0.9,
                      valid_at=datetime(2026, 3, 1),
                      invalid_at=datetime(2026, 4, 1))
    mem.add_event("四月事件", datetime(2026, 4, 5), kind="meeting",
                  life=150.0, decay_rate=0.02)
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 4, 1))
    n_facts = len(mem.store.fetch_facts())
    n_edges = len(mem.store.fetch_edges())
    mem.reflect_monthly(2026, 3)
    mem.reflect_monthly(2026, 4)
    mem.compress_stable()
    # 压缩后源记忆只增不减，原始事实仍 active
    assert len(mem.store.fetch_facts()) == n_facts
    assert len(mem.store.fetch_edges()) >= n_edges
    facts = mem.store.fetch_facts()
    assert all(f.node_id == u for f in facts)
    assert not any(f.tombstoned for f in facts)
    mem.close()


def test_reflect_empty_month_returns_none():
    mem = MemorySystem()
    mem.add_event("一月事件", datetime(2026, 1, 10), kind="life")
    assert mem.reflect_monthly(2026, 2) is None
    mem.close()
