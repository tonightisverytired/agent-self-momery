# -*- coding: utf-8 -*-
"""FastMCP 服务（0.8.0 A-03：自 app/dnamemory_mcp.py 迁移，语义零改动）。

运行（需先安装 dnamemory[mcp]）：
  dnamemory-mcp --path data/mvp_memory.db                      # stdio
  dnamemory-mcp --transport streamable_http --token <token>
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
from datetime import datetime
from typing import Optional

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from dnamemory import MemorySystem, RecallFilters, RecallQuery
from dnamemory.errors import (ConflictError, EmbeddingError,
                              MemoryError, NotFoundError, StorageError,
                              ValidationError)
from dnamemory.models import RelationFilter
from dnamemory.settings import apply_config


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


class ContextInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    text: str = Field(..., min_length=1, max_length=20000,
                      description="自然语言查询，如 '我现在住哪里'")
    query_type: Optional[str] = Field(
        default=None,
        description="current_state/history/timeline/change/why_change/"
                    "semantic_recall，缺省按规则路由")
    time: Optional[str] = Field(default=None,
                                description="查询时间 ISO，缺省当前时间")


class TimelineInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    entity: Optional[str] = Field(default=None, description="实体名称或 id")
    start: Optional[str] = Field(default=None, description="起始时间 ISO")
    end: Optional[str] = Field(default=None, description="结束时间 ISO")



class StatsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class StructuredWriteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    kind: str = Field(..., description="entity/event/fact/belief/"
                                       "intent/impact/evidence/edge")
    name: Optional[str] = Field(default=None, description="entity/event 名")
    entity: Optional[str] = Field(default=None, description="fact 实体名")
    key: Optional[str] = None
    value: Optional[str] = None
    subject: Optional[str] = Field(default=None, description="belief/"
                                                           "intent/"
                                                           "impact 主体")
    proposition: Optional[str] = None
    polarity: Optional[str] = None
    status: Optional[str] = None
    dimension: Optional[str] = None
    direction: Optional[str] = None
    valence: Optional[str] = None
    magnitude: Optional[float] = None
    source_type: Optional[str] = None
    a: Optional[str] = None
    b: Optional[str] = None
    rel: Optional[str] = None
    ts: Optional[str] = None
    source: Optional[str] = None
    confidence: Optional[float] = None
    idempotency_key: Optional[str] = None


class GovernInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: str = Field(..., description="resolve_conflicts/confirm/"
                                         "resolve_entities/reflect/"
                                         "compress_stable/"
                                         "extract_patterns/"
                                         "derive_impact_links/"
                                         "step_day/restore")
    year: Optional[int] = None
    month: Optional[int] = None
    node_id: Optional[int] = None


class ExplainInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    memory_id: int = Field(..., description="记忆 id（node/fact/belief/intent）")
    kind: Optional[str] = Field(
        default=None, description="node/fact/belief/intent，跨表 id 消歧")


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
                  extractor=None, dsn=None) -> FastMCP:
    mem = MemorySystem(path=path, dsn=dsn, embedder=embedder,
                       reranker=reranker, extractor=extractor)
    mcp = FastMCP("dnamemory_mcp")
    # 供 main() 退出时释放存储（PG 后端不关会留下连接）
    mcp._dnamemory_memory = mem

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

    @mcp.tool(name="dnamemory_context",
              annotations={"title": "记忆上下文（状态解析）",
                           "readOnlyHint": True,
                           "destructiveHint": False,
                           "idempotentHint": True,
                           "openWorldHint": False})
    async def dnamemory_context(params: ContextInput) -> str:
        """按 query 路由到状态解析管线，返回结构化 MemoryContext JSON。"""
        try:
            t0 = datetime.fromisoformat(params.time) if params.time else None
            ctx = mem.recall_context(
                RecallQuery(text=params.text), query_type=params.query_type,
                query_time=t0)
            from server.api.serializers import _ctx_dict
            return json.dumps(_ctx_dict(ctx), ensure_ascii=False)
        except MemoryError as e:
            return _err_text(e)

    @mcp.tool(name="dnamemory_timeline",
              annotations={"title": "实体时间线",
                           "readOnlyHint": True,
                           "destructiveHint": False,
                           "idempotentHint": True,
                           "openWorldHint": False})
    async def dnamemory_timeline(params: TimelineInput) -> str:
        """按稳定时间排序返回实体记忆时间线 JSON。"""
        try:
            start = datetime.fromisoformat(params.start) \
                if params.start else None
            end = datetime.fromisoformat(params.end) if params.end else None
            nodes = mem.timeline(entity_id=params.entity, start=start,
                                 end=end)
            return json.dumps({"nodes": [
                {"dimension": n.dimension, "relation": n.relation,
                 "at": n.at.isoformat() if n.at else None,
                 "name": getattr(n.memory, "name",
                                 getattr(n.memory, "value", ""))}
                for n in nodes]}, ensure_ascii=False)
        except MemoryError as e:
            return _err_text(e)

    @mcp.tool(name="dnamemory_explain",
              annotations={"title": "解释记忆来源",
                           "readOnlyHint": True,
                           "destructiveHint": False,
                           "idempotentHint": True,
                           "openWorldHint": False})
    async def dnamemory_explain(params: ExplainInput) -> str:
        """返回记忆的证据、版本、来源与关联（无证据返回 E013）。"""
        try:
            from .api.serializers import _explain_version_dict
            ex = mem.explain(params.memory_id, kind=params.kind)
            return json.dumps({
                "memory_id": params.memory_id,
                "source": ex["source"],
                "evidence": [{"id": e.id, "source_type": e.source_type,
                              "source_ref": e.source_ref}
                             for e in ex["evidence"]],
                # 0.8.1 IA-4：fact/belief/intent 的版本链是领域对象
                # （非 versions 表元组），统一走序列化辅助
                "versions": [_explain_version_dict(v)
                             for v in ex["versions"]],
                "related": ex["related"],
                "audits": ex["audits"],
            }, ensure_ascii=False)
        except MemoryError as e:
            return _err_text(e)

    @mcp.tool(name="dnamemory_stats",
              annotations={"title": "库统计",
                           "readOnlyHint": True,
                           "destructiveHint": False,
                           "idempotentHint": True,
                           "openWorldHint": False})
    async def dnamemory_stats() -> str:
        """返回记忆库各维度计数（节点/事实/观点/意图/影响/证据/边/
        触发器/模式/记忆链接）。"""
        try:
            nodes = mem.store.fetch_nodes()
            return json.dumps({
                "nodes": len(nodes),
                "entities": sum(1 for n in nodes
                                if n.node_type == "entity"),
                "events": sum(1 for n in nodes
                              if n.node_type == "event"),
                "facts": len(mem.store.fetch_facts()),
                "beliefs": len(mem.store.fetch_beliefs()),
                "intents": len(mem.store.fetch_intents()),
                "impacts": len(mem.store.fetch_impacts()),
                "evidence": len(mem.store.fetch_evidence()),
                "edges": len(mem.store.fetch_edges()),
                "triggers": len(mem.store.fetch_triggers()),
                "patterns": len(mem.store.fetch_patterns()),
                "memory_links": len(mem.store.fetch_memory_links()),
            }, ensure_ascii=False)
        except MemoryError as e:
            return _err_text(e)

    @mcp.tool(name="dnamemory_write_structured",
              annotations={"title": "结构化写入记忆",
                           "readOnlyHint": False,
                           "destructiveHint": False,
                           "idempotentHint": True,
                           "openWorldHint": False})
    async def dnamemory_write_structured(params: StructuredWriteInput) -> str:
        """按 kind 分发的结构化写入（不走 LLM 抽取）。

        Args:
            params (StructuredWriteInput): kind 决定必填字段：
              entity(name) / event(name,ts) / fact(entity,key,value) /
              belief(subject,proposition,polarity) /
              intent(subject,proposition,status) /
              impact(subject,dimension,direction,valence,magnitude) /
              evidence(source_type) / edge(a,b,rel)。

        Returns:
            str: JSON {id: ...}。
        """
        try:
            t0 = datetime.fromisoformat(params.ts) if params.ts else None
            kind = params.kind
            if kind == "entity":
                nid = mem.add_entity(params.name or "")
                return json.dumps({"id": nid}, ensure_ascii=False)
            if kind == "event":
                nid = mem.add_event(params.name or "", t0)
                return json.dumps({"id": nid}, ensure_ascii=False)
            if kind == "fact":
                fid = mem.add_fact(params.entity or "", params.key or "",
                                   params.value or "",
                                   source=params.source or "chat",
                                   confidence=params.confidence or 0.7,
                                   idempotency_key=params.idempotency_key)
                return json.dumps({"id": fid}, ensure_ascii=False)
            if kind == "belief":
                bid = mem.add_belief(params.subject or "",
                                     params.proposition or "",
                                     polarity=params.polarity or "neutral",
                                     confidence=params.confidence or 0.7,
                                     idempotency_key=params.idempotency_key)
                return json.dumps({"id": bid}, ensure_ascii=False)
            if kind == "intent":
                iid = mem.add_intent(params.subject or "",
                                     params.proposition or "",
                                     status=params.status or "active",
                                     confidence=params.confidence or 0.7,
                                     idempotency_key=params.idempotency_key)
                return json.dumps({"id": iid}, ensure_ascii=False)
            if kind == "impact":
                iid = mem.add_impact(params.subject or "",
                                     params.dimension or "",
                                     params.direction or "stable",
                                     params.valence or "neutral",
                                     params.magnitude or 0.5,
                                     idempotency_key=params.idempotency_key)
                return json.dumps({"id": iid}, ensure_ascii=False)
            if kind == "evidence":
                eid = mem.add_evidence(params.source_type or "conversation")
                return json.dumps({"id": eid}, ensure_ascii=False)
            if kind == "edge":
                eid = mem.add_edge(params.a or "", params.b or "",
                                   params.rel or "mentions")
                return json.dumps({"id": eid}, ensure_ascii=False)
            return _err_text(ValidationError(
                f"E001 未知 kind: {kind}"))
        except MemoryError as e:
            return _err_text(e)

    @mcp.tool(name="dnamemory_govern",
              annotations={"title": "记忆治理操作",
                           "readOnlyHint": False,
                           "destructiveHint": False,
                           "idempotentHint": True,
                           "openWorldHint": False})
    async def dnamemory_govern(params: GovernInput) -> str:
        """按 action 分发治理操作（反射/压缩/模式提取/影响链/冲突等）。

        Args:
            params (GovernInput): action 枚举 + 对应参数
              （reflect 需 year/month；restore 需 node_id）。

        Returns:
            str: JSON 结果。
        """
        try:
            a = params.action
            if a == "resolve_conflicts":
                decisions = mem.resolve_conflicts()
                return json.dumps({"decisions": [
                    {"kind": d.kind, "node_id": d.node_id,
                     "fact_key": d.fact_key,
                     "winner": str(d.winner.value),
                     "loser": str(d.loser.value)} for d in decisions]},
                    ensure_ascii=False)
            if a == "reflect":
                summary = mem.reflect_monthly(params.year, params.month)
                return json.dumps({"summary_id": summary},
                                  ensure_ascii=False)
            if a == "compress_stable":
                return json.dumps({"created": mem.compress_stable()},
                                  ensure_ascii=False)
            if a == "extract_patterns":
                return json.dumps({"patterns": mem.extract_patterns()},
                                  ensure_ascii=False)
            if a == "derive_impact_links":
                from dnamemory.temporal import derive_impact_links
                return json.dumps(
                    {"links": derive_impact_links(mem.store)},
                    ensure_ascii=False)
            if a == "step_day":
                return json.dumps({"archived": mem.step_day()},
                                  ensure_ascii=False)
            if a == "restore":
                mem.restore(params.node_id)
                return json.dumps({"ok": True}, ensure_ascii=False)
            if a == "resolve_entities":
                merged = mem.resolve_entities()
                return json.dumps({"merged": merged}, ensure_ascii=False)
            if a == "confirm":
                return json.dumps({"ok": False, "reason": "confirm 需经 "
                                   "resolve_conflicts 决策对象，请走 HTTP "
                                   "/confirm"}, ensure_ascii=False)
            return _err_text(ValidationError(
                f"E001 未知 action: {a}"))
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
    parser.add_argument("--dsn", default=None,
                        help="PostgreSQL DSN（默认 DNAMEMORY_DSN；"
                             "提供后改用 PG 存储，忽略 --path）")
    parser.add_argument("--transport", default="stdio",
                        choices=("stdio", "streamable_http"))
    parser.add_argument("--token", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", default=None,
                        help="配置文件路径（默认 ./dnamemory.env 或 "
                             "DNAMEMORY_CONFIG）")
    args = parser.parse_args()
    try:
        apply_config(args.config, strict=True)
    except FileNotFoundError as e:
        parser.error(str(e))
    dsn = args.dsn or os.environ.get("DNAMEMORY_DSN")
    server = create_server(path=args.path, dsn=dsn)
    mem = getattr(server, "_dnamemory_memory", None)
    try:
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
    finally:
        # 退出时释放存储：SQLite 释放文件句柄，PG 释放连接（原实现从不 close）
        if mem is not None:
            mem.close()


if __name__ == "__main__":
    main()
