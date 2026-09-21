# -*- coding: utf-8 -*-
"""FastAPI 应用工厂（0.8.0 A-02：自 app/dnamemory_server.py 迁移，
端点路径/方法/请求/响应语义零改动；B 阶段拆分路由并扩展能力面）。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from dnamemory import MemorySystem, RecallFilters, RecallQuery, __version__
from dnamemory.errors import MemoryError
from dnamemory.models import RelationFilter

from .deps import build_auth
from .routes_govern import router as govern_router
from .routes_query import router as query_router
from .routes_write import router as write_router
from .schemas import (ContextRequest, ForgetRequest, RecallRequest,
                      TimelineRequest)
from .serializers import (_ctx_dict, _evidence_dict,
                          _explain_version_dict, _fact_dict, _hit_dict,
                          _iso, _status)

DEFAULT_CORS_ORIGINS = [
    "http://localhost", "https://localhost",
    "http://127.0.0.1", "https://127.0.0.1",
]


def _cors_origins():
    """默认仅允许本机任意端口；env DNAMEMORY_CORS_ORIGINS 逗号分隔覆盖。"""
    raw = os.environ.get("DNAMEMORY_CORS_ORIGINS", "")
    if raw.strip():
        return [o.strip() for o in raw.split(",") if o.strip()]
    return DEFAULT_CORS_ORIGINS


def create_app(memory=None, path=":memory:", token=None, embedder=None,
               reranker=None, extractor=None, dsn=None, warmup=True):
    """构造侧车应用。token 必须提供（fail-fast），无 token 拒绝启动。

    dsn 非空时改用 PostgreSQL 存储（默认留空则用 path 指定的 SQLite）。
    warmup（默认开）：lifespan 启动时跑一次小 recall，预热嵌入/reranker
    模型与读快照缓存，避免首个真实请求承担冷启动延迟（0.8.2）。
    """
    if token is None:
        raise ValueError("必须提供 token（Bearer 鉴权）")
    owned_memory = memory is None
    if memory is None:
        memory = MemorySystem(path=path, dsn=dsn, embedder=embedder,
                              reranker=reranker, extractor=extractor)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if warmup:
            try:
                memory.recall(RecallQuery(text="预热"), k=1)
            except Exception:  # noqa: BLE001 预热失败不阻断启动
                pass
        yield
        # 仅工厂自建实例负责关闭；注入实例生命周期归调用方
        if owned_memory:
            memory.close()

    app = FastAPI(title="dnamemory-server", version=__version__,
                  lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.memory = memory
    require_auth = build_auth(token)

    @app.exception_handler(HTTPException)
    async def _http_exc_handler(request: Request, exc: HTTPException):
        """统一错误体 {code, message}（0.8.0）。"""
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            body = detail
        else:
            code = "E401" if exc.status_code == 401 else "E001"
            body = {"code": code, "message": str(detail)}
        return JSONResponse(status_code=exc.status_code, content=body)

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _validation_exc_handler(request: Request,
                                      exc: RequestValidationError):
        """请求体校验失败 → 422 {code: E422, message}。"""
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        msg = first.get("msg", "请求体校验失败")
        return JSONResponse(
            status_code=422,
            content={"code": "E422",
                     "message": f"{loc}: {msg}" if loc else msg})

    # 结构化写入路由（0.8.0 B-04；全局 Bearer 鉴权）
    app.include_router(write_router, dependencies=[Depends(require_auth)])
    # 查询/元数据路由（0.8.0 B-05）
    app.include_router(query_router, dependencies=[Depends(require_auth)])
    # 治理路由（0.8.0 B-06）
    app.include_router(govern_router, dependencies=[Depends(require_auth)])

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
        return {"ok": True, "version": __version__}

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
                # 0.8.1 IA-4：证据全字段；versions 支持领域对象版本链；
                # related 带 confidence；附该对象审计记录
                "evidence": [_evidence_dict(e) for e in ex["evidence"]],
                "versions": [_explain_version_dict(v)
                             for v in ex["versions"]],
                "related": ex["related"],
                "audits": ex["audits"]}
        except MemoryError as e:
            raise HTTPException(status_code=_status(e),
                                detail={"code": e.code,
                                        "message": str(e)})

    # 静态后台（0.8.0 C-01）：API 路由之后挂载，避免遮蔽
    from .static import mount_web
    mount_web(app)
    return app
