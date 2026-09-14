# -*- coding: utf-8 -*-
"""0.5.0 阶段 A 状态层数据模型与存储测试（功能卡片 A-01~A-14 测试卡）。"""
import os
import re
import sqlite3
from datetime import datetime, timedelta

import pytest

from dnamemory import MemoryConfig, MemorySystem, __version__
from dnamemory.errors import ValidationError
from dnamemory.models import (Belief, CoherenceResult, Evidence, ExtractedMemory,
                              Intent, MemoryLink, MemoryScore, MemoryState,
                              ResolvedMemory)

START = datetime(2026, 3, 1)


# ---------------- A-01-T 版本 ----------------
def test_version():
    assert __version__ == "0.5.0"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as f:
        text = f.read()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m and m.group(1) == "0.5.0"


# ---------------- A-02-T Belief ----------------
def test_belief_model():
    b = Belief(1, 2, "上海生活成本高", polarity="negative", confidence=0.8)
    assert b.polarity == "negative" and b.subject_id == 2
    with pytest.raises(ValidationError) as ei:
        Belief(1, 2, "x", polarity="neutralx")
    assert "E001" in str(ei.value)
    with pytest.raises(ValidationError) as ei:
        Belief(1, 2, "x", confidence=1.5)
    assert "E002" in str(ei.value)
    with pytest.raises(ValidationError) as ei:
        Belief(1, 2, "x", valid_at=START,
               invalid_at=START - timedelta(days=1))
    assert "E003" in str(ei.value)


# ---------------- A-03-T Intent ----------------
def test_intent_model():
    for st in ("active", "completed", "cancelled", "expired", "superseded"):
        i = Intent(1, 2, "考虑离开上海", status=st)
        assert i.status == st
    with pytest.raises(ValidationError) as ei:
        Intent(1, 2, "x", status="done")
    assert "E001" in str(ei.value)
    with pytest.raises(ValidationError) as ei:
        Intent(1, 2, "x", valid_at=START,
               invalid_at=START - timedelta(days=1))
    assert "E003" in str(ei.value)


# ---------------- A-04-T Evidence ----------------
def test_evidence_model():
    for st in ("user_statement", "conversation", "system_record",
               "external_data", "imported_memory", "inferred"):
        e = Evidence(1, st)
        assert e.source_type == st and e.metadata == {}
    with pytest.raises(ValidationError) as ei:
        Evidence(1, "gossip")
    assert "E001" in str(ei.value)


# ---------------- A-05-T MemoryLink ----------------
def test_memory_link_model():
    for rel in ("before", "after", "overlaps", "changes", "reinforces",
                "contradicts", "supersedes", "caused_by", "summarizes"):
        m = MemoryLink(1, 2, 3, "fact", rel)
        assert m.relation == rel
    with pytest.raises(ValidationError) as ei:
        MemoryLink(1, 2, 3, "fact", "precedes")
    assert "E001" in str(ei.value)


# ---------------- A-06-T 状态层 dataclass ----------------
def test_state_dataclasses():
    ms = MemoryState()
    assert ms.current_facts == [] and ms.historical_facts == [] \
        and ms.events == [] and ms.chains == []
    r = ResolvedMemory(memory=("fact", 1), dimension="fact")
    assert r.dimension == "fact"
    sc = MemoryScore(retrieval_score=0.5, temporal_score=0.3,
                     validity_score=0.2, source_score=0.1,
                     evidence_score=0.05, coherence_score=0.04,
                     conflict_penalty=0.01)
    assert sc.retrieval_score == 0.5 and sc.temporal_score == 0.3
    # 默认权重下 final 恒等于各分项等权求和（§23：final = 各正项和 - penalty）
    assert abs(sc.final() - (0.5 + 0.3 + 0.2 + 0.1 + 0.05 + 0.04 - 0.01)) < 1e-9
    cr = CoherenceResult()
    assert cr.consistent is True and cr.conflicts == []


# ---------------- A-07-T ExtractedMemory 新候选类型 ----------------
def test_extracted_memory_state_types():
    b = ExtractedMemory(type="belief", name="用户", proposition="上海机会多",
                        polarity="positive", source="chat", confidence=0.7)
    i = ExtractedMemory(type="intent", name="用户",
                        proposition="考虑离开上海", status="active")
    e = ExtractedMemory(type="evidence", source_type="conversation",
                        source_ref="msg-1", content_hash="abc", trust_level=5)
    mem = MemorySystem()
    for c in (b, i, e):
        mem._validate_candidate(c)
    mem.close()


# ---------------- A-08-T score_weights ----------------
def test_score_weights():
    cfg = MemoryConfig()
    assert cfg.score_weights == {
        "retrieval": 1.0, "temporal": 1.0, "validity": 1.0,
        "source": 1.0, "evidence": 1.0, "coherence": 1.0,
        "conflict_penalty": 1.0}
    with pytest.raises(ValidationError) as ei:
        MemoryConfig(score_weights={
            "retrieval": -0.1, "temporal": 1.0, "validity": 1.0,
            "source": 1.0, "evidence": 1.0, "coherence": 1.0,
            "conflict_penalty": 1.0})
    assert "E002" in str(ei.value)
    custom = MemoryConfig(score_weights={
        "retrieval": 2.0, "temporal": 1.0, "validity": 1.0,
        "source": 1.0, "evidence": 1.0, "coherence": 1.0,
        "conflict_penalty": 0.5})
    assert custom.score_weights["retrieval"] == 2.0
    assert custom.score_weights["conflict_penalty"] == 0.5


# ---------------- A-09-T beliefs 表 ----------------
def test_belief_store():
    mem = MemorySystem()
    ent = mem.add_entity("用户", "person")
    bid = mem.store.insert_belief(
        ent, "上海生活成本高", "negative", 0.8, "chat", START, None, None,
        "active", "public", [], START)
    rows = mem.store.fetch_beliefs()
    assert len(rows) == 1 and rows[0].id == bid
    assert rows[0].polarity == "negative" and rows[0].proposition == "上海生活成本高"
    bid2 = mem.store.insert_belief(
        ent, "上海工作机会多", "positive", 0.8, "chat", START, None, None,
        "active", "public", [], START, idempotency_key="b1")
    bid3 = mem.store.insert_belief(
        ent, "上海工作机会多", "positive", 0.8, "chat", START, None, None,
        "active", "public", [], START, idempotency_key="b1")
    assert bid3 == bid2
    ops = [r[0] for r in mem.store.read("SELECT op FROM audit_log")]
    assert "write_belief" in ops
    with pytest.raises(ValidationError) as ei:
        mem.store.insert_belief(ent, "x", "badpolarity", 0.8, "chat", START,
                                None, None, "active", "public", [], START)
    assert "E001" in str(ei.value)
    mem.close()


# ---------------- A-10-T intents 表 ----------------
def test_intent_store():
    mem = MemorySystem()
    ent = mem.add_entity("用户", "person")
    ids = []
    for st in ("active", "completed", "cancelled", "expired", "superseded"):
        ids.append(mem.store.insert_intent(
            ent, f"计划{st}", st, 0.7, "chat", START, None,
            "active", "public", [], START))
    rows = mem.store.fetch_intents()
    assert len(rows) == 5
    assert {r.status for r in rows} == {"active", "completed", "cancelled",
                                        "expired", "superseded"}
    ops = [r[0] for r in mem.store.read("SELECT op FROM audit_log")]
    assert ops.count("write_intent") == 5
    mem.close()


# ---------------- A-11-T evidence 表 ----------------
def test_evidence_store():
    mem = MemorySystem()
    e1 = mem.store.insert_evidence(
        "conversation", "conv-1", "c1", "m1", START, "hash1", 5, {},
        START, idempotency_key="e1")
    e2 = mem.store.insert_evidence(
        "user_statement", "stmt-1", None, None, START, None, 4, {"k": "v"},
        START, idempotency_key="e2")
    dup = mem.store.insert_evidence(
        "conversation", "conv-1", "c1", "m1", START, "hash1", 5, {},
        START, idempotency_key="e1")
    assert dup == e1
    rows = mem.store.fetch_evidence()
    assert len(rows) == 2
    got = mem.store.get_evidence([e1, e2])
    assert {g.id for g in got} == {e1, e2}
    assert {g.source_type for g in got} == {"conversation", "user_statement"}
    mem.close()


# ---------------- A-12-T memory_links 表 ----------------
def test_memory_link_store():
    mem = MemorySystem()
    ent = mem.add_entity("用户", "person")
    b1 = mem.store.insert_belief(
        ent, "上海机会多", "positive", 0.8, "chat", START, None, None,
        "active", "public", [], START)
    b2 = mem.store.insert_belief(
        ent, "上海成本高", "negative", 0.8, "chat", START, None, None,
        "active", "public", [], START)
    lid = mem.store.insert_memory_link(b1, b2, "belief", "supersedes", 0.9,
                                       START, None)
    rows = mem.store.fetch_memory_links()
    assert len(rows) == 1 and rows[0].id == lid and rows[0].relation == "supersedes"
    of_b1 = mem.store.memory_links_of(b1)
    of_b2 = mem.store.memory_links_of(b2)
    assert len(of_b1) == 1 and len(of_b2) == 1
    with pytest.raises(ValidationError) as ei:
        mem.store.insert_memory_link(b1, b2, "belief", "precedes", 0.9,
                                     START, None)
    assert "E001" in str(ei.value)
    ops = [r[0] for r in mem.store.read("SELECT op FROM audit_log")]
    assert "write_memory_link" in ops
    mem.close()


# ---------------- A-13-T nodes/facts 加列 ----------------
def test_new_columns():
    mem = MemorySystem()
    cols = {r[1] for r in mem.store.read("PRAGMA table_info(nodes)")}
    assert "evidence_ids" in cols and "source" in cols
    fcols = {r[1] for r in mem.store.read("PRAGMA table_info(facts)")}
    assert "evidence_ids" in fcols
    eid = mem.add_event("事件", START)
    node = next(n for n in mem.store.fetch_nodes() if n.nid == eid)
    assert node.evidence_ids == [] and node.source == ""
    ent = mem.add_entity("用户", "person")
    fid = mem.add_fact(ent, "city", "上海")
    fact = next(f for f in mem.store.fetch_facts() if f.fid == fid)
    assert fact.evidence_ids == []
    mem.close()


# ---------------- A-14-T 向后兼容迁移 ----------------
def test_migration_idempotent(tmp_path):
    path = str(tmp_path / "old.db")
    # 手工建 0.4.0 结构旧库（nodes/facts 无新列，无新表）
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, node_type TEXT NOT NULL,"
        " kind TEXT NOT NULL, name TEXT NOT NULL,"
        " description TEXT NOT NULL DEFAULT '', ts TEXT,"
        " value_score REAL NOT NULL, protected INTEGER NOT NULL DEFAULT 0,"
        " access_label TEXT NOT NULL DEFAULT 'public',"
        " lifecycle TEXT NOT NULL DEFAULT 'active', life REAL NOT NULL,"
        " decay_rate REAL NOT NULL DEFAULT 0, last_access TEXT,"
        " created_at TEXT NOT NULL, idempotency_key TEXT UNIQUE)")
    conn.execute(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL,"
        " fact_key TEXT NOT NULL, fact_value TEXT NOT NULL,"
        " source TEXT NOT NULL, confidence REAL NOT NULL,"
        " valid_at TEXT NOT NULL, recorded_at TEXT NOT NULL,"
        " invalid_at TEXT, superseded_by INTEGER,"
        " tombstoned INTEGER NOT NULL DEFAULT 0, idempotency_key TEXT UNIQUE)")
    conn.execute(
        "INSERT INTO nodes(id,node_type,kind,name,value_score,life,decay_rate,"
        "created_at) VALUES(1,'entity','person','旧实体',0.7,0.0,0.0,"
        "'2026-03-01T00:00:00')")
    conn.commit()
    conn.close()
    # 0.5.0 打开：补列建表，数据完好
    mem = MemorySystem(path=path)
    cols = {r[1] for r in mem.store.read("PRAGMA table_info(nodes)")}
    assert "evidence_ids" in cols and "source" in cols
    fcols = {r[1] for r in mem.store.read("PRAGMA table_info(facts)")}
    assert "evidence_ids" in fcols
    tables = {r[0] for r in mem.store.read(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"beliefs", "intents", "evidence", "memory_links"} <= tables
    nodes = mem.store.fetch_nodes()
    assert nodes and nodes[0].name == "旧实体"
    assert nodes[0].evidence_ids == [] and nodes[0].source == ""
    mem.close()
    # 重复打开 + 再迁移无变化
    mem2 = MemorySystem(path=path)
    nodes2 = mem2.store.fetch_nodes()
    assert [n.nid for n in nodes2] == [n.nid for n in nodes]
    assert len(mem2.store.fetch_facts()) == 0
    mem2.close()
