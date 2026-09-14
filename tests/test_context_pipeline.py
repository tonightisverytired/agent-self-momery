# -*- coding: utf-8 -*-
"""0.5.0 阶段 G：候选扩展/证据校验/上下文组装/recall_context 管线测试卡
（G-01-T ~ G-04-T）。"""
from datetime import datetime, timedelta

from dnamemory import MemorySystem, RecallQuery
from dnamemory.context import (ContextBuilder, MemoryContext, expand,
                               validate)
from dnamemory.models import Fact, MemoryState

T_2026 = datetime(2026, 9, 1)


class Clock:
    def __init__(self, t=T_2026):
        self.now = t

    def __call__(self):
        return self.now


def _build_case_memory():
    """设计稿 §8 案例 A-E 共用语料：三版本事实 + 事件 + 三观点 + 意图。"""
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "北京", source="profile", confidence=0.9,
                 valid_at=datetime(2025, 1, 1),
                 invalid_at=datetime(2025, 8, 1))
    mem.add_fact(u, "city", "南京", source="profile", confidence=0.9,
                 valid_at=datetime(2025, 8, 1),
                 invalid_at=datetime(2026, 7, 1))
    f_sh = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                        valid_at=datetime(2026, 7, 1))
    ev = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
    mem.add_edge(ev, u, "discusses", 0.7, 0.8, valid_at=datetime(2026, 7, 1))
    t1, t2, t3 = (datetime(2025, 1, 1), datetime(2026, 1, 1),
                  datetime(2026, 8, 1))
    mem.store.insert_belief(u, "上海工作机会多", "positive", 0.8, "chat",
                            t1, None, None, "active", "public", [], t1)
    mem.store.insert_belief(u, "上海生活成本高", "negative", 0.8, "chat",
                            t2, None, None, "active", "public", [], t2)
    mem.store.insert_belief(u, "工作机会仍多但不适合长居", "neutral", 0.8,
                            "chat", t3, None, None, "active", "public", [],
                            t3)
    mem.store.insert_intent(u, "考虑离开上海", "active", 0.7, "chat",
                            t3, None, "active", "public", [], t3)
    return mem, u, f_sh


# ---------------- G-01-T 候选四类 Expansion ----------------
def test_expansion():
    mem, u, f_sh = _build_case_memory()
    evi = mem.store.insert_evidence(
        "conversation", "conv-9", None, None, datetime(2026, 7, 1),
        "hash1", 5, {}, datetime(2026, 7, 1))
    # 上海 fact 绑定 evidence
    mem.store.read("UPDATE facts SET evidence_ids=? WHERE id=?",
                   (f'[{evi}]', f_sh))
    sh_fact = next(f for f in mem.store.fetch_facts()
                   if f.fid == f_sh)
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    user_node = nodes[u]
    out = expand(mem.store, [sh_fact, user_node], limit=200)
    # FactVersion：北京/南京版本在扩展结果中
    fact_vals = {f.value for f in out if isinstance(f, Fact)}
    assert {"北京", "南京", "上海"} <= fact_vals
    # Entity Expansion：关联事件在扩展结果中
    ev_names = {n.name for n in out
                if getattr(n, "node_type", None) == "event"}
    assert "搬到上海" in ev_names
    # Evidence Expansion：evidence 行在扩展结果中
    assert any(getattr(x, "source_type", None) == "conversation"
               for x in out)
    # 去重：无重复键
    keys = [(type(x).__name__, getattr(x, "nid", getattr(x, "fid",
               getattr(x, "id", None)))) for x in out]
    assert len(keys) == len(set(keys))
    mem.close()


def test_expansion_no_cycle():
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    b1 = mem.store.insert_belief(u, "观点一", "positive", 0.8, "chat",
                                 datetime(2026, 1, 1), None, None, "active",
                                 "public", [], datetime(2026, 1, 1))
    b2 = mem.store.insert_belief(u, "观点二", "negative", 0.8, "chat",
                                 datetime(2026, 2, 1), None, None, "active",
                                 "public", [], datetime(2026, 2, 1))
    # 互为 links（环）
    mem.store.insert_memory_link(b1, b2, "belief", "contradicts", 0.8,
                                 None, None)
    mem.store.insert_memory_link(b2, b1, "belief", "contradicts", 0.8,
                                 None, None)
    b1_obj = next(b for b in mem.store.fetch_beliefs() if b.id == b1)
    out = expand(mem.store, [b1_obj], limit=100)
    assert len(out) <= 100  # 不死循环
    keys = [(type(x).__name__, getattr(x, "id", None)) for x in out]
    assert len(keys) == len(set(keys))
    mem.close()


# ---------------- G-02-T EvidenceValidator ----------------
def test_evidence_validator():
    t0 = datetime(2026, 1, 1)
    f_no_ev = Fact(1, 1, "city", "上海", "chat", 0.9, t0, t0, None, None,
                   False, [])
    f_with_ev = Fact(2, 1, "city", "上海", "chat", 0.9, t0, t0, None, None,
                     False, [1])
    ev_conversation = type("E", (), {"source_type": "conversation"})()
    ev_inferred = type("E", (), {"source_type": "inferred"})()
    # 无证据 → 置信度下调且 grounded=False
    v1 = validate(f_no_ev, [])
    assert v1.grounded is False
    assert v1.confidence < 0.9
    # 有证据 → 置信度不变
    v2 = validate(f_with_ev, [ev_conversation])
    assert v2.grounded is True
    assert v2.confidence == 0.9
    # inferred 来源 → 标记推断
    v3 = validate(f_with_ev, [ev_inferred])
    assert v3.inferred is True
    assert v3.grounded is True


# ---------------- G-03-T ContextBuilder ----------------
def test_context_build():
    t0 = datetime(2026, 1, 1)
    f_sh = Fact(1, 1, "city", "上海", "profile", 0.9,
                datetime(2026, 7, 1), t0, None, None, False, [])
    f_nj = Fact(2, 1, "city", "南京", "profile", 0.9,
                datetime(2025, 8, 1), t0,
                datetime(2026, 7, 1), None, False, [])
    f_bj = Fact(3, 1, "city", "北京", "profile", 0.9,
                datetime(2025, 1, 1), t0,
                datetime(2025, 8, 1), None, False, [])
    state = MemoryState(current_facts=[f_sh],
                        historical_facts=[f_bj, f_nj])
    cb = ContextBuilder(MemorySystem().config)
    ctx = cb.build(state, query_type="current_state")
    assert [f.value for f in ctx.current_state] == ["上海"]
    assert [f.value for f in ctx.historical_changes] == ["北京", "南京"]
    ctx2 = cb.build(state, query_type="history")
    assert [f.value for f in ctx2.historical_changes] == ["北京", "南京"]
    # 超预算裁剪不报错
    ctx3 = cb.build(state, query_type="current_state",
                    budgets={"current_state": 1, "historical_changes": 1,
                             "recent_events": 1, "beliefs": 1, "intents": 1,
                             "temporal_chains": 1, "evidence": 1,
                             "conflicts": 1})
    assert len(ctx3.current_state) == 1
    assert len(ctx3.historical_changes) == 1


# ---------------- H-01-T 错误码 E012-E017 ----------------
def test_error_codes():
    import pytest
    from dnamemory.errors import (CoherenceConflictError,
                                  ContextBuildFailedError,
                                  EvidenceNotFoundError,
                                  MemoryStateConflictError,
                                  TemporalChainInvalidError,
                                  UnsupportedBeliefError)
    assert MemoryStateConflictError("x").code == "E012"
    assert EvidenceNotFoundError("x").code == "E013"
    assert TemporalChainInvalidError("x").code == "E014"
    assert ContextBuildFailedError("x").code == "E015"
    assert UnsupportedBeliefError("x").code == "E016"
    assert CoherenceConflictError("x").code == "E017"
    # E013 在 explain 无证据时抛出
    mem = MemorySystem()
    e = mem.add_event("事件", datetime(2026, 1, 1))
    with pytest.raises(EvidenceNotFoundError):
        mem.explain(e)
    mem.close()


# ---------------- G-04-T recall_context 端到端 ----------------
def test_recall_context_cases():
    mem, u, f_sh = _build_case_memory()
    # 案例 A：当前状态
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"),
                             query_time=T_2026)
    assert ctx.query_type == "current_state"
    assert [f.value for f in ctx.current_state] == ["上海"]
    # 案例 B：历史回放（按时间序）
    ctx = mem.recall_context(RecallQuery(text="我过去几年住过哪些地方"),
                             query_time=T_2026)
    assert ctx.query_type == "history"
    assert [f.value for f in ctx.historical_changes] == ["北京", "南京"]
    # 案例 C：观点变化
    ctx = mem.recall_context(RecallQuery(text="我对上海的看法发生过什么变化"),
                             query_time=T_2026)
    assert ctx.query_type == "change"
    assert [b.proposition for b in ctx.beliefs] == ["工作机会仍多但不适合长居"]
    # 案例 D：多维共存
    ctx = mem.recall_context(RecallQuery(text="我现在的情况"), query_time=T_2026)
    assert [f.value for f in ctx.current_state] == ["上海"]
    assert ctx.beliefs and ctx.intents
    assert ctx.conflicts == []
    # 案例 E：证据不足 abstain（不编造原因）
    ctx = mem.recall_context(RecallQuery(text="我为什么离开南京"),
                             query_time=T_2026)
    assert ctx.query_type == "why_change"
    assert any("证据" in n for n in ctx.notes)
    # MemoryScore 分项独立可读
    assert hasattr(ctx, "score")
    assert ctx.score.retrieval_score >= 0
    # recall() 零变化：MemoryHit 结构不变
    hits = mem.recall(RecallQuery(text="上海"), k=5, mode="triple")
    assert hits and isinstance(hits[0].score, float)
    assert isinstance(hits[0].sources, tuple)
    mem.close()
