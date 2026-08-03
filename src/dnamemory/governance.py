# -*- coding: utf-8 -*-
"""生命周期、矛盾治理、访问控制、反射压缩。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from .errors import NotFoundError, ValidationError


@dataclass
class ConflictDecision:
    kind: str                      # resolved | confirm
    node_id: int
    fact_key: str
    winner: object
    loser: object


def step_day(store, config, now):
    archived = []
    for n in store.fetch_nodes():
        if n.lifecycle != "active" or n.protected:
            continue
        n.life -= n.decay_rate
        if n.life <= 0:
            store.update_life(n.nid, 0.0)
            store.update_lifecycle(n.nid, "archived", now)
            archived.append(n.nid)
        else:
            store.update_life(n.nid, n.life)
    return archived


def access(store, config, node_id, now):
    node = next((n for n in store.fetch_nodes() if n.nid == node_id), None)
    if node is None:
        raise ValidationError("E006 目标不存在")
    if node.protected:
        store.update_life_decay(node_id, node.life, now)
        return
    base, coef = config.access_boost
    life = node.life - 0.2 + base + coef * min(node.value_score, 1.0)
    store.update_life_decay(node_id, life, now)


def forget(store, config, node_id, reason, force=False, now=None):
    node = next((n for n in store.fetch_nodes() if n.nid == node_id), None)
    if node is None:
        raise NotFoundError("E006 目标不存在")
    if node.protected and not force:
        raise ValidationError("E005 protected 节点需 force=True（合规场景）")
    now = now or datetime.now()
    if node.lifecycle == "deleted":
        return  # 幂等：deleted 终态不再重复墓碑
    if force:
        # 合规删除：deleted 终态 + 关联 fact/edge 级联墓碑，物理数据保留。
        with store.transaction() as conn:
            store.update_lifecycle(node_id, "deleted", now, conn=conn)
            store.add_tombstone("node", node_id, reason, now, conn=conn)
            for f in store.fetch_facts():
                if f.node_id == node_id and not f.tombstoned:
                    store.tombstone_fact(f.fid, now, conn=conn)
            for e in store.fetch_edges():
                if e.lifecycle == "active" and (
                        e.from_id == node_id or e.to_id == node_id):
                    store.update_edge_lifecycle(e.eid, "tombstoned", now,
                                                conn=conn)
    else:
        store.update_lifecycle(node_id, "tombstoned", now)
        store.add_tombstone("node", node_id, reason, now)


def restore(store, node_id, now=None):
    node = next((n for n in store.fetch_nodes() if n.nid == node_id), None)
    if node is None:
        raise NotFoundError("E006 目标不存在")
    if node.lifecycle == "deleted":
        raise ValidationError("E004 deleted 不可恢复，请重建")
    store.update_lifecycle(node_id, "active", now)


def resolve_conflicts(store, config, now):
    groups = {}
    for f in store.fetch_facts():
        if f.tombstoned:
            continue
        groups.setdefault((f.node_id, f.key), []).append(f)
    decisions = []
    for (nid, key), fs in groups.items():
        active = [f for f in fs if f.invalid_at is None or f.invalid_at > now]
        if len(active) < 2:
            continue
        ordered = sorted(
            active,
            key=lambda f: (config.source_rank.get(f.source, 0),
                           f.confidence, f.valid_at),
            reverse=True)
        top, second = ordered[0], ordered[1]
        same_rank = (config.source_rank.get(top.source)
                     == config.source_rank.get(second.source))
        if same_rank and top.confidence >= config.conflict_confirm_threshold \
                and second.confidence >= config.conflict_confirm_threshold:
            decisions.append(ConflictDecision("confirm", nid, key, top, second))
        else:
            for f in active:
                if f is not top:
                    store.supersede_fact(f.fid, top.fid, now)
            decisions.append(ConflictDecision("resolved", nid, key, top, second))
    return decisions


def confirm(store, decision, choice_value, now):
    if decision.kind != "confirm":
        raise ValidationError("E008 该决策不需要确认")
    if decision.winner.value == choice_value:
        store.supersede_fact(decision.loser.fid, decision.winner.fid, now)
    elif decision.loser.value == choice_value:
        store.supersede_fact(decision.winner.fid, decision.loser.fid, now)
    else:
        raise ValidationError("E001 choice 不在候选中")


def reflect_monthly(store, config, year, month, now, extractor=None):
    """确定性规则版月度反射：摘要节点 + summarizes 边 + part_of 簇。"""
    events = [n for n in store.fetch_nodes()
              if n.node_type == "event" and n.lifecycle == "active"
              and n.kind != "summary"
              and n.ts is not None and n.ts.year == year and n.ts.month == month]
    if not events:
        return None
    mean_score = sum(n.value_score for n in events) / len(events)
    label = _max_label(events)
    summary = store.insert_node(
        "event", "summary", f"{year}-{month:02d} 月度摘要", "",
        now, round(mean_score * 0.8, 4), False, label,
        config.event_defaults.get("core").life,
        config.event_defaults.get("core").decay_rate,
        now, idempotency_key=f"summary:{year}:{month}")
    for e in events:
        store.insert_edge(summary, e.nid, "summarizes", 0.9, 0.9,
                          e.ts, None, now,
                          idempotency_key=f"sum:{summary}:{e.nid}")
    cluster = store.insert_node(
        "entity", "cluster", f"{year}-{month:02d} 脉络", "",
        None, 0.6, False, label,
        0.0, 0.0, now, idempotency_key=f"cluster:{year}:{month}")
    store.insert_edge(summary, cluster, "part_of", 0.8, 0.9,
                      now, None, now,
                      idempotency_key=f"po:{summary}:{cluster}")
    _build_theme_clusters(store, events, summary, year, month, now)
    for d in resolve_conflicts(store, config, now):
        store.audit(
            "fact_convergence", target_type="fact", target_id=d.node_id,
            reason=d.kind,
            meta=json.dumps({"fact_key": d.fact_key,
                             "winner": str(d.winner.value),
                             "loser": str(d.loser.value)},
                            ensure_ascii=False),
            at=now)
    return summary


def _max_label(nodes):
    rank = {"public": 0, "private": 1, "sensitive": 2}
    return max((n.access_label for n in nodes),
               key=lambda x: rank.get(x, 0), default="public")


def _build_theme_clusters(store, events, summary, year, month, now):
    """按主题实体建簇：事件 part_of 主题簇，主题簇 part_of 摘要。"""
    event_ids = {e.nid for e in events}
    nodes = {n.nid: n for n in store.fetch_nodes()}
    by_entity = {}
    for e in store.fetch_edges():
        if e.lifecycle != "active":
            continue
        if e.from_id in event_ids and nodes.get(e.to_id) \
                and nodes[e.to_id].node_type == "entity" \
                and nodes[e.to_id].kind != "cluster":
            by_entity.setdefault(e.to_id, set()).add(e.from_id)
        if e.to_id in event_ids and nodes.get(e.from_id) \
                and nodes[e.from_id].node_type == "entity" \
                and nodes[e.from_id].kind != "cluster":
            by_entity.setdefault(e.from_id, set()).add(e.to_id)
    for ent_id, ev_ids in by_entity.items():
        ent = nodes[ent_id]
        tlabel = _max_label([ent] + [nodes[i] for i in ev_ids])
        tcluster = store.insert_node(
            "entity", "cluster", f"{year}-{month:02d} {ent.name}", "",
            None, 0.6, False, tlabel,
            0.0, 0.0, now,
            idempotency_key=f"tcluster:{year}:{month}:{ent_id}")
        for ev_id in ev_ids:
            store.insert_edge(ev_id, tcluster, "part_of", 0.7, 0.8,
                              nodes[ev_id].ts or now, None, now,
                              idempotency_key=f"poev:{tcluster}:{ev_id}")
        store.insert_edge(tcluster, summary, "part_of", 0.8, 0.9,
                          now, None, now,
                          idempotency_key=f"pocl:{tcluster}:{summary}")
