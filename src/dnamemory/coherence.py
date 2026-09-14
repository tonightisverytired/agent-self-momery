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
        # 同 (entity, key) 多个 current fact → 冲突（分类 + 解释）
        groups = {}
        for f in memory_state.current_facts:
            groups.setdefault((f.node_id, f.key), []).append(f)
        for (nid, key), fs in groups.items():
            if len(fs) < 2:
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
