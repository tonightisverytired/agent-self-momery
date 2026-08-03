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
from pydantic import BaseModel, Field  # noqa: E402

from dnamemory import MemorySystem, RecallFilters, RecallQuery  # noqa: E402
from dnamemory.extract import FallbackExtractor  # noqa: E402
from dnamemory.errors import (ConflictError, EmbeddingError,  # noqa: E402
                              MemoryError, NotFoundError, StorageError,
                              ValidationError)
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


def _status(e: MemoryError) -> int:
    code = getattr(e, "code", "")
    if code in ("E004", "E006"):
        return 404
    if isinstance(e, NotFoundError):
        return 404
    if isinstance(e, ConflictError):
        return 409
    if isinstance(e, EmbeddingError):
        return 502
    if isinstance(e, StorageError):
        return 503
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
    app = FastAPI(title="dnamemory-server", version="0.3.0")
    expected_auth = f"Bearer {token}"

    def require_auth(request: Request):
        if not secrets.compare_digest(
                request.headers.get("Authorization", ""), expected_auth):
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

    return app


def main():
    parser = argparse.ArgumentParser(description="dnamemory FastAPI 侧车")
    parser.add_argument("--path", default=":memory:")
    parser.add_argument("--token", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--fallback-extractor", action="store_true",
                        help="注入确定性兜底抽取器，/write 开箱可测")
    args = parser.parse_args()
    token = args.token or os.environ.get("DNAMEMORY_TOKEN")
    if not token:
        parser.error("需要 --token 或环境变量 DNAMEMORY_TOKEN")
    import uvicorn
    extractor = FallbackExtractor() if args.fallback_extractor else None
    app = create_app(path=args.path, token=token, extractor=extractor)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
