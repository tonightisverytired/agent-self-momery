# -*- coding: utf-8 -*-
"""0.8.0 B-08-T MCP 全能力：7+3 工具、16 区块 context、结构化写入。"""
import asyncio
import inspect
import json
from datetime import datetime

import pytest

from server.mcp import create_server

EXPECTED_TOOLS = {
    "dnamemory_recall", "dnamemory_write_memory", "dnamemory_forget",
    "dnamemory_resolve_conflicts", "dnamemory_context",
    "dnamemory_timeline", "dnamemory_explain",
    # 0.8.0 新增
    "dnamemory_stats", "dnamemory_write_structured", "dnamemory_govern",
}

CTX_KEYS = {
    "query_type", "current_state", "recent_events", "historical_changes",
    "beliefs", "intents", "temporal_chains", "evidence", "conflicts",
    "notes", "causes", "impacts", "impact_chains", "patterns", "score",
    "trace",
}


def _tools(mcp):
    tools = mcp.list_tools()
    if inspect.isawaitable(tools):
        tools = asyncio.run(tools)
    return {t.name: (t.fn if hasattr(t, "fn") else t) for t in tools}


def _call(fn, params=None):
    out = fn(params) if params is not None else fn()
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    return json.loads(out)


def test_tool_names_full(tmp_path):
    tools = _tools(create_server(path=str(tmp_path / "m.db")))
    assert set(tools) == EXPECTED_TOOLS


def test_context_16_blocks(tmp_path):
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 1, 1))
    mem.close()
    from server.mcp import ContextInput
    tools = _tools(create_server(path=str(tmp_path / "m.db")))
    data = _call(tools["dnamemory_context"],
                 ContextInput(text="我现在住哪里"))
    assert CTX_KEYS <= set(data), f"缺键: {CTX_KEYS - set(data)}"


def test_write_structured_belief(tmp_path):
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    mem.add_entity("用户", "person")
    mem.close()
    from server.mcp import StructuredWriteInput
    tools = _tools(create_server(path=str(tmp_path / "m.db")))
    data = _call(tools["dnamemory_write_structured"],
                 StructuredWriteInput(kind="belief", subject="用户",
                                      proposition="上海成本高",
                                      polarity="negative"))
    assert data.get("id")
    mem2 = MemorySystem(path=str(tmp_path / "m.db"))
    assert mem2.store.fetch_beliefs()[0].proposition == "上海成本高"
    mem2.close()


def test_stats_and_govern(tmp_path):
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    mem.add_entity("用户", "person")
    mem.close()
    from server.mcp import GovernInput
    tools = _tools(create_server(path=str(tmp_path / "m.db")))
    stats = _call(tools["dnamemory_stats"])
    assert stats["entities"] == 1
    gov = _call(tools["dnamemory_govern"],
                GovernInput(action="extract_patterns"))
    assert "patterns" in gov
