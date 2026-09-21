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
    # IA-1：候选进入第二遍即合成批级证据（无论最终是否落库成功），
    # 但 belief 本身仍被丢弃
    assert len(ids) == 1 and ids[0] == mem.store.fetch_evidence()[0].id
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
    """0.8.1 IA-1 后：批内无显式 evidence 候选时自动合成批级证据，
    不再留下空绑定（原断言 evidence_ids == [] 已随行为改进更新）。"""
    mem = MemorySystem()
    mem.add_entity("用户", "person")
    mem._write_candidates([
        ExtractedMemory(type="fact", name="用户", key="city", value="北京")])
    evs = mem.store.fetch_evidence()
    assert len(evs) == 1
    key = mem.store.read("SELECT idempotency_key FROM evidence")[0][0]
    assert key.startswith("auto:")
    assert mem.store.fetch_facts()[0].evidence_ids == [evs[0].id]
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


# ---------------- 0.8.1 IA-1：批级证据自动闭环 ----------------
class _NoEvidenceExtractor:
    """模拟不产生 evidence 候选的抽取器（DeepSeek/Fallback 实测现状）。"""

    def extract(self, text, meta=None):
        return [
            ExtractedMemory(type="event", name=text[:20], kind="chat"),
            ExtractedMemory(type="fact", name="用户", key="city", value="上海"),
        ]


def test_write_text_auto_evidence(tmp_path):
    """write_text 无 evidence 候选 → 自动合成批级证据并绑定全部候选。"""
    mem = _mem_with_user(tmp_path)
    meta = {"conversation_id": "conv-1", "message_id": "m-9"}
    res = mem.write_text("用户搬到了上海", meta=meta,
                         extractor=_NoEvidenceExtractor())
    assert "_raw_text" not in meta  # 不污染调用方 dict
    evs = mem.store.fetch_evidence()
    assert len(evs) == 1
    ev = evs[0]
    assert ev.id in res.ids
    assert ev.source_type == "conversation"
    assert ev.source_ref == "用户搬到了上海"
    assert ev.content_hash and ev.conversation_id == "conv-1"
    assert ev.message_id == "m-9"
    fact = mem.store.fetch_facts()[0]
    assert fact.evidence_ids == [ev.id]
    event = [n for n in mem.store.fetch_nodes()
             if n.node_type == "event"][0]
    assert event.evidence_ids == [ev.id]
    # explain() 对这些对象不再弃权 E013
    assert mem.explain(fact.fid, kind="fact")["evidence"]
    assert mem.explain(event.nid, kind="node")["evidence"]
    ops = [r[0] for r in mem.store.read(
        "SELECT op FROM audit_log WHERE op='auto_evidence'")]
    assert ops == ["auto_evidence"]
    mem.close()


def test_write_text_auto_evidence_idempotent(tmp_path):
    """同一文本重放：幂等键命中，不产生重复证据。"""
    mem = _mem_with_user(tmp_path)
    for _ in range(2):
        mem.write_text("用户搬到了上海", extractor=_NoEvidenceExtractor())
    assert len(mem.store.fetch_evidence()) == 1
    mem.close()


def test_explicit_evidence_not_duplicated(tmp_path):
    """批内已有显式 evidence 候选时不重复合成。"""
    mem = _mem_with_user(tmp_path)
    mem._write_candidates(
        [ExtractedMemory(type="evidence", source_type="conversation",
                         source_ref="conv-9"),
         ExtractedMemory(type="fact", name="用户", key="city", value="上海")],
        meta={"_raw_text": "用户搬到了上海"})
    evs = mem.store.fetch_evidence()
    assert len(evs) == 1 and evs[0].source_ref == "conv-9"
    assert not mem.store.read(
        "SELECT 1 FROM audit_log WHERE op='auto_evidence'")
    mem.close()


def test_auto_evidence_source_type_from_meta(tmp_path):
    """meta.source_type 合法时透传（如 inferred），非法时回退 conversation。"""
    mem = _mem_with_user(tmp_path)
    mem.write_text("用户应该住在上海",
                   meta={"source_type": "inferred", "trust_level": 0.4},
                   extractor=_NoEvidenceExtractor())
    ev = mem.store.fetch_evidence()[0]
    assert ev.source_type == "inferred" and ev.trust_level == 0.4
    mem.write_text("用户又提到上海", meta={"source_type": "bogus"},
                   extractor=_NoEvidenceExtractor())
    evs = mem.store.fetch_evidence()
    assert len(evs) == 2 and evs[1].source_type == "conversation"
    mem.close()


def test_write_many_auto_evidence_per_batch(tmp_path):
    """write_many：每批各合成一条证据，raw_text 对应该批文本。"""
    from dnamemory.extract import FallbackExtractor
    mem = MemorySystem(path=str(tmp_path / "m.db"),
                       clock=lambda: datetime(2026, 8, 3, 12, 0))
    mem.write_many(["去了健身房", "晚上加班"], extractor=FallbackExtractor())
    evs = mem.store.fetch_evidence()
    assert len(evs) == 2
    assert {e.source_ref for e in evs} == {"去了健身房", "晚上加班"}
    # 重放不重复
    mem.write_many(["去了健身房", "晚上加班"], extractor=FallbackExtractor())
    assert len(mem.store.fetch_evidence()) == 2
    mem.close()


# ---------------- 0.8.1 IA-1：公共 API evidence_ids 往返 ----------------
def test_add_fact_event_entity_evidence_ids(tmp_path):
    mem = _mem_with_user(tmp_path)
    eid = mem.add_evidence("conversation", source_ref="conv-1")
    fid = mem.add_fact("用户", "city", "上海", evidence_ids=[eid])
    assert mem.store.fetch_facts()[0].evidence_ids == [eid]
    nid = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life",
                        evidence_ids=[eid])
    node = [n for n in mem.store.fetch_nodes() if n.nid == nid][0]
    assert node.evidence_ids == [eid]
    eid2 = mem.add_entity("咖啡", "preference", evidence_ids=[eid])
    node2 = [n for n in mem.store.fetch_nodes() if n.nid == eid2][0]
    assert node2.evidence_ids == [eid]
    # 默认不传保持空绑定
    fid2 = mem.add_fact("用户", "lang", "中文")
    assert [f for f in mem.store.fetch_facts()
            if f.fid == fid2][0].evidence_ids == []
    mem.close()
