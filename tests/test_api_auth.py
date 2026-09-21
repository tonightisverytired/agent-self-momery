# -*- coding: utf-8 -*-
"""0.8.0 B-02-T 鉴权与内存依赖。"""
import pytest
from fastapi.testclient import TestClient

from server.api.app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(path=str(tmp_path / "m.db"), token="secret-token")
    with TestClient(app) as c:
        yield c


def test_no_token_401(client):
    r = client.get("/health")
    assert r.status_code == 401
    body = r.json()
    assert set(body) == {"code", "message"}
    assert body["code"] == "E401"


def test_wrong_token_401(client):
    r = client.get("/health",
                   headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert r.json()["code"] == "E401"


def test_correct_token_200(client):
    r = client.get("/health",
                   headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"]
