# -*- coding: utf-8 -*-
"""FastMCP 服务：write_memory / recall / forget / resolve_conflicts。

运行（需先安装 dnamemory[mcp]）：
  py -m app.dnamemory_mcp --path data/mvp_memory.db            # stdio
  py -m app.dnamemory_mcp --transport streamable_http --token <token>
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from fastmcp import FastMCP  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from dnamemory import MemorySystem, RecallFilters, RecallQuery  # noqa: E402
from dnamemory.errors import (ConflictError, EmbeddingError,  # noqa: E402
                              MemoryError, NotFoundError, StorageError,
                              ValidationError)
from dnamemory.models import RelationFilter  # noqa: E402


class RecallInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    text: Optional[str] = Field(default=None,
                                description="语义查询文本，如 '晚饭给娃做了排骨'")
    topic: Optional[list[str]] = Field(default=None, max_length=10,
                                       description="实体主题词，如 ['项目A']")
    time: Optional[str] = Field(default=None,
                                description="ISO 时间，如 2026-08-03")
    tol_days: int = Field(default=2, ge=0, le=365)
    k: int = Field(default=5, ge=1, le=100)
    mode: str = Field(default="triple",
                      pattern="^(time|graph|semantic|dual|triple|quad)$")
    node_types: Optional[list[str]] = Field(default=None, max_length=2)
    access_labels: Optional[list[str]] = Field(default=None, max_length=10)
    rerank_top_n: int = Field(default=20, ge=1, le=200)


class WriteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    text: str = Field(..., min_length=1, max_length=20000,
                      description="日常记录片段，如 '今天和张总讨论了预算'")
    meta: Optional[dict] = Field(default=None, description="可选元信息")


class ForgetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    target: str | int = Field(..., description="节点名称或 id")
    reason: str = Field(..., min_length=1, max_length=500,
                        description="遗忘原因，如 retracted/gdpr")
    force: bool = Field(default=False,
                        description="True=合规删除（deleted 终态+级联）")


def _err_text(e: MemoryError) -> str:
    hint = {
        "E004": "deleted 节点不可恢复，请重建",
        "E005": "protected 节点需要 force=True",
        "E006": "目标不存在，请检查名称或 id",
        "E009": "存储繁忙，请稍后重试",
        "E011": "稠密向量缺失，检查 embedder",
    }.get(e.code, "请检查参数后重试")
    return f"Error {e.code}: {e}. {hint}"


def create_server(path=":memory:", embedder=None, reranker=None,
                  extractor=None) -> FastMCP:
    mem = MemorySystem(path=path, embedder=embedder, reranker=reranker,
                       extractor=extractor)
    mcp = FastMCP("dnamemory_mcp")

    def _query(p: RecallInput) -> RecallQuery:
        t0 = None
        if p.time:
            t0 = datetime.fromisoformat(p.time)
        return RecallQuery(
            text=p.text,
            time=(t0, p.tol_days) if t0 is not None else None,
            topic=p.topic)

    @mcp.tool(name="dnamemory_recall",
              annotations={"title": "召回记忆",
                           "readOnlyHint": True,
                           "destructiveHint": False,
                           "idempotentHint": True,
                           "openWorldHint": False})
    async def dnamemory_recall(params: RecallInput) -> str:
        """按文本/主题/时间召回事件记忆，返回 top-k JSON。

        Args:
            params (RecallInput): text 语义查询；topic 图谱主题；time 时间窗。

        Returns:
            str: JSON 数组，每项含 node_id/name/ts/score/sources。
        """
        try:
            filters = RecallFilters(
                node_types=tuple(params.node_types)
                if params.node_types else None,
                access_labels=tuple(params.access_labels)
                if params.access_labels else None)
            hits = mem.recall(_query(params), filters=filters, k=params.k,
                              mode=params.mode,
                              rerank_top_n=params.rerank_top_n)
            return json.dumps([
                {"node_id": h.node_id, "node_type": h.node_type,
                 "name": h.name,
                 "ts": h.ts.isoformat() if h.ts else None,
                 "score": h.score, "sources": list(h.sources)}
                for h in hits], ensure_ascii=False)
        except MemoryError as e:
            return _err_text(e)

    @mcp.tool(name="dnamemory_write_memory",
              annotations={"title": "写入记忆",
                           "readOnlyHint": False,
                           "destructiveHint": False,
                           "idempotentHint": True,
                           "openWorldHint": False})
    async def dnamemory_write_memory(params: WriteInput) -> str:
        """把一条日常记录拆解并写入记忆库（无 extractor 时写入原始事件）。

        Returns:
            str: JSON，含 accepted/rejected/ids。
        """
        try:
            result = mem.write_text(params.text, meta=params.meta or {})
            return json.dumps({"accepted": result.accepted,
                               "rejected": result.rejected,
                               "ids": result.ids}, ensure_ascii=False)
        except MemoryError as e:
            return _err_text(e)

    @mcp.tool(name="dnamemory_forget",
              annotations={"title": "遗忘记忆",
                           "readOnlyHint": False,
                           "destructiveHint": True,
                           "idempotentHint": False,
                           "openWorldHint": False})
    async def dnamemory_forget(params: ForgetInput) -> str:
        """墓碑/合规删除一个节点；force=True 进入 deleted 终态并级联。"""
        try:
            mem.forget(params.target, reason=params.reason,
                       force=params.force)
            return json.dumps({"ok": True}, ensure_ascii=False)
        except MemoryError as e:
            return _err_text(e)

    @mcp.tool(name="dnamemory_resolve_conflicts",
              annotations={"title": "解决事实冲突",
                           "readOnlyHint": False,
                           "destructiveHint": False,
                           "idempotentHint": True,
                           "openWorldHint": False})
    async def dnamemory_resolve_conflicts() -> str:
        """按来源可信级收敛同 key 活跃事实，返回决策列表。"""
        try:
            decisions = mem.resolve_conflicts()
            return json.dumps([
                {"kind": d.kind, "node_id": d.node_id,
                 "fact_key": d.fact_key,
                 "winner": str(d.winner.value),
                 "loser": str(d.loser.value)}
                for d in decisions], ensure_ascii=False)
        except MemoryError as e:
            return _err_text(e)

    return mcp


class BearerAuthMiddleware:
    """streamable_http 的 Bearer 校验（ASGI 中间件）。"""

    def __init__(self, app, token):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if not secrets.compare_digest(auth, f"Bearer {self.token}"):
            response = JSONResponse({"error": "Unauthorized"},
                                    status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def main():
    parser = argparse.ArgumentParser(description="dnamemory FastMCP 服务")
    parser.add_argument("--path", default=":memory:")
    parser.add_argument("--transport", default="stdio",
                        choices=("stdio", "streamable_http"))
    parser.add_argument("--token", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(path=args.path)
    if args.transport == "stdio":
        server.run()
        return
    token = args.token or os.environ.get("DNAMEMORY_TOKEN")
    if not token:
        raise SystemExit("streamable_http 需要 --token 或 DNAMEMORY_TOKEN")
    import uvicorn
    http_app = server.http_app(transport="streamable-http")
    uvicorn.run(BearerAuthMiddleware(http_app, token),
                host=args.host, port=args.port)


if __name__ == "__main__":
    main()
