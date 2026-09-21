# -*- coding: utf-8 -*-
"""0.7.0 P2 Trigger 存储/生成/关联召回测试卡（P2-02-T/P2-06-T/P2-07-T）。"""
import sqlite3
from datetime import datetime

import pytest

from dnamemory import MemorySystem, RecallQuery
from dnamemory.errors import ValidationError
from dnamemory.extract import ExtractedMemory

START = datetime(2026, 3, 1)


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


# ---------------- P2-02-T triggers 表存取 ----------------
class TestTriggerStore:
    def test_insert_fetch_roundtrip(self):
        mem = MemorySystem(clock=Clock())
        ev = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
        tid = mem.store.insert_trigger(
            ev, "event", "horizon", "未来居住规划", source="llm",
            confidence=0.8, created_at=datetime(2026, 7, 1))
        triggers = mem.store.fetch_triggers()
        assert len(triggers) == 1
        t = triggers[0]
        assert t.id == tid and t.memory_id == ev
        assert t.memory_type == "event" and t.trigger_type == "horizon"
        assert t.text == "未来居住规划" and t.source == "llm"
        mem.close()

    def test_idempotency(self):
        mem = MemorySystem(clock=Clock())
        ev = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
        t1 = mem.store.insert_trigger(ev, "event", "horizon", "未来居住规划",
                                      idempotency_key="trg-1")
        t2 = mem.store.insert_trigger(ev, "event", "horizon", "未来居住规划",
                                      idempotency_key="trg-1")
        assert t1 == t2 and len(mem.store.fetch_triggers()) == 1
        mem.close()

    def test_bad_type_rejected(self):
        mem = MemorySystem(clock=Clock())
        ev = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
        with pytest.raises(ValidationError):
            mem.store.insert_trigger(ev, "event", "magic", "x")
        with pytest.raises(ValidationError):
            mem.store.insert_trigger(ev, "node", "horizon", "x")
        mem.close()

    def test_legacy_db_gets_table(self, tmp_path):
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            "CREATE TABLE nodes ("
            " id INTEGER PRIMARY KEY,"
            " node_type TEXT NOT NULL, kind TEXT NOT NULL,"
            " name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',"
            " ts TEXT, value_score REAL NOT NULL,"
            " protected INTEGER NOT NULL DEFAULT 0,"
            " access_label TEXT NOT NULL DEFAULT 'public',"
            " lifecycle TEXT NOT NULL DEFAULT 'active',"
            " life REAL NOT NULL, decay_rate REAL NOT NULL DEFAULT 0,"
            " last_access TEXT, created_at TEXT NOT NULL,"
            " idempotency_key TEXT UNIQUE,"
            " evidence_ids TEXT NOT NULL DEFAULT '[]',"
            " source TEXT NOT NULL DEFAULT '');")
        conn.commit()
        conn.close()
        mem = MemorySystem(path=str(db), clock=Clock())
        tables = {r[0] for r in mem.store.read(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "triggers" in tables
        mem.close()


# ---------------- P2-06-T Trigger 写入（规则确定性 + LLM 提议） ----------------
class TestTriggerGeneration:
    def test_rule_triggers_from_fact(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        f = mem.add_fact(u, "位置", "上海", source="profile",
                         confidence=0.9, valid_at=datetime(2026, 1, 1))
        trigs = mem.store.fetch_triggers()
        assert trigs, "写入 fact 后应自动生成规则 trigger"
        kinds = {t.trigger_type for t in trigs}
        assert "entity" in kinds
        assert all(t.source == "rule" for t in trigs)
        assert all(t.memory_id == f and t.memory_type == "fact"
                   for t in trigs)
        mem.close()

    def test_llm_trigger_candidate_write(self):
        mem = MemorySystem(clock=Clock())
        ev = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
        cands = [
            ExtractedMemory(type="trigger", name="搬到上海", trigger_type="horizon",
                            trigger_text="未来居住规划", trigger_target_type="event"),
        ]
        ids, dropped = mem._write_candidates(
            cands, {"recorded_at": datetime(2026, 7, 1)})
        assert len(ids) == 2 and not dropped  # trigger + 批级自动证据（IA-1）
        trigs = mem.store.fetch_triggers()
        assert any(t.source == "llm" and t.memory_id == ev
                   and t.trigger_type == "horizon"
                   and t.text == "未来居住规划" for t in trigs)
        mem.close()

    def test_trigger_target_unresolved_dropped(self):
        mem = MemorySystem(clock=Clock())
        cands = [
            ExtractedMemory(type="trigger", name="不存在的事件",
                            trigger_type="horizon", trigger_text="未来居住规划",
                            trigger_target_type="event"),
            ExtractedMemory(type="event", name="有效事件"),
        ]
        ids, dropped = mem._write_candidates(
            cands, {"recorded_at": datetime(2026, 7, 1)})
        # trigger 丢弃、主记忆保留（事件 + 批级自动证据 IA-1）
        assert len(ids) == 2
        assert len(dropped) == 1 and dropped[0][0] == "trigger"
        assert mem.store.fetch_triggers() == []
        mem.close()

    def test_trigger_not_evidence(self):
        """触达路径不是证据也不是事实：不产生 evidence、不进 current_state。"""
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        mem.add_fact(u, "位置", "上海", source="profile", confidence=0.9,
                     valid_at=datetime(2026, 1, 1))
        # 规则 trigger 不写 evidence 表
        assert mem.store.fetch_evidence() == []
        ctx = mem.recall_context(RecallQuery(text="用户 位置"),
                                 query_type="current_state")
        assert ctx.current_state, "fact 正常进入 current_state"
        # trigger 不影响 current_state 内容（只多 trigger 行）
        assert {f.key for f in ctx.current_state} == {"位置"}
        mem.close()


# ---------------- P2-07-T Trigger 命中融合（Associative Recall） ----------------
class TestAssociativeRecall:
    def _mem_with_triggers(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        ev = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
        mem.store.insert_trigger(
            ev, "event", "horizon", "未来居住规划", source="llm",
            confidence=0.8, created_at=datetime(2026, 7, 1))
        mem.store.insert_intent(
            u, "考虑离开上海", "active", 0.7, "chat",
            datetime(2026, 8, 1), None, "active", "public", [],
            datetime(2026, 8, 1))
        # bridge trigger 挂 intent
        mem.store.insert_trigger(
            1, "intent", "bridge", "生活成本", source="llm",
            confidence=0.8, created_at=datetime(2026, 8, 1))
        return mem, ev

    def test_trigger_found_lexically_unrelated(self):
        mem, ev = self._mem_with_triggers()
        # 词面与事件名无重叠，只能经 horizon trigger 找到
        ctx = mem.recall_context(RecallQuery(text="我的未来居住规划是什么"),
                                 query_type="semantic_recall")
        names = {n.name for n in ctx.recent_events}
        assert "搬到上海" in names, "trigger 命中应把关联事件带入候选"
        assert ctx.trace.trigger_count >= 1
        mem.close()

    def test_trigger_candidate_adjudicated(self):
        """trigger 找到的记忆若不可见（archived）被访问控制过滤
        （Trigger 找到，State 判断——裁决层仍执行访问/生命周期）。"""
        mem, ev = self._mem_with_triggers()
        # 事件 archived → 不可见（recall 与 trigger 融合均须过滤）
        with mem.store.transaction() as conn:
            conn.execute(
                "UPDATE nodes SET lifecycle='archived' WHERE id=?", (ev,))
        ctx = mem.recall_context(RecallQuery(text="我的未来居住规划是什么"),
                                 query_type="semantic_recall")
        names = {n.name for n in ctx.recent_events}
        assert "搬到上海" not in names
        mem.close()

    def test_no_triggers_noop(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                     valid_at=datetime(2026, 1, 1))
        ctx = mem.recall_context(RecallQuery(text="我现在住哪里"),
                                 query_type="current_state")
        assert ctx.trace.trigger_count == 0
        mem.close()

    def test_bigram_threshold(self):
        mem, ev = self._mem_with_triggers()
        # 「未来 安排」与「未来居住规划」重叠率低 → 不命中
        ctx = mem.recall_context(RecallQuery(text="未来 安排"),
                                 query_type="semantic_recall")
        assert ctx.trace.trigger_count == 0
        mem.close()
