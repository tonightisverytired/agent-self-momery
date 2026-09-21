# -*- coding: utf-8 -*-
"""0.8.0 B-03-T 统一错误格式：状态码 + {code, message} 恰两键。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from server.api.app import create_app

AUTH = {"Authorization": "Bearer t"}


@pytest.fixture
def client(tmp_path):
    app = create_app(path=str(tmp_path / "m.db"), token="t")
    with TestClient(app) as c:
        yield c


def _seed(tmp_path):
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    u = mem.add_entity("用户", "person")
    mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
    mem.close()
    return u


def _err(client, method, url, **kw):
    r = getattr(client, method)(url, headers=AUTH, **kw)
    body = r.json()
    assert set(body) == {"code", "message"}, body
    return r.status_code, body


def test_e006_404(tmp_path, client):
    status, body = _err(client, "get", "/memory/9999/explain")
    assert status == 404 and body["code"] == "E006"


def test_e013_404(tmp_path, client):
    u = _seed(tmp_path)
    # fact 无证据 → E013
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 1, 1))
    fid = mem.store.fetch_facts()[0].fid
    mem.close()
    status, body = _err(client, "get", f"/memory/{fid}/explain",
                        params={"kind": "fact"})
    assert status == 404 and body["code"] == "E013"


def test_bad_time_400(client):
    status, body = _err(client, "post", "/context",
                        json={"text": "查询", "time": "not-a-time"})
    assert status == 400 and body["code"] == "E001"


def test_request_validation_422(client):
    status, body = _err(client, "post", "/context", json={})
    assert status == 422 and body["code"] == "E422"


def test_e010_no_extractor_400(client):
    status, body = _err(client, "post", "/write", json={"text": "你好"})
    # 核心库 ValidationError code 恒 E001，消息文本含 E010 提示
    assert status == 400 and body["code"] == "E001"
    assert "E010" in body["message"]


def test_e006_entity_missing_404(tmp_path, client):
    status, body = _err(client, "post", "/forget",
                        json={"target": "不存在的实体", "reason": "r"})
    assert status == 404 and body["code"] == "E006"
