# -*- coding: utf-8 -*-
"""0.5.0 阶段 I：状态正确性 7 项指标测试卡（I-01-T/I-03-T）。"""
from dnamemory.metrics import (abstain_accuracy, belief_evolution_accuracy,
                               conflict_resolution_accuracy,
                               cross_dimension_coherence,
                               current_fact_accuracy, evidence_grounding_rate,
                               temporal_ordering_accuracy)


class _M:
    def __init__(self, eids):
        self.evidence_ids = eids


# ---------------- I-01-T 七项指标 ----------------
def test_state_metrics():
    # CurrentFactAccuracy：满分/零分
    assert current_fact_accuracy(["上海"], ["上海"]) == 1.0
    assert current_fact_accuracy([], ["上海"]) == 0.0
    assert current_fact_accuracy(["南京"], ["上海"]) == 0.0
    # TemporalOrderingAccuracy：相邻顺序正确率
    assert temporal_ordering_accuracy([("A", 1), ("B", 2), ("C", 3)]) == 1.0
    assert temporal_ordering_accuracy([("B", 2), ("A", 1)]) == 0.0
    assert temporal_ordering_accuracy([("A", 1), ("B", 2), ("C", 3)]) == 1.0
    # BeliefEvolutionAccuracy：顺序敏感的公共节点占比
    assert belief_evolution_accuracy(["B1", "B2"], ["B1", "B2"]) == 1.0
    assert belief_evolution_accuracy([], ["B1"]) == 0.0
    assert belief_evolution_accuracy(["B1", "B2"], ["B2", "B1"]) == 0.5
    # EvidenceGroundingRate
    assert evidence_grounding_rate([_M([1]), _M([])]) == 0.5
    assert evidence_grounding_rate([_M([1])]) == 1.0
    # ConflictResolutionAccuracy
    assert conflict_resolution_accuracy(
        [("temporal", "city")], [("temporal", "city")]) == 1.0
    assert conflict_resolution_accuracy([], [("hard", "city")]) == 0.0
    # CrossDimensionCoherence
    assert cross_dimension_coherence([(True, True)]) == 1.0
    assert cross_dimension_coherence([(True, False)]) == 0.0
    # AbstainAccuracy
    assert abstain_accuracy([(True, True)]) == 1.0
    assert abstain_accuracy([(False, True)]) == 0.0
    # 空输入返回 0 不报错
    assert current_fact_accuracy([], []) == 0.0
    assert belief_evolution_accuracy([], []) == 0.0
    assert evidence_grounding_rate([]) == 0.0
    assert conflict_resolution_accuracy([], []) == 0.0
    assert cross_dimension_coherence([]) == 0.0
    assert abstain_accuracy([]) == 0.0
    assert temporal_ordering_accuracy([]) == 0.0


# ---------------- I-03-T eval_state.py 对内置数据集全绿 ----------------
def test_eval_state_script(tmp_path):
    import json
    import os
    import sys
    tools_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "tools")
    sys.path.insert(0, tools_dir)
    from eval_state import run  # noqa: E402
    data_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "eval_state.json")
    out_path = str(tmp_path / "state_eval_result.json")
    report = run(data_path, out_path)
    assert report["overall"] >= 0.95
    assert os.path.exists(out_path)
    data = json.load(open(data_path, encoding="utf-8"))
    for ab in ("belief_change", "fact_conflict", "evidence_grounding",
               "cross_dimension", "temporal_chain"):
        assert sum(1 for i in data["items"] if i["ability"] == ab) >= 4
