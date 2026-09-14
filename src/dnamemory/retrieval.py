# -*- coding: utf-8 -*-
"""多路召回（时间/图谱/稠密语义/稀疏）+ 分数加权 RRF + 过滤链。"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

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
    """时间路：SQL 索引预筛（窗口放宽 ±(tol+2) 天）+ Python 精确复算。"""
    out = {}
    lo = t0 - timedelta(days=tol + 2)
    hi = t0 + timedelta(days=tol + 2)
    rows = store.read(
        "SELECT id, ts FROM nodes WHERE node_type='event' "
        "AND ts IS NOT NULL AND ts >= ? AND ts <= ? ORDER BY id",
        (lo.isoformat(), hi.isoformat()))
    for nid, ts_text in rows:
        ts = datetime.fromisoformat(ts_text)
        diff = abs((ts - t0).days)
        if diff <= tol:
            out[nid] = 1.0 / (1.0 + diff)
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
    adj = store.adjacency()
    reach = {}
    frontier = list(seeds)
    visited = set()
    while frontier:
        nid, hop = frontier.pop(0)
        if nid in visited or hop > max_hops:
            continue
        visited.add(nid)
        reach[nid] = hop
        for nb, _w, _c, rel, inv in adj.get(nid, ()):
            if inv is not None and inv <= now:
                continue
            if rel_filter and rel != rel_filter:
                continue
            if nb not in visited:
                frontier.append((nb, hop + 1))
    out = {}
    for src in reach:
        src_node = by_id.get(src)
        if src_node is None:
            continue
        for dst, w, c, rel, inv in adj.get(src, ()):
            if inv is not None and inv <= now:
                continue
            if rel_filter and rel != rel_filter:
                continue
            node = by_id.get(dst)
            if node is None:
                continue
            if node.node_type == "event":
                score = (1.0 / (reach[src] + 1)) * w * c \
                    * src_node.value_score
            elif dst != src:
                score = (1.0 / (reach[src] + 1)) * w * c
            else:
                continue
            if score > out.get(dst, 0.0):
                out[dst] = score
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


def _dense_path(store, embedder, text, filters, min_sim=0.55,
                ann_top_k=None):
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
    indexed = None
    if getattr(store, "_vec", None) is not None and qres.model:
        ann_k = min(len(nodes), ann_top_k) if ann_top_k else len(nodes)
        indexed = store.search_vectors(qv.tolist(), qres.model, qres.dim,
                                       k=ann_k)
    if indexed is not None:
        for nid, dist in indexed:
            node = nodes.get(nid)
            if node is None or not _access_allowed(node, filters):
                continue
            sim = 1.0 - dist
            if sim >= min_sim:
                out[nid] = sim * (0.5 + 0.5 * node.value_score)
        return out
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


def _graph_seeds(store, topics, filters):
    """图谱路种子发现：access 允许的实体中按 topic 词面匹配（供后端注入用）。"""
    seeds = []
    for n in store.fetch_nodes():
        if n.node_type != "entity" or not _access_allowed(n, filters):
            continue
        if any(t.lower() in n.name.lower() or t.lower() in n.description.lower()
               for t in topics):
            seeds.append(n.nid)
    return seeds


def _sparse_path(store, embedder, text, filters, min_sim=0.25):
    """bge-m3 稀疏第四路：查询/节点 lexical_weights 余弦相似度。

    embedder 无 sparse 输出或节点 sparse 缺失时跳过（best-effort）。
    """
    try:
        qres = embedder.embed(text)
        q = qres.sparse or {}
    except Exception:  # noqa: BLE001 嵌入失败不阻断其他路径
        return {}
    if not q:
        return {}
    qn = math.sqrt(sum(float(v) * float(v) for v in q.values()))
    if qn == 0:
        return {}
    nodes = {n.nid: n for n in store.fetch_nodes()}
    out = {}
    for nid, _dense, sparse, _model, _dim, _at in store.fetch_node_vectors():
        if not sparse:
            continue
        node = nodes.get(nid)
        if node is None or not _access_allowed(node, filters):
            continue
        dot = 0.0
        dn = 0.0
        for k, v in sparse.items():
            v = float(v)
            dn += v * v
            qv = q.get(k)
            if qv is not None:
                dot += qv * v
        if dn == 0:
            continue
        sim = dot / (qn * math.sqrt(dn))
        if sim >= min_sim:
            out[nid] = sim * (0.5 + 0.5 * node.value_score)
    return out


def neighbors(store, node_name, rel=None, now=None):
    now = now or datetime.now()
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
           tol_days=None, embedder=None, time_backend=None,
           graph_backend=None):
    filters = filters or RecallFilters()
    max_hops = max_hops or config.max_hops_default
    tol_days = tol_days or config.time_tolerance_days_default

    paths = {}
    if mode in ("time", "dual", "triple", "quad") and query.time:
        t0, tol = query.time
        tol = tol or tol_days
        if time_backend is not None:
            paths["time"] = time_backend.time_window(t0, tol)
        else:
            paths["time"] = _time_path(store, t0, tol)
    if mode in ("graph", "dual", "triple", "quad") and query.topic:
        rel = query.relation.rel_type if query.relation else None
        if graph_backend is not None:
            seeds = _graph_seeds(store, query.topic, filters)
            paths["graph"] = (graph_backend.traverse(seeds, max_hops, rel)
                              if seeds else {})
        else:
            paths["graph"] = _graph_path(store, query.topic, max_hops, rel,
                                         filters, now)
    if mode in ("semantic", "triple", "quad") and query.text:
        if embedder is not None:
            paths["semantic"] = _dense_path(store, embedder, query.text,
                                            filters, config.dense_min_sim,
                                            ann_top_k=max(
                                                config.dense_ann_top_k,
                                                k * 20))
        else:
            paths["semantic"] = _semantic_path(store, query.text, filters)
    if mode == "quad" and query.text and embedder is not None:
        paths["sparse"] = _sparse_path(store, embedder, query.text, filters,
                                       config.sparse_min_sim)

    scores = {}
    contrib = {}
    for pname, ps in paths.items():
        ranked = sorted(ps.items(), key=lambda x: (-x[1], x[0]))
        for rk, (nid, sc) in enumerate(ranked):
            scores[nid] = scores.get(nid, 0.0) + sc / (config.rrf_k + rk)
            contrib.setdefault(nid, []).append(pname)

    if query.relation:
        allowed = {nb.nid for nb, _, _ in neighbors(
            store, query.relation.node, query.relation.rel_type, now)}
        scores = {nid: sc for nid, sc in scores.items() if nid in allowed}

    nodes = {n.nid: n for n in store.fetch_nodes()}
    # 统一过滤：类型/kind/时间范围 + 访问控制兜底（deleted/tombstoned/archived
    # 与 access_label 对所有路径生效，含后端注入返回的节点 id）。
    filtered = {}
    for nid, sc in scores.items():
        node = nodes.get(nid)
        if node is None or not _access_allowed(node, filters):
            continue
        if filters.node_types and node.node_type not in filters.node_types:
            continue
        if filters.kinds and node.kind not in filters.kinds:
            continue
        if filters.time_range:
            lo, hi = filters.time_range
            if not (node.ts and lo <= node.ts <= hi):
                continue
        filtered[nid] = sc
    scores = filtered

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
