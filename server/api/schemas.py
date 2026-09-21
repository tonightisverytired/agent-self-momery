# -*- coding: utf-8 -*-
"""API 请求模型（0.8.0 A-02：自 app/dnamemory_server.py 原样迁移；
B-01 扩展结构化写入与治理请求族）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RecallRequest(BaseModel):
    text: Optional[str] = Field(default=None, description="语义查询文本")
    time: Optional[str] = Field(default=None, description="ISO 时间，如 2026-08-03")
    tol_days: Optional[int] = Field(default=None, ge=0, le=365)
    topic: Optional[list[str]] = Field(default=None, max_length=10)
    relation_node: Optional[str] = None
    relation_type: Optional[str] = None
    k: int = Field(default=8, ge=1, le=100)
    mode: str = Field(
        default="triple",
        pattern="^(time|graph|semantic|dual|triple|quad)$")
    node_types: Optional[list[str]] = Field(default=None, max_length=2)
    kinds: Optional[list[str]] = Field(default=None, max_length=50)
    include_archived: bool = False
    access_labels: Optional[list[str]] = Field(default=None, max_length=10)
    rerank_top_n: int = Field(default=20, ge=1, le=200)


class WriteRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)
    meta: Optional[dict] = None


class ForgetRequest(BaseModel):
    target: str | int
    reason: str = Field(..., min_length=1, max_length=500)
    force: bool = False


class ContextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)
    query_type: Optional[str] = None
    time: Optional[str] = None
    include_history: bool = False
    include_beliefs: bool = True
    include_evidence: bool = True


class TimelineRequest(BaseModel):
    entity: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None


# ---------------- 0.8.0 B-01：结构化写入与治理请求族 ----------------

from pydantic import field_validator  # noqa: E402

from dnamemory.models import (BELIEF_POLARITIES, DEFAULT_RELATIONS,  # noqa: E402
                              EVIDENCE_SOURCE_TYPES, IMPACT_DIRECTIONS,
                              IMPACT_KINDS, IMPACT_VALENCES,
                              INTENT_STATUSES)


class _Base(BaseModel):
    model_config = {"str_strip_whitespace": True, "extra": "forbid"}
    idempotency_key: Optional[str] = None


def _in(values: frozenset, name: str):
    def _check(cls, v):
        if v not in values:
            raise ValueError(f"{name} 非法: {v}")
        return v
    return field_validator(name)(_check)


class EntityCreate(_Base):
    name: str = Field(..., min_length=1, max_length=500)
    kind: str = "concept"
    description: str = ""
    protected: bool = False
    access_label: str = "public"
    evidence_ids: Optional[list[int]] = None


class EventCreate(_Base):
    name: str = Field(..., min_length=1, max_length=500)
    ts: Optional[str] = None
    kind: str = "meeting"
    value_score: Optional[float] = Field(default=None, ge=0, le=1)
    protected: bool = False
    access_label: str = "public"
    life: Optional[float] = None
    decay_rate: Optional[float] = None
    evidence_ids: Optional[list[int]] = None


class FactCreate(_Base):
    entity: str = Field(..., min_length=1, max_length=500)
    key: str = Field(..., min_length=1, max_length=200)
    value: str = Field(..., min_length=1, max_length=2000)
    source: str = "chat"
    confidence: float = Field(default=0.7, ge=0, le=1)
    valid_at: Optional[str] = None
    invalid_at: Optional[str] = None
    explicit_confirmation: bool = False
    evidence_ids: Optional[list[int]] = None


class BeliefCreate(_Base):
    subject: str = Field(..., min_length=1, max_length=500)
    proposition: str = Field(..., min_length=1, max_length=2000)
    polarity: str = "neutral"
    _v_polarity = _in(BELIEF_POLARITIES, "polarity")
    confidence: float = Field(default=0.7, ge=0, le=1)
    source: str = "chat"
    valid_at: Optional[str] = None
    invalid_at: Optional[str] = None
    access_label: str = "public"
    evidence_ids: Optional[list[int]] = None


class IntentCreate(_Base):
    subject: str = Field(..., min_length=1, max_length=500)
    proposition: str = Field(..., min_length=1, max_length=2000)
    status: str = "active"
    _v_status = _in(INTENT_STATUSES, "status")
    confidence: float = Field(default=0.7, ge=0, le=1)
    source: str = "chat"
    valid_at: Optional[str] = None
    invalid_at: Optional[str] = None
    access_label: str = "public"
    evidence_ids: Optional[list[int]] = None


class ImpactCreate(_Base):
    subject: str = Field(..., min_length=1, max_length=500)
    dimension: str = Field(..., min_length=1, max_length=200)
    direction: str
    valence: str
    _v_direction = _in(IMPACT_DIRECTIONS, "direction")
    _v_valence = _in(IMPACT_VALENCES, "valence")
    magnitude: float = Field(default=0.5, ge=0, le=1)
    kind: str = "objective"
    evaluator: str = "agent"
    description: str = ""
    cause_event: Optional[str] = None
    _v_kind = _in(IMPACT_KINDS, "kind")
    source: str = "chat"
    confidence: float = Field(default=0.7, ge=0, le=1)
    valid_at: Optional[str] = None
    invalid_at: Optional[str] = None
    access_label: str = "public"
    evidence_ids: Optional[list[int]] = None


class EvidenceCreate(_Base):
    source_type: str
    _v_source_type = _in(EVIDENCE_SOURCE_TYPES, "source_type")
    source_ref: str = ""
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    observed_at: Optional[str] = None
    content_hash: Optional[str] = None
    trust_level: Optional[float] = None
    access_label: str = "public"
    metadata: Optional[dict] = None


class EdgeCreate(_Base):
    a: str = Field(..., min_length=1, max_length=500)
    b: str = Field(..., min_length=1, max_length=500)
    rel: str
    _v_rel = _in(DEFAULT_RELATIONS, "rel")
    weight: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.8, ge=0, le=1)
    valid_at: Optional[str] = None
    invalid_at: Optional[str] = None


class BatchWriteRequest(_Base):
    texts: list[str] = Field(..., min_length=1, max_length=100)
    meta: Optional[dict] = None
    batch_size: int = Field(default=20, ge=1, le=100)


class ReflectRequest(BaseModel):
    year: int = Field(..., ge=2000, le=2200)
    month: int = Field(..., ge=1, le=12)


class RestoreRequest(BaseModel):
    node_id: int


class ConfirmRequest(BaseModel):
    decision: dict
    choice_value: str


class ResolveEntitiesRequest(BaseModel):
    merge_similar: bool = False
    min_similarity: float = Field(default=0.85, ge=0, le=1)
