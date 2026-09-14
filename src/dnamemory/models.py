# -*- coding: utf-8 -*-
"""数据模型与配置。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional

from .errors import ValidationError

DEFAULT_RELATIONS = frozenset({
    "participates", "discusses", "mentions", "occurs_with", "precedes",
    "causes", "depends_on", "similar_to", "part_of", "prefers",
    "updates_to", "contradicts", "summarizes", "step_of",
})

BELIEF_POLARITIES = frozenset({"positive", "negative", "neutral"})
INTENT_STATUSES = frozenset({
    "active", "completed", "cancelled", "expired", "superseded"})
EVIDENCE_SOURCE_TYPES = frozenset({
    "user_statement", "conversation", "system_record", "external_data",
    "imported_memory", "inferred"})
MEMORY_LINK_RELATIONS = frozenset({
    "before", "after", "overlaps", "changes", "reinforces",
    "contradicts", "supersedes", "caused_by", "summarizes"})
SCORE_WEIGHT_KEYS = ("retrieval", "temporal", "validity", "source",
                     "evidence", "coherence", "conflict_penalty")


@dataclass(frozen=True)
class LifecycleDefaults:
    life: float
    decay_rate: float


@dataclass(frozen=True)
class MemoryConfig:
    """全部可变配置集中在调用层（构造级）。"""

    relations: frozenset = DEFAULT_RELATIONS
    source_rank: Mapping[str, int] = field(
        default_factory=lambda: {"profile": 3, "agent": 2, "chat": 1})
    event_defaults: Mapping[str, LifecycleDefaults] = field(
        default_factory=lambda: {
            "transient": LifecycleDefaults(life=50.0, decay_rate=0.28),
            "core": LifecycleDefaults(life=150.0, decay_rate=0.02),
        })
    rrf_k: float = 60.0
    access_boost: tuple = (3.0, 6.0)
    conflict_confirm_threshold: float = 0.8
    lifecycle_order: tuple = ("active", "archived", "tombstoned", "deleted")
    max_hops_default: int = 2
    time_tolerance_days_default: int = 2
    dense_min_sim: float = 0.55
    sparse_min_sim: float = 0.25
    dense_ann_top_k: int = 1000
    score_weights: Mapping[str, float] = field(
        default_factory=lambda: dict.fromkeys(SCORE_WEIGHT_KEYS, 1.0))

    def __post_init__(self):
        if not self.relations:
            raise ValidationError("E001 relations 不能为空")
        if "profile" not in self.source_rank:
            raise ValidationError("E001 source_rank 必须包含 profile")
        if not (0 <= self.conflict_confirm_threshold <= 1):
            raise ValidationError("E002 阈值必须在 [0,1]")
        if self.rrf_k <= 0:
            raise ValidationError("E001 rrf_k 必须 > 0")
        if not (0 <= self.dense_min_sim <= 1):
            raise ValidationError("E002 dense_min_sim 必须在 [0,1]")
        if not (0 <= self.sparse_min_sim <= 1):
            raise ValidationError("E002 sparse_min_sim 必须在 [0,1]")
        if self.dense_ann_top_k <= 0:
            raise ValidationError("E001 dense_ann_top_k 必须 > 0")
        for st in ("active", "archived", "tombstoned", "deleted"):
            if st not in self.lifecycle_order:
                raise ValidationError(f"E001 lifecycle_order 缺少 {st}")
        missing = set(SCORE_WEIGHT_KEYS) - set(self.score_weights)
        if missing:
            raise ValidationError(
                f"E001 score_weights 缺少键: {sorted(missing)}")
        for k, v in self.score_weights.items():
            if v < 0:
                raise ValidationError(f"E002 score_weights[{k}] 必须 ≥ 0")


@dataclass
class Node:
    nid: int
    node_type: str
    kind: str
    name: str
    description: str
    ts: Optional[datetime]
    value_score: float
    protected: bool
    access_label: str
    lifecycle: str
    life: float
    decay_rate: float
    last_access: Optional[datetime]
    created_at: datetime
    evidence_ids: list = field(default_factory=list)
    source: str = ""


@dataclass
class Edge:
    eid: int
    from_id: int
    to_id: int
    rel: str
    weight: float
    confidence: float
    valid_at: datetime
    invalid_at: Optional[datetime]
    created_at: datetime
    access_count: int
    lifecycle: str


@dataclass
class Fact:
    fid: int
    node_id: int
    key: str
    value: str
    source: str
    confidence: float
    valid_at: datetime
    recorded_at: datetime
    invalid_at: Optional[datetime]
    superseded_by: Optional[int]
    tombstoned: bool
    evidence_ids: list = field(default_factory=list)


@dataclass
class Tombstone:
    tid: int
    target_type: str
    target_id: int
    reason: str
    at: datetime


@dataclass
class Belief:
    """主体对命题的主观认知（0.5.0）；历史变化不覆盖，演化链走 memory_links。"""

    id: int
    subject_id: int
    proposition: str
    polarity: str = "neutral"
    confidence: float = 0.7
    source: str = "chat"
    valid_at: Optional[datetime] = None
    invalid_at: Optional[datetime] = None
    superseded_by: Optional[int] = None
    lifecycle: str = "active"
    access_label: str = "public"
    evidence_ids: list = field(default_factory=list)
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if self.polarity not in BELIEF_POLARITIES:
            raise ValidationError(
                f"E001 非法 polarity: {self.polarity}")
        if not (0 <= self.confidence <= 1):
            raise ValidationError("E002 confidence 必须在 [0,1]")
        if self.invalid_at is not None and self.valid_at is not None \
                and self.invalid_at < self.valid_at:
            raise ValidationError("E003 双时态顺序错误")


@dataclass
class Intent:
    """未来倾向/计划；status 独立于 lifecycle，过期计划不当现在意图。"""

    id: int
    subject_id: int
    proposition: str
    status: str = "active"
    confidence: float = 0.7
    source: str = "chat"
    valid_at: Optional[datetime] = None
    invalid_at: Optional[datetime] = None
    lifecycle: str = "active"
    access_label: str = "public"
    evidence_ids: list = field(default_factory=list)
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if self.status not in INTENT_STATUSES:
            raise ValidationError(f"E001 非法 status: {self.status}")
        if not (0 <= self.confidence <= 1):
            raise ValidationError("E002 confidence 必须在 [0,1]")
        if self.invalid_at is not None and self.valid_at is not None \
                and self.invalid_at < self.valid_at:
            raise ValidationError("E003 双时态顺序错误")


@dataclass
class Evidence:
    """记忆的证据来源；inferred 必须标注推断，不得伪装为客观事实。"""

    id: int
    source_type: str
    source_ref: str = ""
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    observed_at: Optional[datetime] = None
    content_hash: Optional[str] = None
    trust_level: Optional[float] = None
    metadata: dict = field(default_factory=dict)
    created_at: Optional[datetime] = None

    def __post_init__(self):
        if self.source_type not in EVIDENCE_SOURCE_TYPES:
            raise ValidationError(
                f"E001 非法 source_type: {self.source_type}")


@dataclass
class MemoryLink:
    """高阶记忆逻辑关系层（与实体邻接 edges 分层）。"""

    id: int
    source_id: int
    target_id: int
    source_type: str
    relation: str
    confidence: float = 0.7
    valid_at: Optional[datetime] = None
    invalid_at: Optional[datetime] = None

    def __post_init__(self):
        if self.relation not in MEMORY_LINK_RELATIONS:
            raise ValidationError(f"E001 非法 relation: {self.relation}")
        if not (0 <= self.confidence <= 1):
            raise ValidationError("E002 confidence 必须在 [0,1]")
        if self.invalid_at is not None and self.valid_at is not None \
                and self.invalid_at < self.valid_at:
            raise ValidationError("E003 双时态顺序错误")


@dataclass
class MemoryState:
    """Resolver 输出的当前业务状态（0.5.0）。"""

    current_facts: list = field(default_factory=list)
    historical_facts: list = field(default_factory=list)
    events: list = field(default_factory=list)
    beliefs: list = field(default_factory=list)
    intents: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    chains: list = field(default_factory=list)


@dataclass
class ResolvedMemory:
    """经 Resolver 处理后的记忆封装（维度标记）。"""

    memory: object = None
    dimension: str = ""


@dataclass
class MemoryScore:
    """状态层分项评分（仅 recall_context 管线使用，§23 可解释）。"""

    retrieval_score: float = 0.0
    temporal_score: float = 0.0
    validity_score: float = 0.0
    source_score: float = 0.0
    evidence_score: float = 0.0
    coherence_score: float = 0.0
    conflict_penalty: float = 0.0

    def final(self, weights: Optional[Mapping[str, float]] = None) -> float:
        """final = α·retrieval + … + ζ·coherence − η·conflict_penalty。

        默认权重全 1.0（等权求和）。
        """
        w = weights or dict.fromkeys(SCORE_WEIGHT_KEYS, 1.0)
        return (w["retrieval"] * self.retrieval_score
                + w["temporal"] * self.temporal_score
                + w["validity"] * self.validity_score
                + w["source"] * self.source_score
                + w["evidence"] * self.evidence_score
                + w["coherence"] * self.coherence_score
                - w["conflict_penalty"] * self.conflict_penalty)


@dataclass
class CoherenceResult:
    """多维一致性检查结果。"""

    consistent: bool = True
    conflicts: list = field(default_factory=list)
    explanations: list = field(default_factory=list)


@dataclass(frozen=True)
class RelationFilter:
    node: str
    rel_type: Optional[str] = None


@dataclass(frozen=True)
class RecallQuery:
    text: Optional[str] = None
    time: Optional[tuple] = None          # (datetime, tol_days)
    topic: Optional[list] = None
    relation: Optional[RelationFilter] = None


@dataclass(frozen=True)
class RecallFilters:
    node_types: Optional[tuple] = None
    kinds: Optional[tuple] = None
    time_range: Optional[tuple] = None
    include_archived: bool = False
    access_labels: Optional[tuple] = None   # None = public+private，sensitive 隐藏


@dataclass(frozen=True)
class MemoryHit:
    node_id: int
    node_type: str
    name: str
    ts: Optional[datetime]
    score: float
    sources: tuple


@dataclass
class ExtractedMemory:
    type: str
    name: str = ""
    kind: Optional[str] = None
    ts: Optional[datetime] = None
    value_score: Optional[float] = None
    protected: bool = False
    source: str = "chat"
    confidence: float = 0.7
    rel: Optional[str] = None
    from_: Optional[str] = None
    to: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    idempotency_key: Optional[str] = None
    # 0.5.0 状态维度候选字段（belief/intent/evidence）
    proposition: Optional[str] = None
    polarity: Optional[str] = None
    status: Optional[str] = None
    source_type: Optional[str] = None
    source_ref: Optional[str] = None
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    content_hash: Optional[str] = None
    trust_level: Optional[float] = None
    evidence_ids: Optional[list] = None


@dataclass
class WriteResult:
    accepted: int
    rejected: list
    ids: list
