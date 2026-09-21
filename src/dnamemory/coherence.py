# -*- coding: utf-8 -*-
"""跨维度一致性检查（0.5.0，设计稿 §12）。

验证 Event/Fact/Belief/Intent 能否在同一时间上下文同时成立：
Fact+Belief+Intent 共存合法；同 (entity,key) 两个 current fact → 冲突
（按 ConflictResolver 分类并给出解释）。独立于 Embedding。
"""
from __future__ import annotations

from .models import CoherenceResult
from .resolve import ConflictResolver


class CrossDimensionCoherence:
    def __init__(self, config):
        self.config = config
        self.conflict_resolver = ConflictResolver()

    def check(self, memory_state) -> CoherenceResult:
        conflicts, explanations = [], []
        # 0.8.0：冲突由 FactResolver 以 ConflictGroup 产出（同 (node,
        # canonical_key) 上的并列 contender）。原实现自己按 raw key 对
        # `current_facts` 重新分组并要求 len>=2——而 FactResolver 结构上保证
        # 每个 (node, canonical_key) 至多一个 current，所以那个分支永远不成立：
        # `consistent` 恒 True、`trace.conflict_count` 恒 0、conflict_penalty
        # 恒 0，一致性维度全程是常数。
        for c in (getattr(memory_state, "conflicts", None) or []):
            values = list(getattr(c, "values", []) or [])
            if len(values) < 2:
                continue
            nid = getattr(c, "node_id", None)
            key = getattr(c, "key", None)
            conflicts.append({"kind": getattr(c, "kind", None)
                              or "fact_conflict",
                              "node_id": nid, "key": key,
                              "facts": list(values)})
            explanations.append(
                f"实体 {nid} 的属性 {key} 有 {len(values)} 个并列候选："
                f"{'、'.join(str(v) for v in values)}")
        # 兼容手工构造的 MemoryState（current_facts 里有同 key 多值）：
        # 该状态在管线里不可达，仅为直接构造 state 的调用方保留
        groups = {}
        for f in memory_state.current_facts:
            groups.setdefault((f.node_id, f.key), []).append(f)
        seen = {(c["node_id"], c["key"]) for c in conflicts}
        for (nid, key), fs in groups.items():
            if len(fs) < 2 or (nid, key) in seen:
                continue
            kind, why = self.conflict_resolver.classify(fs, dimension="fact")
            conflicts.append({"kind": kind, "node_id": nid, "key": key,
                              "facts": fs})
            explanations.append(
                f"实体 {nid} 的属性 {key} 存在 {len(fs)} 个当前候选："
                f"{why}")
        consistent = not conflicts
        return CoherenceResult(consistent=consistent, conflicts=conflicts,
                               explanations=explanations)
