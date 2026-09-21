# -*- coding: utf-8 -*-
"""0.8.0 A-03-T MCP 迁移回归：7 工具名不变、stdio 直调可执行。"""
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
}


def _tool_names(tools):
    names = set()
    for t in tools:
        try:
            names.add(t.name)
        except AttributeError:
            names.add(t.fn.__name__ if hasattr(t, "fn") else str(t))
    return names


def test_create_server_returns_fastmcp(tmp_path):
    from fastmcp import FastMCP
    mcp = create_server(path=str(tmp_path / "m.db"))
    assert isinstance(mcp, FastMCP)


def test_tool_names_exact(tmp_path):
    """B-08 扩展后：既有 7 工具必须仍在（新增工具另测）。"""
    mcp = create_server(path=str(tmp_path / "m.db"))
    tools = mcp.list_tools()
    if inspect.isawaitable(tools):
        tools = asyncio.run(tools)
    names = _tool_names(tools)
    assert EXPECTED_TOOLS <= names


def test_stdio_direct_call(tmp_path):
    from server.mcp import RecallInput
    mcp = create_server(path=str(tmp_path / "m.db"))
    tools = mcp.list_tools()
    if inspect.isawaitable(tools):
        tools = asyncio.run(tools)
    recall_tool = next(t for t in tools if t.name == "dnamemory_recall")
    fn = recall_tool.fn if hasattr(recall_tool, "fn") else recall_tool
    out = fn(RecallInput(text="今天天气", k=3))
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    data = json.loads(out)
    assert isinstance(data, list)
