# -*- coding: utf-8 -*-
"""0.7.0 P1 Impact Memory 存储与写入测试卡（P1-02-T / P1-04-T）。"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from dnamemory import MemorySystem
from dnamemory.errors import ValidationError
from dnamemory.extract import ExtractedMemory

START = datetime(2026, 3, 1)


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


# ---------------- P1-02-T impacts 表存取 ----------------
class TestImpactStore:
    def test_insert_fetch_roundtrip(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        eid = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
        iid = mem.store.insert_impact(
            u, "income", "increase", "positive", 0.7, kind="objective",
            evaluator="user", description="收入提高了",
            cause_event_id=eid, source="chat", confidence=0.8,
            valid_at=datetime(2026, 7, 1),
            evidence_ids=[], created_at=datetime(2026, 7, 1))
        impacts = mem.store.fetch_impacts()
        assert len(impacts) == 1
        i = impacts[0]
        assert i.id == iid and i.subject_id == u
        assert i.dimension == "income" and i.direction == "increase"
        assert i.valence == "positive" and i.magnitude == 0.7
        assert i.kind == "objective" and i.evaluator == "user"
        assert i.cause_event_id == eid
        mem.close()

    def test_idempotency_key(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        args = dict(dimension="income", direction="increase",
                    valence="positive", magnitude=0.7,
                    created_at=datetime(2026, 7, 1))
        i1 = mem.store.insert_impact(u, idempotency_key="imp-1", **args)
        i2 = mem.store.insert_impact(u, idempotency_key="imp-1", **args)
        assert i1 == i2
        assert len(mem.store.fetch_impacts()) == 1
        mem.close()

    def test_enum_rejected(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        base = dict(created_at=datetime(2026, 7, 1))
        with pytest.raises(ValidationError):
            mem.store.insert_impact(u, "income", "up", "positive", 0.7,
                                    **base)
        with pytest.raises(ValidationError):
            mem.store.insert_impact(u, "income", "increase", "good", 0.7,
                                    **base)
        with pytest.raises(ValidationError):
            mem.store.insert_impact(u, "income", "increase", "positive", 0.7,
                                    kind="magic", **base)
        mem.close()

    def test_legacy_db_migration(self, tmp_path):
        """0.6 旧库（无 impacts 表）打开后自动补表，原数据完好。"""
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        # 构造 0.6 时代的 nodes 表（含索引依赖的 ts/lifecycle 列）
        conn.executescript(
            "CREATE TABLE nodes ("
            " id INTEGER PRIMARY KEY,"
            " node_type TEXT NOT NULL,"
            " kind TEXT NOT NULL,"
            " name TEXT NOT NULL,"
            " description TEXT NOT NULL DEFAULT '',"
            " ts TEXT, value_score REAL NOT NULL,"
            " protected INTEGER NOT NULL DEFAULT 0,"
            " access_label TEXT NOT NULL DEFAULT 'public',"
            " lifecycle TEXT NOT NULL DEFAULT 'active',"
            " life REAL NOT NULL, decay_rate REAL NOT NULL DEFAULT 0,"
            " last_access TEXT, created_at TEXT NOT NULL,"
            " idempotency_key TEXT UNIQUE,"
            " evidence_ids TEXT NOT NULL DEFAULT '[]',"
            " source TEXT NOT NULL DEFAULT '');"
            "INSERT INTO nodes(id,node_type,kind,name,value_score,life,"
            " created_at) VALUES(1,'entity','person','旧节点',0.7,100.0,"
            " '2026-01-01');")
        conn.commit()
        conn.close()
        mem = MemorySystem(path=str(db), clock=Clock())
        tables = {r[0] for r in mem.store.read(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "impacts" in tables
        assert mem.store.read("SELECT name FROM nodes") == [("旧节点",)]
        mem.close()


# ---------------- P1-04-T add_impact 门面与写入分支 ----------------
class TestImpactWritePath:
    def test_add_impact_facade(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        ev = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
        iid = mem.add_impact(u, "income", "increase", "positive", 0.7,
                             kind="objective", evaluator="user",
                             cause_event=ev, confidence=0.8,
                             valid_at=datetime(2026, 7, 1))
        impacts = mem.store.fetch_impacts()
        assert len(impacts) == 1
        assert impacts[0].id == iid and impacts[0].cause_event_id == ev
        mem.close()

    def test_write_candidates_impact(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        ev = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
        cands = [
            ExtractedMemory(type="evidence", source_type="conversation",
                            source_ref="conv-1"),
            ExtractedMemory(type="impact", name="用户", dimension="income",
                            direction="increase", valence="positive",
                            magnitude=0.7, impact_kind="objective",
                            evaluator="user", cause="搬到上海",
                            confidence=0.8),
        ]
        ids, dropped = mem._write_candidates(
            cands, {"recorded_at": datetime(2026, 7, 1)})
        assert len(ids) == 2 and not dropped
        impacts = mem.store.fetch_impacts()
        assert len(impacts) == 1
        assert impacts[0].evidence_ids, "impact 应绑定本批 evidence"
        assert impacts[0].cause_event_id == ev
        mem.close()

    def test_impact_subject_fuzzy(self):
        mem = MemorySystem(clock=Clock())
        mem.add_entity("用户小号", "person")
        cands = [ExtractedMemory(
            type="impact", name="用户小号2", dimension="stress",
            direction="increase", valence="negative", magnitude=0.6)]
        ids, dropped = mem._write_candidates(
            cands, {"recorded_at": datetime(2026, 7, 1)})
        assert len(ids) == 2  # impact + 批级自动证据（IA-1）
        ops = [r[0] for r in mem.store.read(
            "SELECT op FROM audit_log ORDER BY id")]
        assert "endpoint_fuzzy_match" in ops
        mem.close()

    def test_impact_cause_unresolved_kept(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        cands = [ExtractedMemory(
            type="impact", name="用户", dimension="income",
            direction="increase", valence="positive", magnitude=0.5,
            cause="不存在的事件")]
        ids, dropped = mem._write_candidates(
            cands, {"recorded_at": datetime(2026, 7, 1)})
        assert len(ids) == 2 and not dropped  # impact + 批级自动证据（IA-1）
        impact = mem.store.fetch_impacts()[0]
        assert impact.cause_event_id is None
        ops = [r[0] for r in mem.store.read(
            "SELECT op FROM audit_log ORDER BY id")]
        assert "impact_cause_unresolved" in ops
        mem.close()

    def test_impact_bad_candidate_dropped_isolated(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        cands = [
            ExtractedMemory(type="impact", name="用户", dimension="income",
                            direction="increase", valence="positive",
                            magnitude=1.7),   # 越界 magnitude → 丢弃
            ExtractedMemory(type="event", name="有效事件"),
        ]
        ids, dropped = mem._write_candidates(
            cands, {"recorded_at": datetime(2026, 7, 1)})
        # 事件 + 批级自动证据（IA-1）落库，越界 impact 丢弃
        assert len(ids) == 2 and len(dropped) == 1
        assert mem.store.fetch_impacts() == []
        mem.close()
