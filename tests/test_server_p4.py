# -*- coding: utf-8 -*-
"""P4 服务化验收用例：FastAPI 鉴权/往返/错误映射、MCP 服务冒烟。"""
import pytest

from dnamemory.extract import FallbackExtractor


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.dnamemory_server import create_app
    app = create_app(token="secret-token",
                     extractor=FallbackExtractor())
    return TestClient(app)


def test_health_requires_token(client):
    assert client.get("/health").status_code == 401
    assert client.get("/health",
                      headers={"Authorization": "Bearer secret-token"}
                      ).status_code == 200


def test_write_recall_forget_roundtrip(client):
    headers = {"Authorization": "Bearer secret-token"}
    r = client.post("/write", json={"text": "今天和张总讨论了预算"},
                    headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["accepted"] >= 1

    r2 = client.post("/recall", json={"text": "预算", "k": 5, "mode": "triple"},
                     headers=headers)
    assert r2.status_code == 200
    items = r2.json()["items"]
    assert items and any("预算" in h["name"] for h in items)

    target = data["ids"][0]
    r3 = client.post("/forget", json={"target": target, "reason": "retracted"},
                     headers=headers)
    assert r3.status_code == 200
    r4 = client.post("/recall", json={"text": "预算", "k": 5, "mode": "triple"},
                     headers=headers)
    names = {h["name"] for h in r4.json()["items"]}
    assert not any("预算" in n for n in names)


def test_error_mapping_and_validation(client):
    headers = {"Authorization": "Bearer secret-token"}
    r = client.post("/forget", json={"target": "不存在的节点",
                                     "reason": "retracted"},
                    headers=headers)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "E006"

    r2 = client.post("/recall", json={"text": "x", "mode": "badmode"},
                     headers=headers)
    assert r2.status_code == 422


def test_mcp_server_factory():
    from app.dnamemory_mcp import create_server
    server = create_server()
    assert server.name == "dnamemory_mcp"
