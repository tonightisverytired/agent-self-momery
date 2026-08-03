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


def test_reflect_empty_month_returns_none():
    mem = MemorySystem()
    mem.add_event("一月事件", datetime(2026, 1, 10), kind="life")
    assert mem.reflect_monthly(2026, 2) is None
    mem.close()
