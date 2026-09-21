# -*- coding: utf-8 -*-
"""0.5.0 阶段 H：FastAPI 新端点 + MCP 新工具测试卡（H-03-T/H-04-T）。"""
from datetime import datetime

import pytest

from dnamemory import MemorySystem


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient
    from server.api.app import create_app
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    u = mem.add_entity("用户", "person")
    mem.add_fact(u, "city", "南京", source="profile", confidence=0.9,
                 valid_at=datetime(2025, 1, 1),
                 invalid_at=datetime(2026, 7, 1))
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 7, 1))
    mem.close()
    app = create_app(path=str(tmp_path / "m.db"), token="secret-token")
    return TestClient(app)


# ---------------- H-03-T FastAPI 4 端点 ----------------
def test_server_state_endpoints(client):
    headers = {"Authorization": "Bearer secret-token"}
    # /context 返回结构化 context JSON
    r = client.post("/context", json={"text": "我现在住哪里"},
                    headers=headers)
    assert r.status_code == 200
    ctx = r.json()
    assert ctx["query_type"] == "current_state"
    assert any(item["value"] == "上海" for item in ctx["current_state"])
    # /timeline 返回有序时间线
    r2 = client.post("/timeline", json={"entity": "用户"}, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["nodes"]
    # /memory/{id}/history
    r3 = client.get("/memory/1/history", params={"dimension": "fact"},
                    headers=headers)
    assert r3.status_code == 200
    # explain：不存在 → 404 E006；无证据 → 404 E013
    r4 = client.get("/memory/999999/explain", headers=headers)
    assert r4.status_code == 404 and r4.json()["code"] == "E006"
    r5 = client.get("/memory/1/explain", headers=headers)
    assert r5.status_code == 404 and r5.json()["code"] == "E013"
    # 无 token 401
    assert client.post("/context", json={"text": "x"}).status_code == 401


# ---------------- O-02-T /context trace 输出 ----------------
def test_context_trace(client):
    headers = {"Authorization": "Bearer secret-token"}
    r = client.post("/context", json={"text": "我现在住哪里"},
                    headers=headers)
    assert r.status_code == 200
    trace = r.json().get("trace")
    assert trace is not None
    for key in ("query", "query_type", "retrieval_mode", "candidate_count",
                "rrf_candidates", "resolved_count", "current_state_count",
                "history_count", "belief_count", "chain_count",
                "conflict_count", "evidence_count", "final_context_count",
                "llm_used", "fallback_used"):
        assert key in trace


# ---------------- H-04-T MCP 3 工具 ----------------
def test_mcp_state_tools():
    import asyncio
    import inspect
    from server.mcp import create_server
    server = create_server()
    result = server.list_tools()
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    names = {t.name for t in result}
    assert {"dnamemory_context", "dnamemory_timeline",
            "dnamemory_explain"} <= names
