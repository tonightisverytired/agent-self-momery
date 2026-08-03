# -*- coding: utf-8 -*-
"""指标与评测（docs/09 C9）。"""
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
