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


def derive_causes(store, window_days=7):
    """规则因果推导（0.6.0 P2）：fact 版本切换点 + 同实体时间邻近事件
    → memory_links caused_by（低置信 0.5 确定性候选，LLM 提议可后续升级）。

    返回本次新生成的链接数；幂等（同 (事件, 新事实) 只落一次）。
    """
    from datetime import timedelta
    facts = [f for f in store.fetch_facts() if not f.tombstoned]
    nodes = {n.nid: n for n in store.fetch_nodes()}
    created = 0
    for f in facts:
        t = f.invalid_at
        if t is None:
            continue
        related_events = []
        for e in store.fetch_edges():
            other = None
            if e.from_id == f.node_id:
                other = nodes.get(e.to_id)
            elif e.to_id == f.node_id:
                other = nodes.get(e.from_id)
            if other is not None and other.node_type == "event" \
                    and other.ts is not None:
                related_events.append(other)
        for ev in related_events:
            if abs((ev.ts - t).days) > window_days:
                continue
            newer = next((x for x in facts
                          if x.node_id == f.node_id and x.key == f.key
                          and x.valid_at is not None and x.valid_at >= t
                          and x.fid != f.fid), None)
            if newer is None:
                continue
            key = f"cause:{ev.nid}:{newer.fid}"
            if store.read(
                    "SELECT id FROM memory_links WHERE idempotency_key=?",
                    (key,)):
                continue
            store.insert_memory_link(ev.nid, newer.fid, "event",
                                     "caused_by", 0.5, ev.ts, None,
                                     idempotency_key=key)
            created += 1
    return created


def derive_impact_links(store, window_days=7):
    """规则影响链推导（0.7.0 P2，文档 §8.2）：确定性生成低置信
    memory_links caused_by，幂等（同 (源, 目标) 只落一次）：

    (a) 同主体 stress/压力类 negative impact 后 ±7 天同主体 belief
        → impact→belief caused_by；
    (b) belief 后 ±7 天同主体 intent → belief→intent caused_by；
    (c) 同主体不同维 impact 时序相邻且方向延续 → impact→impact。
    Event→Impact 不落 link（用 impacts.cause_event_id）。
    只新增 memory_links，不修改任何原始记忆（Ground Truth）。
    """
    from datetime import timedelta
    impacts = store.fetch_impacts()
    beliefs = store.fetch_beliefs()
    intents = store.fetch_intents()
    created = 0

    def _time(m):
        return (getattr(m, "valid_at", None)
                or getattr(m, "created_at", None))

    def _close(a, b):
        ta, tb = _time(a), _time(b)
        return ta is not None and tb is not None \
            and abs((tb - ta).days) <= window_days

    def _link(src_id, src_dim, tgt_id, tgt_type, key_prefix, t):
        key = f"{key_prefix}:{src_dim}:{src_id}:{tgt_type}:{tgt_id}"
        if store.read(
                "SELECT id FROM memory_links WHERE idempotency_key=?",
                (key,)):
            return 0
        # source_type 标记 target 维度（项目语义）；source_dim 显式
        # 标记 source 维度（跨表 id 碰撞消歧，0.7.0）。
        store.insert_memory_link(src_id, tgt_id, tgt_type, "caused_by",
                                 0.5, t, None, idempotency_key=key,
                                 source_dim=src_dim)
        return 1

    stress_words = ("stress", "压力")
    # (a) impact → belief：同主体、压力维或负向、时间邻近
    for imp in impacts:
        if not (imp.dimension in stress_words or imp.valence == "negative"):
            continue
        for b in beliefs:
            if b.subject_id != imp.subject_id or b.superseded_by is not None:
                continue
            if _close(imp, b):
                created += _link(imp.id, "impact", b.id, "belief",
                                 "iml:ib", _time(b) or _time(imp))
    # (b) belief → intent：同主体、时间邻近
    for b in beliefs:
        if b.superseded_by is not None:
            continue
        for i in intents:
            if i.subject_id != b.subject_id:
                continue
            if _close(b, i):
                created += _link(b.id, "belief", i.id, "intent",
                                 "iml:bi", _time(i) or _time(b))
    # (c) impact → impact：同主体不同维、方向延续、时序相邻
    for a in impacts:
        for b2 in impacts:
            if a.id >= b2.id or a.subject_id != b2.subject_id:
                continue
            if a.dimension == b2.dimension:
                continue
            if a.direction != b2.direction:
                continue
            ta, tb = _time(a), _time(b2)
            if ta is None or tb is None or not (0 < (tb - ta).days
                                                <= window_days):
                continue
            created += _link(a.id, "impact", b2.id, "impact",
                             "iml:ii", tb)
    return created


class TemporalChainBuilder:
    """时间排序 → 版本链 → 显式 memory_links → edges → Chain。

    edges 参与时间关系推导（设计稿 §10.2 构建输入）：precedes → before、
    causes → caused_by、updates_to → changes（仅两端节点都在链上时生效）。
    """

    EDGE_REL_MAP = {"precedes": "before", "causes": "caused_by",
                    "updates_to": "changes"}

    def __init__(self, config):
        self.config = config

    def build(self, memories, links=None, edges=None):
        memories = [m for m in memories if m is not None]
        if not memories:
            return []
        ordered = stable_sort(memories)
        ids = {_mem_id(m): m for m in ordered}
        pos = {mid: i for i, mid in enumerate(ids)}
        dims = {mid: _dimension(m) for mid, m in ids.items()}
        # 关系映射：(target_id, source_type) -> [(source_id, relation)]。
        # source_type 标记 target 的维度（跨表 id 空间重叠时必须区分）。
        rel_map = {}
        for l in (links or []):
            rel_map.setdefault((l.target_id, l.source_type), []).append(
                (l.source_id, l.relation))
        # edges 方向即语义：precedes A→B = A 在 B 前 → B 标 before 源 A
        for e in (edges or []):
            rel = self.EDGE_REL_MAP.get(e.rel)
            if not rel:
                continue
            tdim = dims.get(e.to_id)
            if tdim is not None:
                rel_map.setdefault((e.to_id, tdim), []).append(
                    (e.from_id, rel))
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
