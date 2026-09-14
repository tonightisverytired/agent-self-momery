# -*- coding: utf-8 -*-
"""0.5.0 阶段 C：QueryRouter + FactResolver + MemoryStateResolver 测试卡
（C-01-T ~ C-05-T）。"""
from datetime import datetime, timedelta

from dnamemory import MemorySystem, RecallFilters
from dnamemory.resolve import FactResolver, MemoryStateResolver, query_router

START = datetime(2026, 3, 1)


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


# ---------------- C-01-T QueryRouter ----------------
def test_query_router():
    assert query_router("我现在住哪里？") == "current_state"
    assert query_router("我以前住哪里？") == "history"
    assert query_router("我什么时候搬到上海的？") == "timeline"
    assert query_router("我为什么开始考虑离开上海？") == "why_change"
    assert query_router("今天天气如何") == "semantic_recall"


# ---------------- C-02-T FactResolver 访问控制 ----------------
def test_fact_resolver_access():
    t0 = datetime(2026, 1, 1)
    mem = MemorySystem()
    pub = mem.add_entity("用户", "person")
    sen = mem.add_entity("健康档案", "preference", access_label="sensitive")
    mem.add_fact(pub, "city", "上海", source="profile", confidence=0.9,
                 valid_at=t0)
    mem.add_fact(sen, "condition", "高血压", source="profile", confidence=0.9,
                 valid_at=t0)
    fr = FactResolver(mem.config)
    out = fr.resolve(mem.store, START, filters=RecallFilters())
    vals = {f.value for f in out["current"]}
    assert "上海" in vals and "高血压" not in vals
    out2 = fr.resolve(mem.store, START,
                      filters=RecallFilters(access_labels=("sensitive",)))
    vals2 = {f.value for f in out2["current"]}
    assert "高血压" in vals2
    # tombstoned 实体的事实任何条件下不可见
    mem.forget("用户", "retracted")
    out3 = fr.resolve(mem.store, START)
    assert "上海" not in {f.value for f in out3["current"]}
    mem.close()
    # archived 仅 include_archived=True 可见
    mem2 = MemorySystem()
    e = mem2.add_entity("旧项目", "project")
    mem2.add_fact(e, "status", "进行中", source="chat", confidence=0.9,
                  valid_at=t0)
    mem2.store.update_lifecycle(e, "archived", START)
    fr2 = FactResolver(mem2.config)
    assert fr2.resolve(mem2.store, START)["current"] == []
    out4 = fr2.resolve(mem2.store, START,
                       filters=RecallFilters(include_archived=True))
    assert [f.value for f in out4["current"]] == ["进行中"]
    mem2.close()


# ---------------- C-03-T 时间有效集 ----------------
def test_fact_validity_window():
    mem = MemorySystem()
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "北京", source="profile", confidence=0.9,
                 valid_at=datetime(2025, 1, 1),
                 invalid_at=datetime(2025, 8, 1))
    mem.add_fact(u, "city", "南京", source="profile", confidence=0.9,
                 valid_at=datetime(2025, 8, 1),
                 invalid_at=datetime(2026, 7, 1))
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 7, 1))
    fr = FactResolver(mem.config)
    out = fr.resolve(mem.store, datetime(2026, 9, 1))
    assert [f.value for f in out["current"]] == ["上海"]
    assert {f.value for f in out["history"]} == {"北京", "南京"}
    out = fr.resolve(mem.store, datetime(2025, 5, 1))
    assert [f.value for f in out["current"]] == ["北京"]
    out = fr.resolve(mem.store, datetime(2026, 1, 1))
    assert [f.value for f in out["current"]] == ["南京"]
    mem.close()


# ---------------- C-04-T 裁决链 ----------------
def test_fact_resolve_chain():
    # 场景1：写入端固化 superseded_by（低 rank 版本 invalid）→ current=高 rank
    clock = Clock(datetime(2026, 3, 1))
    mem = MemorySystem(clock=clock)
    u = mem.add_entity("用户", "person")
    t0 = datetime(2026, 1, 1)
    mem.add_fact(u, "city", "南京", source="chat", confidence=0.6,
                 valid_at=t0)
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=t0)
    mem.resolve_conflicts()
    fr = FactResolver(mem.config)
    out = fr.resolve(mem.store, datetime(2026, 6, 1))
    assert [f.value for f in out["current"]] == ["上海"]
    assert "南京" in {f.value for f in out["history"]}
    mem.close()
    # 场景2：同 source_rank 同高置信 → conflict
    mem2 = MemorySystem(clock=Clock(datetime(2026, 3, 1)))
    u2 = mem2.add_entity("用户2", "person")
    mem2.add_fact(u2, "口味", "辣的", source="chat", confidence=0.9,
                  valid_at=t0)
    mem2.add_fact(u2, "口味", "甜的", source="chat", confidence=0.9,
                  valid_at=t0)
    fr2 = FactResolver(mem2.config)
    out2 = fr2.resolve(mem2.store, datetime(2026, 6, 1))
    assert {f.value for f in out2["conflict"]} == {"辣的", "甜的"}
    assert out2["current"] == []
    mem2.close()
    # 场景3：source_rank 不同 → 高 rank 者 current（即使 confidence 更低）
    mem3 = MemorySystem()
    u3 = mem3.add_entity("用户3", "person")
    mem3.add_fact(u3, "城市", "广州", source="chat", confidence=0.9,
                  valid_at=t0)
    mem3.add_fact(u3, "城市", "深圳", source="profile", confidence=0.5,
                  valid_at=t0)
    fr3 = FactResolver(mem3.config)
    out3 = fr3.resolve(mem3.store, datetime(2026, 6, 1))
    assert [f.value for f in out3["current"]] == ["深圳"]
    mem3.close()
    # 场景4：同 rank 不同 confidence（低于 confirm 阈值）→ 高 confidence 者 current
    mem4 = MemorySystem()
    u4 = mem4.add_entity("用户4", "person")
    mem4.add_fact(u4, "爱好", "跑步", source="chat", confidence=0.6,
                  valid_at=t0)
    mem4.add_fact(u4, "爱好", "游泳", source="chat", confidence=0.9,
                  valid_at=t0)
    fr4 = FactResolver(mem4.config)
    out4 = fr4.resolve(mem4.store, datetime(2026, 6, 1))
    assert [f.value for f in out4["current"]] == ["游泳"]
    mem4.close()


# ---------------- C-05-T MemoryStateResolver 编排 ----------------
def test_memory_state_resolve():
    mem = MemorySystem(clock=Clock(datetime(2026, 6, 1)))
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "南京", source="chat", confidence=0.6,
                 valid_at=datetime(2026, 1, 1),
                 invalid_at=datetime(2026, 5, 1))
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 5, 1))
    mem.add_event("搬到上海", datetime(2026, 5, 1), kind="life")
    facts = mem.store.fetch_facts()
    event_nodes = [n for n in mem.store.fetch_nodes()
                   if n.node_type == "event"]
    resolver = MemoryStateResolver(mem.store, mem.config)
    state = resolver.resolve(facts + event_nodes,
                             query_time=datetime(2026, 6, 1))
    assert [f.value for f in state.current_facts] == ["上海"]
    assert "南京" in {f.value for f in state.historical_facts}
    assert {e.name for e in state.events} == {"搬到上海"}
    # belief 维度局部失败不阻断 fact/event
    class BadResolver:
        def resolve(self, *a, **k):
            raise RuntimeError("boom")
    resolver.belief_resolver = BadResolver()
    state = resolver.resolve(facts + event_nodes,
                             query_time=datetime(2026, 6, 1))
    assert [f.value for f in state.current_facts] == ["上海"]
    assert {e.name for e in state.events} == {"搬到上海"}
    mem.close()
