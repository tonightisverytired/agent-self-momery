# -*- coding: utf-8 -*-
"""时间链构建（0.5.0）：稳定时间排序 + 确定性链拼接（设计稿 §10）。

排序优先级（§10.4）：occurred_at(event.ts) > valid_at > observed_at
> created_at > id tie-break；同时间按 (时间层, id) 稳定排序，
不依赖数据库返回顺序。第一阶段完全确定性，LLM 因果辅助为后续可选。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .models import Belief, Fact, Intent


@dataclass
class ChainNode:
    """链上节点：记忆引用 + 维度 + 排序时间 + 与前一节点的关系。"""

    memory: object
    dimension: str
    at: Optional[datetime]
    relation: Optional[str] = None  # before/changes/supersedes/…（相对前一节点）
    source_id: Optional[int] = None  # 关系来源节点 id（无关系时为 None）


@dataclass
class Chain:
    """有序记忆演化链。"""

    nodes: list = field(default_factory=list)


def _mem_id(m):
    return getattr(m, "nid", getattr(m, "fid", getattr(m, "id", None)))


def _dimension(m):
    t = getattr(m, "node_type", None)
    if t == "event":
        return "event"
    if t == "entity":
        return "entity"
    if isinstance(m, Fact):
        return "fact"
    if isinstance(m, Belief):
        return "belief"
    if isinstance(m, Intent):
        return "intent"
    return "memory"


def _sort_time(m):
    """排序时间：occurred_at(event.ts) > valid_at > observed_at > created_at。"""
    return (getattr(m, "ts", None) or getattr(m, "valid_at", None)
            or getattr(m, "observed_at", None)
            or getattr(m, "created_at", None))


def _sort_key(m):
    return (_sort_time(m) or datetime.min, _mem_id(m) or 0)


def stable_sort(memories):
    """稳定时间排序：多次调用结果逐位一致，不依赖数据库返回顺序。"""
    return sorted(memories, key=_sort_key)


class TemporalChainBuilder:
    """时间排序 → 版本链 → 显式 memory_links → 同实体邻接 → Chain。"""

    def __init__(self, config):
        self.config = config

    def build(self, memories, links=None):
        memories = [m for m in memories if m is not None]
        if not memories:
            return []
        ordered = stable_sort(memories)
        ids = {_mem_id(m): m for m in ordered}
        pos = {mid: i for i, mid in enumerate(ids)}
        # 关系映射：(target_id, source_type) -> [(source_id, relation)]。
        # source_type 标记 target 的维度（跨表 id 空间重叠时必须区分）。
        rel_map = {}
        for l in (links or []):
            rel_map.setdefault((l.target_id, l.source_type), []).append(
                (l.source_id, l.relation))
        # 版本链：superseded_by（old.superseded_by=new → new 相对 old 为 changes）
        for m in memories:
            mid = _mem_id(m)
            sup = getattr(m, "superseded_by", None)
            if mid is not None and sup:
                rel_map.setdefault((sup, "fact"), []).append(
                    (mid, "changes"))
        nodes = []
        for i, m in enumerate(ordered):
            mid = _mem_id(m)
            key = (mid, _dimension(m))
            relation, src = None, None
            for src_id, rel in rel_map.get(key, []):
                if src_id in pos and pos[src_id] < i:
                    relation, src = rel, src_id
                    break
            if relation is None and nodes:
                relation = "before"
            nodes.append(ChainNode(memory=m, dimension=_dimension(m),
                                   at=_sort_time(m), relation=relation,
                                   source_id=src))
        return [Chain(nodes=nodes)]
