# -*- coding: utf-8 -*-
"""0.7.0 P3 Personal Memory Model 测试卡（P3-02-T ~ P3-06-T）。"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from dnamemory import MemorySystem, RecallQuery
from dnamemory.errors import ValidationError

START = datetime(2026, 3, 1)


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


# ---------------- P3-02-T patterns 表存取 ----------------
class TestPatternStore:
    def test_insert_fetch_roundtrip(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        pid = mem.store.insert_pattern(
            u, "preference", "偏好低加班工作", confidence=0.7, support=2,
            window_days=60, created_at=datetime(2026, 1, 1))
        rows = mem.store.fetch_patterns()
        assert len(rows) == 1
        p = rows[0]
        assert p.id == pid and p.subject_id == u
        assert p.pattern_type == "preference"
        assert p.proposition == "偏好低加班工作"
        assert p.source == "inferred" and p.support == 2
        mem.close()

    def test_idempotency(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        p1 = mem.store.insert_pattern(u, "preference", "偏好早睡",
                                      created_at=datetime(2026, 1, 1))
        p2 = mem.store.insert_pattern(u, "preference", "偏好早睡",
                                      created_at=datetime(2026, 1, 1))
        assert p1 == p2 and len(mem.store.fetch_patterns()) == 1
        mem.close()

    def test_bad_type_rejected(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        with pytest.raises(ValidationError):
            mem.store.insert_pattern(u, "magic", "x",
                                     created_at=datetime(2026, 1, 1))
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
        assert "patterns" in tables
        mem.close()


# ---------------- P3-03-T extract_patterns 规则引擎 ----------------
class TestExtractPatterns:
    def test_impact_pattern_extracted(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        t0, t1 = datetime(2026, 1, 1), datetime(2026, 2, 1)
        mem.store.insert_impact(u, "stress", "increase", "negative", 0.8,
                                valid_at=t0, created_at=t0)
        mem.store.insert_impact(u, "stress", "increase", "negative", 0.7,
                                valid_at=t1, created_at=t1)
        n = mem.extract_patterns()
        assert n >= 1
        pats = mem.store.fetch_patterns()
        assert any(p.pattern_type == "impact" and p.support == 2
                   and p.source == "inferred"
                   and "stress" in p.proposition for p in pats)
        mem.close()

    def test_temporal_pattern_cooccurrence(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        t0 = datetime(2026, 1, 1)
        # 压力持续 4 周（两次 stress 负向影响），其后 ±14 天出现离职意图
        mem.store.insert_impact(u, "stress", "increase", "negative", 0.8,
                                valid_at=t0, created_at=t0)
        mem.store.insert_impact(u, "stress", "increase", "negative", 0.7,
                                valid_at=t0 + timedelta(days=28),
                                created_at=t0 + timedelta(days=28))
        mem.store.insert_intent(u, "考虑离职", "active", 0.7, "chat",
                                t0 + timedelta(days=35), None, "active",
                                "public", [], t0 + timedelta(days=35))
        n = mem.extract_patterns()
        pats = mem.store.fetch_patterns()
        assert any(p.pattern_type == "temporal"
                   and "离职" in p.proposition and p.source == "inferred"
                   for p in pats)
        mem.close()

    def test_preference_pattern_two_months(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        mem.add_fact(u, "food", "辣的", source="chat", confidence=0.8,
                     valid_at=datetime(2026, 1, 10))
        mem.add_fact(u, "food", "辣的", source="chat", confidence=0.8,
                     valid_at=datetime(2026, 3, 10))
        n = mem.extract_patterns()
        pats = mem.store.fetch_patterns()
        assert any(p.pattern_type == "preference" and p.support == 2
                   and "辣的" in p.proposition for p in pats)
        mem.close()

    def test_below_support_no_pattern(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        mem.store.insert_impact(u, "stress", "increase", "negative", 0.8,
                                valid_at=datetime(2026, 1, 1),
                                created_at=datetime(2026, 1, 1))
        n = mem.extract_patterns()
        pats = mem.store.fetch_patterns()
        assert not any(p.pattern_type == "impact" for p in pats), \
            "support 不足不生成 impact pattern"
        mem.close()

    def test_idempotent_rerun(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        t0, t1 = datetime(2026, 1, 1), datetime(2026, 2, 1)
        mem.store.insert_impact(u, "stress", "increase", "negative", 0.8,
                                valid_at=t0, created_at=t0)
        mem.store.insert_impact(u, "stress", "increase", "negative", 0.7,
                                valid_at=t1, created_at=t1)
        mem.extract_patterns()
        n1 = len(mem.store.fetch_patterns())
        mem.extract_patterns()
        assert len(mem.store.fetch_patterns()) == n1
        mem.close()

    def test_reflect_return_unchanged(self):
        """挂接 reflect_monthly 尾部后：返回值/节点行为不变。"""
        mem = MemorySystem(clock=Clock())
        mem.add_entity("项目A", "project")
        for d in range(5, 25, 5):
            eid = mem.add_event(f"讨论{d}", Clock().now + timedelta(days=d),
                                kind="meeting", value_score=0.6,
                                life=150.0, decay_rate=0.02)
            mem.add_edge(eid, "项目A", "discusses", 0.8, 0.9,
                         valid_at=Clock().now + timedelta(days=d))
        n_nodes = len(mem.store.fetch_nodes())
        s1 = mem.reflect_monthly(2026, 3)
        assert s1 is not None
        # 重复 reflect 幂等
        assert mem.reflect_monthly(2026, 3) == s1
        mem.close()


# ---------------- P3-04-T PatternResolver ----------------
class TestPatternResolver:
    def test_patterns_current_filtered(self):
        from dnamemory.resolve import PatternResolver
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        mem.store.insert_pattern(u, "preference", "偏好早睡", confidence=0.8,
                                 support=2, created_at=datetime(2026, 1, 1))
        mem.store.insert_pattern(u, "preference", "低置信模式", confidence=0.2,
                                 support=1, created_at=datetime(2026, 1, 1))
        out = PatternResolver(mem.config).resolve(mem.store, START)
        assert len(out["current"]) == 1
        assert out["current"][0].proposition == "偏好早睡"
        mem.close()

    def test_pattern_resolver_failure_isolated(self, monkeypatch):
        from dnamemory.resolve import MemoryStateResolver
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                     valid_at=datetime(2026, 1, 1))

        def boom(store, query_time=None, filters=None, entity_scope=None):
            raise RuntimeError("pattern 解析爆炸")

        monkeypatch.setattr(
            "dnamemory.resolve.PatternResolver.resolve", boom)
        resolver = MemoryStateResolver(mem.store, mem.config)
        state = resolver.resolve(mem.store.fetch_nodes(), START,
                                 query_type="current_state")
        assert state.current_facts, "fact 维度不受 pattern 异常影响"
        mem.close()


# ---------------- P3-05-T / P3-06-T MemoryContext patterns 区块与管线 ----------------
def test_patterns_block_and_budget():
    from dnamemory.context import ContextBuilder
    from dnamemory.models import MemoryState
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    for i in range(8):
        mem.store.insert_pattern(u, "preference", f"偏好{i}", support=2,
                                 created_at=datetime(2026, 1, 1))
    state = MemoryState()
    state.patterns = mem.store.fetch_patterns()
    ctx = ContextBuilder(mem.config).build(state)
    assert len(ctx.patterns) == 5, "budget 截断生效"
    mem.close()


def test_recall_context_with_patterns():
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 1, 1))
    mem.store.insert_pattern(u, "preference", "偏好早睡", confidence=0.8,
                             support=2, created_at=datetime(2026, 1, 1))
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"),
                             query_type="current_state")
    assert ctx.patterns, "patterns 应出现在上下文"
    assert ctx.patterns[0].proposition == "偏好早睡"
    assert ctx.trace.pattern_count == 1
    # current_state 不被 pattern 污染（pattern 不是事实）
    assert {f.key for f in ctx.current_state} == {"city"}
    mem.close()


def test_behavior_pattern_per_subject():
    """规则4 按主体生成：support 是该主体自己的事件数。

    回归：原实现对「每个邻接主体 × 每个 kind」都生成一行、support 写全库
    该 kind 的事件数（实测 330 行只有 13 个命题、286 个主体），偏离
    「模式属于主体」的设计（docs/业务与项目的演进.md §17）。
    """
    mem = MemorySystem(clock=Clock())
    a = mem.add_entity("甲", "person")
    b = mem.add_entity("乙", "person")
    for i in range(3):
        ev = mem.add_event(f"甲聊天{i}", datetime(2026, 6, 1 + i), kind="chat")
        mem.add_edge(ev, a, "participates", 0.6, 0.7)
    for i in range(2):                      # 乙不到 3 条 → 不构成行为模式
        ev = mem.add_event(f"乙聊天{i}", datetime(2026, 6, 10 + i), kind="chat")
        mem.add_edge(ev, b, "participates", 0.6, 0.7)
    mem.extract_patterns()
    pats = [p for p in mem.store.fetch_patterns()
            if p.pattern_type == "behavior"]
    assert pats, "甲的事件 ≥3 应生成行为模式"
    assert {p.subject_id for p in pats} == {a}, "只有自己的事件够数的主体才生成"
    assert all(p.support == 3 for p in pats), "support 是主体自己的事件数"
    mem.close()
