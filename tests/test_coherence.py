# -*- coding: utf-8 -*-
"""0.5.0 阶段 F：ConflictResolver + CrossDimensionCoherence 测试卡
（F-01-T/F-02-T）。"""
from datetime import datetime, timedelta

from dnamemory import MemoryConfig
from dnamemory.coherence import CrossDimensionCoherence
from dnamemory.models import Fact, MemoryState
from dnamemory.resolve import ConflictResolver

T0 = datetime(2026, 3, 1)


def _fact(fid, key, value, source="chat", confidence=0.9,
          valid_at=T0):
    return Fact(fid, 1, key, value, source, confidence, valid_at, T0,
                None, None, False)


# ---------------- F-01-T 四类冲突 ----------------
def test_conflict_classify():
    cr = ConflictResolver()
    # 同 (entity,key) 同有效时刻同源互斥 → hard
    f1 = _fact(1, "city", "上海")
    f2 = _fact(2, "city", "南京")
    kind, _ = cr.classify([f1, f2])
    assert kind == "hard"
    # 不同 valid_at 各自成立 → temporal（不算真冲突）
    f3 = _fact(3, "city", "北京", valid_at=T0 - timedelta(days=60))
    kind, _ = cr.classify([f1, f3])
    assert kind == "temporal"
    # 不同来源分歧 → source
    f4 = _fact(4, "city", "广州", source="profile", confidence=0.5)
    kind, _ = cr.classify([f1, f4])
    assert kind == "source"
    # 观点差异 → soft（进 BeliefTimeline 不覆盖）
    kind, _ = cr.classify([f1, f2], dimension="belief")
    assert kind == "soft"


# ---------------- F-02-T 多维一致性 ----------------
def test_cross_dimension():
    cc = CrossDimensionCoherence(MemoryConfig())
    # 案例 D：Fact(住上海)+Belief(成本高)+Intent(考虑离开) → 一致
    f_sh = _fact(1, "city", "上海")
    ms = MemoryState(current_facts=[f_sh], beliefs=[object()],
                     intents=[object()])
    res = cc.check(ms)
    assert res.consistent is True and res.conflicts == []
    # 同 key 两个 current fact → 冲突并带解释
    f_nj = _fact(2, "city", "南京", valid_at=T0 + timedelta(days=30))
    ms2 = MemoryState(current_facts=[f_sh, f_nj])
    res2 = cc.check(ms2)
    assert res2.consistent is False
    assert res2.conflicts and res2.conflicts[0]["kind"] == "temporal"
    assert res2.explanations
    # 同 key 不同来源 → source 分类
    f_gz = _fact(3, "city", "广州", source="profile", confidence=0.5)
    ms3 = MemoryState(current_facts=[f_sh, f_gz])
    res3 = cc.check(ms3)
    assert res3.consistent is False
    assert res3.conflicts[0]["kind"] == "source"
