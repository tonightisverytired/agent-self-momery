# -*- coding: utf-8 -*-
"""上下文构建（0.5.0，设计稿 §8/§11/§13）。

候选四类扩展（Entity/FactVersion/TemporalNeighbor/Evidence）→ 证据校验
（无证据降置信/abstain，inferred 不得伪装为 Fact）→ 按 query_type 组装
MemoryContext。LLM 只能在 MemoryContext 给出的状态与证据范围内组织语言。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import Belief, Fact, Intent


def _mem_key(m):
    return (type(m).__name__,
            getattr(m, "nid", getattr(m, "fid", getattr(m, "id", None))))


@dataclass
class MemoryContext:
    """交给 LLM 的结构化上下文（不再是裸 Top-K）。"""

    query_type: str = "semantic_recall"
    current_state: list = field(default_factory=list)
    recent_events: list = field(default_factory=list)
    historical_changes: list = field(default_factory=list)
    beliefs: list = field(default_factory=list)
    intents: list = field(default_factory=list)
    temporal_chains: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    causes: list = field(default_factory=list)  # why_change 因果链
    # 0.7.0 P1/P2/P3
    impacts: list = field(default_factory=list)        # 当前影响
    impact_chains: list = field(default_factory=list)  # 影响链（P2）
    patterns: list = field(default_factory=list)       # 个人模式（P3）
    score: Optional[object] = None  # MemoryScore（仅 recall_context 管线）
    trace: Optional[object] = None  # RecallTrace（仅 recall_context 管线）
    # 0.8.1 IA-4：单记忆分项分（MemoryScore）按记忆 id 透出。key 统一为
    # ``str(fid)``（Fact.fid 的字符串化）而非 _mem_key 元组——JSON 对象的
    # key 必须是字符串，序列化层直接 ``score_by_item.get(str(f.fid))``
    # 匹配 current_state/historical_changes 各项，无需再拆元组。
    score_by_item: Optional[dict] = None


def expand(store, candidates, limit=200, filters=None):
    """候选四类扩展（设计稿 §8.2）：去重、防环、有上限、**过访问控制**。

    - FactVersion：同 (node,key) 全部历史版本；
    - Entity：同实体其它维度（事实/事件邻接）；
    - TemporalNeighbor：memory_links 邻接（按 source_type 定位对象）；
    - Evidence：evidence_ids → evidence 行。

    访问控制：邻接扩展是「从可见对象走到相邻对象」，必须重新过闸——
    原实现直接 `queue.append(nodes[...])`，会把 sensitive / 已删除的事件
    灌进 `state.events`（`recall()` 层的兜底管不到这条路径）。
    """
    from .retrieval import _access_allowed
    from .models import RecallFilters
    filters = filters or RecallFilters()
    seen, out, queue = set(), [], list(candidates)
    nodes = {n.nid: n for n in store.fetch_nodes()}
    visible = {nid for nid, n in nodes.items()
               if _access_allowed(n, filters)}
    facts = [f for f in store.fetch_facts() if f.node_id in visible]
    beliefs = {b.id: b for b in store.fetch_beliefs()
               if getattr(b, "subject_id", None) in visible}
    intents = {i.id: i for i in store.fetch_intents()
               if getattr(i, "subject_id", None) in visible}
    links = store.fetch_memory_links()
    ev_by_id = {e.id: e for e in store.fetch_evidence()}
    while queue and len(out) < limit:
        c = queue.pop(0)
        key = _mem_key(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if isinstance(c, Fact):
            # FactVersion Expansion：同 (node,key) 全部版本
            for f in facts:
                if f.node_id == c.node_id and f.key == c.key:
                    queue.append(f)
        elif getattr(c, "node_type", None) == "entity":
            # Entity Expansion：同实体事实 + 关联事件
            for f in facts:
                if f.node_id == c.nid:
                    queue.append(f)
            for e in store.fetch_edges():
                if e.from_id == c.nid and e.to_id in visible:
                    queue.append(nodes[e.to_id])
                elif e.to_id == c.nid and e.from_id in visible:
                    queue.append(nodes[e.from_id])
        # TemporalNeighbor Expansion：memory_links 邻接（防环靠 seen）
        mid = getattr(c, "nid", getattr(c, "fid", getattr(c, "id", None)))
        if mid is not None:
            for l in links:
                if l.target_id != mid:
                    continue
                if l.source_type == "belief" and l.source_id in beliefs:
                    queue.append(beliefs[l.source_id])
                elif l.source_type == "fact":
                    for f in facts:
                        if f.fid == l.source_id:
                            queue.append(f)
                elif l.source_type == "intent" and l.source_id in intents:
                    queue.append(intents[l.source_id])
                elif l.source_type == "event" and l.source_id in visible:
                    queue.append(nodes[l.source_id])
        # Evidence Expansion：evidence_ids → evidence 行
        for eid in (getattr(c, "evidence_ids", None) or []):
            if eid in ev_by_id:
                queue.append(ev_by_id[eid])
        # Impact Expansion（0.7.0 P1）：cause_event_id → 关联事件节点
        cause_id = getattr(c, "cause_event_id", None)
        if cause_id is not None and cause_id in visible:
            queue.append(nodes[cause_id])
    return out


@dataclass
class ValidatedMemory:
    """证据校验后的记忆：置信度调整 + 推断标记 + 五项检查状态。"""

    memory: object
    confidence: float
    grounded: bool
    inferred: bool
    accessible: bool = False
    trusted: bool = True
    consistent: bool = True
    timely: bool = True


def _evidence_allowed(e, filters):
    """证据访问控制：sensitive 默认隐藏（与节点 access_label 语义一致）。"""
    allowed = set(filters.access_labels) if filters and filters.access_labels \
        else {"public", "private"}
    return getattr(e, "access_label", "public") in allowed


def validate(memory, evidence_rows, filters=None, config=None):
    """五项检查（设计稿 §11.2）：

    ① exists：有证据；② accessible：证据经 access_label 过滤可见；
    ③ trusted：trust_level ≥ config.evidence_min_trust；
    ④ consistent：记忆与证据 content_hash 均存在时须一致；
    ⑤ timely：observed_at 不晚于记忆时间。

    无证据/不可访问 → 视同无证据降置信（不得伪装为事实）；
    其余任一不过 → confidence × 0.85；inferred → 强制标记推断。
    """
    from .models import RecallFilters
    filters = filters or RecallFilters()
    conf = getattr(memory, "confidence", 0.7)
    grounded = bool(evidence_rows)
    if not grounded:
        return ValidatedMemory(memory=memory, confidence=round(conf * 0.6, 4),
                               grounded=False, inferred=False,
                               accessible=False)
    accessible_rows = [e for e in evidence_rows
                       if _evidence_allowed(e, filters)]
    if not accessible_rows:
        return ValidatedMemory(memory=memory, confidence=round(conf * 0.6, 4),
                               grounded=False, inferred=False,
                               accessible=False)
    inferred = any(e.source_type == "inferred" for e in accessible_rows)
    min_trust = getattr(config, "evidence_min_trust", 1.0) if config else 1.0
    trusted = all((getattr(e, "trust_level", None) is None)
                  or (getattr(e, "trust_level", None) >= min_trust)
                  for e in accessible_rows)
    mem_hash = getattr(memory, "content_hash", None)
    consistent = not any(
        mem_hash and getattr(e, "content_hash", None)
        and mem_hash != getattr(e, "content_hash", None)
        for e in accessible_rows)
    mem_time = (getattr(memory, "created_at", None)
                or getattr(memory, "valid_at", None))
    timely = not any(getattr(e, "observed_at", None) and mem_time
                     and getattr(e, "observed_at", None) > mem_time
                     for e in accessible_rows)
    adj = conf
    if not (trusted and consistent and timely):
        adj = round(conf * 0.85, 4)
    return ValidatedMemory(memory=memory, confidence=adj,
                           grounded=True, inferred=inferred,
                           accessible=True, trusted=trusted,
                           consistent=consistent, timely=timely)


class ContextBuilder:
    """按 query_type 组装 MemoryContext；分块预算裁剪（§13）。

    预算语义：`conflicts` 计**冲突组**数（不是 fact 条数，否则会把一个
    并列组从中间腰斩）；`causes` 由 recall_context 填充，此处只提供上限。
    """

    DEFAULT_BUDGETS = {"current_state": 10, "recent_events": 10,
                       "historical_changes": 20, "beliefs": 10,
                       "intents": 10, "temporal_chains": 5,
                       "evidence": 20, "conflicts": 10, "causes": 10,
                       "impacts": 8, "impact_chains": 5, "patterns": 5}

    def __init__(self, config):
        self.config = config

    def build(self, memory_state, query_type="semantic_recall", budgets=None,
              chains=None, notes=None, scores=None, prefer_nids=None) -> MemoryContext:
        b = dict(self.DEFAULT_BUDGETS)
        b.update(budgets or {})
        ctx = MemoryContext(query_type=query_type)
        facts = list(memory_state.current_facts)
        # §22/§23：current_state 按状态层 final 分排序。排序必须覆盖**全量**
        # 候选后再取预算——先 `[:10]` 再排序只是「前 10 名内部重排」，更相关
        # 的事实永远捞不回来（其余区块保持时间确定性语义序）
        if scores:
            def _sc(f):
                return scores.get(_mem_key(f))

            def _rank(f):
                sc = _sc(f)
                return -(sc.final() if sc is not None else 0.0)

            def _rel(f):
                sc = _sc(f)
                return sc.retrieval_score if sc is not None else 0.0

            facts_sorted = sorted(facts, key=lambda f: (_rank(f),
                                                        getattr(f, "fid", 0)))
            # 两阶段收口（0.8.1）：查询相关事实（retrieval>0）先进预算，
            # 无关事实按 final 补位。状态裁决仍是全库确定性（§30），
            # 这里只决定**展示预算**的分配，不影响谁当前有效
            relevant = [f for f in facts_sorted if _rel(f) > 0]
            rest = [f for f in facts_sorted if _rel(f) <= 0]
            facts = relevant + rest
        # 主体偏好（0.8.2）：查询点名的实体（person 等锚定节点）的事实
        # 优先占预算——多主体库下同族事实成百上千，不偏好会让被问主体
        # 的答案事实被其它主体的同族事实挤出 current_state
        if prefer_nids:
            pri = [f for f in facts if getattr(f, "node_id", None)
                   in prefer_nids]
            oth = [f for f in facts if getattr(f, "node_id", None)
                   not in prefer_nids]
            facts = pri + oth
        ctx.current_state = facts[:b["current_state"]]
        ctx.historical_changes = \
            memory_state.historical_facts[:b["historical_changes"]]
        ctx.recent_events = memory_state.events[:b["recent_events"]]
        ctx.beliefs = memory_state.beliefs[:b["beliefs"]]
        ctx.intents = memory_state.intents[:b["intents"]]
        ctx.conflicts = memory_state.conflicts[:b["conflicts"]]
        ctx.evidence = memory_state.evidence[:b["evidence"]]
        ctx.temporal_chains = (chains or [])[:b["temporal_chains"]]
        ctx.impacts = getattr(memory_state, "impacts", [])[:b["impacts"]]
        ctx.patterns = getattr(memory_state, "patterns", [])[:b["patterns"]]
        ctx.notes = list(notes or [])
        return ctx
