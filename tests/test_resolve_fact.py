# -*- coding: utf-8 -*-
"""0.5.0 阶段 C：QueryRouter + FactResolver + MemoryStateResolver 测试卡
（C-01-T ~ C-05-T）。"""
from datetime import datetime, timedelta

from dnamemory import MemorySystem, RecallFilters
from dnamemory.resolve import FactResolver, MemoryStateResolver, query_router

START = datetime(2026, 3, 1)


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


# ---------------- C-01-T QueryRouter ----------------
def test_query_router():
    assert query_router("我现在住哪里？") == "current_state"
    assert query_router("我以前住哪里？") == "history"
    assert query_router("我什么时候搬到上海的？") == "timeline"
    assert query_router("我为什么开始考虑离开上海？") == "why_change"
    assert query_router("今天天气如何") == "semantic_recall"


# ---------------- C-02-T FactResolver 访问控制 ----------------
def test_fact_resolver_access():
    t0 = datetime(2026, 1, 1)
    mem = MemorySystem()
    pub = mem.add_entity("用户", "person")
    sen = mem.add_entity("健康档案", "preference", access_label="sensitive")
    mem.add_fact(pub, "city", "上海", source="profile", confidence=0.9,
                 valid_at=t0)
    mem.add_fact(sen, "condition", "高血压", source="profile", confidence=0.9,
                 valid_at=t0)
    fr = FactResolver(mem.config)
    out = fr.resolve(mem.store, START, filters=RecallFilters())
    vals = {f.value for f in out["current"]}
    assert "上海" in vals and "高血压" not in vals
    out2 = fr.resolve(mem.store, START,
                      filters=RecallFilters(access_labels=("sensitive",)))
    vals2 = {f.value for f in out2["current"]}
    assert "高血压" in vals2
    # tombstoned 实体的事实任何条件下不可见
    mem.forget("用户", "retracted")
    out3 = fr.resolve(mem.store, START)
    assert "上海" not in {f.value for f in out3["current"]}
    mem.close()
    # archived 仅 include_archived=True 可见
    mem2 = MemorySystem()
    e = mem2.add_entity("旧项目", "project")
    mem2.add_fact(e, "status", "进行中", source="chat", confidence=0.9,
                  valid_at=t0)
    mem2.store.update_lifecycle(e, "archived", START)
    fr2 = FactResolver(mem2.config)
    assert fr2.resolve(mem2.store, START)["current"] == []
    out4 = fr2.resolve(mem2.store, START,
                       filters=RecallFilters(include_archived=True))
    assert [f.value for f in out4["current"]] == ["进行中"]
    mem2.close()


# ---------------- C-03-T 时间有效集 ----------------
def test_fact_validity_window():
    mem = MemorySystem()
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "北京", source="profile", confidence=0.9,
                 valid_at=datetime(2025, 1, 1),
                 invalid_at=datetime(2025, 8, 1))
    mem.add_fact(u, "city", "南京", source="profile", confidence=0.9,
                 valid_at=datetime(2025, 8, 1),
                 invalid_at=datetime(2026, 7, 1))
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 7, 1))
    fr = FactResolver(mem.config)
    out = fr.resolve(mem.store, datetime(2026, 9, 1))
    assert [f.value for f in out["current"]] == ["上海"]
    assert {f.value for f in out["history"]} == {"北京", "南京"}
    out = fr.resolve(mem.store, datetime(2025, 5, 1))
    assert [f.value for f in out["current"]] == ["北京"]
    out = fr.resolve(mem.store, datetime(2026, 1, 1))
    assert [f.value for f in out["current"]] == ["南京"]
    mem.close()


# ---------------- C-04-T 裁决链 ----------------
def test_fact_resolve_chain():
    # 场景1：写入端固化 superseded_by（低 rank 版本 invalid）→ current=高 rank
    clock = Clock(datetime(2026, 3, 1))
    mem = MemorySystem(clock=clock)
    u = mem.add_entity("用户", "person")
    t0 = datetime(2026, 1, 1)
    mem.add_fact(u, "city", "南京", source="chat", confidence=0.6,
                 valid_at=t0)
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=t0)
    mem.resolve_conflicts()
    fr = FactResolver(mem.config)
    out = fr.resolve(mem.store, datetime(2026, 6, 1))
    assert [f.value for f in out["current"]] == ["上海"]
    assert "南京" in {f.value for f in out["history"]}
    mem.close()
    # 场景2：同 source_rank 同高置信 → conflict
    mem2 = MemorySystem(clock=Clock(datetime(2026, 3, 1)))
    u2 = mem2.add_entity("用户2", "person")
    mem2.add_fact(u2, "口味", "辣的", source="chat", confidence=0.9,
                  valid_at=t0)
    mem2.add_fact(u2, "口味", "甜的", source="chat", confidence=0.9,
                  valid_at=t0)
    fr2 = FactResolver(mem2.config)
    out2 = fr2.resolve(mem2.store, datetime(2026, 6, 1))
    # 0.8.0：冲突以 ConflictGroup 为单位上报（组结构不可丢）
    assert out2["current"] == []
    assert len(out2["conflict"]) == 1
    grp = out2["conflict"][0]
    assert set(grp.values) == {"辣的", "甜的"}
    assert len(grp.fact_ids) == 2
    assert grp.node_id == u2 and grp.kind == "fact_conflict"
    mem2.close()
    # 场景3：source_rank 不同 → 高 rank 者 current（即使 confidence 更低）
    mem3 = MemorySystem()
    u3 = mem3.add_entity("用户3", "person")
    mem3.add_fact(u3, "城市", "广州", source="chat", confidence=0.9,
                  valid_at=t0)
    mem3.add_fact(u3, "城市", "深圳", source="profile", confidence=0.5,
                  valid_at=t0)
    fr3 = FactResolver(mem3.config)
    out3 = fr3.resolve(mem3.store, datetime(2026, 6, 1))
    assert [f.value for f in out3["current"]] == ["深圳"]
    mem3.close()
    # 场景4：同 rank 不同 confidence（低于 confirm 阈值）→ 高 confidence 者 current
    mem4 = MemorySystem()
    u4 = mem4.add_entity("用户4", "person")
    mem4.add_fact(u4, "爱好", "跑步", source="chat", confidence=0.6,
                  valid_at=t0)
    mem4.add_fact(u4, "爱好", "游泳", source="chat", confidence=0.9,
                  valid_at=t0)
    fr4 = FactResolver(mem4.config)
    out4 = fr4.resolve(mem4.store, datetime(2026, 6, 1))
    assert [f.value for f in out4["current"]] == ["游泳"]
    mem4.close()


# ---------------- J-02-T 裁决顺序对齐 §9.3 ----------------
def test_resolve_order_align():
    # 五档裁决：user_statement 胜 imported_memory 胜 chat 胜 inferred
    mem = MemorySystem()
    u = mem.add_entity("用户", "person")
    t0 = datetime(2026, 1, 1)
    mem.add_fact(u, "k1", "用户陈述", source="user_statement",
                 confidence=0.5, valid_at=t0)
    mem.add_fact(u, "k2", "导入记忆", source="imported_memory",
                 confidence=0.9, valid_at=t0)
    mem.add_fact(u, "k3", "对话提取", source="chat",
                 confidence=0.9, valid_at=t0)
    mem.add_fact(u, "k4", "推断", source="inferred",
                 confidence=0.9, valid_at=t0)
    fr = FactResolver(mem.config)
    out = fr.resolve(mem.store, datetime(2026, 6, 1))
    cur = {f.value for f in out["current"]}
    assert cur == {"用户陈述", "导入记忆", "对话提取", "推断"}
    # 同 key 不同来源：高 rank 胜（即使 confidence 更低）
    mem2 = MemorySystem()
    u2 = mem2.add_entity("用户2", "person")
    mem2.add_fact(u2, "城市", "来自导入", source="imported_memory",
                  confidence=0.9, valid_at=t0)
    mem2.add_fact(u2, "城市", "来自陈述", source="user_statement",
                  confidence=0.5, valid_at=t0)
    fr2 = FactResolver(mem2.config)
    out2 = fr2.resolve(mem2.store, datetime(2026, 6, 1))
    assert [f.value for f in out2["current"]] == ["来自陈述"]
    mem.close()
    mem2.close()


# ---------------- N-01-T explicit_confirmation ----------------
def test_explicit_confirmation():
    mem = MemorySystem()
    u = mem.add_entity("用户", "person")
    t0 = datetime(2026, 1, 1)
    f1 = mem.add_fact(u, "城市", "未确认", source="chat", confidence=0.9,
                      valid_at=t0)
    f2 = mem.add_fact(u, "城市", "已确认", source="chat", confidence=0.9,
                      valid_at=t0, explicit_confirmation=True)
    # 同 rank 同 confidence：confirmed 胜
    fr = FactResolver(mem.config)
    out = fr.resolve(mem.store, datetime(2026, 6, 1))
    assert [f.value for f in out["current"]] == ["已确认"]
    # 门面参数回读一致
    facts = {f.fid: f for f in mem.store.fetch_facts()}
    assert facts[f1].explicit_confirmation is False
    assert facts[f2].explicit_confirmation is True
    mem.close()
    # 迁移幂等：旧库补列
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL,"
        " fact_key TEXT NOT NULL, fact_value TEXT NOT NULL,"
        " source TEXT NOT NULL, confidence REAL NOT NULL,"
        " valid_at TEXT NOT NULL, recorded_at TEXT NOT NULL,"
        " invalid_at TEXT, superseded_by INTEGER,"
        " tombstoned INTEGER NOT NULL DEFAULT 0, idempotency_key TEXT UNIQUE,"
        " evidence_ids TEXT NOT NULL DEFAULT '[]')")
    conn.commit()
    conn.close()


# ---------------- C-05-T MemoryStateResolver 编排 ----------------
def test_memory_state_resolve():
    mem = MemorySystem(clock=Clock(datetime(2026, 6, 1)))
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "南京", source="chat", confidence=0.6,
                 valid_at=datetime(2026, 1, 1),
                 invalid_at=datetime(2026, 5, 1))
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 5, 1))
    mem.add_event("搬到上海", datetime(2026, 5, 1), kind="life")
    facts = mem.store.fetch_facts()
    event_nodes = [n for n in mem.store.fetch_nodes()
                   if n.node_type == "event"]
    resolver = MemoryStateResolver(mem.store, mem.config)
    state = resolver.resolve(facts + event_nodes,
                             query_time=datetime(2026, 6, 1))
    assert [f.value for f in state.current_facts] == ["上海"]
    assert "南京" in {f.value for f in state.historical_facts}
    assert {e.name for e in state.events} == {"搬到上海"}
    # belief 维度局部失败不阻断 fact/event
    class BadResolver:
        def resolve(self, *a, **k):
            raise RuntimeError("boom")
    resolver.belief_resolver = BadResolver()
    state = resolver.resolve(facts + event_nodes,
                             query_time=datetime(2026, 6, 1))
    assert [f.value for f in state.current_facts] == ["上海"]
    assert {e.name for e in state.events} == {"搬到上海"}
    mem.close()


# ---------------- S0-01-T 同义 key 归一化 ----------------
class TestKeyAliasNormalization:
    def _mem(self):
        mem = MemorySystem(clock=Clock())
        u = mem.add_entity("用户", "person")
        return mem, u

    def test_alias_same_group_conflict(self):
        mem, u = self._mem()
        t0 = datetime(2026, 1, 1)
        mem.add_fact(u, "位置", "海淀区学院路", source="profile",
                     confidence=0.9, valid_at=t0)
        mem.add_fact(u, "location", "深圳", source="profile",
                     confidence=0.9, valid_at=t0)
        fr = FactResolver(mem.config)
        out = fr.resolve(mem.store, START)
        # 同族同高置信：进入冲突态，不得两个 current 并存；
        # 且两个别名必须在**同一组**里（回归：曾被按原始 key 拆成单值组）
        assert out["conflict"], "同义 key 应同组裁决"
        assert not out["current"]
        assert len(out["conflict"]) == 1
        assert sorted(out["conflict"][0].values) == \
            sorted(["海淀区学院路", "深圳"])
        mem.close()

    def test_alias_no_single_value_group(self):
        """单值绝不构成冲突组（回归：曾被扁平截断/重复分组造成假冲突）。"""
        mem, u = self._mem()
        t0 = datetime(2026, 1, 1)
        mem.add_fact(u, "所在地", "上海", source="profile", confidence=0.9,
                     valid_at=t0)
        mem.add_fact(u, "location", "苏州", source="profile",
                     confidence=0.9, valid_at=t0)
        mem.add_fact(u, "phone_brand", "华为", source="chat",
                     confidence=0.9, valid_at=t0)
        fr = FactResolver(mem.config)
        out = fr.resolve(mem.store, START)
        assert all(len(g.values) >= 2 for g in out["conflict"])
        keys = {g.key for g in out["conflict"]}
        # 同义 key 只出一组（展示 key 取组内第一条的原始 key）
        assert len(keys & {"所在地", "location"}) == 1
        assert "phone_brand" not in keys, "单值 key 不得进冲突"
        mem.close()

    def test_alias_single_winner(self):
        mem, u = self._mem()
        t0 = datetime(2026, 1, 1)
        mem.add_fact(u, "location", "深圳", source="profile",
                     confidence=0.5, valid_at=t0)
        mem.add_fact(u, "位置", "海淀区学院路", source="profile",
                     confidence=0.9, valid_at=t0)
        fr = FactResolver(mem.config)
        out = fr.resolve(mem.store, START)
        assert len(out["current"]) == 1
        assert out["current"][0].value == "海淀区学院路"
        assert [f.value for f in out["history"]] == ["深圳"]
        mem.close()

    def test_unknown_key_identity(self):
        # 无族映射的 key（偏好）分组行为与 0.6.0 一致
        mem, u = self._mem()
        t0 = datetime(2026, 1, 1)
        mem.add_entity("口味", "preference")
        mem.add_fact("口味", "偏好", "辣的", source="chat", confidence=0.9,
                     valid_at=t0)
        mem.add_fact("口味", "偏好", "甜的", source="chat", confidence=0.9,
                     valid_at=t0)
        fr = FactResolver(mem.config)
        out = fr.resolve(mem.store, START)
        assert out["conflict"] and not out["current"]
        mem.close()

    def test_fact_lookup_alias(self):
        mem, u = self._mem()
        t0 = datetime(2026, 1, 1)
        mem.add_fact(u, "位置", "海淀区学院路", source="profile",
                     confidence=0.9, valid_at=t0)
        assert mem.fact_lookup("用户", "location") is not None
        assert mem.fact_lookup("用户", "city") is not None
        mem.close()

    def test_original_key_preserved(self):
        mem, u = self._mem()
        t0 = datetime(2026, 1, 1)
        mem.add_fact(u, "位置", "海淀区学院路", source="profile",
                     confidence=0.9, valid_at=t0)
        mem.add_fact(u, "location", "深圳", source="profile",
                     confidence=0.5, valid_at=t0)
        fr = FactResolver(mem.config)
        fr.resolve(mem.store, START)
        keys = {f.key for f in mem.store.fetch_facts()}
        assert keys == {"位置", "location"}, "裁决不改写原始 key"
        mem.close()


def test_duplicate_value_is_not_conflict():
    """同值重复行不构成冲突：无互斥可裁决，取一条为当前、其余进历史。

    若不拦，两条同值事实都会被判进冲突态而从 current_state 消失，且冲突
    区块里也看不出「谁赢了」。
    """
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    t0 = datetime(2026, 1, 1)
    mem.add_fact(u, "city", "上海", source="chat", confidence=0.9,
                 valid_at=t0)
    mem.add_fact(u, "city", "上海", source="chat", confidence=0.9,
                 valid_at=t0)
    assert len(mem.store.fetch_facts()) == 2, "构造出两条同值事实"
    out = FactResolver(mem.config).resolve(mem.store, datetime(2026, 6, 1))
    assert out["conflict"] == []
    assert [f.value for f in out["current"]] == ["上海"]
    mem.close()
