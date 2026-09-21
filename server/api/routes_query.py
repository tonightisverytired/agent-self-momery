# -*- coding: utf-8 -*-
"""查询/元数据端点（0.8.0 B-05）：stats / facts / neighbors / graph；
0.8.1 IA-4 新增 audit。"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from dnamemory.errors import MemoryError, NotFoundError

from .deps import get_memory
from .serializers import (_fact_dict, _graph_edge_dict, _graph_node_dict,
                          _hit_dict, _neighbor_dict, _status)

router = APIRouter()

_DEFAULT_LABELS = {"public", "private"}


def _visible_nodes(nodes, include_archived, allowed_labels, allowed_types):
    out = []
    for n in nodes:
        if n.lifecycle in ("tombstoned", "deleted"):
            continue
        if n.lifecycle == "archived" and not include_archived:
            continue
        if n.access_label not in allowed_labels:
            continue
        if allowed_types and n.node_type not in allowed_types:
            continue
        out.append(n)
    return out


def _visible_edges(edges, node_ids, now):
    out = []
    for e in edges:
        if e.lifecycle != "active":
            continue
        if e.invalid_at is not None and e.invalid_at <= now:
            continue
        if e.from_id not in node_ids or e.to_id not in node_ids:
            continue
        out.append(e)
    return out


def _entity_current_facts(memory, entity):
    """实体的全部当前事实（/facts 不带 key 时的分面浏览口径）。

    与 fact_lookup 同口径（节点非 tombstoned/deleted、事实未墓碑、未失效），
    差异只在 key 维度不做匹配——留空 key 时不再落到空结果。
    名字解析复用 memory._name2id，与 fact_lookup 同源（含实体消解后的指向）。
    """
    nid = memory._name2id.get(entity)
    if nid is None:
        return []
    nodes = memory.store.fetch_nodes()
    node = next((n for n in nodes if n.nid == nid), None)
    if node is None or node.lifecycle in ("tombstoned", "deleted"):
        return []
    now = memory.clock()
    items = [f for f in memory.store.fetch_facts()
             if f.node_id == nid and not f.tombstoned
             and (f.invalid_at is None or f.invalid_at > now)]
    items.sort(key=lambda f: (f.key, f.fid))
    return items


@router.get("/stats")
def stats(memory=Depends(get_memory)):
    """库统计：各维度计数（active 视角）。"""
    try:
        nodes = memory.store.fetch_nodes()
        return {
            "nodes": len(nodes),
            "entities": sum(1 for n in nodes if n.node_type == "entity"),
            "events": sum(1 for n in nodes if n.node_type == "event"),
            "facts": len(memory.store.fetch_facts()),
            "beliefs": len(memory.store.fetch_beliefs()),
            "intents": len(memory.store.fetch_intents()),
            "impacts": len(memory.store.fetch_impacts()),
            "evidence": len(memory.store.fetch_evidence()),
            "edges": len(memory.store.fetch_edges()),
            "triggers": len(memory.store.fetch_triggers()),
            "patterns": len(memory.store.fetch_patterns()),
            "memory_links": len(memory.store.fetch_memory_links()),
        }
    except MemoryError as e:
        raise HTTPException(status_code=_status(e),
                            detail={"code": e.code, "message": str(e)})


@router.get("/entities")
def entities(kind: str = "person", limit: int = 500,
             memory=Depends(get_memory)):
    """实体清单（0.8.2 人物聚合视图数据源）：按 kind 列出 active 实体，
    附当前事实数，按事实数降序（人物页面板用；快照缓存，重复调用便宜）。
    """
    limit = max(1, min(limit, 5000))
    try:
        nodes = memory.store.fetch_nodes()
        facts = memory.store.fetch_facts()
        fact_n = {}
        for f in facts:
            fact_n[f.node_id] = fact_n.get(f.node_id, 0) + 1
        items = [{"id": n.nid, "name": n.name, "kind": n.kind,
                  "fact_count": fact_n.get(n.nid, 0)}
                 for n in nodes
                 if n.node_type == "entity" and n.kind == kind
                 and n.lifecycle == "active"]
        items.sort(key=lambda x: (-x["fact_count"], x["id"]))
        return {"total": len(items), "items": items[:limit]}
    except MemoryError as e:
        raise HTTPException(status_code=_status(e),
                            detail={"code": e.code, "message": str(e)})


@router.get("/facts")
def facts(entity: Optional[str] = None, key: Optional[str] = None,
          memory=Depends(get_memory)):
    """事实查询。

    - 带 key：fact_lookup（canonical key 归一后的当前值）；
    - 不带 key：列出该实体全部当前事实（分面浏览语义）；
    - entity 缺失返回空 items（与门面语义一致，不 404）。
    """
    if not entity:
        return {"items": []}
    try:
        if key:
            items = memory.fact_lookup(entity, key)
        else:
            items = _entity_current_facts(memory, entity)
        return {"items": [_fact_dict(f) for f in items]}
    except MemoryError as e:
        raise HTTPException(status_code=_status(e),
                            detail={"code": e.code, "message": str(e)})


@router.get("/audit")
def audit(target_type: Optional[str] = None, target_id: Optional[int] = None,
          op: Optional[str] = None, limit: int = 50,
          memory=Depends(get_memory)):
    """审计日志查询（0.8.1 IA-4）：裁决/写入/生命周期等操作的事后追溯。

    - 过滤参数均可选：target_type（fact/node/belief/...）、target_id、op
      （supersede_fact/fact_adjudicate/conflict_resolve/...）；
    - limit 默认 50，上限 200；结果按 id 倒序（最新在前）；
    - items[i] = {id, op, target_type, target_id, reason, meta, at}，
      meta 为已解析的 JSON（历史非 JSON 行保留原始字符串）。
    """
    limit = max(1, min(limit, 200))
    try:
        rows = memory.store.fetch_audit_logs(target_type=target_type,
                                             target_id=target_id, op=op,
                                             limit=limit)
        return {"items": rows}
    except MemoryError as e:
        raise HTTPException(status_code=_status(e),
                            detail={"code": e.code, "message": str(e)})


@router.get("/neighbors")
def neighbors_api(node: str, rel: Optional[str] = None,
                  memory=Depends(get_memory)):
    """节点邻接：按节点名精确匹配，返回 (邻居节点, rel, 方向, 权重)。

    响应 items[i] = {node:{...}, rel, direction:"in"|"out",
                    weight, confidence, valid_at, invalid_at}。
    """
    try:
        hits = memory.neighbors(node, rel=rel)
        return {"items": [_neighbor_dict(h) for h in hits]}
    except MemoryError as e:
        raise HTTPException(status_code=_status(e),
                            detail={"code": e.code, "message": str(e)})


@router.get("/graph")
def graph(limit: int = 500, node_types: Optional[str] = None,
          include_archived: bool = False,
          access_labels: Optional[str] = None,
          center: Optional[str] = None, hops: int = 2,
          memory=Depends(get_memory)):
    """记忆图谱快照（0.8.0 静态后台图谱页数据源）。

    - limit：截断目标为边数（1-5000 钳制）；truncated 标记是否截断。
    - center：节点名精确匹配，BFS（出+入）hops 层（1-3）；未命中返回
      空图 + center.matched=false（不 404）。
    - counts 统计截断前全量可见对象，仪表盘分布图在 truncated 时仍准确。
    """
    limit = max(1, min(limit, 5000))
    hops = max(1, min(hops, 3))
    labels = set(_DEFAULT_LABELS)
    if access_labels:
        labels = {s.strip() for s in access_labels.split(",") if s.strip()}
    types = None
    if node_types:
        types = {s.strip() for s in node_types.split(",") if s.strip()}
    now = datetime.now()
    try:
        all_nodes = memory.store.fetch_nodes()
        all_edges = memory.store.fetch_edges()

        vis_nodes = _visible_nodes(all_nodes, include_archived, labels, types)
        vis_ids = {n.nid for n in vis_nodes}
        vis_edges = _visible_edges(all_edges, vis_ids, now)

        # ---- 截断前全量统计 ----
        counts = {
            "nodes": len(vis_nodes),
            "nodes_active": sum(1 for n in vis_nodes
                                if n.lifecycle == "active"),
            "entities": sum(1 for n in vis_nodes if n.node_type == "entity"),
            "entities_active": sum(1 for n in vis_nodes
                                   if n.node_type == "entity"
                                   and n.lifecycle == "active"),
            "events": sum(1 for n in vis_nodes if n.node_type == "event"),
            "events_active": sum(1 for n in vis_nodes
                                 if n.node_type == "event"
                                 and n.lifecycle == "active"),
            "edges": len(vis_edges),
            "edges_active": sum(1 for e in vis_edges
                                if e.lifecycle == "active"),
            # 分布图口径：原始全表（含 tombstoned/deleted），展示真实分布
            "lifecycle": dict(Counter(n.lifecycle for n in all_nodes)),
            "access": dict(Counter(n.access_label for n in all_nodes)),
            "rel_counts": dict(Counter(e.rel for e in all_edges)),
        }

        # ---- 节点选择：center BFS 或 边截断 ----
        center_info = {"matched": False, "node": None, "hops": hops}
        truncated = False
        if center:
            center_info["matched"] = False
            by_name = {n.name: n for n in vis_nodes}
            start = by_name.get(center)
            if start is not None:
                center_info["matched"] = True
                center_info["node"] = start.nid
                adj = defaultdict(list)
                for e in vis_edges:
                    adj[e.from_id].append(e.to_id)
                    adj[e.to_id].append(e.from_id)
                seen = {start.nid}
                frontier = deque([(start.nid, 0)])
                while frontier:
                    cur, depth = frontier.popleft()
                    if depth >= hops:
                        continue
                    for nxt in adj[cur]:
                        if nxt not in seen:
                            if len(seen) >= limit:
                                truncated = True
                                break
                            seen.add(nxt)
                            frontier.append((nxt, depth + 1))
                    if truncated:
                        break
                sel_ids = seen
            else:
                sel_ids = set()
        else:
            # 按 (weight desc) 取前 limit 条可见边做种子
            ordered = sorted(vis_edges,
                             key=lambda e: (-e.weight, e.eid))
            seed_edges = ordered[:limit]
            sel_ids = set()
            for e in seed_edges:
                sel_ids.add(e.from_id)
                sel_ids.add(e.to_id)
            # 补足高度数可见节点（按全图度数降序），防大度枢纽被孤立节点挤掉
            degree = Counter()
            for e in vis_edges:
                degree[e.from_id] += 1
                degree[e.to_id] += 1
            target = min(len(vis_nodes), max(limit * 2, 1))
            for nid, _ in sorted(degree.items(), key=lambda kv: (-kv[1], kv[0])):
                if len(sel_ids) >= target:
                    break
                sel_ids.add(nid)
            # 孤立节点（无任何边）也补入，按重要度降序；超出 target 才截断
            if len(sel_ids) < target:
                orphans = sorted(
                    (n for n in vis_nodes if n.nid not in degree),
                    key=lambda n: (-(n.value_score or 0.0), n.nid))
                for n in orphans:
                    if len(sel_ids) >= target:
                        break
                    sel_ids.add(n.nid)
            if len(sel_ids) < len(vis_nodes) or len(seed_edges) < len(vis_edges):
                truncated = True

        by_id = {n.nid: n for n in vis_nodes}
        sel_edges = [e for e in vis_edges
                     if e.from_id in sel_ids and e.to_id in sel_ids]
        degree = Counter()
        for e in sel_edges:
            degree[e.from_id] += 1
            degree[e.to_id] += 1

        # ---- 最近事件（独立于边截断） ----
        events = [n for n in vis_nodes if n.node_type == "event"]
        events.sort(key=lambda n: (n.ts is None, n.ts or datetime.min),
                    reverse=True)
        recent = events[:10]

        return {
            "nodes": [_graph_node_dict(by_id[nid], degree.get(nid, 0))
                      for nid in sel_ids],
            "edges": [_graph_edge_dict(e) for e in sel_edges],
            "counts": counts,
            "recent_events": [_graph_node_dict(n, degree.get(n.nid, 0))
                              for n in recent],
            "center": center_info,
            "truncated": truncated,
        }
    except MemoryError as e:
        raise HTTPException(status_code=_status(e),
                            detail={"code": e.code, "message": str(e)})
