# -*- coding: utf-8 -*-
"""0.8.1 IA-4 解释性暴露（核心库）：裁决审计落库（supersede_fact /
conflict_resolve / fact_adjudicate）、explain 版本链+audits、
score_by_item、coherence explanations 进 notes。"""
from datetime import datetime, timedelta

from dnamemory import MemorySystem, RecallQuery
from dnamemory.resolve import FactResolver, adjudication_reason

T0 = datetime(2026, 9, 1)


def _mem():
    return MemorySystem(clock=lambda: T0)


def _bind_evidence(mem, fid):
    evid = mem.store.insert_evidence(
        "conversation", "原文", "conv-1", "m-1", T0, "hash-1", 5,
        {"k": "v"}, T0)
    with mem.store.transaction() as conn:
        conn.execute("UPDATE facts SET evidence_ids=? WHERE id=?",
                     (f"[{evid}]", fid))
    return evid


# ---------------- 裁决审计落库 ----------------
def test_supersede_fact_audit_minimal_reason():
    """store 层直接 supersede：调用方未给 reason → 最小描述。"""
    mem = _mem()
    u = mem.add_entity("用户", "person")
    f1 = mem.add_fact(u, "city", "南京", source="chat", confidence=0.8,
                      valid_at=T0)
    f2 = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                      valid_at=T0)
    mem.store.supersede_fact(f1, f2, T0)
    rows = mem.store.fetch_audit_logs(op="supersede_fact")
    assert len(rows) == 1
    r = rows[0]
    assert r["target_type"] == "fact" and r["target_id"] == f1
    assert r["reason"] == f"superseded_by={f2}"
    assert r["meta"]["superseded_by"] == f2
    mem.close()


def test_resolve_conflicts_writes_audit():
    """resolved 分支：supersede_fact + conflict_resolve 双审计，含裁决依据。"""
    mem = _mem()
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=T0)
    mem.add_fact(u, "city", "慕尼黑", source="chat", confidence=0.6,
                 valid_at=T0)
    ds = mem.resolve_conflicts()
    assert ds[0].kind == "resolved"
    sup = mem.store.fetch_audit_logs(op="supersede_fact")
    assert len(sup) == 1
    assert sup[0]["target_id"] == ds[0].loser.fid
    assert "source_rank: profile(4)>chat(2)" in sup[0]["reason"]
    cr = mem.store.fetch_audit_logs(op="conflict_resolve")
    assert len(cr) == 1
    assert cr[0]["meta"]["kind"] == "resolved"
    assert cr[0]["meta"]["winner"] == ds[0].winner.fid
    assert cr[0]["meta"]["loser"] == ds[0].loser.fid
    assert "source_rank" in cr[0]["reason"]
    mem.close()


def test_resolve_conflicts_confirm_audit_and_confirm_reason():
    """confirm 分支：待确认也写 conflict_resolve；人工确认后 supersede
    审计带人工依据。"""
    mem = _mem()
    u = mem.add_entity("口味", "preference")
    mem.add_fact(u, "taste", "辣的", source="chat", confidence=0.9,
                 valid_at=T0)
    mem.add_fact(u, "taste", "甜的", source="chat", confidence=0.9,
                 valid_at=T0)
    ds = mem.resolve_conflicts()
    assert ds[0].kind == "confirm"
    cr = mem.store.fetch_audit_logs(op="conflict_resolve")
    assert len(cr) == 1 and cr[0]["meta"]["kind"] == "confirm"
    # 待人工确认阶段不产生 supersede
    assert not mem.store.fetch_audit_logs(op="supersede_fact")
    mem.confirm(ds[0], "辣的")
    sup = mem.store.fetch_audit_logs(op="supersede_fact")
    assert len(sup) == 1
    assert "人工确认" in sup[0]["reason"]
    assert sup[0]["meta"]["superseded_by"] == ds[0].winner.fid
    mem.close()


def test_fact_resolver_audit_opt_in():
    """fact_adjudicate：opt-in 才写（读取路径零开销），含裁决依据字符串。"""
    mem = _mem()
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "南京", source="chat", confidence=0.8,
                 valid_at=T0 - timedelta(days=30))
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=T0)
    fr = FactResolver(mem.config)
    # 默认（recall_context 读取路径口径）：裁决照常但不写审计
    fr.resolve(mem.store, T0)
    assert not mem.store.fetch_audit_logs(op="fact_adjudicate")
    # opt-in：多值角逐出胜者 → 写 fact_adjudicate（outcome=winner）
    fr.resolve(mem.store, T0, audit=True)
    rows = mem.store.fetch_audit_logs(op="fact_adjudicate")
    assert len(rows) == 1
    assert rows[0]["meta"]["outcome"] == "winner"
    assert "source_rank" in rows[0]["reason"]
    mem.close()

    # 冲突裁决（同源同高置信并列）→ outcome=conflict
    mem2 = _mem()
    u2 = mem2.add_entity("口味", "preference")
    mem2.add_fact(u2, "taste", "辣的", source="chat", confidence=0.9,
                  valid_at=T0)
    mem2.add_fact(u2, "taste", "甜的", source="chat", confidence=0.9,
                  valid_at=T0)
    FactResolver(mem2.config).resolve(mem2.store, T0, audit=True)
    rows2 = mem2.store.fetch_audit_logs(op="fact_adjudicate")
    assert len(rows2) == 1
    assert rows2[0]["meta"]["outcome"] == "conflict"
    assert len(rows2[0]["meta"]["contenders"]) == 2
    mem2.close()


def test_adjudication_reason_format():
    mem = _mem()
    u = mem.add_entity("用户", "person")
    top = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                       valid_at=T0)
    second = mem.add_fact(u, "city", "南京", source="chat", confidence=0.7,
                          valid_at=T0)
    facts = {f.fid: f for f in mem.store.fetch_facts()}
    reason = adjudication_reason(facts[top], facts[second], mem.config)
    assert "source_rank: profile(4)>chat(2)" in reason
    assert "confidence 0.9>0.7" in reason
    mem.close()


# ---------------- explain() 整合 ----------------
def test_explain_fact_version_chain_and_audits():
    """kind=fact：versions 为同 (node, key) 全版本时间链；audits 挂接。"""
    mem = _mem()
    u = mem.add_entity("用户", "person")
    f1 = mem.add_fact(u, "city", "南京", source="chat", confidence=0.8,
                      valid_at=T0 - timedelta(days=30))
    f2 = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                      valid_at=T0)
    _bind_evidence(mem, f2)
    mem.store.insert_memory_link(f1, f2, "fact", "supersedes", 0.9, T0, None)
    ex = mem.explain(f2, kind="fact")
    # 版本链：同 (node, canonical key) 全部版本，按 (valid_at, fid) 升序
    assert [v.fid for v in ex["versions"]] == [f1, f2]
    # related 带 confidence
    assert ex["related"] and ex["related"][0]["confidence"] == 0.9
    # 尚无该对象审计
    assert ex["audits"] == []
    # 裁决后：败方 f1 的 explain 能看到 supersede 审计
    _bind_evidence(mem, f1)
    mem.resolve_conflicts()
    ex1 = mem.explain(f1, kind="fact")
    ops = [a["op"] for a in ex1["audits"]]
    assert "supersede_fact" in ops
    mem.close()


def test_explain_belief_intent_versions():
    """kind=belief/intent：versions 复用对应 Resolver 的演化时间线。"""
    mem = _mem()
    u = mem.add_entity("用户", "person")
    evid = mem.store.insert_evidence("conversation", "c", None, None, T0,
                                     None, 5, {}, T0)
    b1 = mem.store.insert_belief(u, "旧观点", "positive", 0.8, "chat",
                                 T0 - timedelta(days=30), None, None,
                                 "active", "public", [evid],
                                 T0 - timedelta(days=30))
    b2 = mem.store.insert_belief(u, "新观点", "negative", 0.8, "chat",
                                 T0, None, None, "active", "public", [evid],
                                 T0)
    mem.store.insert_memory_link(b1, b2, "belief", "supersedes", 0.9,
                                 T0, None)
    ex = mem.explain(b2, kind="belief")
    assert [b.id for b in ex["versions"]] == [b1, b2]

    i1 = mem.store.insert_intent(u, "旧计划", "completed", 0.7, "chat",
                                 T0 - timedelta(days=30), None, "active",
                                 "public", [evid], T0 - timedelta(days=30))
    i2 = mem.store.insert_intent(u, "新计划", "active", 0.7, "chat",
                                 T0, None, "active", "public", [evid], T0)
    exi = mem.explain(i2, kind="intent")
    assert [i.id for i in exi["versions"]] == [i1, i2]
    mem.close()


# ---------------- score_by_item / explanations 进 notes ----------------
def test_recall_context_score_by_item():
    """recall_context 把单记忆 MemoryScore 填进 ctx.score_by_item
    （key=str(fid)）。"""
    mem = _mem()
    u = mem.add_entity("用户", "person")
    f = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                     valid_at=T0)
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"), query_time=T0)
    assert ctx.current_state and ctx.current_state[0].fid == f
    scores = ctx.score_by_item
    assert scores and str(f) in scores
    assert all(isinstance(k, str) for k in scores)
    sc = scores[str(f)]
    assert sc.validity_score == 1.0 and sc.final() is not None
    mem.close()


def test_coherence_explanations_into_notes():
    """冲突的一致性解释（中文）进 ctx.notes，不再生成后丢弃。"""
    mem = _mem()
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "上海", source="chat", confidence=0.9,
                 valid_at=T0)
    mem.add_fact(u, "city", "北京", source="chat", confidence=0.9,
                 valid_at=T0)
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"), query_time=T0)
    assert ctx.conflicts, "同源同高置信双值应判冲突"
    assert any("并列候选" in n for n in ctx.notes)
    mem.close()


# ---------------- fetch_audit_logs 过滤 ----------------
def test_fetch_audit_logs_filters():
    mem = _mem()
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=T0)
    mem.add_fact(u, "city", "慕尼黑", source="chat", confidence=0.6,
                 valid_at=T0)
    mem.resolve_conflicts()
    all_rows = mem.store.fetch_audit_logs(limit=200)
    assert all_rows, "resolve_conflicts 后应有审计行"
    ids = [r["id"] for r in all_rows]
    assert ids == sorted(ids, reverse=True), "最新在前"
    by_op = mem.store.fetch_audit_logs(op="supersede_fact")
    assert by_op and all(r["op"] == "supersede_fact" for r in by_op)
    loser_id = by_op[0]["target_id"]
    by_target = mem.store.fetch_audit_logs(target_type="fact",
                                           target_id=loser_id)
    assert by_target and all(r["target_id"] == loser_id for r in by_target)
    assert len(mem.store.fetch_audit_logs(limit=1)) == 1
    mem.close()
