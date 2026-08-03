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
    assert res.accepted == 1
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
    assert res.accepted == 1
    assert any(t == "fact" and "key" in r for t, r in res.rejected)
    assert len(mem.store.fetch_nodes()) == 1
    mem.close()


def test_find_deepseek_key_prefers_exact_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "exact-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("DEEPSEEK_LEGACY", "legacy-key")
    assert find_deepseek_key() == ("DEEPSEEK_API_KEY", "exact-key")
