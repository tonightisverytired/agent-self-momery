# -*- coding: utf-8 -*-
"""FastAPI 侧车：GET /health、POST /recall|write|forget。

运行（需先安装 dnamemory[server]）：
  py -m app.dnamemory_server --path data/mvp_memory.db --token <token>
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.security import (HTTPAuthorizationCredentials,  # noqa: E402
                              HTTPBearer)
from pydantic import BaseModel, Field  # noqa: E402

from dnamemory import (MemorySystem, RecallFilters, RecallQuery,  # noqa: E402
                       __version__)
from dnamemory.extract import FallbackExtractor  # noqa: E402
from dnamemory.errors import (CoherenceConflictError,  # noqa: E402
                              ConflictError, ContextBuildFailedError,
                              EmbeddingError, EvidenceNotFoundError,
                              MemoryError, MemoryStateConflictError,
                              NotFoundError, StorageError,
                              TemporalChainInvalidError,
                              UnsupportedBeliefError, ValidationError)
from dnamemory.models import RelationFilter  # noqa: E402


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


def _iso(v):
    return v.isoformat() if v else None


def _fact_dict(f):
    return {"id": f.fid, "key": f.key, "value": f.value,
            "source": f.source, "confidence": f.confidence,
            "valid_at": _iso(f.valid_at), "invalid_at": _iso(f.invalid_at),
            "evidence_ids": list(f.evidence_ids or [])}


def _ctx_dict(ctx):
    return {
        "query_type": ctx.query_type,
        "current_state": [_fact_dict(f) for f in ctx.current_state],
        "historical_changes": [_fact_dict(f) for f in ctx.historical_changes],
        "recent_events": [{"id": n.nid, "name": n.name, "ts": _iso(n.ts)}
                          for n in ctx.recent_events],
        "beliefs": [{"id": b.id, "proposition": b.proposition,
                     "polarity": b.polarity} for b in ctx.beliefs],
        "intents": [{"id": i.id, "proposition": i.proposition,
                     "status": i.status} for i in ctx.intents],
        "temporal_chains": [
            [{"dimension": n.dimension, "relation": n.relation,
              "at": _iso(n.at),
              "id": getattr(n.memory, "nid",
                            getattr(n.memory, "fid",
                                    getattr(n.memory, "id", None)))}
             for n in ch.nodes]
            for ch in ctx.temporal_chains],
        "evidence": [{"id": e.id, "source_type": e.source_type,
                      "source_ref": e.source_ref} for e in ctx.evidence],
        "conflicts": [{"kind": c["kind"], "node_id": c["node_id"],
                       "key": c["key"]} for c in ctx.conflicts],
        "notes": list(ctx.notes),
    }


def _status(e: MemoryError) -> int:
    code = getattr(e, "code", "")
    if code in ("E004", "E006", "E013"):
        return 404
    if isinstance(e, NotFoundError) or isinstance(e, EvidenceNotFoundError):
        return 404
    if isinstance(e, ConflictError) or isinstance(e, MemoryStateConflictError):
        return 409
    if isinstance(e, EmbeddingError):
        return 502
    if isinstance(e, StorageError):
        return 503
    if isinstance(e, (TemporalChainInvalidError, ContextBuildFailedError,
                      CoherenceConflictError)):
        return 422
    if isinstance(e, UnsupportedBeliefError):
        return 400
    if isinstance(e, ValidationError):
        return 400
    return 500


def _hit_dict(h):
    return {
        "node_id": h.node_id,
        "node_type": h.node_type,
        "name": h.name,
        "ts": h.ts.isoformat() if h.ts else None,
        "score": h.score,
        "sources": list(h.sources),
    }


def create_app(memory=None, path=":memory:", token=None, embedder=None,
               reranker=None, extractor=None):
    """构造侧车应用。token 必须提供（fail-fast），无 token 拒绝启动。"""
    if token is None:
        raise ValueError("必须提供 token（Bearer 鉴权）")
    if memory is None:
        memory = MemorySystem(path=path, embedder=embedder,
                              reranker=reranker, extractor=extractor)
    app = FastAPI(title="dnamemory-server", version=__version__)
    expected_auth = f"Bearer {token}"
    security = HTTPBearer(auto_error=False)

    def require_auth(request: Request,
                     credentials: HTTPAuthorizationCredentials | None =
                     Depends(security)):
        provided = f"Bearer {credentials.credentials}" \
            if credentials is not None else ""
        if not secrets.compare_digest(provided, expected_auth):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _query(req: RecallRequest) -> RecallQuery:
        t0 = None
        if req.time:
            try:
                t0 = datetime.fromisoformat(req.time)
            except ValueError as e:
                raise HTTPException(status_code=400,
                                    detail={"code": "E001",
                                            "message": f"时间格式非法: {e}"})
        relation = None
        if req.relation_node:
            relation = RelationFilter(node=req.relation_node,
                                      rel_type=req.relation_type)
        return RecallQuery(
            text=req.text,
            time=(t0, req.tol_days) if t0 is not None else None,
            topic=req.topic,
            relation=relation)

    def _filters(req: RecallRequest) -> RecallFilters:
        return RecallFilters(
            node_types=tuple(req.node_types) if req.node_types else None,
            kinds=tuple(req.kinds) if req.kinds else None,
            include_archived=req.include_archived,
            access_labels=tuple(req.access_labels)
            if req.access_labels else None)

    @app.get("/health", dependencies=[Depends(require_auth)])
    def health():
        return {"ok": True}

    @app.post("/recall", dependencies=[Depends(require_auth)])
    def recall(req: RecallRequest):
        try:
            hits = memory.recall(
                _query(req), filters=_filters(req), k=req.k, mode=req.mode,
                rerank_top_n=req.rerank_top_n)
            return {"items": [_hit_dict(h) for h in hits]}
        except MemoryError as e:
            raise HTTPException(status_code=_status(e),
                                detail={"code": e.code,
                                        "message": str(e)})

    @app.post("/write", dependencies=[Depends(require_auth)])
    def write(req: WriteRequest):
        try:
            result = memory.write_text(req.text, meta=req.meta or {})
            return {"accepted": result.accepted,
                    "rejected": result.rejected,
                    "ids": result.ids}
        except MemoryError as e:
            raise HTTPException(status_code=_status(e),
                                detail={"code": e.code,
                                        "message": str(e)})

    @app.post("/forget", dependencies=[Depends(require_auth)])
    def forget(req: ForgetRequest):
        try:
            memory.forget(req.target, reason=req.reason, force=req.force)
            return {"ok": True}
        except MemoryError as e:
            raise HTTPException(status_code=_status(e),
                                detail={"code": e.code,
                                        "message": str(e)})

    @app.post("/context", dependencies=[Depends(require_auth)])
    def context(req: ContextRequest):
        try:
            t0 = None
            if req.time:
                try:
                    t0 = datetime.fromisoformat(req.time)
                except ValueError as e:
                    raise HTTPException(
                        status_code=400,
                        detail={"code": "E001",
                                "message": f"时间格式非法: {e}"})
            ctx = memory.recall_context(
                RecallQuery(text=req.text), query_type=req.query_type,
                query_time=t0, include_history=req.include_history,
                include_beliefs=req.include_beliefs,
                include_evidence=req.include_evidence)
            return _ctx_dict(ctx)
        except MemoryError as e:
            raise HTTPException(status_code=_status(e),
                                detail={"code": e.code,
                                        "message": str(e)})

    @app.post("/timeline", dependencies=[Depends(require_auth)])
    def timeline(req: TimelineRequest):
        try:
            start = datetime.fromisoformat(req.start) if req.start else None
            end = datetime.fromisoformat(req.end) if req.end else None
            nodes = memory.timeline(entity_id=req.entity, start=start,
                                    end=end)
            return {"nodes": [
                {"dimension": n.dimension, "relation": n.relation,
                 "at": _iso(n.at),
                 "id": getattr(n.memory, "nid",
                               getattr(n.memory, "fid",
                                       getattr(n.memory, "id", None))),
                 "name": getattr(n.memory, "name",
                                 getattr(n.memory, "value", ""))}
                for n in nodes]}
        except MemoryError as e:
            raise HTTPException(status_code=_status(e),
                                detail={"code": e.code,
                                        "message": str(e)})

    @app.get("/memory/{memory_id}/history",
             dependencies=[Depends(require_auth)])
    def memory_history(memory_id: int, dimension: str = "fact"):
        try:
            items = memory.memory_history(memory_id, dimension=dimension)
            if dimension in ("fact",):
                return {"items": [_fact_dict(f) for f in items]}
            return {"items": [
                {"id": getattr(m, "id", getattr(m, "nid", None)),
                 "proposition": getattr(m, "proposition",
                                        getattr(m, "name", "")),
                 "polarity": getattr(m, "polarity", None),
                 "status": getattr(m, "status", None),
                 "ts": _iso(getattr(m, "ts", None))} for m in items]}
        except MemoryError as e:
            raise HTTPException(status_code=_status(e),
                                detail={"code": e.code,
                                        "message": str(e)})

    @app.get("/memory/{memory_id}/explain",
             dependencies=[Depends(require_auth)])
    def memory_explain(memory_id: int, kind: Optional[str] = None):
        try:
            ex = memory.explain(memory_id, kind=kind)
            m = ex["memory"]
            return {
                "memory": {"id": memory_id,
                           "name": getattr(m, "name",
                                           getattr(m, "value", "")),
                           "source": ex["source"],
                           "evidence_ids": getattr(m, "evidence_ids", [])},
                "evidence": [{"id": e.id, "source_type": e.source_type,
                              "source_ref": e.source_ref}
                             for e in ex["evidence"]],
                "versions": [{"version": v[0], "content": v[1],
                              "created_at": v[2]} for v in ex["versions"]],
                "related": ex["related"]}
        except MemoryError as e:
            raise HTTPException(status_code=_status(e),
                                detail={"code": e.code,
                                        "message": str(e)})

    return app


def main():
    parser = argparse.ArgumentParser(description="dnamemory FastAPI 侧车")
    parser.add_argument("--path", default=":memory:")
    parser.add_argument("--token", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--fallback-extractor", action="store_true",
                        help="注入确定性兜底抽取器，/write 开箱可测")
    parser.add_argument("--bge-m3", action="store_true",
                        help="加载本地缓存的 bge-m3 模型，启用真实语义路")
    args = parser.parse_args()
    token = args.token or os.environ.get("DNAMEMORY_TOKEN")
    if not token:
        parser.error("需要 --token 或环境变量 DNAMEMORY_TOKEN")
    import uvicorn
    extractor = FallbackExtractor() if args.fallback_extractor else None
    embedder = None
    if args.bge_m3:
        from dnamemory.embeddings import BGEM3Embedder
        embedder = BGEM3Embedder()
    app = create_app(path=args.path, token=token, extractor=extractor,
                     embedder=embedder)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
