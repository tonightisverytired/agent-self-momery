# -*- coding: utf-8 -*-
"""0.8.0 B-06-T 治理端点全集。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from server.api.app import create_app

AUTH = {"Authorization": "Bearer t"}


@pytest.fixture
def client(tmp_path):
    # 先建库：用户实体 + 冲突事实对 + 事件 + 压力影响
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    mem.add_entity("用户", "person")
    mem.add_fact("用户", "口味", "辣的", source="chat", confidence=0.9,
                 valid_at=datetime(2026, 1, 1))
    mem.add_fact("用户", "口味", "甜的", source="chat", confidence=0.9,
                 valid_at=datetime(2026, 1, 1))
    mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
    mem.store.insert_impact(1, "stress", "increase", "negative", 0.8,
                            valid_at=datetime(2026, 7, 2),
                            created_at=datetime(2026, 7, 2))
    mem.store.insert_belief(1, "压力太大", "negative", 0.8, "chat",
                            datetime(2026, 7, 5), None, None, "active",
                            "public", [], datetime(2026, 7, 5))
    mem.close()
    app = create_app(path=str(tmp_path / "m.db"), token="t")
    with TestClient(app) as c:
        yield c


def _post(client, url, expect=200, **kw):
    r = client.post(url, headers=AUTH, **kw)
    assert r.status_code == expect, r.text
    return r.json()


def test_resolve_conflicts(client):
    data = _post(client, "/resolve-conflicts", json={})
    assert isinstance(data["decisions"], list)
    assert len(data["decisions"]) == 1  # 口味 辣/甜 冲突
    assert data["decisions"][0]["fact_key"] == "口味"


def test_confirm_choice(client):
    dec = _post(client, "/resolve-conflicts", json={})["decisions"][0]
    data = _post(client, "/confirm",
                 json={"decision": dec, "choice_value": "辣的"})
    assert data["ok"] is True


def test_derive_impact_links_idempotent(client):
    d1 = _post(client, "/derive-impact-links", json={})
    assert d1["links"] >= 1
    d2 = _post(client, "/derive-impact-links", json={})
    assert d2["links"] == 0, "幂等重跑不重复生成"


def test_compress_and_patterns(client):
    # 无月度摘要时 compress 返回 0
    d1 = _post(client, "/compress-stable", json={})
    assert d1["created"] == 0
    d2 = _post(client, "/extract-patterns", json={})
    assert isinstance(d2["patterns"], int)


def test_step_day(client):
    data = _post(client, "/step-day", json={})
    assert isinstance(data, dict)


def test_forget_and_restore(client, tmp_path):
    _post(client, "/forget", json={"target": "搬到上海", "reason": "测试"})
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    ev = next(n for n in mem.store.fetch_nodes() if n.name == "搬到上海")
    assert ev.lifecycle == "tombstoned"
    mem.close()
    data = _post(client, "/restore", json={"node_id": ev.nid})
    assert data["ok"] is True


def test_restore_missing_404(client):
    _post(client, "/restore", json={"node_id": 9999}, expect=404)


def test_reflect_rule_based(client):
    # 规则版反射不依赖 extractor（0.6.0 语义）
    data = _post(client, "/reflect", json={"year": 2026, "month": 7})
    assert data["summary_id"] is not None
