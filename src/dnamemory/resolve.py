# -*- coding: utf-8 -*-
"""记忆状态解析层（0.5.0，核心模块）。

QueryRouter（确定性查询意图路由）+ FactResolver（当前/历史/冲突/未知四区
确定性裁决）+ MemoryStateResolver（维度编排，局部失败不阻断）。

原则：系统拥有记忆状态，LLM 只负责语言。当前事实由确定性规则解析，
不使用向量相似度决定，也不依赖 LLM。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from .models import ConflictGroup, MemoryState, RecallFilters
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


def canonical_fact_key(key, config=None):
    """同义 key 归一化（0.7.0 S0）：命中族返回 canonical，未映射恒等。

    仅作用于裁决分组与查询过滤视角，不改写已落库行（Ground Truth）。
    注意：裁决分组只并**真同义** key（key_aliases）；查询侧的宽族
    匹配（含「族_实例」前缀）在 _relevance 的 key_query_aliases，
    两侧不可混用——否则同节点的不同属性事实会互踢出 current_state。
    """
    for canonical, aliases in (config.key_aliases.items()
                               if config is not None else {}):
        if key in aliases:
            return canonical
    return key


def adjudication_reason(top, second, config):
    """裁决依据字符串（0.8.1 IA-4）：按 §9.3 裁决链顺序列出双方取值
    有差异的步骤，形如 ``source_rank: profile(4)>chat(2); confidence
    0.9>0.7``；完全并列时返回占位说明。供 FactResolver 审计与治理
    裁决（resolve_conflicts/confirm）共用同一口径。
    """
    rank = config.source_rank
    steps = []
    tr = rank.get(getattr(top, "source", ""), 0)
    sr = rank.get(getattr(second, "source", ""), 0)
    if tr != sr:
        steps.append(f"source_rank: {getattr(top, 'source', '')}({tr})>"
                     f"{getattr(second, 'source', '')}({sr})")
    tc = getattr(top, "confidence", 0.0)
    sc = getattr(second, "confidence", 0.0)
    if tc != sc:
        steps.append(f"confidence {tc}>{sc}")
    te = bool(getattr(top, "explicit_confirmation", False))
    se = bool(getattr(second, "explicit_confirmation", False))
    if te != se:
        steps.append(f"explicit_confirmation {te}>{se}")
    tv = getattr(top, "valid_at", None)
    sv = getattr(second, "valid_at", None)
    if tv != sv:
        steps.append(f"freshness {tv}>{sv}")
    return "; ".join(steps) if steps else "规则全并列（保持现有序）"


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
                entity_scope=None, audit=False):
        """全量分组裁决。``audit=True`` 时把**非平凡裁决**（多值角逐出
        胜者 / 宣告冲突）写入 audit_log（op=fact_adjudicate，含裁决依据
        字符串与规则步骤）；默认 False——本方法在 recall_context 读取路径
        上被高频调用，默认零写入开销，只有治理/批处理等调用方才应打开。
        """
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
            ckey = canonical_fact_key(f.key, self.config)
            groups.setdefault((f.node_id, ckey), []).append(f)
        current, history, conflict, unknown = [], [], [], []
        for (nid, ckey), fs in groups.items():
            cur, hist, conf, unk = self._resolve_group(
                fs, now, audit_store=store if audit else None,
                group=(nid, ckey))
            current.extend(cur)
            history.extend(hist)
            unknown.extend(unk)
            if conf:
                # 冲突以「组」为单位上报：并列 contender 的组结构不能丢
                # （下游若按 key 重新分组会把同义 key 劈开、把组腰斩）
                conflict.append(ConflictGroup(
                    node_id=nid, key=fs[0].key, canonical_key=ckey,
                    fact_ids=[f.fid for f in conf],
                    values=[f.value for f in conf]))
        history.sort(key=lambda f: (f.valid_at or datetime.min, f.fid))
        return {"current": current, "history": history,
                "conflict": conflict, "unknown": unknown}

    def _resolve_group(self, fs, now, audit_store=None, group=None):
        valid, expired, future = [], [], []
        for f in fs:
            # valid_at 可空（历史行/导入数据），不能直接与 now 比较
            if f.valid_at is not None and f.valid_at > now:
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
        history.extend(sorted(superseded,
                              key=lambda f: f.valid_at or datetime.min))
        if not contenders:
            return [], history, [], unknown
        rank = self.config.source_rank
        # 设计稿 §9.3 顺序：rank → confidence → explicit_confirmation → freshness
        ordered = sorted(
            contenders,
            key=lambda f: (rank.get(f.source, 0), f.confidence,
                           bool(f.explicit_confirmation),
                           f.valid_at or datetime.min),
            reverse=True)
        top, second = ordered[0], (ordered[1] if len(ordered) > 1 else None)
        # 值完全相同的重复行不是冲突（无互斥可裁决）：若判成冲突，两条都会
        # 从 current_state 消失且 conflicts 里又看不出谁赢——退回正常裁决
        distinct_values = len({f.value for f in ordered})
        if second is not None and distinct_values > 1 \
                and rank.get(top.source, 0) == rank.get(second.source, 0) \
                and bool(top.explicit_confirmation) \
                == bool(second.explicit_confirmation) \
                and top.confidence >= self.config.conflict_confirm_threshold \
                and second.confidence >= self.config.conflict_confirm_threshold:
            # 同源同高置信且确认状态相同：查询端冲突态，交人工裁决。
            # 第三个返回值是并列 contender，由 resolve() 包装成 ConflictGroup
            if audit_store is not None:
                nid, ckey = group
                audit_store.audit(
                    "fact_adjudicate", "fact", top.fid,
                    "同源同高置信并列，转人工裁决",
                    meta=json.dumps(
                        {"node_id": nid, "key": ckey, "outcome": "conflict",
                         "contenders": [f.fid for f in ordered],
                         "values": [f.value for f in ordered]},
                        ensure_ascii=False))
            return [], history, ordered, unknown
        if audit_store is not None and second is not None \
                and distinct_values > 1:
            # 多值角逐出胜者（败方在解析视角被压入历史，等同 supersede
            # 裁决）；单值/同值组是平凡裁决，不写审计
            nid, ckey = group
            audit_store.audit(
                "fact_adjudicate", "fact", top.fid,
                adjudication_reason(top, second, self.config),
                meta=json.dumps(
                    {"node_id": nid, "key": ckey, "outcome": "winner",
                     "winner": top.fid,
                     "losers": [f.fid for f in ordered[1:]],
                     "steps": "source_rank>confidence>"
                              "explicit_confirmation>freshness"},
                    ensure_ascii=False))
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


class ImpactResolver:
    """影响状态解析（0.7.0 P1，文档 §6/§20.6）。

    按 (subject_id, dimension) 分组——同维不同事件的 impact 共存，
    不互斥（income↑ 与 stress↑ 并行合法）；每组最新 valid_at 为
    current，其余 history；disappear 的 current 保留（表达"该维度
    已消失"）；superseded_by 与过期归历史。
    """

    def __init__(self, config):
        self.config = config

    def resolve(self, store, query_time=None, filters=None,
                entity_scope=None):
        now = query_time or datetime.now()
        filters = filters or RecallFilters()
        impacts = [i for i in store.fetch_impacts()
                   if _access_allowed(i, filters)]
        if entity_scope:
            scope = set(entity_scope)
            impacts = [i for i in impacts if i.subject_id in scope]
        groups = {}
        for i in impacts:
            groups.setdefault((i.subject_id, i.dimension), []).append(i)
        current, history = [], []
        for (_sid, _dim), items in groups.items():
            expired = [i for i in items
                       if i.invalid_at is not None and i.invalid_at <= now]
            future = [i for i in items
                      if i.valid_at is not None and i.valid_at > now]
            valid = [i for i in items
                     if i not in expired and i not in future]
            history.extend(expired + future)
            superseded = [i for i in valid if i.superseded_by is not None]
            contenders = [i for i in valid if i.superseded_by is None]
            history.extend(superseded)
            if not contenders:
                continue
            order = lambda i: (i.valid_at or i.created_at or datetime.min,
                               i.id)
            contenders.sort(key=order)
            current.append(contenders[-1])
            history.extend(contenders[:-1])
        history.sort(key=lambda i: (i.valid_at or datetime.min, i.id))
        return {"current": current, "history": history}


class PatternResolver:
    """个人模式解析（0.7.0 P3，文档 §17）。

    patterns 是总结/推断（source=inferred），仅附加区块输出，
    不参与 current_state 裁决；confidence 阈值过滤低置信模式。
    """

    def __init__(self, config):
        self.config = config

    def resolve(self, store, query_time=None, filters=None,
                entity_scope=None):
        now = query_time or datetime.now()
        threshold = getattr(self.config, "pattern_min_confidence", 0.5)
        patterns = [p for p in store.fetch_patterns()
                    if p.confidence >= threshold]
        if entity_scope:
            scope = set(entity_scope)
            patterns = [p for p in patterns if p.subject_id in scope]
        patterns.sort(key=lambda p: (p.valid_at or p.created_at
                                     or datetime.min, p.id))
        return {"current": patterns}


class MemoryStateResolver:
    """维度编排：fact 全量裁决 + event 候选 + belief/intent 时间线
    + impact 状态 + pattern（0.7.0）。

    局部失败不阻断全流程：单维度异常只留空该维度。
    """

    def __init__(self, store, config):
        self.store = store
        self.config = config
        self.fact_resolver = FactResolver(config)
        self.belief_resolver = BeliefResolver(config)
        self.intent_resolver = IntentResolver(config)
        self.impact_resolver = ImpactResolver(config)
        self.pattern_resolver = PatternResolver(config)

    def resolve(self, candidates, query_time=None,
                query_type="semantic_recall", entity_scope=None,
                filters=None, audit=False) -> MemoryState:
        now = query_time or datetime.now()
        state = MemoryState()

        def _degraded(dimension, exc):
            """局部失败不阻断，但**必须留信号**（原实现静默清空维度）。"""
            state.degraded.append({"dimension": dimension,
                                   "error": f"{type(exc).__name__}: {exc}"})

        # fact 维度：全量分组裁决（candidates 为入口，扩展见 context.py）
        try:
            result = self.fact_resolver.resolve(
                self.store, now, filters=filters, entity_scope=entity_scope,
                audit=audit)
            state.current_facts = result["current"]
            state.historical_facts = result["history"]
            state.conflicts = result["conflict"]
        except Exception as e:  # noqa: BLE001 局部失败不阻断
            _degraded("fact", e)
            state.current_facts, state.historical_facts, state.conflicts = \
                [], [], []
        # event 维度：候选中的事件节点
        state.events = [c for c in candidates
                        if getattr(c, "node_type", None) == "event"]
        # belief 维度：观点演化时间线（异常不阻断）
        try:
            res = self.belief_resolver.resolve(self.store, now, filters)
            state.beliefs = res["current"]
            state.changes = res["changes"]
        except Exception as e:  # noqa: BLE001 局部失败不阻断
            _degraded("belief", e)
            state.beliefs, state.changes = [], []
        # intent 维度：当前意图（异常不阻断）
        try:
            res = self.intent_resolver.resolve(self.store, now, filters)
            state.intents = res["current"]
        except Exception as e:  # noqa: BLE001 局部失败不阻断
            _degraded("intent", e)
            state.intents = []
        # impact 维度：当前影响（异常不阻断）
        try:
            res = self.impact_resolver.resolve(
                self.store, now, filters, entity_scope=entity_scope)
            state.impacts = res["current"]
            state.impact_history = res["history"]
        except Exception as e:  # noqa: BLE001 局部失败不阻断
            _degraded("impact", e)
            state.impacts, state.impact_history = [], []
        # pattern 维度：个人模式（异常不阻断；仅附加，不进 current_state）
        try:
            res = self.pattern_resolver.resolve(
                self.store, now, filters, entity_scope=entity_scope)
            state.patterns = res["current"]
        except Exception as e:  # noqa: BLE001 局部失败不阻断
            _degraded("pattern", e)
            state.patterns = []
        return state
