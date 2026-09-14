# -*- coding: utf-8 -*-
"""上下文构建（0.5.0，设计稿 §8/§11/§13）。

候选四类扩展（Entity/FactVersion/TemporalNeighbor/Evidence）→ 证据校验
（无证据降置信/abstain，inferred 不得伪装为 Fact）→ 按 query_type 组装
MemoryContext。LLM 只能在 MemoryContext 给出的状态与证据范围内组织语言。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import Belief, Fact, Intent


def _mem_key(m):
    return (type(m).__name__,
            getattr(m, "nid", getattr(m, "fid", getattr(m, "id", None))))


@dataclass
class MemoryContext:
    """交给 LLM 的结构化上下文（不再是裸 Top-K）。"""

    query_type: str = "semantic_recall"
    current_state: list = field(default_factory=list)
    recent_events: list = field(default_factory=list)
    historical_changes: list = field(default_factory=list)
    beliefs: list = field(default_factory=list)
    intents: list = field(default_factory=list)
    temporal_chains: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    score: Optional[object] = None  # MemoryScore（仅 recall_context 管线）


def expand(store, candidates, limit=200):
    """候选四类扩展（设计稿 §8.2）：去重、防环、有上限。

    - FactVersion：同 (node,key) 全部历史版本；
    - Entity：同实体其它维度（事实/事件邻接）；
    - TemporalNeighbor：memory_links 邻接（按 source_type 定位对象）；
    - Evidence：evidence_ids → evidence 行。
    """
    seen, out, queue = set(), [], list(candidates)
    nodes = {n.nid: n for n in store.fetch_nodes()}
    facts = store.fetch_facts()
    beliefs = {b.id: b for b in store.fetch_beliefs()}
    intents = {i.id: i for i in store.fetch_intents()}
    links = store.fetch_memory_links()
    ev_by_id = {e.id: e for e in store.fetch_evidence()}
    while queue and len(out) < limit:
        c = queue.pop(0)
        key = _mem_key(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if isinstance(c, Fact):
            # FactVersion Expansion：同 (node,key) 全部版本
            for f in facts:
                if f.node_id == c.node_id and f.key == c.key:
                    queue.append(f)
        elif getattr(c, "node_type", None) == "entity":
            # Entity Expansion：同实体事实 + 关联事件
            for f in facts:
                if f.node_id == c.nid:
                    queue.append(f)
            for e in store.fetch_edges():
                if e.from_id == c.nid and e.to_id in nodes:
                    queue.append(nodes[e.to_id])
                elif e.to_id == c.nid and e.from_id in nodes:
                    queue.append(nodes[e.from_id])
        # TemporalNeighbor Expansion：memory_links 邻接（防环靠 seen）
        mid = getattr(c, "nid", getattr(c, "fid", getattr(c, "id", None)))
        if mid is not None:
            for l in links:
                if l.target_id != mid:
                    continue
                if l.source_type == "belief" and l.source_id in beliefs:
                    queue.append(beliefs[l.source_id])
                elif l.source_type == "fact":
                    for f in facts:
                        if f.fid == l.source_id:
                            queue.append(f)
                elif l.source_type == "intent" and l.source_id in intents:
                    queue.append(intents[l.source_id])
                elif l.source_type == "event" and l.source_id in nodes:
                    queue.append(nodes[l.source_id])
        # Evidence Expansion：evidence_ids → evidence 行
        for eid in (getattr(c, "evidence_ids", None) or []):
            if eid in ev_by_id:
                queue.append(ev_by_id[eid])
    return out


@dataclass
class ValidatedMemory:
    """证据校验后的记忆：置信度调整 + 推断标记。"""

    memory: object
    confidence: float
    grounded: bool
    inferred: bool


def validate(memory, evidence_rows) -> ValidatedMemory:
    """无证据 → 降置信（不得伪装为事实）；inferred → 强制标记推断。"""
    grounded = bool(evidence_rows)
    inferred = any(e.source_type == "inferred" for e in evidence_rows)
    conf = getattr(memory, "confidence", 0.7)
    if not grounded:
        conf = round(conf * 0.6, 4)
    return ValidatedMemory(memory=memory, confidence=conf,
                           grounded=grounded, inferred=inferred)


class ContextBuilder:
    """按 query_type 组装 MemoryContext；分块预算裁剪（§13）。"""

    DEFAULT_BUDGETS = {"current_state": 10, "recent_events": 10,
                       "historical_changes": 20, "beliefs": 10,
                       "intents": 10, "temporal_chains": 5,
                       "evidence": 20, "conflicts": 10}

    def __init__(self, config):
        self.config = config

    def build(self, memory_state, query_type="semantic_recall", budgets=None,
              chains=None, notes=None) -> MemoryContext:
        b = dict(self.DEFAULT_BUDGETS)
        b.update(budgets or {})
        ctx = MemoryContext(query_type=query_type)
        ctx.current_state = memory_state.current_facts[:b["current_state"]]
        ctx.historical_changes = \
            memory_state.historical_facts[:b["historical_changes"]]
        ctx.recent_events = memory_state.events[:b["recent_events"]]
        ctx.beliefs = memory_state.beliefs[:b["beliefs"]]
        ctx.intents = memory_state.intents[:b["intents"]]
        ctx.conflicts = memory_state.conflicts[:b["conflicts"]]
        ctx.evidence = memory_state.evidence[:b["evidence"]]
        ctx.temporal_chains = (chains or [])[:b["temporal_chains"]]
        ctx.notes = list(notes or [])
        return ctx
