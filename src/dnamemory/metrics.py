# -*- coding: utf-8 -*-
"""指标与评测（0.5.0 扩展状态正确性 7 项指标，设计稿 §10/§24）。"""
from __future__ import annotations

import math


def average_precision(ranked_ids, truth, k):
    if not truth:
        return 0.0
    hits = 0
    ap = 0.0
    for i, nid in enumerate(ranked_ids[:k]):
        if nid in truth:
            hits += 1
            ap += hits / (i + 1.0)
    return ap / min(len(truth), k)


def ndcg_at_k(ranked_ids, truth, k):
    dcg = sum(1.0 / math.log2(i + 2)
              for i, nid in enumerate(ranked_ids[:k]) if nid in truth)
    ideal = min(len(truth), k)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal))
    return dcg / idcg if idcg else 0.0


def mrr_at_k(ranked_ids, truth, k):
    for i, nid in enumerate(ranked_ids[:k]):
        if nid in truth:
            return 1.0 / (i + 1.0)
    return 0.0


def evaluate(memory, dataset, k=5, mode="dual"):
    """dataset: list[(RecallQuery, truth:set, label:str)]"""
    agg = {"recall": 0.0, "precision": 0.0, "mrr": 0.0, "map": 0.0,
           "ndcg": 0.0}
    by_label = {}
    n = len(dataset)
    for query, truth, label in dataset:
        ids = [h.node_id for h in memory.recall(
            query, k=k, mode=mode, node_types=("event",))]
        hits = set(ids) & truth
        agg["recall"] += 1.0 if hits else 0.0
        agg["precision"] += len(hits) / k
        agg["mrr"] += mrr_at_k(ids, truth, k)
        agg["map"] += average_precision(ids, truth, k)
        agg["ndcg"] += ndcg_at_k(ids, truth, k)
        bl = by_label.setdefault(label, {"recall": 0.0, "map": 0.0, "n": 0})
        bl["recall"] += 1.0 if hits else 0.0
        bl["map"] += average_precision(ids, truth, k)
        bl["n"] += 1
    for key in agg:
        agg[key] /= n
    for label, bl in by_label.items():
        bl["recall"] /= bl["n"]
        bl["map"] /= bl["n"]
    return agg, by_label


# ---------------- 0.5.0 状态正确性指标 ----------------
def current_fact_accuracy(predicted, truth):
    """当前有效事实识别正确率：预测值集合与真值集合的召回率。"""
    if not truth:
        return 0.0
    return len(set(predicted) & set(truth)) / len(truth)


def temporal_ordering_accuracy(sequence):
    """时间链顺序正确率：sequence 为 [(值, 时间), ...]，相邻对有序占比。"""
    if len(sequence) < 2:
        return 0.0
    ok = sum(1 for (_a, ta), (_b, tb) in zip(sequence, sequence[1:])
             if ta <= tb)
    return ok / (len(sequence) - 1)


def belief_evolution_accuracy(predicted, truth):
    """观点变化链正确率：顺序敏感的公共节点占比（贪心对齐，近似 LCS）。"""
    if not truth:
        return 0.0
    hits, j = 0, 0
    for t in truth:
        while j < len(predicted) and predicted[j] != t:
            j += 1
        if j < len(predicted):
            hits += 1
            j += 1
    return hits / len(truth)


def evidence_grounding_rate(memories):
    """证据支撑率：memories 中带非空 evidence_ids 的占比。"""
    if not memories:
        return 0.0
    return sum(1 for m in memories
               if getattr(m, "evidence_ids", None)) / len(memories)


def conflict_resolution_accuracy(predicted, truth):
    """冲突解析正确率：predicted/truth 为 [(kind, key), ...]。"""
    if not truth:
        return 0.0
    return len(set(predicted) & set(truth)) / len(truth)


def cross_dimension_coherence(judgements):
    """多维一致判定率：judgements 为 [(预测, 真值), ...] 的布尔对。"""
    if not judgements:
        return 0.0
    return sum(1 for p, t in judgements if p == t) / len(judgements)


def abstain_accuracy(judgements):
    """证据不足正确拒答率：judgements 为 [(是否拒答, 是否应拒答), ...]。"""
    if not judgements:
        return 0.0
    return sum(1 for p, t in judgements if p == t) / len(judgements)
