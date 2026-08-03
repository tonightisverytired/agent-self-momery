# -*- coding: utf-8 -*-
"""生命周期、矛盾治理、访问控制、反射压缩（docs/09 C5-C8）。"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import ValidationError


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
        raise ValidationError("E006 目标不存在")
    if node.protected and not force:
        raise ValidationError("E005 protected 节点需 force=True（合规场景）")
    store.update_lifecycle(node_id, "tombstoned", now)
    store.add_tombstone("node", node_id, reason, now)


def restore(store, node_id, now=None):
    node = next((n for n in store.fetch_nodes() if n.nid == node_id), None)
    if node is None:
        raise ValidationError("E006 目标不存在")
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
              and n.ts is not None and n.ts.year == year and n.ts.month == month]
    if not events:
        return None
    summary = store.insert_node(
        "event", "summary", f"{year}-{month:02d} 月度摘要", "",
        now, 0.8, False, "private",
        config.event_defaults.get("core").life,
        config.event_defaults.get("core").decay_rate,
        now, idempotency_key=f"summary:{year}:{month}")
    for e in events:
        store.insert_edge(summary, e.nid, "summarizes", 0.9, 0.9,
                          e.ts, None, now,
                          idempotency_key=f"sum:{summary}:{e.nid}")
    cluster = store.insert_node(
        "entity", "cluster", f"{year}-{month:02d} 脉络", "",
        None, 0.6, False, "private",
        0.0, 0.0, now, idempotency_key=f"cluster:{year}:{month}")
    store.insert_edge(summary, cluster, "part_of", 0.8, 0.9,
                      now, None, now,
                      idempotency_key=f"po:{summary}:{cluster}")
    return summary
