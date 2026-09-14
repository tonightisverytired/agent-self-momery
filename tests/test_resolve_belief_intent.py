# -*- coding: utf-8 -*-
"""0.5.0 阶段 D：BeliefResolver + IntentResolver 测试卡（D-01-T/D-02-T）。"""
from datetime import datetime

from dnamemory import MemorySystem
from dnamemory.resolve import BeliefResolver, IntentResolver


# ---------------- D-01-T BeliefTimeline ----------------
def test_belief_timeline():
    mem = MemorySystem()
    u = mem.add_entity("用户", "person")
    t1, t2, t3 = (datetime(2025, 1, 1), datetime(2026, 1, 1),
                  datetime(2026, 8, 1))
    b1 = mem.store.insert_belief(u, "上海工作机会多", "positive", 0.8, "chat",
                                 t1, None, None, "active", "public", [], t1)
    b2 = mem.store.insert_belief(u, "上海生活成本高", "negative", 0.8, "chat",
                                 t2, None, None, "active", "public", [], t2)
    b3 = mem.store.insert_belief(u, "上海工作机会仍然不错，但不适合长期生活",
                                 "neutral", 0.8, "chat",
                                 t3, None, None, "active", "public", [], t3)
    br = BeliefResolver(mem.config)
    out = br.resolve(mem.store, datetime(2026, 9, 1))
    # 三节点按时间序、当前=最新
    assert out["current"][0].id == b3
    assert [b.id for b in out["history"]] == [b1, b2]
    # supersedes 链接后：旧观点标 superseded 但仍在 history
    mem.store.insert_memory_link(b1, b2, "belief", "supersedes", 0.9,
                                 t2, None)
    out2 = br.resolve(mem.store, datetime(2026, 9, 1))
    assert out2["current"][0].id == b3
    assert any(b.id == b1 for b in out2["history"])
    assert any(c[0] == b1 and c[1] == b2 and c[2] == "supersedes"
               for c in out2["changes"])
    mem.close()


def test_belief_timeline_respects_access():
    mem = MemorySystem()
    u = mem.add_entity("用户", "person")
    t0 = datetime(2026, 1, 1)
    mem.store.insert_belief(u, "公开观点", "positive", 0.8, "chat",
                            t0, None, None, "active", "public", [], t0)
    mem.store.insert_belief(u, "敏感观点", "negative", 0.8, "chat",
                            t0, None, None, "active", "sensitive", [], t0)
    br = BeliefResolver(mem.config)
    out = br.resolve(mem.store, datetime(2026, 6, 1))
    assert {b.proposition for b in out["history"] + out["current"]} == \
        {"公开观点"}
    mem.close()


# ---------------- D-02-T IntentResolver ----------------
def test_intent_resolve():
    mem = MemorySystem()
    u = mem.add_entity("用户", "person")
    t0 = datetime(2026, 1, 1)
    i1 = mem.store.insert_intent(u, "考虑换工作", "active", 0.7, "chat",
                                 t0, None, "active", "public", [], t0)
    i2 = mem.store.insert_intent(u, "准备搬家", "completed", 0.7, "chat",
                                 t0, None, "active", "public", [], t0)
    i3 = mem.store.insert_intent(u, "想学钢琴", "active", 0.7, "chat",
                                 t0, datetime(2026, 3, 1), "active",
                                 "public", [], t0)
    i4 = mem.store.insert_intent(u, "旧计划", "superseded", 0.7, "chat",
                                 t0, None, "active", "public", [], t0)
    ir = IntentResolver(mem.config)
    out = ir.resolve(mem.store, datetime(2026, 6, 1))
    # active + 有效期内 → 当前意图；其余 → 历史
    assert [i.id for i in out["current"]] == [i1]
    assert {i.id for i in out["history"]} == {i2, i3, i4}
    mem.close()
