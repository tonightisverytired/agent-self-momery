# -*- coding: utf-8 -*-
"""记忆状态解析层（0.5.0，核心模块）。

QueryRouter（确定性查询意图路由）+ FactResolver（当前/历史/冲突/未知四区
确定性裁决）+ MemoryStateResolver（维度编排，局部失败不阻断）。

原则：系统拥有记忆状态，LLM 只负责语言。当前事实由确定性规则解析，
不使用向量相似度决定，也不依赖 LLM。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .models import MemoryState, RecallFilters
from .retrieval import _access_allowed  # 复用统一访问控制（不重写）

QUERY_TYPE_CURRENT = ("当前", "现在", "目前", "最新")
QUERY_TYPE_HISTORY = ("以前", "曾经", "历史", "过去")
QUERY_TYPE_TIMELINE = ("什么时候", "时间线", "先后", "顺序")
QUERY_TYPE_WHY = ("为什么", "为何", "为啥", "怎么变", "为什么变")
CHANGE_WORDS = ("变", "改", "换", "迁", "离开", "放弃", "考虑", "搬")


def query_router(text: Optional[str]) -> str:
    """确定性中文关键词路由（无 LLM 可运行）。

    规则顺序：current_state > history > timeline > why_change/change
    > semantic_recall（默认）。
    """
    t = (text or "").strip()
    if not t:
        return "semantic_recall"
    if any(k in t for k in QUERY_TYPE_CURRENT):
        return "current_state"
    if any(k in t for k in QUERY_TYPE_HISTORY):
        return "history"
    if any(k in t for k in QUERY_TYPE_TIMELINE):
        return "timeline"
    if any(k in t for k in QUERY_TYPE_WHY):
        if any(w in t for w in CHANGE_WORDS):
            return "why_change"
        return "change"
    if any(w in t for w in CHANGE_WORDS):
        return "change"
    return "semantic_recall"


class FactResolver:
    """同一 (entity, key) 事实组的确定性裁决（设计稿 §9.3）。

    裁决链（以写入端固化的 superseded_by/invalid_at 为准，查询端只补
    freshness/explicit_confirmation）：
    过滤（deleted/tombstoned/访问控制）→ 时间有效集 → source_rank →
    explicit_confirmation → confidence → freshness；仍并列且同源高置信
    → conflict。
    """

    def __init__(self, config):
        self.config = config

    def resolve(self, store, query_time=None, filters=None,
                entity_scope=None):
        filters = filters or RecallFilters()
        now = query_time or datetime.now()
        nodes = {n.nid: n for n in store.fetch_nodes()}
        facts = [f for f in store.fetch_facts() if not f.tombstoned]
        # 访问控制复用 retrieval._access_allowed（含 deleted/tombstoned/
        # archived/sensitive 语义）
        visible = {nid for nid, n in nodes.items()
                   if _access_allowed(n, filters)}
        facts = [f for f in facts if f.node_id in visible]
        if entity_scope:
            scope = set(entity_scope)
            facts = [f for f in facts if f.node_id in scope]
        groups = {}
        for f in facts:
            groups.setdefault((f.node_id, f.key), []).append(f)
        current, history, conflict, unknown = [], [], [], []
        for (_nid, _key), fs in groups.items():
            cur, hist, conf, unk = self._resolve_group(fs, now)
            current.extend(cur)
            history.extend(hist)
            conflict.extend(conf)
            unknown.extend(unk)
        history.sort(key=lambda f: (f.valid_at, f.fid))
        return {"current": current, "history": history,
                "conflict": conflict, "unknown": unknown}

    def _resolve_group(self, fs, now):
        valid, expired, future = [], [], []
        for f in fs:
            if f.valid_at > now:
                future.append(f)
            elif f.invalid_at is not None and f.invalid_at <= now:
                expired.append(f)
            else:
                valid.append(f)
        history = list(expired)
        unknown = list(future)
        if not valid:
            return [], history, [], unknown
        # 写入端已固化的 superseded_by（若仍落入有效集，防御性归历史）
        superseded = [f for f in valid if f.superseded_by is not None]
        contenders = [f for f in valid if f.superseded_by is None]
        history.extend(sorted(superseded, key=lambda f: f.valid_at))
        if not contenders:
            return [], history, [], unknown
        rank = self.config.source_rank
        ordered = sorted(
            contenders,
            key=lambda f: (rank.get(f.source, 0),
                           bool(getattr(f, "explicit_confirmation", False)),
                           f.confidence, f.valid_at),
            reverse=True)
        top, second = ordered[0], (ordered[1] if len(ordered) > 1 else None)
        if second is not None \
                and rank.get(top.source, 0) == rank.get(second.source, 0) \
                and top.confidence >= self.config.conflict_confirm_threshold \
                and second.confidence >= self.config.conflict_confirm_threshold:
            # 同源同高置信：查询端冲突态，交 ConflictResolver/人工裁决
            return [], history, ordered, unknown
        return [top], history + ordered[1:], [], unknown


class BeliefResolver:
    """观点演化时间线（设计稿 §9.4）：不覆盖历史，全部保留。

    同一 subject 的 beliefs 按 valid_at 成时间序列；memory_links 的
    supersedes/contradicts/changes/reinforces 标注演化类型；当前观点
    = 时间最新且未被 supersede 者。
    """

    def __init__(self, config):
        self.config = config

    def resolve(self, store, query_time=None, filters=None):
        now = query_time or datetime.now()
        filters = filters or RecallFilters()
        beliefs = [b for b in store.fetch_beliefs()
                   if _access_allowed(b, filters)]
        links = [l for l in store.fetch_memory_links()
                 if l.source_type == "belief"]
        # supersedes A→B 表示 B 替代 A：A（source）被 supersede
        superseded_ids = {l.source_id for l in links
                          if l.relation == "supersedes"}
        superseded_ids |= {b.id for b in beliefs
                           if b.superseded_by is not None}
        active, history = [], []
        for b in beliefs:
            expired = b.invalid_at is not None and b.invalid_at <= now
            future = b.valid_at is not None and b.valid_at > now
            if b.id in superseded_ids or expired or future:
                history.append(b)
            else:
                active.append(b)
        order = lambda b: (b.valid_at or b.created_at or datetime.min, b.id)
        active.sort(key=order)
        # 非最新的活跃观点同样是历史观点（观点演化链不覆盖历史）
        current = []
        if active:
            current = [active[-1]]
            history.extend(active[:-1])
        history.sort(key=order)
        changes = [(l.source_id, l.target_id, l.relation, l.valid_at)
                   for l in links
                   if l.relation in ("supersedes", "contradicts", "changes",
                                     "reinforces")]
        return {"current": current, "history": history, "changes": changes}


class IntentResolver:
    """意图状态解析（设计稿 §9.5）：status + 时间有效性。

    active 且时间有效 → 当前意图；completed/cancelled/expired/superseded
    或已过期 → 历史意图。过期计划不得当现在意图输出。
    """

    def __init__(self, config):
        self.config = config

    def resolve(self, store, query_time=None, filters=None):
        now = query_time or datetime.now()
        filters = filters or RecallFilters()
        current, history = [], []
        for i in store.fetch_intents():
            if not _access_allowed(i, filters):
                continue
            valid = (i.valid_at is None or i.valid_at <= now) \
                and (i.invalid_at is None or i.invalid_at > now)
            if i.status == "active" and valid:
                current.append(i)
            else:
                history.append(i)
        order = lambda i: (i.valid_at or i.created_at or datetime.min, i.id)
        current.sort(key=order)
        history.sort(key=order)
        return {"current": current, "history": history}


class ConflictResolver:
    """四类冲突分类（设计稿 §5.4）：不同类型采用不同治理策略。

    - hard：同 (entity,key) 同有效时刻互斥（真冲突，交人工/写入端裁决）
    - temporal：不同 valid_at 各自成立（不算真冲突，保留历史即可）
    - source：不同来源分歧（按 source_rank 治理）
    - soft：观点/偏好差异（进 BeliefTimeline，不覆盖）
    """

    @staticmethod
    def classify(memories, dimension="fact"):
        """memories：同 (entity,key) 或同主体的多个候选。

        返回 (kind, 说明)；不足两个候选时返回 (None, None)。
        """
        if len(memories) < 2:
            return None, None
        if dimension == "belief":
            return "soft", "观点差异：进入观点时间线，不覆盖历史"
        valid_ats = {getattr(m, "valid_at", None) for m in memories}
        sources = {getattr(m, "source", None) for m in memories}
        if len(valid_ats) > 1:
            return "temporal", "不同时间各自成立，保留历史即一致"
        if len(sources) > 1:
            return "source", "不同来源给出不同结果，按来源可信级治理"
        return "hard", "同一有效时刻互斥，需人工裁决"


class MemoryStateResolver:
    """维度编排：fact 全量裁决 + event 候选 + belief/intent 时间线。

    局部失败不阻断全流程：单维度异常只留空该维度。
    """

    def __init__(self, store, config):
        self.store = store
        self.config = config
        self.fact_resolver = FactResolver(config)
        self.belief_resolver = BeliefResolver(config)
        self.intent_resolver = IntentResolver(config)

    def resolve(self, candidates, query_time=None,
                query_type="semantic_recall", entity_scope=None,
                filters=None) -> MemoryState:
        now = query_time or datetime.now()
        state = MemoryState()
        # fact 维度：全量分组裁决（candidates 为入口，扩展见 context.py）
        try:
            result = self.fact_resolver.resolve(
                self.store, now, filters=filters, entity_scope=entity_scope)
            state.current_facts = result["current"]
            state.historical_facts = result["history"]
            state.conflicts = result["conflict"]
        except Exception:  # noqa: BLE001 局部失败不阻断
            state.conflicts = []
        # event 维度：候选中的事件节点
        state.events = [c for c in candidates
                        if getattr(c, "node_type", None) == "event"]
        # belief 维度：观点演化时间线（异常不阻断）
        try:
            res = self.belief_resolver.resolve(self.store, now, filters)
            state.beliefs = res["current"]
            state.changes = res["changes"]
        except Exception:  # noqa: BLE001 局部失败不阻断
            state.beliefs, state.changes = [], []
        # intent 维度：当前意图（异常不阻断）
        try:
            res = self.intent_resolver.resolve(self.store, now, filters)
            state.intents = res["current"]
        except Exception:  # noqa: BLE001 局部失败不阻断
            state.intents = []
        return state
