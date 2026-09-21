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


# ---------------- K-02-T validate 五项检查 ----------------
def test_evidence_validator_full():
    from dnamemory import MemoryConfig, RecallFilters
    t0 = datetime(2026, 1, 1)
    f = Fact(1, 1, "city", "上海", "chat", 0.9, t0, t0, None, None,
             False, [1])

    def ev(**kw):
        base = {"id": 1, "source_type": "conversation", "source_ref": "c",
                "conversation_id": None, "message_id": None,
                "observed_at": t0 - timedelta(days=1),
                "content_hash": None, "trust_level": 5, "metadata": {},
                "created_at": t0, "access_label": "public"}
        base.update(kw)
        return type("E", (), base)()

    # 全部通过 → confidence 不变
    v = validate(f, [ev()], config=MemoryConfig())
    assert v.grounded and v.accessible and v.trusted and v.consistent \
        and v.timely and v.confidence == 0.9
    # sensitive evidence 默认不可访问 → 视同无证据降置信
    v = validate(f, [ev(access_label="sensitive")], config=MemoryConfig())
    assert v.accessible is False and v.grounded is False
    assert v.confidence < 0.9
    # 显式 sensitive 过滤 → 可访问
    v = validate(f, [ev(access_label="sensitive")],
                 filters=RecallFilters(access_labels=("sensitive",)),
                 config=MemoryConfig())
    assert v.accessible is True
    # trust_level 低于阈值 → 不可信且降置信
    v = validate(f, [ev(trust_level=0)], config=MemoryConfig())
    assert v.trusted is False and v.confidence < 0.9
    # content_hash 不一致 → consistent=False 且降置信
    f_hash = Fact(1, 1, "city", "上海", "chat", 0.9, t0, t0, None, None,
                  False, [1])
    f_hash.content_hash = "abc"
    v = validate(f_hash, [ev(content_hash="def")], config=MemoryConfig())
    assert v.consistent is False and v.confidence < 0.9
    # observed_at 晚于记忆时间 → timely=False 且降置信
    v = validate(f, [ev(observed_at=t0 + timedelta(days=1))],
                 config=MemoryConfig())
    assert v.timely is False and v.confidence < 0.9


# ---------------- K-03-T recall_context 证据可见性 ----------------
def test_recall_context_evidence_filter():
    from dnamemory import RecallFilters
    mem, u, f_sh = _build_case_memory()
    evi = mem.store.insert_evidence("conversation", "conv-9", None, None,
                                    datetime(2026, 7, 1), "h1", 5, {},
                                    datetime(2026, 7, 1),
                                    access_label="sensitive")
    with mem.store.transaction() as conn:
        conn.execute("UPDATE facts SET evidence_ids=? WHERE id=?",
                     (f"[{evi}]", f_sh))
    # 默认过滤：sensitive 证据不进上下文
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"),
                             query_time=T_2026)
    assert not any(e.id == evi for e in ctx.evidence)
    mem.close()


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


# ---------------- M-01-T / M-02-T 分项评分与排序 ----------------
def test_per_memory_score():
    from dnamemory.models import MemoryState
    t0 = datetime(2026, 1, 1)
    f_cur = Fact(1, 1, "city", "上海", "profile", 0.9,
                 datetime(2026, 7, 1), t0, None, None, False, [])
    f_his = Fact(2, 1, "city", "南京", "profile", 0.9,
                 datetime(2025, 8, 1), t0, datetime(2026, 7, 1), None,
                 False, [])
    state = MemoryState(current_facts=[f_cur], historical_facts=[f_his])
    mem = MemorySystem(clock=Clock())
    coh = type("C", (), {"consistent": True, "conflicts": []})()
    s1 = mem._memory_score(f_cur, state, coh, {})
    s2 = mem._memory_score(f_his, state, coh, {})
    assert s1.validity_score == 1.0
    assert s2.validity_score == 0.5
    assert s1.source_score == 0.8  # profile rank 4 / 5
    assert s1.evidence_score == 0.0  # 无证据
    # 涉冲突 → penalty
    f_bad = Fact(3, 2, "口味", "辣的", "chat", 0.9, t0, t0, None, None,
                 False, [])
    state2 = MemoryState(current_facts=[f_bad, f_bad],
                         conflicts=[{"node_id": 2, "key": "口味"}])
    s3 = mem._memory_score(f_bad, state2, coh, {})
    assert s3.conflict_penalty == 1.0
    mem.close()


def test_context_score_order():
    t0 = datetime(2026, 1, 1)
    f_low = Fact(1, 1, "k1", "低分事实", "chat", 0.5, t0, t0, None, None,
                 False, [])
    f_high = Fact(2, 1, "k2", "高分事实", "system_record", 0.9, t0, t0,
                  None, None, False, [])
    f_old = Fact(3, 1, "k3", "旧事实", "profile", 0.9,
                 datetime(2025, 1, 1), t0, datetime(2026, 1, 1), None,
                 False, [])
    state = MemoryState(current_facts=[f_low, f_high],
                        historical_facts=[f_old])
    mem = MemorySystem(clock=Clock())
    cb = ContextBuilder(mem.config)
    # 无 scores 参数 → 原顺序不变（向后兼容）
    ctx0 = cb.build(state, query_type="current_state")
    assert [f.value for f in ctx0.current_state] == ["低分事实", "高分事实"]
    # 带 scores → current_state 按 final 降序，历史保持时间序
    from dnamemory.models import MemoryScore
    coh = type("C", (), {"consistent": True, "conflicts": []})()
    scores = {("Fact", 1): MemoryScore(validity_score=1.0, source_score=0.4),
              ("Fact", 2): MemoryScore(validity_score=1.0, source_score=1.0),
              ("Fact", 3): MemoryScore(validity_score=0.5, source_score=0.8)}
    ctx = cb.build(state, query_type="current_state", scores=scores)
    assert [f.value for f in ctx.current_state] == ["高分事实", "低分事实"]
    assert [f.value for f in ctx.historical_changes] == ["旧事实"]
    mem.close()


# ---------------- Q-03-T why_change 因果链 ----------------
def test_why_change_cause():
    from dnamemory.temporal import derive_causes
    mem, u, f_sh = _build_case_memory()
    # 无 caused_by 链 → abstain（案例 E 既有行为不回归）
    ctx = mem.recall_context(RecallQuery(text="我为什么离开南京"),
                             query_time=T_2026)
    assert ctx.causes == []
    assert any("证据" in n for n in ctx.notes)
    # 补规则推导的 caused_by 链 → causes 非空且不再 abstain
    derive_causes(mem.store, window_days=60)
    ctx2 = mem.recall_context(RecallQuery(text="我为什么离开南京"),
                              query_time=T_2026)
    assert ctx2.causes
    assert not any("证据" in n for n in ctx2.notes)
    mem.close()


# ---------------- O-01-T RecallTrace ----------------
def test_recall_trace():
    from dnamemory.context import MemoryContext
    # 无管线时 trace 默认 None
    assert MemoryContext().trace is None
    mem, u, f_sh = _build_case_memory()
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"),
                             query_time=T_2026)
    tr = ctx.trace
    assert tr is not None
    assert tr.query_type == "current_state"
    assert tr.retrieval_mode == "triple"
    assert tr.candidate_count >= 0
    assert tr.resolved_count >= 1
    assert tr.current_state_count == 1
    # current_state 查询默认不带历史（G-04 语义）
    assert tr.history_count == 0
    assert tr.belief_count == 1
    assert tr.chain_count >= 1
    assert tr.conflict_count == 0
    assert tr.llm_used is False and tr.fallback_used is False
    assert tr.final_context_count >= 1
    # history 查询自动携带历史
    ctx2 = mem.recall_context(RecallQuery(text="我过去几年住过哪些地方"),
                              query_time=T_2026)
    assert ctx2.trace.history_count == 2  # 北京/南京
    mem.close()


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


# ---------------- S0-02-T current_state 查询相关性排序 ----------------
class TestCurrentStateRelevanceRanking:
    def test_relevant_fact_ranked_first(self):
        mem = MemorySystem(clock=Clock())
        c = mem.add_entity("公司", "organization")
        t0 = datetime(2026, 1, 1)
        # 先写无关事实（fid 更小；同 source 同置信使其余分项持平，
        # 无相关性时状态事实排前）
        mem.add_fact(c, "状态", "关门", source="profile", confidence=0.9,
                     valid_at=t0)
        mem.add_fact(c, "位置", "海淀区学院路", source="profile",
                     confidence=0.9, valid_at=t0)
        ctx = mem.recall_context(RecallQuery(text="公司在哪"),
                                 query_type="current_state")
        assert ctx.current_state, "current_state 应含两个事实"
        assert ctx.current_state[0].key == "位置", \
            "相关性应让位置事实置顶，而非无关的状态事实"
        assert ctx.current_state[0].value == "海淀区学院路"
        mem.close()

    def test_no_query_relevance_falls_back(self):
        mem, u, _ = _build_case_memory()
        ctx1 = mem.recall_context(RecallQuery(text=None),
                                  query_type="current_state")
        keys1 = [f.fid for f in ctx1.current_state]
        # 无查询文本 → hits 为空 → 相关性全 0 → 0.6.0 排序不变
        assert keys1, "案例 A current_state 应非空"
        mem.close()

    def test_history_order_unchanged(self):
        mem, u, _ = _build_case_memory()
        ctx = mem.recall_context(RecallQuery(text="我以前住在哪里"),
                                 query_type="history")
        hist = ctx.historical_changes
        assert [f.value for f in hist] == ["北京", "南京"], \
            "historical_changes 保持时间确定性序"
        mem.close()


# ---------------- P1-06-T MemoryContext impacts 区块 ----------------
class TestImpactContext:
    def test_impacts_block_filled_and_budget(self):
        mem, u, _ = _build_case_memory()
        t0 = datetime(2026, 7, 1)
        for d in range(10):
            mem.store.insert_impact(
                u, f"dim{d}", "increase", "positive", 0.6,
                valid_at=t0, created_at=t0)
        state = MemoryState()
        state.current_facts = mem.store.fetch_facts()
        state.impacts = mem.store.fetch_impacts()
        ctx = ContextBuilder(mem.config).build(state)
        assert len(ctx.impacts) == 8, "budget 截断生效"
        mem.close()

    def test_expand_impact_cause_event(self):
        mem, u, _ = _build_case_memory()
        ev = mem.add_event("换工作", datetime(2026, 7, 1), kind="life")
        iid = mem.store.insert_impact(
            u, "income", "increase", "positive", 0.7,
            cause_event_id=ev, valid_at=datetime(2026, 7, 1),
            created_at=datetime(2026, 7, 1))
        impact = mem.store.fetch_impacts()[0]
        out = expand(mem.store, [impact], limit=50)
        names = {n.name for n in out
                 if getattr(n, "node_type", None) == "event"}
        assert "换工作" in names, "impact 扩展应带出关联事件"
        mem.close()

    def test_memory_context_defaults_backward_compat(self):
        ctx = MemoryContext()
        assert ctx.impacts == []
        mem = MemorySystem()
        mem.close()


# ---------------- P1-07-T recall_context 管线接入 impact ----------------
def test_recall_context_with_impacts():
    mem, u, _ = _build_case_memory()
    t0 = datetime(2026, 7, 1)
    evi = mem.store.insert_evidence("conversation", "conv-i1", None, None,
                                    t0, None, 5, {}, t0)
    mem.store.insert_impact(
        u, "income", "increase", "positive", 0.7,
        cause_event_id=None, confidence=0.8, valid_at=t0, created_at=t0,
        evidence_ids=[evi])
    ctx = mem.recall_context(RecallQuery(text="我现在的情况"),
                             query_type="current_state")
    assert ctx.impacts, "impact 应出现在上下文 impacts 区块"
    assert ctx.impacts[0].dimension == "income"
    assert ctx.trace is not None
    assert ctx.trace.impact_count == 1
    # impact 的证据进入 evidence 区块
    ev_ids = {e.id for e in ctx.evidence}
    assert evi in ev_ids
    mem.close()


# ---------------- P2-05-T recall_context 接入 impact chains ----------------
def test_impact_chains_in_context():
    from dnamemory.temporal import derive_impact_links
    mem, u, _ = _build_case_memory()
    t0 = datetime(2026, 7, 1)
    ev = mem.add_event("换工作", t0, kind="life")
    i1 = mem.store.insert_impact(
        u, "income", "increase", "positive", 0.7, cause_event_id=ev,
        valid_at=t0 + timedelta(days=1), created_at=t0 + timedelta(days=1))
    mem.store.insert_impact(
        u, "stress", "increase", "negative", 0.8, cause_event_id=ev,
        valid_at=t0 + timedelta(days=2), created_at=t0 + timedelta(days=2))
    mem.store.insert_belief(
        u, "压力太大想离开", "negative", 0.8, "chat",
        t0 + timedelta(days=5), None, None, "active", "public", [],
        t0 + timedelta(days=5))
    mem.store.insert_intent(
        u, "考虑离职", "active", 0.7, "chat", t0 + timedelta(days=8),
        None, "active", "public", [], t0 + timedelta(days=8))
    derive_impact_links(mem.store)
    ctx = mem.recall_context(RecallQuery(text="换工作 的后果"),
                             query_type="change")
    assert ctx.impact_chains, "影响链应出现在上下文"
    chain = ctx.impact_chains[0]
    dims = [n.dimension for n in chain.nodes]
    assert dims[0] == "event"
    assert "impact" in dims and "belief" in dims
    assert ctx.trace.impact_chain_count == len(ctx.impact_chains)
    mem.close()


# ---------------- 0.8.0 查询在场地：推理类区块收口 ----------------
def test_scope_unrelated_query_empties_reason_blocks():
    """无关查询：推理类区块为空 + notes 说明。

    回归：检索 0 命中时管线退化成「全库倾倒」——causes/impacts/patterns
    全部与查询无关地灌进上下文（实测 503/6/333 条），任意查询都命中
    14/16 个区块。状态层仍按设计全库裁决（相关性不等于状态）。
    """
    mem, u, f_sh = _build_case_memory()
    t0 = datetime(2026, 7, 1)
    mem.store.insert_impact(u, "income", "increase", "positive", 0.7,
                            valid_at=t0, created_at=t0)
    mem.store.insert_pattern(u, "preference", "偏好早睡", confidence=0.8,
                             support=3, created_at=t0)
    ctx = mem.recall_context(RecallQuery(text="zzz 完全不相干的查询 zzz"),
                             query_time=T_2026)
    assert ctx.causes == [] and ctx.impacts == [] and ctx.patterns == []
    assert ctx.impact_chains == []
    assert any("未命中相关记忆" in n for n in ctx.notes)
    assert ctx.current_state, "状态层不受收口影响（仍全库裁决）"
    # 相关查询照常带出推理类区块，且不带「未命中」提示
    ctx2 = mem.recall_context(RecallQuery(text="我现在住哪里"),
                              query_time=T_2026)
    assert ctx2.impacts and ctx2.patterns
    assert not any("未命中相关记忆" in n for n in ctx2.notes)
    mem.close()


def test_causes_scoped_to_shown_facts_and_budget():
    """causes 只解释上下文里展示的事实，并受预算约束。

    回归：原实现取全库 relation=caused_by 且无上限（实测 503 条）。
    """
    from dnamemory.temporal import derive_causes
    mem, u, f_sh = _build_case_memory()
    for i in range(14):          # 15 条因果 > current_state 预算 10
        e = mem.add_entity(f"主体{i}", "person")
        mem.add_fact(e, "city", f"旧城{i}", source="profile", confidence=0.9,
                     valid_at=datetime(2025, 1, 1),
                     invalid_at=datetime(2026, 7, 1))
        mem.add_fact(e, "city", f"新城{i}", source="profile", confidence=0.9,
                     valid_at=datetime(2026, 7, 1))
        ev = mem.add_event(f"搬迁{i}", datetime(2026, 7, 1), kind="life")
        mem.add_edge(ev, e, "discusses", 0.6, 0.7,
                     valid_at=datetime(2026, 7, 1))
    assert derive_causes(mem.store, window_days=60) == 15
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"),
                             query_time=T_2026)
    budget = ContextBuilder.DEFAULT_BUDGETS["causes"]
    shown = {f.fid for f in ctx.current_state} \
        | {f.fid for f in ctx.historical_changes}
    assert ctx.causes, "相关的因果链仍应展示"
    assert len(ctx.causes) <= budget, "causes 必须受预算约束"
    assert all(c["caused"].fid in shown for c in ctx.causes), \
        "causes 只解释上下文里展示的事实"
    mem.close()


def test_conflicts_budget_counts_groups_not_facts():
    """conflicts 预算按「组」计，不得把一个并列组腰斩成单值条目。"""
    from dnamemory.models import ConflictGroup
    state = MemoryState()
    state.conflicts = [
        ConflictGroup(node_id=1, key="city", canonical_key="位置",
                      fact_ids=[1, 2], values=["上海", "北京"]),
        ConflictGroup(node_id=1, key="口味", canonical_key="口味",
                      fact_ids=[3, 4], values=["辣的", "甜的"]),
    ]
    ctx = ContextBuilder(None).build(state, budgets={"conflicts": 1})
    assert len(ctx.conflicts) == 1
    assert list(ctx.conflicts[0].values) == ["上海", "北京"]
    mem2 = MemorySystem()
    full = ContextBuilder(mem2.config).build(state)
    assert len(full.conflicts) == 2, "默认预算 10 组：两组都在"
    mem2.close()
