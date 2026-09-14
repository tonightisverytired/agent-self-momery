# -*- coding: utf-8 -*-
"""0.5.0 阶段 B：Evidence 写入与绑定测试（功能卡片 B-03/B-04 测试卡）。"""
from datetime import datetime

import pytest

from dnamemory import MemorySystem
from dnamemory.models import ExtractedMemory


def _mem_with_user(tmp_path, clock=None):
    mem = MemorySystem(path=str(tmp_path / "m.db"),
                       clock=clock or (lambda: datetime(2026, 8, 3, 12, 0)))
    mem.add_entity("用户", "person")
    return mem


# ---------------- B-03-T：_write_candidates 写 belief/intent/evidence ----------------
def test_write_state_candidates(tmp_path):
    mem = _mem_with_user(tmp_path)
    cands = [
        ExtractedMemory(type="evidence", source_type="conversation",
                        source_ref="conv-9", content_hash="h1"),
        ExtractedMemory(type="fact", name="用户", key="city", value="上海",
                        source="chat", confidence=0.8),
        ExtractedMemory(type="belief", name="用户", proposition="上海成本高",
                        polarity="negative", confidence=0.7),
        ExtractedMemory(type="intent", name="用户", proposition="考虑离开上海",
                        status="active"),
    ]
    ids, dropped = mem._write_candidates(cands)
    assert len(dropped) == 0 and len(ids) == 4
    assert len(mem.store.fetch_beliefs()) == 1
    assert len(mem.store.fetch_intents()) == 1
    assert len(mem.store.fetch_evidence()) == 1
    assert len(mem.store.fetch_facts()) == 1
    mem.close()


def test_write_belief_missing_subject_dropped(tmp_path):
    mem = _mem_with_user(tmp_path)
    ids, dropped = mem._write_candidates([
        ExtractedMemory(type="belief", name="不存在的人", proposition="x",
                        polarity="neutral")])
    assert not ids
    assert dropped and dropped[0][0] == "belief"
    assert len(mem.store.fetch_beliefs()) == 0
    mem.close()


def test_transaction_rollback_on_fk_violation(tmp_path):
    """事务原子性：后置非法插入触发 FK 失败 → 整批回滚。"""
    mem = _mem_with_user(tmp_path)
    now = datetime(2026, 8, 3, 12, 0)
    with pytest.raises(Exception):
        with mem.store.transaction() as conn:
            mem.store.insert_belief(
                mem._name2id["用户"], "合法观点", "neutral", 0.7, "chat",
                now, None, None, "active", "public", [], now, conn=conn)
            mem.store.insert_belief(
                99999, "非法主体", "neutral", 0.7, "chat",
                now, None, None, "active", "public", [], now, conn=conn)
    assert len(mem.store.fetch_beliefs()) == 0  # 整体回滚
    mem.close()


# ---------------- B-04-T：evidence_ids 回填 ----------------
def test_evidence_binding(tmp_path):
    mem = _mem_with_user(tmp_path)
    cands = [
        ExtractedMemory(type="evidence", source_type="conversation",
                        source_ref="conv-9"),
        ExtractedMemory(type="fact", name="用户", key="city", value="上海"),
        ExtractedMemory(type="belief", name="用户", proposition="上海成本高",
                        polarity="negative"),
    ]
    ids, dropped = mem._write_candidates(cands)
    assert len(dropped) == 0
    ev = mem.store.fetch_evidence()[0]
    fact = mem.store.fetch_facts()[0]
    assert fact.evidence_ids == [ev.id]
    belief = mem.store.fetch_beliefs()[0]
    assert belief.evidence_ids == [ev.id]
    mem.close()


def test_no_evidence_keeps_empty_binding():
    mem = MemorySystem()
    mem.add_entity("用户", "person")
    mem._write_candidates([
        ExtractedMemory(type="fact", name="用户", key="city", value="北京")])
    assert mem.store.fetch_facts()[0].evidence_ids == []
    mem.close()


def test_state_write_audit_ops(tmp_path):
    mem = _mem_with_user(tmp_path)
    mem._write_candidates([
        ExtractedMemory(type="evidence", source_type="conversation",
                        source_ref="c1"),
        ExtractedMemory(type="belief", name="用户", proposition="x",
                        polarity="neutral"),
        ExtractedMemory(type="intent", name="用户", proposition="y",
                        status="active"),
    ])
    ops = [r[0] for r in mem.store.read("SELECT op FROM audit_log")]
    for op in ("write_belief", "write_intent", "write_evidence"):
        assert op in ops
    mem.close()
