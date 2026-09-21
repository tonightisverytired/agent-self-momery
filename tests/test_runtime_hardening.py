# -*- coding: utf-8 -*-
"""0.8.0 核心审计修复回归：静默失败可见化 / 时间类型归一 / 一致性口径 / CLI。

覆盖审计批次 1（P0）与批次 2（P1）修掉的缺陷，每条对应一个真实故障：
- 局部解析失败静默清空维度 → notes + trace.degraded_count
- aware 时间混入链路 → 全链路 TypeError
- coherence 恒不报冲突 → trace.conflict_count 恒 0 与 ctx.conflicts 脱节
- `dnamemory eval` 不传 --db 崩溃（os.path.exists(None)）
- PG 的 insert_* 忽略 conn=（见 tests/test_pg_parity.py，需 DSN）
"""
from datetime import datetime, timedelta

from dnamemory import MemoryConfig, MemorySystem, RecallQuery
from dnamemory.context import ContextBuilder, expand
from dnamemory.extract import _parse_ts
from dnamemory.models import MemoryState, to_naive
from dnamemory.resolve import MemoryStateResolver

T0 = datetime(2026, 3, 1)


def _bind_evidence(mem, fid, evidence_ids):
    """add_fact 不暴露 evidence_ids，测试里直接改行绑定。"""
    import json as _json
    with mem.store.transaction() as conn:
        conn.execute("UPDATE facts SET evidence_ids=? WHERE id=?",
                     (_json.dumps(list(evidence_ids)), fid))


class Clock:
    def __init__(self, t=T0):
        self.now = t

    def __call__(self):
        return self.now


# ---------------- 时间类型归一 ----------------
def test_to_naive_normalizes_aware():
    aware = datetime(2026, 8, 3, 10, 30) - timedelta(hours=0)
    aware = aware.replace(tzinfo=None)
    assert to_naive(aware) == aware
    assert to_naive("2026-08-03T10:30:00+08:00") is not None
    assert to_naive("2026-08-03T10:30:00+08:00").tzinfo is None
    assert to_naive("2026-08-03T10:30:00Z").tzinfo is None
    assert to_naive("") is None and to_naive(None) is None
    assert to_naive("不是时间") is None


def test_extract_parse_ts_returns_naive():
    """带偏移量的 ISO 必须归一成 naive，否则与 datetime.now() 比较即崩。"""
    ts = _parse_ts("2026-08-03T10:30:00+08:00")
    assert ts is not None and ts.tzinfo is None
    assert _parse_ts("2026-08-03T10:30:00Z").tzinfo is None
    assert _parse_ts("2026-08-03T10:30:00").tzinfo is None


def test_aware_row_in_db_does_not_break_pipeline(tmp_path):
    """库里已有带偏移量的历史行时，读路径归一后管线不崩。"""
    db = str(tmp_path / "tz.db")
    mem = MemorySystem(path=db, clock=Clock())
    u = mem.add_entity("用户", "person")
    # 绕过写入层直接落一行 aware 时间（模拟旧数据/外部导入）。
    # 注意必须走 transaction：file 后端的 read() 是独立连接且不提交，
    # 写语句会被静默丢弃（见审计 S3）
    with mem.store.transaction() as conn:
        conn.execute(
            "INSERT INTO facts(node_id,fact_key,fact_value,source,confidence,"
            "valid_at,recorded_at,evidence_ids) VALUES(?,?,?,?,?,?,?,?)",
            (u, "city", "上海", "profile", 0.9,
             "2026-01-01T10:00:00+08:00", "2026-01-01T10:00:00+08:00", "[]"))
    mem.close()
    mem2 = MemorySystem(path=db, clock=Clock())
    facts = mem2.store.fetch_facts()
    assert facts and all(f.valid_at.tzinfo is None for f in facts), \
        "读边界必须把 aware 归一成 naive"
    ctx = mem2.recall_context(RecallQuery(text="我现在住哪里"),
                              query_time=T0)
    assert [f.value for f in ctx.current_state] == ["上海"]
    mem2.close()


# ---------------- 静默失败可见化 ----------------
def test_degraded_dimension_is_visible(monkeypatch):
    """某个维度解析抛错时必须留信号，而不是静默清空。"""
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=T0 - timedelta(days=30))

    def boom(*a, **kw):
        raise RuntimeError("fact 解析爆炸")

    monkeypatch.setattr("dnamemory.resolve.FactResolver.resolve", boom)
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"),
                             query_time=T0)
    assert ctx.trace.degraded_count == 1
    assert any("维度解析失败" in n for n in ctx.notes), ctx.notes
    assert ctx.current_state == []
    mem.close()


def test_null_valid_at_does_not_empty_dimensions(tmp_path):
    """NULL 时间不再让排序抛错（原实现会让整个维度静默清空）。"""
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    iid = mem.store.insert_impact(u, "stress", "increase", "negative", 0.7,
                                  created_at=T0)      # valid_at 默认 None
    mem.store.insert_impact(u, "stress", "decrease", "positive", 0.5,
                            valid_at=T0, created_at=T0)
    state = MemoryStateResolver(mem.store, mem.config).resolve([], T0)
    assert state.degraded == []
    assert state.impacts, "impact 维度不应被 NULL 时间清空"
    assert iid is not None
    mem.close()


# ---------------- 一致性口径 ----------------
def test_coherence_reports_pipeline_conflicts():
    """trace.conflict_count 必须与 ctx.conflicts 同源。

    回归：coherence 原先按 raw key 对 current_facts 分组并要求 len>=2，
    而 FactResolver 结构上保证每组至多一个 current → 该分支永不成立，
    trace 永远报 0 冲突。
    """
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "上海", source="chat", confidence=0.9,
                 valid_at=T0)
    mem.add_fact(u, "city", "南京", source="chat", confidence=0.9,
                 valid_at=T0)
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"),
                             query_time=T0)
    assert len(ctx.conflicts) == 1, "同源同置信并列 → 一个冲突组"
    assert ctx.trace.conflict_count == 1, "trace 必须如实反映冲突数"
    assert ctx.score.conflict_penalty > 0
    mem.close()


def test_conflict_penalty_is_capped():
    """罚分封顶 1.0：与其它分项同量纲，冲突一多也不会把总分压成负数。"""
    from dnamemory.context import MemoryContext
    from dnamemory.models import ConflictGroup
    mem = MemorySystem()
    ctx = MemoryContext()
    ctx.current_state = []
    ctx.conflicts = [ConflictGroup(i, "city", "位置", [i, i + 1],
                                   ["上海", "南京"]) for i in range(12)]
    coh = type("C", (), {"consistent": False, "conflicts": []})()
    s = mem._state_score(ctx, coh, [])
    assert s.conflict_penalty == 1.0, "12 组冲突 → 罚分封顶 1.0"
    assert s.validity_score == 0.0
    assert ConflictGroup(1, "city", "位置", [1, 2], ["上海", "南京"]).kind \
        == "fact_conflict"
    mem.close()


# ---------------- 访问控制（expand） ----------------
def test_expand_respects_access_control():
    """expand 的邻接扩展不得带出 sensitive / 已删除节点。"""
    from dnamemory.models import RecallFilters
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    secret = mem.add_event("秘密会议", T0, kind="meeting",
                           access_label="sensitive")
    mem.add_edge(u, secret, "participates", 0.8, 0.9, valid_at=T0)
    out = expand(mem.store, [mem.store.fetch_nodes()[0]]
                 if False else [n for n in mem.store.fetch_nodes()
                                if n.nid == u])
    ids = {getattr(m, "nid", None) for m in out}
    assert secret not in ids, "sensitive 事件不得经邻接扩展进入候选"
    mem.close()


# ---------------- CLI ----------------
def test_eval_command_without_db(tmp_path, monkeypatch, capsys):
    """`dnamemory eval` 不传 --db 也不得崩（os.path.exists(None) 回归）。"""
    import json
    from dnamemory import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DNAMEMORY_DB", raising=False)
    queries = tmp_path / "q.json"
    queries.write_text(json.dumps({
        "name": "t", "corpus": {
            "entities": [{"name": "用户", "kind": "person"}],
            "events": [{"name": "搬到上海", "ts": "2026-07-01T00:00:00"}],
            "facts": [{"entity": "用户", "key": "city", "value": "上海",
                       "source": "profile", "confidence": 0.9,
                       "valid_at": "2026-07-01T00:00:00"}],
        },
        "items": [{"id": "t1", "ability": "temporal", "query": "我现在住哪里",
                   "expected": {"current_fact": "上海"}}],
    }, ensure_ascii=False), encoding="utf-8")
    args = type("A", (), {"queries": str(queries), "db": None, "dsn": None,
                          "config": None, "k": 5, "mode": "triple",
                          "bge_m3": False})()
    assert cli.cmd_eval(args) == 0
    out = capsys.readouterr().out
    assert "overall" in out


def test_open_memory_uses_env_db(monkeypatch, tmp_path):
    """--db 缺省时走 DNAMEMORY_DB，而不是 None。"""
    from dnamemory import cli
    target = tmp_path / "env.db"
    monkeypatch.setenv("DNAMEMORY_DB", str(target))
    args = type("A", (), {"db": None, "dsn": None})()
    assert cli._db_path(args) == str(target)
    mem = cli._open_memory(args)
    assert mem.store.path == str(target)
    mem.close()


# ---------------- 批次 2：治理与写入正确性 ----------------
def test_restore_resets_life():
    """restore 必须重置 life：只改 lifecycle 的话下次 step_day 立刻再归档。"""
    mem = MemorySystem(clock=Clock())
    nid = mem.add_event("要恢复的事件", T0, kind="meeting",
                        life=0.2, decay_rate=0.5)
    mem.step_day()                       # life 0.2-0.5 <= 0 → archived
    node = next(n for n in mem.store.fetch_nodes() if n.nid == nid)
    assert node.lifecycle == "archived"
    mem.restore(nid)
    mem.step_day()                       # 复位后不应立刻再归档
    node = next(n for n in mem.store.fetch_nodes() if n.nid == nid)
    assert node.lifecycle == "active", "restore 后 life 仍为 0 → 被再次归档"
    assert node.life > node.decay_rate
    mem.close()


def test_future_fact_cannot_supersede_current():
    """未来生效的事实参与角逐但不得胜出，否则当前状态会变空。"""
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "上海", source="chat", confidence=0.9,
                 valid_at=T0)
    # 未来才生效、且来源更可信 + 置信更高
    mem.add_fact(u, "city", "慕尼黑", source="user_statement",
                 confidence=0.99, valid_at=T0 + timedelta(days=30))
    mem.resolve_conflicts()
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"), query_time=T0)
    assert [f.value for f in ctx.current_state] == ["上海"], \
        "当前值不得被未来值顶掉（顶掉后该 key 当前状态为空）"
    mem.close()


def test_confirm_rejects_same_value():
    """同值重复行不构成可裁决冲突（否则 /confirm 会自 supersede 抹掉该值）。"""
    from dnamemory import governance
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    for _ in range(2):
        mem.add_fact(u, "city", "上海", source="chat", confidence=0.9,
                     valid_at=T0)
    decisions = mem.resolve_conflicts()
    assert decisions and all(d.kind == "resolved" for d in decisions),         "同值重复是去重（resolved），不得判为 confirm"
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"), query_time=T0)
    assert [f.value for f in ctx.current_state] == ["上海"], "值必须保住"
    # 即便构造出同值决策，confirm 也必须拒绝
    fs = mem.store.fetch_facts()
    d = governance.ConflictDecision("confirm", u, "city", fs[0], fs[1])
    try:
        governance.confirm(mem.store, d, "上海", T0)
        raise AssertionError("同值 confirm 应被拒绝")
    except Exception as e:  # noqa: BLE001
        assert "E008" in str(e)
    mem.close()


def test_validate_discount_is_applied():
    """无证据事实必须落地 0.6 折置信（原实现算完就丢）。"""
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=T0 - timedelta(days=1))
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"), query_time=T0)
    assert ctx.current_state, "事实应在 current_state"
    assert abs(ctx.current_state[0].confidence - 0.54) < 1e-6, \
        "无证据 → 0.9 × 0.6 = 0.54"
    mem.close()


def test_forget_force_cascades_state_rows():
    """合规删除必须级联主体的观点/意图/影响/模式/证据。"""
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    evi = mem.store.insert_evidence("conversation", "conv-1", None, None,
                                    T0, None, 5, {}, T0)
    fid = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                       valid_at=T0)
    _bind_evidence(mem, fid, [evi])
    mem.store.insert_belief(u, "上海成本高", "negative", 0.8, "chat", T0,
                            None, None, "active", "public", [evi], T0)
    mem.store.insert_intent(u, "考虑离开", "active", 0.7, "chat", T0, None,
                            "active", "public", [evi], T0)
    mem.store.insert_impact(u, "stress", "increase", "negative", 0.7,
                            valid_at=T0, created_at=T0, evidence_ids=[evi])
    mem.store.insert_pattern(u, "behavior", "经常熬夜", confidence=0.8,
                             support=3, created_at=T0)
    result = mem.forget(u, reason="gdpr", force=True)
    assert result and result.get("beliefs") == 1
    assert all(b.lifecycle == "tombstoned"
               for b in mem.store.fetch_beliefs() if b.subject_id == u)
    assert not [p for p in mem.store.fetch_patterns() if p.subject_id == u]
    # 解释接口不得再讲出被删除对象
    try:
        mem.explain(u, kind="node")
        raise AssertionError("已删除节点不应可解释")
    except Exception as e:  # noqa: BLE001
        assert "E006" in str(e) or "E013" in str(e)
    mem.close()


def test_explain_hides_sensitive_evidence():
    """explain 的证据路径必须过访问控制（与 recall_context 同口径）。"""
    mem = MemorySystem(clock=Clock())
    u = mem.add_entity("用户", "person")
    evi = mem.store.insert_evidence("conversation", "conv-secret", None, None,
                                    T0, None, 5, {}, T0,
                                    access_label="sensitive")
    fid = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                       valid_at=T0)
    _bind_evidence(mem, fid, [evi])
    out = mem.explain(fid, kind="fact")
    assert out["evidence"] == [], "sensitive 证据不得出现在 explain 结果里"
    mem.close()


def test_resolve_entities_respects_protected_and_reattaches():
    """实体合并：protected 需 force；状态行主体必须重挂、名字索引排除墓碑。"""
    mem = MemorySystem(clock=Clock())
    a = mem.add_entity("项目A", "project")
    b = mem.add_entity("项目A", "project", protected=True)
    mem.store.insert_belief(b, "这个项目很重要", "positive", 0.8, "chat",
                            T0, None, None, "active", "public", [], T0)
    assert mem.resolve_entities() == 0, "protected 实体默认不合并"
    assert mem.resolve_entities(force=True) == 1
    dup = next(n for n in mem.store.fetch_nodes() if n.nid == b)
    assert dup.lifecycle == "tombstoned"
    beliefs = [x for x in mem.store.fetch_beliefs()
               if x.proposition == "这个项目很重要"]
    assert beliefs and beliefs[0].subject_id == a, "状态行主体必须重挂"
    # 名字索引不得指向墓碑：新写入应落到 a 上
    fid = mem.add_fact("项目A", "status", "进行中", source="chat",
                       confidence=0.9, valid_at=T0)
    fact = next(f for f in mem.store.fetch_facts() if f.fid == fid)
    assert fact.node_id == a
    mem.close()


def test_extract_impact_kind_reads_prompt_field():
    """LLM 按提示词把主观/客观写在 `kind`，必须落到 impact_kind。

    回归：模型字段叫 impact_kind，提示词示例写的是 kind → 恒为 None →
    所有影响被静默存成 objective。
    """
    from dnamemory.extract import MemoryCandidate
    c = MemoryCandidate.model_validate({
        "type": "impact", "subject": "用户", "dimension": "stress",
        "direction": "increase", "valence": "negative", "magnitude": 0.7,
        "kind": "subjective", "evaluator": "user"})
    assert c.impact_kind == "subjective"
    assert c.to_extracted().impact_kind == "subjective"
    # 非法值回落 objective
    c2 = MemoryCandidate.model_validate({
        "type": "impact", "subject": "用户", "dimension": "stress",
        "direction": "increase", "valence": "negative", "kind": "瞎写"})
    assert c2.impact_kind == "objective"


def test_extract_rejects_empty_fact_value():
    """空值/非标量 value 必须被拒，不得字符串化后落库。"""
    import pytest
    from dnamemory.extract import MemoryCandidate
    for bad in ("", "   ", [], {}, None):
        with pytest.raises(Exception):
            # 校验期即拒（_check_required: fact 缺少 value）→ 该候选进 rejects，
            # 不会以 "[]"/"False" 这类字符串落库
            MemoryCandidate.model_validate({"type": "fact", "entity": "用户",
                                            "key": "drink", "value": bad})
    c = MemoryCandidate.model_validate({"type": "fact", "entity": "用户",
                                        "key": "drink", "value": "拿铁"})
    assert c.to_extracted().value == "拿铁"
