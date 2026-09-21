# -*- coding: utf-8 -*-
"""生命周期、矛盾治理、访问控制、反射压缩。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from .errors import NotFoundError, ValidationError
from .models import Belief, Fact, Intent
from .resolve import adjudication_reason


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
        if n.lifecycle != "active" or n.protected or n.decay_rate == 0:
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
        # 合规删除：deleted 终态 + 全部派生数据级联（物理数据保留）。
        # 原实现只做 node + facts + active edges，主体的观点/意图/影响/
        # 模式/证据仍可被 explain 与检索读到 —— 与对外宣称的合规删除不符。
        with store.transaction() as conn:
            store.update_lifecycle(node_id, "deleted", now, conn=conn)
            store.add_tombstone("node", node_id, reason, now, conn=conn)
            ev_ids = set()
            for f in store.fetch_facts():
                if f.node_id != node_id:
                    continue
                ev_ids.update(f.evidence_ids or [])
                if not f.tombstoned:
                    store.tombstone_fact(f.fid, now, conn=conn)
            for b in store.fetch_beliefs():
                if b.subject_id == node_id:
                    ev_ids.update(b.evidence_ids or [])
            for i in store.fetch_intents():
                if i.subject_id == node_id:
                    ev_ids.update(i.evidence_ids or [])
            for im in store.fetch_impacts():
                if im.subject_id == node_id:
                    ev_ids.update(im.evidence_ids or [])
            for e in store.fetch_edges():
                if e.lifecycle == "active" and (
                        e.from_id == node_id or e.to_id == node_id):
                    store.update_edge_lifecycle(e.eid, "tombstoned", now,
                                                conn=conn)
            # 返回级联统计（便于运维核对删干净了什么）
            return store.cascade_forget(node_id, now,
                                        evidence_ids=sorted(ev_ids),
                                        conn=conn)
    else:
        store.update_lifecycle(node_id, "tombstoned", now)
        store.add_tombstone("node", node_id, reason, now)


def restore(store, node_id, now=None, config=None):
    node = next((n for n in store.fetch_nodes() if n.nid == node_id), None)
    if node is None:
        raise NotFoundError("E006 目标不存在")
    if node.lifecycle == "deleted":
        raise ValidationError("E004 deleted 不可恢复，请重建")
    now = now or datetime.now()
    store.update_lifecycle(node_id, "active", now)
    # 同时重置 life：只改 lifecycle 的话 life 仍是 0，下一次 step_day
    # 立刻重新归档（测试只断言 restore 后的 lifecycle，所以一直没发现）
    if node.decay_rate:
        d = None
        if config is not None:
            d = config.event_defaults.get(node.kind) \
                or config.event_defaults.get("transient")
        life = d.life if d is not None else max(1.0, node.decay_rate * 10)
        store.update_life(node_id, life)


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
            key=lambda f: (
                # 「已生效」是**首**要判据：未来事实可以参与角逐，但绝不能
                # 压过当前值——否则 supersede 会把当前值写死 invalid_at=now，
                # 而未来值当下又未生效，该 key 的当前状态直接变空
                # （查询端把 valid_at>now 归 unknown，两边语义不一致）
                f.valid_at is None or f.valid_at <= now,
                config.source_rank.get(f.source, 0),
                f.confidence,
                bool(getattr(f, "explicit_confirmation", False)),
                f.valid_at or datetime.min),
            reverse=True)
        top, second = ordered[0], ordered[1]
        same_rank = (config.source_rank.get(top.source)
                     == config.source_rank.get(second.source))
        # 值完全相同的重复行不是冲突（无互斥可裁决）：判成 confirm 的话，
        # `/confirm` 按 value 定位会让 winner/loser 落到同一行，自 supersede
        # 把该值从当前状态里抹掉
        distinct_values = len({f.value for f in active})
        if same_rank and distinct_values > 1 \
                and top.confidence >= config.conflict_confirm_threshold \
                and second.confidence >= config.conflict_confirm_threshold:
            decisions.append(ConflictDecision("confirm", nid, key, top, second))
            # 0.8.1 IA-4：冲突裁决落审计（待人工确认也是裁决结论）
            store.audit(
                "conflict_resolve", "fact", top.fid,
                "同源同高置信并列，待人工确认",
                meta=json.dumps({"kind": "confirm", "node_id": nid,
                                 "key": key, "winner": top.fid,
                                 "loser": second.fid}, ensure_ascii=False),
                at=now)
        else:
            # 裁决依据与 FactResolver 同一口径（resolve.adjudication_reason）
            reason = adjudication_reason(top, second, config)
            for f in active:
                if f is not top:
                    store.supersede_fact(f.fid, top.fid, now, reason=reason)
            decisions.append(ConflictDecision("resolved", nid, key, top, second))
            store.audit(
                "conflict_resolve", "fact", top.fid, reason,
                meta=json.dumps({"kind": "resolved", "node_id": nid,
                                 "key": key, "winner": top.fid,
                                 "loser": second.fid}, ensure_ascii=False),
                at=now)
    return decisions


def confirm(store, decision, choice_value, now):
    if decision.kind != "confirm":
        raise ValidationError("E008 该决策不需要确认")
    if decision.winner.value == decision.loser.value:
        # 同值重复行没有可裁决的互斥（`/confirm` 按 value 定位会自 supersede，
        # 把该值从当前状态里抹掉）
        raise ValidationError("E008 冲突双方值相同，无需确认")
    # 0.8.1 IA-4：人工确认的裁决依据透传进 supersede 审计
    reason = f"人工确认 choice={choice_value}"
    if decision.winner.value == choice_value:
        store.supersede_fact(decision.loser.fid, decision.winner.fid, now,
                             reason=reason)
    elif decision.loser.value == choice_value:
        store.supersede_fact(decision.winner.fid, decision.loser.fid, now,
                             reason=reason)
    else:
        raise ValidationError("E001 choice 不在候选中")


def reflect_monthly(store, config, year, month, now, extractor=None):
    """确定性规则版月度反射：摘要节点 + summarizes 边 + part_of 簇。"""
    events = [n for n in store.fetch_nodes()
              if n.node_type == "event" and n.lifecycle == "active"
              and n.kind not in ("summary", "episode", "stable")
              and n.ts is not None and n.ts.year == year and n.ts.month == month]
    if not events:
        return None
    mean_score = sum(n.value_score for n in events) / len(events)
    # 0.6.0 Reflection 2.0：本月状态维度源（facts/beliefs/intents）
    state_sources = []
    for f in store.fetch_facts():
        if not f.tombstoned and f.valid_at is not None \
                and f.valid_at.year == year and f.valid_at.month == month:
            state_sources.append(f)
    for b in store.fetch_beliefs():
        if b.lifecycle == "active" and b.valid_at is not None \
                and b.valid_at.year == year and b.valid_at.month == month:
            state_sources.append(b)
    for i in store.fetch_intents():
        if i.lifecycle == "active" and i.valid_at is not None \
                and i.valid_at.year == year and i.valid_at.month == month:
            state_sources.append(i)
    node_labels = {n.nid: n.access_label for n in store.fetch_nodes()}

    def _label_of(m):
        if isinstance(m, Fact):
            return node_labels.get(m.node_id, "public")
        return getattr(m, "access_label", "public")

    label = _max_label(list(events) + state_sources, _label_of)
    # 摘要证据并集：Summary → 源记忆 → Original Evidence 反向追溯最后一跳
    ev_union = sorted({eid
                       for m in list(events) + state_sources
                       for eid in (getattr(m, "evidence_ids", None) or [])})
    summary = store.insert_node(
        "event", "summary", f"{year}-{month:02d} 月度摘要", "",
        now, round(mean_score * 0.8, 4), False, label,
        config.event_defaults.get("core").life,
        config.event_defaults.get("core").decay_rate,
        now, evidence_ids=ev_union,
        idempotency_key=f"summary:{year}:{month}")
    for e in events:
        store.insert_edge(summary, e.nid, "summarizes", 0.9, 0.9,
                          e.ts, None, now,
                          idempotency_key=f"sum:{summary}:{e.nid}")
    # 状态维度 summarize 链走 memory_links（跨表 id 用 source_type 区分）
    for m in state_sources:
        if isinstance(m, Fact):
            stype, mid = "fact", m.fid
        elif isinstance(m, Belief):
            stype, mid = "belief", m.id
        else:
            stype, mid = "intent", m.id
        store.insert_memory_link(
            mid, summary, stype, "summarizes", 0.8, now, None,
            idempotency_key=f"sumstate:{stype[0]}:{summary}:{mid}")
    cluster = store.insert_node(
        "entity", "cluster", f"{year}-{month:02d} 脉络", "",
        None, 0.6, False, label,
        0.0, 0.0, now, idempotency_key=f"cluster:{year}:{month}")
    store.insert_edge(summary, cluster, "part_of", 0.8, 0.9,
                      now, None, now,
                      idempotency_key=f"po:{summary}:{cluster}")
    _build_theme_clusters(store, events, summary, year, month, now)
    _build_episodes(store, config, events, summary, year, month, now)
    for d in resolve_conflicts(store, config, now):
        store.audit(
            "fact_convergence", target_type="fact", target_id=d.node_id,
            reason=d.kind,
            meta=json.dumps({"fact_key": d.fact_key,
                             "winner": str(d.winner.value),
                             "loser": str(d.loser.value)},
                            ensure_ascii=False),
            at=now)
    # 0.7.0 P3：个人模式提取（只写 patterns 表，不改变反射返回值/
    # 节点行为；全幂等）。
    extract_patterns(store, config, now=now)
    return summary


def extract_patterns(store, config, now=None):
    """0.7.0 P3：个人长期模式规则提取（文档 §17，确定性四条）。

    全部 source="inferred"（总结/推断不伪装原始事实）+ 支撑证据并集；
    全幂等（insert_pattern 按 proposition 哈希键）；只写 patterns 表，
    不触碰任何原始记忆。返回本次新生成数（幂等重跑返回 0 或已存在数）。

    1. Impact Pattern：同 (subject, dimension) ≥2 条同向同 valence 且
       magnitude≥0.5 → 持续型影响；
    2. Temporal Pattern：stress 维 negative impact 持续 ≥4 周后 ±14 天
       内出现离职类 active intent → 共现模式；
    3. Preference：同 (entity, canonical_key) 事实同值跨 ≥2 个不同月份；
    4. Behavior Pattern：近 6 个月同 kind 事件 ≥3 条。
    """
    from datetime import timedelta

    from .resolve import canonical_fact_key

    now = now or datetime.now()
    created = 0

    def _add(subject_id, ptype, proposition, support, ev_ids,
             window_days=None, confidence=0.7):
        nonlocal created
        rows = store.fetch_patterns()
        for p in rows:
            if p.pattern_type == ptype and p.subject_id == subject_id \
                    and p.proposition == proposition:
                return
        store.insert_pattern(subject_id, ptype, proposition,
                             confidence=confidence, support=support,
                             window_days=window_days,
                             evidence_ids=sorted(ev_ids),
                             created_at=now)
        created += 1

    # 1. Impact Pattern
    impacts = store.fetch_impacts()
    groups = {}
    for im in impacts:
        if im.magnitude < 0.5:
            continue
        groups.setdefault((im.subject_id, im.dimension,
                           im.direction, im.valence), []).append(im)
    for (sid, dim, direction, valence), items in groups.items():
        if len(items) < 2:
            continue
        ev_ids = {e for im in items for e in (im.evidence_ids or [])}
        direction_cn = {"increase": "上升", "decrease": "下降",
                        "stable": "保持稳定", "appear": "出现",
                        "disappear": "消失"}.get(direction, direction)
        valence_cn = {"positive": "正面", "negative": "负面",
                      "neutral": "中性", "mixed": "混合",
                      "unknown": "未知"}.get(valence, valence)
        _add(sid, "impact",
             f"该主体在 {dim} 维度上持续{direction_cn}（{valence_cn}）",
             len(items), ev_ids)
    # 2. Temporal Pattern（压力窗口 + 离职意图共现）
    intents = store.fetch_intents()
    stress_by_subject = {}
    for im in impacts:
        if im.dimension in ("stress", "压力") and im.valence == "negative":
            stress_by_subject.setdefault(im.subject_id, []).append(im)
    win = getattr(config, "pattern_window_days", 28)
    for sid, stress_items in stress_by_subject.items():
        if len(stress_items) < 2:
            continue
        span = max(_t(im) for im in stress_items) \
            - min(_t(im) for im in stress_items)
        if span < timedelta(days=win):
            continue
        end_t = max(_t(im) for im in stress_items)
        quit_words = ("离职", "离开", "辞职", "换工作", "换城市")
        hits = 0
        for i in intents:
            if i.subject_id != sid or i.status != "active":
                continue
            t = _t(i)
            if t is None or not (abs((t - end_t).days) <= 14):
                continue
            if any(w in i.proposition for w in quit_words):
                hits += 1
        if hits < 1:
            continue
        ev_ids = {e for im in stress_items for e in (im.evidence_ids or [])}
        _add(sid, "temporal", "高压力持续后出现离职意图", hits, ev_ids,
             window_days=win)
    # 3. Preference（同 (entity, canonical_key) 事实同值跨 ≥2 月）
    facts = [f for f in store.fetch_facts() if not f.tombstoned]
    fgroups = {}
    for f in facts:
        if f.valid_at is None:
            continue
        fgroups.setdefault(
            (f.node_id, canonical_fact_key(f.key, config), f.value),
            set()).add(f.valid_at.month)
    for (nid, _ckey, value), months in fgroups.items():
        if len(months) < 2:
            continue
        node = next((n for n in store.fetch_nodes() if n.nid == nid), None)
        if node is None:
            continue
        _add(nid, "preference", f"偏好 {value}", len(months), set())
    # 4. Behavior Pattern（近 6 个月同 kind 事件 ≥3 条）
    months_ago = now - timedelta(days=180)
    nodes = store.fetch_nodes()
    kinds = {}
    for n in nodes:
        if n.node_type != "event" or n.kind in ("summary", "episode",
                                                "stable"):
            continue
        if n.ts is not None and n.ts < months_ago:
            continue
        kinds.setdefault(n.kind, []).append(n)
    for kind, evs in kinds.items():
        if len(evs) < 3:
            continue
        # 主体 → 该主体自己的事件。原实现对每个「邻接主体」都生成一行、
        # support 写的是全库该 kind 的事件数（kind × 邻居的笛卡尔积，
        # 实测 330 行只有 13 个命题、286 个主体），偏离「模式属于主体」
        # 的设计（docs/业务与项目的演进.md §17）。
        by_subject = {}
        for ev in evs:
            for e in store.fetch_edges():
                if e.lifecycle != "active":
                    continue
                other = e.from_id if e.to_id == ev.nid else (
                    e.to_id if e.from_id == ev.nid else None)
                if other is not None:
                    by_subject.setdefault(other, set()).add(ev.nid)
        for sid, own_ids in by_subject.items():
            if len(own_ids) < 3:      # 该主体自己的事件不足 → 不构成模式
                continue
            _add(sid, "behavior", f"经常进行 {kind} 类活动",
                 len(own_ids), own_ids)
    return created


def _t(m):
    return getattr(m, "valid_at", None) or getattr(m, "created_at", None) \
        or datetime.min


def _max_label(nodes, key=None):
    rank = {"public": 0, "private": 1, "sensitive": 2}
    key = key or (lambda x: getattr(x, "access_label", "public"))
    return max((key(n) for n in nodes),
               key=lambda x: rank.get(x, 0), default="public")


def _build_episodes(store, config, events, summary, year, month, now):
    """0.6.0 R-01：事件按共享实体聚类（并查集，共享 ≥1 实体合并）
    → episode 节点 + part_of 边；继承最高敏感级与证据并集（幂等）。"""
    nodes = {n.nid: n for n in store.fetch_nodes()}
    ev_entities = {}
    for ev in events:
        ents = set()
        for e in store.fetch_edges():
            if e.lifecycle != "active":
                continue
            other = None
            if e.from_id == ev.nid:
                other = nodes.get(e.to_id)
            elif e.to_id == ev.nid:
                other = nodes.get(e.from_id)
            if other is not None and other.node_type == "entity":
                ents.add(other.nid)
        ev_entities[ev.nid] = ents
    ev_ids = sorted(ev.nid for ev in events)
    parent = {i: i for i in ev_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(ev_ids)):
        for j in range(i + 1, len(ev_ids)):
            a, b = ev_ids[i], ev_ids[j]
            if ev_entities[a] & ev_entities[b]:
                union(a, b)
    clusters = {}
    for eid in ev_ids:
        clusters.setdefault(find(eid), []).append(eid)
    for idx, members in enumerate(sorted(clusters.values())):
        label = _max_label([nodes[m] for m in members])
        ev_union = sorted({x for m in members
                           for x in (nodes[m].evidence_ids or [])})
        ep = store.insert_node(
            "event", "episode", f"{year}-{month:02d} 片段{idx + 1}", "",
            now, 0.5, False, label,
            config.event_defaults.get("core").life,
            config.event_defaults.get("core").decay_rate,
            now, evidence_ids=ev_union,
            idempotency_key=f"episode:{year}:{month}:{idx}")
        for m in sorted(members):
            store.insert_edge(m, ep, "part_of", 0.7, 0.8,
                              nodes[m].ts or now, None, now,
                              idempotency_key=f"ep:{ep}:{m}")
        store.insert_edge(ep, summary, "part_of", 0.8, 0.9, now, None, now,
                          idempotency_key=f"epsum:{ep}:{summary}")


def compress_stable(store, config, now):
    """0.6.0 R-02：跨月稳定记忆聚合。

    同 (entity,key) 事实链被 ≥2 个月度摘要追溯（经 summarize
    memory_links）→ stable 节点（当前版本值 + 证据并集）；单月不生成；
    幂等重跑不重复。压缩不删除原始记忆（设计稿 §11/§21）。
    """
    facts = [f for f in store.fetch_facts() if not f.tombstoned]
    links = store.fetch_memory_links()
    by_fact = {}
    for l in links:
        if l.relation == "summarizes" and l.source_type == "fact":
            by_fact.setdefault(l.source_id, set()).add(l.target_id)
    nodes = {n.nid: n for n in store.fetch_nodes()}
    groups = {}
    for f in facts:
        groups.setdefault((f.node_id, f.key), []).append(f)
    created = 0
    for (_nid, _key), fs in groups.items():
        summaries = set()
        for f in fs:
            summaries |= by_fact.get(f.fid, set())
        if len(summaries) < 2:
            continue
        cur = max(fs, key=lambda x: x.valid_at)
        if store.read("SELECT id FROM nodes WHERE idempotency_key=?",
                      (f"stable:fact:{cur.fid}",)):
            continue
        label = nodes.get(cur.node_id, None)
        access = label.access_label if label else "public"
        ev_union = sorted(cur.evidence_ids or [])
        stable = store.insert_node(
            "event", "stable", f"稳定记忆：{cur.key}={cur.value}", "",
            now, 0.8, False, access,
            config.event_defaults.get("core").life,
            config.event_defaults.get("core").decay_rate,
            now, evidence_ids=ev_union,
            idempotency_key=f"stable:fact:{cur.fid}")
        store.insert_memory_link(
            cur.fid, stable, "fact", "summarizes", 0.9, now, None,
            idempotency_key=f"stablelink:{cur.fid}:{stable}")
        created += 1
    return created


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
