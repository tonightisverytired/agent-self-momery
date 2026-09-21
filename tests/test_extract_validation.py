# -*- coding: utf-8 -*-
"""M2.1 抽取校验回归：Pydantic 逐条校验、残缺候选不拖垮整批、
无时间信息不回填当天时间戳。"""
from datetime import datetime

from dnamemory import MemorySystem
from dnamemory.extract import DeepSeekExtractor, find_deepseek_key
from dnamemory.models import ExtractedMemory


class FakeExtractor:
    def __init__(self, batches):
        self.batches = batches

    def extract(self, text, meta=None):
        return self.batches[0]

    def extract_many(self, texts, meta=None, batch_size=20):
        return self.batches


def test_parse_keeps_valid_and_rejects_malformed():
    ex = DeepSeekExtractor(api_key="test-key")
    data = {
        "memories": [
            {"type": "event", "name": "与张总开会", "kind": "meeting",
             "ts": "2026-08-03T10:00:00", "value_score": 0.8},
            {"type": "fact", "name": "咖啡"},
            {"type": "edge", "from": "张总", "to": "项目A",
             "rel": "unknown_rel"},
            {"type": "entity", "name": "张总", "kind": "person",
             "description": "项目负责人"},
            {"type": "wat", "name": "x"},
        ]
    }
    cands, rejects = ex._parse(data)
    assert len(cands) == 2
    assert len(rejects) == 3
    by_type = {c.type: c for c in cands}
    assert by_type["event"].ts == datetime(2026, 8, 3, 10, 0)
    assert by_type["entity"].value == "项目负责人"  # description -> 实体描述
    reasons = " | ".join(f"{t}:{r}" for t, r in rejects)
    assert "fact" in reasons and "缺少 key" in reasons
    assert "edge" in reasons and "unknown_rel" in reasons
    assert "wat" in reasons


def test_write_many_no_ts_does_not_fill_today(tmp_path):
    clock = datetime(2026, 8, 3, 12, 0)
    ex = FakeExtractor([[ExtractedMemory(
        type="event", name="无时间事件", kind="chat", value_score=0.3)]])
    mem = MemorySystem(path=str(tmp_path / "m.db"), clock=lambda: clock)
    res = mem.write_many(["随便一条没有时间的记录"], extractor=ex)
    assert res.accepted == 2  # 事件 + 批级自动证据（IA-1）
    node = mem.store.fetch_nodes()[0]
    assert node.ts is None
    assert node.created_at == clock
    mem.close()


def test_write_many_keeps_valid_items_from_mixed_batch(tmp_path):
    ex = FakeExtractor([[
        ExtractedMemory(type="event", name="有效事件", kind="meeting",
                        ts=datetime(2026, 8, 1), value_score=0.6),
        ExtractedMemory(type="fact", name="咖啡", key="", value="拿铁"),
    ]])
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    res = mem.write_many(["混合批次"], extractor=ex)
    assert res.accepted == 2  # 有效事件 + 批级自动证据（IA-1）
    assert any(t == "fact" and "key" in r for t, r in res.rejected)
    assert len(mem.store.fetch_nodes()) == 1
    mem.close()


def test_find_deepseek_key_prefers_exact_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "exact-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("DEEPSEEK_LEGACY", "legacy-key")
    assert find_deepseek_key() == ("DEEPSEEK_API_KEY", "exact-key")


# ---------------- B-01-T / B-02-T：0.5.0 状态维度候选 ----------------
def test_prompt_has_state_types():
    from dnamemory.extract import SYSTEM_PROMPT
    for kw in ("belief", "intent", "evidence", "polarity", "status",
               "source_type"):
        assert kw in SYSTEM_PROMPT


# ---------------- Q-02-T CausalProposer 协议 ----------------
def test_causal_proposer_protocol():
    from dnamemory.extract import DeepSeekCausalProposer
    # 构造不依赖网络（惰性：不建连接、不查环境变量除非调用 propose）
    p = DeepSeekCausalProposer(api_key="test-key", model="deepseek-v4-flash")
    assert p.model == "deepseek-v4-flash"

    class FakeProposer:
        def propose(self, event, fact_change):
            return [("搬迁工作调动", 0.8), ("家庭原因", 0.6)]

    fp = FakeProposer()
    cands = fp.propose("搬到上海", "city: 南京→上海")
    assert isinstance(cands, list) and isinstance(cands[0], tuple)
    assert cands[0][1] == 0.8


def test_state_candidate_validation():
    ex = DeepSeekExtractor(api_key="test-key")
    data = {
        "memories": [
            {"type": "belief", "proposition": "上海机会多",
             "polarity": "positive", "confidence": 0.8},
            {"type": "belief", "polarity": "positive"},          # 缺 proposition
            {"type": "belief", "proposition": "x",
             "polarity": "angry"},                                # 降级 neutral
            {"type": "intent", "proposition": "考虑离开上海",
             "status": "active"},
            {"type": "intent", "proposition": "x",
             "status": "doing"},                                  # 降级 active
            {"type": "evidence", "source_type": "conversation",
             "source_ref": "c1"},
            {"type": "evidence", "source_type": "gossip"},        # rejects
            {"type": "event", "name": "有效事件", "kind": "meeting"},
        ]
    }
    cands, rejects = ex._parse(data)
    assert len(cands) == 6
    assert len(rejects) == 2
    beliefs = [c for c in cands if c.type == "belief"]
    assert {b.polarity for b in beliefs} == {"positive", "neutral"}
    intents = [c for c in cands if c.type == "intent"]
    assert {i.status for i in intents} == {"active"}
    evs = [c for c in cands if c.type == "evidence"]
    assert evs and evs[0].source_ref == "c1"
    reasons = " | ".join(f"{t}:{r}" for t, r in rejects)
    assert "belief" in reasons and "proposition" in reasons
    assert "evidence" in reasons
    # 同批其它合法候选不受影响
    assert any(c.type == "event" and c.name == "有效事件" for c in cands)


# ---------------- P1-03-T impact 候选抽取 ----------------
class TestImpactCandidate:
    def _ex(self):
        return DeepSeekExtractor(api_key="test-key")

    def test_impact_valid_pass(self):
        ex = self._ex()
        data = {"memories": [
            {"type": "impact", "subject": "用户", "dimension": "income",
             "direction": "increase", "valence": "positive",
             "magnitude": 0.7, "kind": "objective", "evaluator": "user",
             "cause": "搬到上海", "description": "收入提高了",
             "confidence": 0.8},
        ]}
        cands, rejects = ex._parse(data)
        assert len(cands) == 1 and not rejects
        c = cands[0]
        assert c.type == "impact" and c.dimension == "income"
        assert c.direction == "increase" and c.valence == "positive"
        assert c.magnitude == 0.7 and c.impact_kind == "objective"
        assert c.evaluator == "user" and c.cause == "搬到上海"
        assert c.description == "收入提高了"

    def test_impact_missing_dimension_rejected(self):
        ex = self._ex()
        data = {"memories": [
            {"type": "impact", "direction": "increase",
             "valence": "positive"},
            {"type": "event", "name": "有效事件"},
        ]}
        cands, rejects = ex._parse(data)
        assert len(cands) == 1 and cands[0].type == "event"
        assert any(t == "impact" for t, _r in rejects)

    def test_impact_bad_direction_rejected(self):
        ex = self._ex()
        data = {"memories": [
            {"type": "impact", "dimension": "income", "direction": "up",
             "valence": "positive"},
        ]}
        cands, rejects = ex._parse(data)
        assert not cands and rejects

    def test_impact_unknown_dimension_kept(self):
        ex = self._ex()
        data = {"memories": [
            {"type": "impact", "dimension": "commute",
             "direction": "decrease", "valence": "negative",
             "magnitude": 0.4},
        ]}
        cands, rejects = ex._parse(data)
        assert len(cands) == 1 and not rejects
        assert cands[0].dimension == "commute"
