# -*- coding: utf-8 -*-
"""领域对象 → JSON 序列化与错误映射（0.8.0 A-02：自
app/dnamemory_server.py 原样迁移；B-03/B-05 在此扩展唯一实现）。"""
from __future__ import annotations

from dnamemory.errors import (CoherenceConflictError, ConflictError,
                              ContextBuildFailedError, EmbeddingError,
                              EvidenceNotFoundError, MemoryError,
                              MemoryStateConflictError, NotFoundError,
                              StorageError, TemporalChainInvalidError,
                              UnsupportedBeliefError, ValidationError)


def _iso(v):
    return v.isoformat() if v else None


def _fact_dict(f):
    return {"id": f.fid, "key": f.key, "value": f.value,
            "source": f.source, "confidence": f.confidence,
            "valid_at": _iso(f.valid_at), "invalid_at": _iso(f.invalid_at),
            "evidence_ids": list(f.evidence_ids or [])}


def _evidence_dict(e):
    """证据全字段（0.8.1 IA-4，additive）：content_hash/trust_level/
    conversation_id/message_id/observed_at/metadata 透出。

    访问控制语义不变：sensitive 证据在 recall_context/explain 上游已被
    过滤，本函数只做格式转换，不过滤。
    """
    return {"id": e.id, "source_type": e.source_type,
            "source_ref": e.source_ref,
            "content_hash": getattr(e, "content_hash", None),
            "trust_level": getattr(e, "trust_level", None),
            "conversation_id": getattr(e, "conversation_id", None),
            "message_id": getattr(e, "message_id", None),
            "observed_at": _iso(getattr(e, "observed_at", None)),
            "metadata": dict(getattr(e, "metadata", None) or {})}


def _mem_node_dict(n):
    """链节点（temporal/impact 共用）：dimension/relation/at/id/name。"""
    m = n.memory
    return {
        "dimension": n.dimension, "relation": n.relation, "at": _iso(n.at),
        "id": getattr(m, "nid", getattr(m, "fid", getattr(m, "id", None))),
        "name": getattr(m, "name", getattr(m, "value", "")),
    }


def _impact_dict(i):
    return {"id": i.id, "subject_id": i.subject_id, "dimension": i.dimension,
            "direction": i.direction, "valence": i.valence,
            "magnitude": i.magnitude, "kind": i.kind,
            "evaluator": i.evaluator, "description": i.description,
            "cause_event_id": i.cause_event_id, "source": i.source,
            "confidence": i.confidence, "valid_at": _iso(i.valid_at),
            "invalid_at": _iso(i.invalid_at), "lifecycle": i.lifecycle,
            "access_label": i.access_label,
            "evidence_ids": list(i.evidence_ids or [])}


def _pattern_dict(p):
    return {"id": p.id, "pattern_type": p.pattern_type,
            "subject_id": p.subject_id, "proposition": p.proposition,
            "confidence": p.confidence, "support": p.support,
            "window_days": p.window_days, "source": p.source,
            "valid_at": _iso(p.valid_at)}


def _score_dict(s):
    if s is None:
        return None
    try:
        final = round(s.final(), 4)
    except Exception:  # noqa: BLE001
        final = None
    return {
        "retrieval": s.retrieval_score, "temporal": s.temporal_score,
        "validity": s.validity_score, "source": s.source_score,
        "evidence": s.evidence_score, "coherence": s.coherence_score,
        "conflict_penalty": s.conflict_penalty, "final": final,
    }


def _conflict_dicts(conflicts):
    """冲突区块序列化：核心库产出 ConflictGroup（resolve.py 查询端冲突态），
    治理接口产出 {kind, node_id, key} 字典，两种形态都兼容。

    这里**只做格式转换，不再二次分组**：ConflictGroup 已带组结构，按 key
    重新分组会把同义 key（位置/所在地/location）劈开、出现「只有一个值」的
    假冲突；`len(values) < 2` 守卫兜底任何来源的单值条目。
    """
    out = []
    for c in conflicts or []:
        if isinstance(c, dict):
            item = {"kind": c.get("kind"), "node_id": c.get("node_id"),
                    "key": c.get("key")}
            # 0.8.1 IA-4：源 dict 带 facts 时保留（原先丢弃，事后看不到
            # 冲突双方）；元素是 Fact 时走 _fact_dict，裸值（字符串）原样
            if c.get("facts") is not None:
                item["facts"] = [_fact_dict(f) if hasattr(f, "fid") else f
                                 for f in c.get("facts")]
            out.append(item)
            continue
        values = list(getattr(c, "values", []) or [])
        if len(values) < 2:
            continue
        out.append({"kind": getattr(c, "kind", None) or "fact_conflict",
                    "node_id": getattr(c, "node_id", None),
                    "key": getattr(c, "key", None),
                    "fact_ids": list(getattr(c, "fact_ids", []) or []),
                    "values": values})
    return out


def _ctx_dict(ctx):
    # 0.8.1 IA-4：单记忆分项分按 fid（str 化，见 MemoryContext.
    # score_by_item docstring）匹配到 current_state/historical_changes
    # 各项；该次未评分（非 recall_context 管线）则不附加该键
    scores = getattr(ctx, "score_by_item", None) or {}

    def _scored_fact(f):
        d = _fact_dict(f)
        sc = scores.get(str(f.fid))
        if sc is not None:
            d["memory_score"] = _score_dict(sc)
        return d

    return {
        "query_type": ctx.query_type,
        "current_state": [_scored_fact(f) for f in ctx.current_state],
        "historical_changes": [_scored_fact(f)
                               for f in ctx.historical_changes],
        "recent_events": [{"id": n.nid, "name": n.name, "ts": _iso(n.ts)}
                          for n in ctx.recent_events],
        "beliefs": [{"id": b.id, "proposition": b.proposition,
                     "polarity": b.polarity} for b in ctx.beliefs],
        "intents": [{"id": i.id, "proposition": i.proposition,
                     "status": i.status} for i in ctx.intents],
        "temporal_chains": [
            [_mem_node_dict(n) for n in ch.nodes]
            for ch in ctx.temporal_chains],
        "evidence": [_evidence_dict(e) for e in ctx.evidence],
        "conflicts": _conflict_dicts(ctx.conflicts),
        "notes": list(ctx.notes),
        # ---- 0.8.0 B-05 新增区块（只增不减） ----
        "causes": [{"cause": {"id": getattr(c["cause"], "nid", None),
                              "name": getattr(c["cause"], "name", "")},
                    "caused": _fact_dict(c["caused"]),
                    "confidence": c["confidence"]} for c in ctx.causes],
        "impacts": [_impact_dict(i) for i in ctx.impacts],
        "impact_chains": [
            [_mem_node_dict(n) for n in ch.nodes]
            for ch in getattr(ctx, "impact_chains", [])],
        "patterns": [_pattern_dict(p) for p in ctx.patterns],
        "score": _score_dict(ctx.score),
        "trace": ctx.trace.to_dict() if ctx.trace is not None else None,
    }


def _status(e: MemoryError) -> int:
    code = getattr(e, "code", "")
    if code in ("E004", "E006", "E013"):
        return 404
    if isinstance(e, NotFoundError) or isinstance(e, EvidenceNotFoundError):
        return 404
    if isinstance(e, ConflictError) or isinstance(e, MemoryStateConflictError):
        return 409
    if isinstance(e, EmbeddingError):
        return 502
    if isinstance(e, StorageError):
        return 503
    if isinstance(e, (TemporalChainInvalidError, ContextBuildFailedError,
                      CoherenceConflictError)):
        return 422
    if isinstance(e, UnsupportedBeliefError):
        return 400
    if isinstance(e, ValidationError):
        return 400
    return 500


def _graph_node_dict(n, degree=0):
    return {
        "id": n.nid, "node_type": n.node_type, "kind": n.kind,
        "name": n.name, "description": n.description, "ts": _iso(n.ts),
        "value_score": n.value_score, "protected": n.protected,
        "access_label": n.access_label, "lifecycle": n.lifecycle,
        "life": n.life, "decay_rate": n.decay_rate,
        "source": getattr(n, "source", None), "degree": degree,
    }


def _graph_edge_dict(e):
    return {
        "id": e.eid, "from": e.from_id, "to": e.to_id, "rel": e.rel,
        "weight": e.weight, "confidence": e.confidence,
        "valid_at": _iso(e.valid_at), "invalid_at": _iso(e.invalid_at),
        "lifecycle": e.lifecycle,
    }


def _hit_dict(h):
    return {
        "node_id": h.node_id,
        "node_type": h.node_type,
        "name": h.name,
        "ts": h.ts.isoformat() if h.ts else None,
        "score": h.score,
        "sources": list(h.sources),
        "path_scores": dict(h.path_scores or {}),
    }


def _explain_version_dict(v):
    """explain() 版本链条目（0.8.1 IA-4）：node 是 versions 表
    (version, content, created_at) 元组；fact/belief/intent 是领域对象
    （版本链/演化时间线，见 MemorySystem.explain docstring）。"""
    if isinstance(v, (tuple, list)):
        return {"version": v[0], "content": v[1], "created_at": v[2]}
    if hasattr(v, "fid"):  # Fact
        return _fact_dict(v)
    d = {"id": getattr(v, "id", None),
         "proposition": getattr(v, "proposition", ""),
         "confidence": getattr(v, "confidence", None),
         "source": getattr(v, "source", None),
         "valid_at": _iso(getattr(v, "valid_at", None)),
         "invalid_at": _iso(getattr(v, "invalid_at", None))}
    polarity = getattr(v, "polarity", None)
    if polarity is not None:
        d["polarity"] = polarity
    status = getattr(v, "status", None)
    if status is not None:
        d["status"] = status
    return d


def _neighbor_dict(item):
    """retrieval.neighbors 三元组 (neighbor_node, edge, direction) → JSON。

    0.8.0 静态后台图谱页依赖 rel/direction/weight 做边可视化。
    """
    node, edge, direction = item
    return {
        "node": {
            "node_id": node.nid, "node_type": node.node_type,
            "kind": node.kind, "name": node.name, "ts": _iso(node.ts),
            "value_score": node.value_score,
            "access_label": node.access_label, "lifecycle": node.lifecycle,
        },
        "rel": edge.rel,
        "direction": direction,
        "weight": edge.weight,
        "confidence": edge.confidence,
        "valid_at": _iso(edge.valid_at),
        "invalid_at": _iso(edge.invalid_at),
    }
