# -*- coding: utf-8 -*-
"""治理端点（0.8.0 B-06）：冲突/确认/实体消解/反射/压缩/模式/影响链/
步进/恢复。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from dnamemory.errors import MemoryError
from dnamemory.temporal import derive_impact_links

from .deps import get_memory
from .schemas import (ConfirmRequest, ReflectRequest, ResolveEntitiesRequest,
                      RestoreRequest)
from .serializers import _status

router = APIRouter()


def _raise(e: MemoryError):
    raise HTTPException(status_code=_status(e),
                        detail={"code": e.code, "message": str(e)})


@router.post("/resolve-conflicts")
def resolve_conflicts(memory=Depends(get_memory)):
    try:
        decisions = memory.resolve_conflicts()
        return {"decisions": [
            {"kind": d.kind, "node_id": d.node_id, "fact_key": d.fact_key,
             "winner": str(d.winner.value), "loser": str(d.loser.value)}
            for d in decisions]}
    except MemoryError as e:
        _raise(e)


@router.post("/confirm")
def confirm(req: ConfirmRequest, memory=Depends(get_memory)):
    try:
        from dnamemory.governance import ConflictDecision
        d = req.decision
        facts = [f for f in memory.store.fetch_facts()
                 if f.node_id == d.get("node_id")
                 and f.key == d.get("fact_key")]
        winner = next((f for f in facts
                       if str(f.value) == d.get("winner")), None)
        loser = next((f for f in facts
                      if str(f.value) == d.get("loser")), None)
        if winner is None or loser is None:
            raise HTTPException(status_code=400,
                                detail={"code": "E001",
                                        "message": "decision 事实无法定位"})
        decision = ConflictDecision(d.get("kind", "confirm"),
                                    d["node_id"], d["fact_key"],
                                    winner, loser)
        memory.confirm(decision, req.choice_value)
        return {"ok": True}
    except HTTPException:
        raise
    except MemoryError as e:
        _raise(e)


@router.post("/resolve-entities")
def resolve_entities(req: ResolveEntitiesRequest,
                     memory=Depends(get_memory)):
    try:
        merged = memory.resolve_entities(merge_similar=req.merge_similar,
                                         min_similarity=req.min_similarity)
        return {"merged": merged}
    except MemoryError as e:
        _raise(e)


@router.post("/reflect")
def reflect(req: ReflectRequest, memory=Depends(get_memory)):
    try:
        summary = memory.reflect_monthly(req.year, req.month)
        return {"summary_id": summary}
    except MemoryError as e:
        _raise(e)


@router.post("/compress-stable")
def compress_stable(memory=Depends(get_memory)):
    try:
        return {"created": memory.compress_stable()}
    except MemoryError as e:
        _raise(e)


@router.post("/extract-patterns")
def extract_patterns(memory=Depends(get_memory)):
    try:
        return {"patterns": memory.extract_patterns()}
    except MemoryError as e:
        _raise(e)


@router.post("/derive-impact-links")
def derive_links(memory=Depends(get_memory)):
    try:
        return {"links": derive_impact_links(memory.store)}
    except MemoryError as e:
        _raise(e)


@router.post("/step-day")
def step_day(memory=Depends(get_memory)):
    try:
        archived = memory.step_day()
        return {"archived": archived}
    except MemoryError as e:
        _raise(e)


@router.post("/restore")
def restore(req: RestoreRequest, memory=Depends(get_memory)):
    try:
        memory.restore(req.node_id)
        return {"ok": True}
    except MemoryError as e:
        _raise(e)
