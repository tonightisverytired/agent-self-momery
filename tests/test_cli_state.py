# -*- coding: utf-8 -*-
"""0.5.0 阶段 H：memory_history/timeline/explain API + CLI 新命令测试卡
（H-02-T/H-05-T）。"""
from datetime import datetime

import pytest

from dnamemory import MemorySystem
from dnamemory.errors import EvidenceNotFoundError


def _build(tmp_path):
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    u = mem.add_entity("用户", "person")
    t1, t2 = datetime(2025, 1, 1), datetime(2026, 7, 1)
    f1 = mem.add_fact(u, "city", "南京", source="profile", confidence=0.9,
                      valid_at=t1, invalid_at=t2)
    f2 = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                      valid_at=t2)
    ev = mem.add_event("搬到上海", t2, kind="life")
    mem.add_edge(ev, u, "discusses", 0.7, 0.8, valid_at=t2)
    b = mem.store.insert_belief(u, "上海成本高", "negative", 0.8, "chat",
                                t2, None, None, "active", "public", [], t2)
    i = mem.store.insert_intent(u, "考虑离开", "active", 0.7, "chat",
                                t2, None, "active", "public", [], t2)
    return mem, u, f1, f2


# ---------------- H-02-T memory_history/timeline/explain ----------------
def test_memory_api_state(tmp_path):
    mem, u, f1, f2 = _build(tmp_path)
    # fact 维度：按 valid_at 序列
    facts = mem.memory_history(u, dimension="fact")
    assert [f.value for f in facts] == ["南京", "上海"]
    # belief/intent 维度
    assert len(mem.memory_history(u, dimension="belief")) == 1
    assert len(mem.memory_history(u, dimension="intent")) == 1
    # event 维度
    assert [n.name for n in mem.memory_history(u, dimension="event")] == \
        ["搬到上海"]
    # timeline 时间范围过滤
    tl = mem.timeline(entity_id=u, start=datetime(2026, 1, 1),
                      end=datetime(2026, 9, 1))
    assert tl
    for node in tl:
        t = getattr(node.memory, "ts", None) \
            or getattr(node.memory, "valid_at", None)
        assert t is None or datetime(2026, 1, 1) <= t <= datetime(2026, 9, 1)
    # explain：无证据 → E013（kind 消歧：fact id 与 node id 空间重叠）
    with pytest.raises(EvidenceNotFoundError):
        mem.explain(f1, kind="fact")
    # explain：有证据 → 五要素（memory/evidence/versions/source/related）
    evi = mem.store.insert_evidence("conversation", "c1", None, None,
                                    datetime(2026, 7, 1), "h1", 5, {},
                                    datetime(2026, 7, 1))
    with mem.store.transaction() as conn:
        conn.execute("UPDATE facts SET evidence_ids=? WHERE id=?",
                     (f"[{evi}]", f2))
    ex = mem.explain(f2, kind="fact")
    assert ex["memory"].fid == f2
    assert ex["evidence"] and ex["evidence"][0].id == evi
    assert "versions" in ex and "source" in ex and "related" in ex
    mem.close()


# ---------------- H-05-T CLI 4 命令 ----------------
def test_cli_state_commands(tmp_path, capsys):
    mem, u, f1, f2 = _build(tmp_path)
    mem.close()
    from dnamemory.cli import main
    db = str(tmp_path / "m.db")
    rc = main(["history", "--db", db, "--entity", "用户"])
    out = capsys.readouterr().out
    assert rc == 0 and "上海" in out and "南京" in out
    rc = main(["timeline", "--db", db, "--entity", "用户",
               "--start", "2025-01-01", "--end", "2026-09-01"])
    assert rc == 0
    capsys.readouterr()
    rc = main(["context", "--db", db, "我现在住哪里"])
    assert rc == 0
    capsys.readouterr()
    # 参数错误退出码 2
    with pytest.raises(SystemExit) as ei:
        main(["history"])  # 缺 --entity
    assert ei.value.code == 2
