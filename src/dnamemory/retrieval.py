# -*- coding: utf-8 -*-
"""三路召回 + 分数加权 RRF + 过滤链（docs/09 C4）。"""
from __future__ import annotations

import re

from .models import MemoryHit, RecallFilters


def _active_edges(store, now):
    return [e for e in store.fetch_edges()
            if e.lifecycle == "active"
            and (e.invalid_at is None or e.invalid_at > now)]


def _access_allowed(node, filters):
    if node.lifecycle == "deleted":
        return False
    if node.lifecycle == "archived" and not (filters and filters.include_archived):
        return False
    if node.lifecycle == "tombstoned":
        return False
    allowed = set(filters.access_labels) if (filters and filters.access_labels) \
        else {"public", "private"}
    return node.access_label in allowed


def _time_path(store, t0, tol):
    out = {}
    for n in store.fetch_nodes():
        if n.node_type != "event" or n.ts is None:
            continue
        diff = abs((n.ts - t0).days)
        if diff <= tol:
            out[n.nid] = 1.0 / (1.0 + diff)
    return out


def _graph_path(store, topics, max_hops, rel_filter, filters, now):
    nodes = store.fetch_nodes()
    by_id = {n.nid: n for n in nodes}
    seeds = []
    for n in nodes:
        if n.node_type != "entity" or not _access_allowed(n, filters):
            continue
        if any(t.lower() in n.name.lower() or t.lower() in n.description.lower()
               for t in topics):
            seeds.append((n.nid, 0))
    if not seeds:
        return {}
    edges = _active_edges(store, now)
    reach = {}
    frontier = list(seeds)
    visited = set()
    while frontier:
        nid, hop = frontier.pop(0)
        if nid in visited or hop > max_hops:
            continue
        visited.add(nid)
        reach[nid] = hop
        for e in edges:
            if rel_filter and e.rel != rel_filter:
                continue
            if e.from_id == nid and e.to_id not in visited:
                frontier.append((e.to_id, hop + 1))
            elif e.to_id == nid and e.from_id not in visited:
                frontier.append((e.from_id, hop + 1))
    out = {}
    for e in edges:
        for src, dst in ((e.from_id, e.to_id), (e.to_id, e.from_id)):
            if src not in reach or dst not in by_id:
                continue
            node = by_id[dst]
            if node.node_type == "event":
                hop = reach[src]
                ent = by_id[src]
                out[dst] = max(out.get(dst, 0.0),
                               (1.0 / (hop + 1)) * e.weight * e.confidence
                               * ent.value_score)
            elif node.node_type == "entity" and dst != src:
                hop = reach[src]
                out[dst] = max(out.get(dst, 0.0),
                               (1.0 / (hop + 1)) * e.weight * e.confidence)
    return out


def _semantic_path(store, text, filters):
    tokens = re.findall(r"[0-9a-zA-Z\u4e00-\u9fff]+", text or "")
    tokens = [t.lower() for t in tokens]
    if not tokens:
        return {}
    out = {}
    for n in store.fetch_nodes():
        if not _access_allowed(n, filters):
            continue
        hay = (n.name + " " + n.description).lower()
        hits = sum(1 for t in tokens if t in hay)
        if hits:
            out[n.nid] = (hits / len(tokens)) * n.value_score
    return out


def _dense_path(store, embedder, text, filters, min_sim=0.55):
    """真实稠密向量路：query 嵌入后与已存稠密向量做余弦相似度。

    numpy 缺失时优雅降级为空（不阻断双路/图谱检索）。
    """
    try:
        import numpy
    except ImportError:  # pragma: no cover
        return {}
    try:
        qres = embedder.embed(text)
        qv = numpy.asarray(qres.dense, dtype=float)
    except Exception:  # noqa: BLE001 嵌入失败不阻断其他路径
        return {}
    qn = numpy.linalg.norm(qv)
    if qn == 0:
        return {}
    qv = qv / qn
    nodes = {n.nid: n for n in store.fetch_nodes()}
    out = {}
    for nid, dense, _sparse, _model, _dim, _at in store.fetch_node_vectors():
        node = nodes.get(nid)
        if node is None or not _access_allowed(node, filters):
            continue
        v = numpy.asarray(dense, dtype=float)
        norm = numpy.linalg.norm(v)
        if norm == 0:
            continue
        sim = float(qv @ (v / norm))
        if sim >= min_sim:
            # 语义为主、价值为辅：避免低价值但高度相关的事件被完全压掉
            out[nid] = sim * (0.5 + 0.5 * node.value_score)
    return out


def neighbors(store, node_name, rel=None, now=None):
    nodes = store.fetch_nodes()
    nid = next((n.nid for n in nodes if n.name == node_name), None)
    if nid is None:
        return []
    by_id = {n.nid: n for n in nodes}
    out = []
    for e in _active_edges(store, now):
        if rel and e.rel != rel:
            continue
        if e.from_id == nid:
            out.append((by_id[e.to_id], e, "out"))
        elif e.to_id == nid:
            out.append((by_id[e.from_id], e, "in"))
    return out


def recall(store, config, query, filters, k, mode, now, max_hops=None,
           tol_days=None, embedder=None):
    filters = filters or RecallFilters()
    max_hops = max_hops or config.max_hops_default
    tol_days = tol_days or config.time_tolerance_days_default

    paths = {}
    if mode in ("time", "dual", "triple") and query.time:
        t0, tol = query.time
        paths["time"] = _time_path(store, t0, tol or tol_days)
    if mode in ("graph", "dual", "triple") and query.topic:
        rel = query.relation.rel_type if query.relation else None
        paths["graph"] = _graph_path(store, query.topic, max_hops, rel,
                                     filters, now)
    if mode in ("semantic", "triple") and query.text:
        if embedder is not None:
            paths["semantic"] = _dense_path(store, embedder, query.text,
                                            filters, config.dense_min_sim)
        else:
            paths["semantic"] = _semantic_path(store, query.text, filters)

    scores = {}
    contrib = {}
    for pname, ps in paths.items():
        ranked = sorted(ps.items(), key=lambda x: -x[1])
        for rk, (nid, sc) in enumerate(ranked):
            scores[nid] = scores.get(nid, 0.0) + sc / (config.rrf_k + rk)
            contrib.setdefault(nid, []).append(pname)

    if query.relation:
        allowed = {nb.nid for nb, _, _ in neighbors(
            store, query.relation.node, query.relation.rel_type, now)}
        scores = {nid: sc for nid, sc in scores.items() if nid in allowed}

    nodes = {n.nid: n for n in store.fetch_nodes()}
    if filters.node_types:
        scores = {nid: sc for nid, sc in scores.items()
                  if nodes[nid].node_type in filters.node_types}
    if filters.kinds:
        scores = {nid: sc for nid, sc in scores.items()
                  if nodes[nid].kind in filters.kinds}
    if filters.time_range:
        lo, hi = filters.time_range
        scores = {nid: sc for nid, sc in scores.items()
                  if nodes[nid].ts and lo <= nodes[nid].ts <= hi}

    # 并列规则（与模拟基线一致）：分数降序 → 贡献路径优先级
    # (time < graph < semantic) → id 升序；保证回归数值可复刻
    path_index = {name: i for i, name in enumerate(paths)}
    prio = {nid: min(path_index[p] for p in ps)
            for nid, ps in contrib.items()}
    ranked = sorted(scores.items(),
                    key=lambda x: (-x[1], prio[x[0]], x[0]))
    return [
        MemoryHit(nid, nodes[nid].node_type, nodes[nid].name, nodes[nid].ts,
                  sc, tuple(p for p, ps in paths.items() if nid in ps))
        for nid, sc in ranked[:k]
    ]
