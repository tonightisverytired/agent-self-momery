# -*- coding: utf-8 -*-
"""结构化写入端点（0.8.0 B-04）：entities/events/facts/beliefs/intents/
impacts/evidence/edges + write/batch。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from dnamemory.errors import MemoryError

from .deps import get_memory
from .schemas import (BatchWriteRequest, BeliefCreate, EdgeCreate,
                      EntityCreate, EventCreate, EvidenceCreate, FactCreate,
                      ImpactCreate, IntentCreate, WriteRequest)
from .serializers import _status

router = APIRouter()


def _dt(v: str | None) -> datetime | None:
    """ISO 时间解析；非法格式 400 E001（统一错误体）。"""
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError as e:
        raise HTTPException(status_code=400,
                            detail={"code": "E001",
                                    "message": f"时间格式非法: {e}"})


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except MemoryError as e:
        raise HTTPException(status_code=_status(e),
                            detail={"code": e.code, "message": str(e)})


@router.post("/entities")
def create_entity(req: EntityCreate,
                  memory=Depends(get_memory)):
    nid = _guard(memory.add_entity, req.name, kind=req.kind,
                 description=req.description, protected=req.protected,
                 access_label=req.access_label,
                 evidence_ids=req.evidence_ids,
                 idempotency_key=req.idempotency_key)
    return {"id": nid}


@router.post("/events")
def create_event(req: EventCreate, memory=Depends(get_memory)):
    kwargs = {"kind": req.kind, "protected": req.protected,
              "access_label": req.access_label, "life": req.life,
              "decay_rate": req.decay_rate,
              "evidence_ids": req.evidence_ids,
              "idempotency_key": req.idempotency_key}
    if req.value_score is not None:
        kwargs["value_score"] = req.value_score
    eid = _guard(memory.add_event, req.name, _dt(req.ts), **kwargs)
    return {"id": eid}


@router.post("/facts")
def create_fact(req: FactCreate, memory=Depends(get_memory)):
    fid = _guard(memory.add_fact, req.entity, req.key, req.value,
                 source=req.source, confidence=req.confidence,
                 valid_at=_dt(req.valid_at), invalid_at=_dt(req.invalid_at),
                 explicit_confirmation=req.explicit_confirmation,
                 evidence_ids=req.evidence_ids,
                 idempotency_key=req.idempotency_key)
    return {"id": fid}


@router.post("/beliefs")
def create_belief(req: BeliefCreate, memory=Depends(get_memory)):
    bid = _guard(memory.add_belief, req.subject, req.proposition,
                 polarity=req.polarity, confidence=req.confidence,
                 source=req.source, valid_at=_dt(req.valid_at),
                 invalid_at=_dt(req.invalid_at),
                 access_label=req.access_label,
                 evidence_ids=req.evidence_ids,
                 idempotency_key=req.idempotency_key)
    return {"id": bid}


@router.post("/intents")
def create_intent(req: IntentCreate, memory=Depends(get_memory)):
    iid = _guard(memory.add_intent, req.subject, req.proposition,
                 status=req.status, confidence=req.confidence,
                 source=req.source, valid_at=_dt(req.valid_at),
                 invalid_at=_dt(req.invalid_at),
                 access_label=req.access_label,
                 evidence_ids=req.evidence_ids,
                 idempotency_key=req.idempotency_key)
    return {"id": iid}


@router.post("/impacts")
def create_impact(req: ImpactCreate, memory=Depends(get_memory)):
    iid = _guard(memory.add_impact, req.subject, req.dimension,
                 req.direction, req.valence, req.magnitude,
                 kind=req.kind, evaluator=req.evaluator,
                 description=req.description, cause_event=req.cause_event,
                 source=req.source, confidence=req.confidence,
                 valid_at=_dt(req.valid_at), invalid_at=_dt(req.invalid_at),
                 access_label=req.access_label,
                 evidence_ids=req.evidence_ids,
                 idempotency_key=req.idempotency_key)
    return {"id": iid}


@router.post("/evidence")
def create_evidence(req: EvidenceCreate, memory=Depends(get_memory)):
    eid = _guard(memory.add_evidence, req.source_type,
                 source_ref=req.source_ref,
                 conversation_id=req.conversation_id,
                 message_id=req.message_id,
                 observed_at=_dt(req.observed_at),
                 content_hash=req.content_hash,
                 trust_level=req.trust_level,
                 access_label=req.access_label,
                 metadata=req.metadata,
                 idempotency_key=req.idempotency_key)
    return {"id": eid}


@router.post("/edges")
def create_edge(req: EdgeCreate, memory=Depends(get_memory)):
    eid = _guard(memory.add_edge, req.a, req.b, req.rel,
                 weight=req.weight, confidence=req.confidence,
                 valid_at=_dt(req.valid_at), invalid_at=_dt(req.invalid_at),
                 idempotency_key=req.idempotency_key)
    return {"id": eid}


@router.post("/write")
def write_text(req: WriteRequest, memory=Depends(get_memory)):
    try:
        result = memory.write_text(req.text, meta=req.meta or {})
        return {"accepted": result.accepted,
                "rejected": result.rejected,
                "ids": result.ids}
    except MemoryError as e:
        raise HTTPException(status_code=_status(e),
                            detail={"code": e.code, "message": str(e)})


@router.post("/batch")
def write_batch(req: BatchWriteRequest, memory=Depends(get_memory)):
    try:
        result = memory.write_many(req.texts, meta=req.meta or {},
                                   batch_size=req.batch_size)
        return {"accepted": result.accepted,
                "rejected": result.rejected,
                "ids": result.ids}
    except MemoryError as e:
        raise HTTPException(status_code=_status(e),
                            detail={"code": e.code, "message": str(e)})
