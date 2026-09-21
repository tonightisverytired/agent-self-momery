# -*- coding: utf-8 -*-
"""影响链构建（0.7.0 P2，设计稿 §8.2）。

Impact Chain 回答「事情产生了什么结果」：Event → Impact →
Secondary Impact → Belief Change → Intent（与 Temporal Chain 的
「什么时候」正交，可交叉）。构建输入：记忆对象集合 + memory_links；
Event→Impact 的归属用 impacts.cause_event_id 隐式边（读取时视为
caused_by），Impact→Impact/Belief/Intent 走 caused_by memory_links。
防环 + 稳定序，输出 Chain 序列（复用 temporal.Chain/ChainNode）。
"""
from __future__ import annotations

from .models import Belief, Fact, Impact, Intent
from .temporal import Chain, ChainNode, stable_sort


def _dimension(m):
    if isinstance(m, Impact):
        return "impact"
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


def _mem_id(m):
    return getattr(m, "nid", getattr(m, "fid", getattr(m, "id", None)))


def _sort_time(m):
    return (getattr(m, "ts", None) or getattr(m, "valid_at", None)
            or getattr(m, "created_at", None))


class ImpactChainBuilder:
    """事件/影响/观点/意图的前向影响链（确定性）。"""

    def __init__(self, config=None):
        self.config = config

    def build(self, memories, links=None):
        memories = [m for m in memories if m is not None]
        if not memories:
            return []
        by_key = {}
        for m in memories:
            dim = _dimension(m)
            key = (dim, _mem_id(m))
            by_key[key] = m
        # 邻接：(type, id) → [(type, id)] 前向边
        adj = {k: [] for k in by_key}
        # ① Event→Impact：cause_event_id 隐式边（读取视为 caused_by）
        for k, m in by_key.items():
            cause = getattr(m, "cause_event_id", None)
            if k[0] == "impact" and cause is not None:
                src = ("event", cause)
                if src in by_key:
                    adj[src].append(k)
        # ② memory_links caused_by：source_type 标记 target 维度（项目
        # 既有语义），target 精确键；source 优先用 source_dim 精确定位，
        # 无 source_dim（0.6 旧链）时按 id 唯一匹配，跨表碰撞弃边。
        for l in (links or []):
            if l.relation != "caused_by":
                continue
            tgt = (l.source_type, l.target_id)
            if tgt not in by_key:
                continue
            src = None
            if getattr(l, "source_dim", ""):
                cand = (l.source_dim, l.source_id)
                if cand in by_key:
                    src = cand
            else:
                srcs = [k for k in by_key if k[1] == l.source_id]
                if len(srcs) == 1:
                    src = srcs[0]
            if src is None or src == tgt:
                continue
            t_src = _sort_time(by_key[src])
            t_tgt = _sort_time(by_key[tgt])
            if t_src is not None and t_tgt is not None and t_tgt < t_src:
                continue
            adj[src].append(tgt)
        # ③ 拓扑前向展开（防环 visited），从入度为 0 的根出发
        indeg = {k: 0 for k in by_key}
        for src, tgts in adj.items():
            for t in tgts:
                indeg[t] = indeg.get(t, 0) + 1
        roots = sorted((k for k, d in indeg.items() if d == 0),
                       key=lambda k: (_sort_time(by_key[k]),
                                      _mem_id(by_key[k]) or 0))
        chains, visited = [], set()
        for root in roots:
            if root in visited:
                continue
            path = []

            def walk(k):
                if k in visited:
                    return
                visited.add(k)
                path.append(k)
                for nxt in sorted(
                        adj.get(k, []),
                        key=lambda x: (_sort_time(by_key[x]),
                                       _mem_id(by_key[x]) or 0)):
                    walk(nxt)

            walk(root)
            ordered = sorted(path, key=lambda k: (_sort_time(by_key[k]),
                                                  _mem_id(by_key[k]) or 0))
            nodes = []
            for i, k in enumerate(ordered):
                rel = "before" if i > 0 else None
                nodes.append(ChainNode(memory=by_key[k],
                                       dimension=k[0],
                                       at=_sort_time(by_key[k]),
                                       relation=rel,
                                       source_id=None))
            chains.append(Chain(nodes=nodes))
        # 孤立环节点（全部有入度）单独成链
        leftover = [k for k in by_key if k not in visited]
        for k in leftover:
            chains.append(Chain(nodes=[
                ChainNode(memory=by_key[k], dimension=k[0],
                          at=_sort_time(by_key[k]))]))
        # 有边的长链优先，孤立节点链排后（影响链语义优先）
        chains.sort(key=lambda c: (-len(c.nodes),
                                   _sort_time(c.nodes[0].memory)))
        return chains
