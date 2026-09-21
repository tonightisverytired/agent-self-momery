# -*- coding: utf-8 -*-
"""多路召回（时间/图谱/词面/稠密语义/稀疏）+ 分数加权 RRF + 过滤链。"""
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


def _bigram_containment(q, hay, bq=None):
    """字符 bigram 包含度：q 的去重 bigram 集中出现在 hay 里的比例。

    q 已归一（lower + 去空白）；任一为空返回 0；**q 是 hay 的子串**短路
    1.0（节点文本完整覆盖查询）；反向（hay 是 q 的子串，如实体名「用户」
    之于「用户昨天三点」）不短路——短名节点只覆盖查询的一小部分，给
    1.0 会让高频实体灌满榜首。单字符 q 无 bigram，由子串短路覆盖。
    bq：调用方预计算的 q bigram 集（0.8.2 性能：全节点扫描时避免逐
    节点重建）。
    """
    if not q or not hay:
        return 0.0
    if q in hay:
        return 1.0
    if len(q) < 2:
        return 0.0
    if bq is None:
        bq = {q[i:i + 2] for i in range(len(q) - 1)}
    return sum(1 for b in bq if b in hay) / len(bq)


def _lexical_path(store, text, filters, min_score=0.15):
    """词面路：字符 bigram 包含度打分（中文免分词）。

    候选准入只看相关度：containment ≥ min_score（未乘价值分的原始
    包含度）；节点得分 = containment × (0.5 + 0.5 × value_score)——
    与稠密路「语义为主、价值为辅」同构，避免高价值分的弱相关节点
    在 RRF 里挤掉低价值分的精确/前缀匹配（导航型查询不被价值稀释）。
    """
    q = re.sub(r"\s+", "", (text or "").lower())
    if not q:
        return {}
    bq = {q[i:i + 2] for i in range(len(q) - 1)} if len(q) >= 2 else None
    out = {}
    for n in store.fetch_nodes():
        if not _access_allowed(n, filters):
            continue
        name_l = n.name.lower()
        c = max(_bigram_containment(q, name_l, bq),
                _bigram_containment(q, n.description.lower(), bq))
        # 实体锚定：节点名（≥2 字）是查询子串时保底 0.5——「公公的健康
        # 状况是什么」里的「公公」是问句主语，须保证进候选池供扩展/事实
        # 关联，但不能压过完整覆盖查询的事件（top-k 不被短名实体灌满）
        if name_l and len(name_l) >= 2 and name_l in q:
            c = max(c, 0.5)
        if c <= 0 or c < min_score:  # c=0 不入候选（min_score=0 时防灌水）
            continue
        out[n.nid] = c * (0.5 + 0.5 * n.value_score)
    return out


def _dense_path(store, embedder, text, filters, min_sim=0.55,
                ann_top_k=None, by_kind=None):
    """真实稠密向量路：query 嵌入后与已存稠密向量做余弦相似度。

    by_kind：分类型阈值（如长文 chat 节点低于全局 min_sim）。
    numpy 缺失时优雅降级为空（不阻断双路/图谱检索）。
    """
    def _thr(node):
        return by_kind.get(node.kind, min_sim) if by_kind else min_sim
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
            if sim >= _thr(node):
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
        if sim >= _thr(node):
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


_REL_DAYS = {"今天": 0, "昨天": -1, "前天": -2, "明天": 1, "后天": 2}


def _extract_query_time(text, now):
    """从查询文本提取时间锚点（0.8.1 时间感知）。

    显式日期（2022年5月12日 / 2022-05-12 / 2022/05/12）精确锚定 tol=0；
    相对词（今天/昨天/前天/明天/后天/上周/本周）按 now 换算，tol 放宽；
    无年份的「5月12日」不锚定（跨年误判代价大，留给词面/语义路）。
    """
    if not text:
        return None
    m = re.search(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?",
                  text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)),
                            int(m.group(3))), 0
        except ValueError:
            pass
    for w, d in _REL_DAYS.items():
        if w in text:
            return now + timedelta(days=d), 1
    if "上周" in text or "上星期" in text:
        return now - timedelta(days=7), 4
    if "本周" in text or "这周" in text:
        return now, 3
    return None


def _extract_monthday(text):
    """无年份月日（「5月12日」）→ "MM-DD"；带年份的查询由显式日期优先处理。"""
    if not text:
        return None
    if re.search(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}", text):
        return None    # 带年份的完整日期由显式日期路径处理
    m = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text)
    if not m:
        return None
    mo, d = int(m.group(1)), int(m.group(2))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{mo:02d}-{d:02d}"


def _monthday_path(store, md):
    """月日周年匹配：任一年的同月同日事件（ts 以 ISO 存储，切 5:10 位）。"""
    rows = store.read(
        "SELECT id, ts FROM nodes WHERE node_type='event' "
        "AND ts IS NOT NULL ORDER BY id")
    return {nid: 1.0 for nid, ts in rows if ts[5:10] == md}


def _merged_max(dicts):
    """多查询变体的路径结果按节点取 max（HyDE 变体融合）。"""
    out = {}
    for d in dicts:
        for nid, sc in d.items():
            if sc > out.get(nid, 0.0):
                out[nid] = sc
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
           graph_backend=None, alt_texts=None):
    filters = filters or RecallFilters()
    max_hops = max_hops or config.max_hops_default
    tol_days = tol_days or config.time_tolerance_days_default
    # HyDE 查询变体（0.8.1 二轮）：去重保序，原文恒在首位
    qtexts = [query.text] if query.text else []
    for t in (alt_texts or []):
        if t and t not in qtexts:
            qtexts.append(t)

    paths = {}
    # 时间感知（0.8.1）：query.time 未显式给出时，从查询文本提取时间锚点；
    # 无年份月日退到跨年周年匹配
    qtime = query.time
    q_md = None
    if qtime is None and getattr(config, "time_aware_text", False) \
            and query.text:
        qtime = _extract_query_time(query.text, now)
        if qtime is None:
            q_md = _extract_monthday(query.text)
    if mode in ("time", "dual", "triple", "quad") and qtime:
        t0, tol = qtime
        tol = tol_days if tol is None else tol
        if time_backend is not None:
            paths["time"] = time_backend.time_window(t0, tol)
        else:
            paths["time"] = _time_path(store, t0, tol)
    elif mode in ("time", "dual", "triple", "quad") and q_md:
        paths["time"] = _monthday_path(store, q_md)
    if mode in ("graph", "dual", "triple", "quad") and query.topic:
        rel = query.relation.rel_type if query.relation else None
        if graph_backend is not None:
            seeds = _graph_seeds(store, query.topic, filters)
            paths["graph"] = (graph_backend.traverse(seeds, max_hops, rel)
                              if seeds else {})
        else:
            paths["graph"] = _graph_path(store, query.topic, max_hops, rel,
                                         filters, now)
    if mode in ("semantic", "triple", "quad") and qtexts:
        # 词面路始终计算；注入 embedder 时稠密路并行（不替换词面路）。
        # HyDE 变体按路径内逐节点 max 融合
        paths["lexical"] = _merged_max(
            _lexical_path(store, t, filters, config.lexical_min_score)
            for t in qtexts)
        if embedder is not None:
            paths["semantic"] = _merged_max(
                _dense_path(store, embedder, t, filters,
                            config.dense_min_sim,
                            ann_top_k=max(config.dense_ann_top_k, k * 20),
                            by_kind=getattr(config, "dense_min_sim_by_kind",
                                            None))
                for t in qtexts)
    if mode == "quad" and qtexts and embedder is not None:
        paths["sparse"] = _merged_max(
            _sparse_path(store, embedder, t, filters, config.sparse_min_sim)
            for t in qtexts)

    scores = {}
    contrib = {}
    path_scores = {}
    for pname, ps in paths.items():
        ranked = sorted(ps.items(), key=lambda x: (-x[1], x[0]))
        for rk, (nid, sc) in enumerate(ranked):
            scores[nid] = scores.get(nid, 0.0) + sc / (config.rrf_k + rk)
            contrib.setdefault(nid, []).append(pname)
            path_scores.setdefault(nid, {})[pname] = {"score": sc,
                                                      "rank": rk}

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
    # (time < graph < lexical < semantic < sparse) → id 升序；
    # 保证回归数值可复刻
    path_index = {name: i for i, name in enumerate(paths)}
    prio = {nid: min(path_index[p] for p in ps)
            for nid, ps in contrib.items()}
    ranked = sorted(scores.items(),
                    key=lambda x: (-x[1], prio[x[0]], x[0]))
    return [
        MemoryHit(nid, nodes[nid].node_type, nodes[nid].name, nodes[nid].ts,
                  sc, tuple(p for p, ps in paths.items() if nid in ps),
                  path_scores.get(nid))
        for nid, sc in ranked[:k]
    ]
