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


@dataclass
class Tombstone:
    tid: int
    target_type: str
    target_id: int
    reason: str
    at: datetime


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
    name: str
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


@dataclass
class WriteResult:
    accepted: int
    rejected: list
    ids: list
