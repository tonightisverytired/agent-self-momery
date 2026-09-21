# -*- coding: utf-8 -*-
"""0.8.0 B-05-T 查询端点补全：context 16 区块 / stats / facts / neighbors。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from server.api.app import create_app

AUTH = {"Authorization": "Bearer t"}
EXPECTED_CTX_KEYS = {
    "query_type", "current_state", "recent_events", "historical_changes",
    "beliefs", "intents", "temporal_chains", "evidence", "conflicts",
    "notes", "causes", "impacts", "impact_chains", "patterns", "score",
    "trace",
}


@pytest.fixture
def client(tmp_path):
    # 先建库（app 实例构造时的 _name2id 快照需包含实体）
    _seed(tmp_path)
    app = create_app(path=str(tmp_path / "m.db"), token="t")
    with TestClient(app) as c:
        yield c


def _post(client, url, **kw):
    r = client.post(url, headers=AUTH, **kw)
    assert r.status_code == 200, r.text
    return r.json()


def _seed(tmp_path):
    _post_c = None
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    u = mem.add_entity("用户", "person")
    ev = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 7, 1))
    mem.store.insert_belief(u, "上海生活成本高", "negative", 0.8, "chat",
                            datetime(2026, 1, 1), None, None, "active",
                            "public", [], datetime(2026, 1, 1))
    mem.store.insert_intent(u, "考虑离开上海", "active", 0.7, "chat",
                            datetime(2026, 8, 1), None, "active", "public",
                            [], datetime(2026, 8, 1))
    mem.store.insert_impact(u, "income", "increase", "positive", 0.7,
                            cause_event_id=ev,
                            valid_at=datetime(2026, 7, 2),
                            created_at=datetime(2026, 7, 2))
    mem.store.insert_pattern(u, "preference", "偏好早睡", confidence=0.8,
                             support=2, created_at=datetime(2026, 1, 1))
    mem.close()
    return u, ev


def test_context_16_blocks(tmp_path, client):
    data = _post(client, "/context",
                 json={"text": "我现在的情况", "include_history": True,
                       "include_beliefs": True, "include_evidence": True})
    assert EXPECTED_CTX_KEYS <= set(data), \
        f"缺键: {EXPECTED_CTX_KEYS - set(data)}"
    assert data["impacts"], "impacts 区块应有内容"
    assert data["impacts"][0]["dimension"] == "income"
    assert data["patterns"], "patterns 区块应有内容"
    assert data["trace"] is not None


def test_stats_counts(tmp_path, client):
    r = client.get("/stats", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["entities"] == 1 and data["events"] == 1
    assert data["facts"] == 1 and data["beliefs"] == 1
    assert data["intents"] == 1 and data["impacts"] == 1
    assert data["patterns"] == 1


def test_facts_lookup_canonical(tmp_path, client):
    r = client.get("/facts", params={"entity": "用户", "key": "city"},
                   headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and items[0]["value"] == "上海"
    # 未命中的 key 返回空列表（不 404）
    r2 = client.get("/facts", params={"entity": "用户", "key": "nope"},
                    headers=AUTH)
    assert r2.status_code == 200 and r2.json()["items"] == []


def test_facts_without_key_lists_all(tmp_path, client):
    """分面浏览：不带 key 时列出该实体全部当前事实。

    回归：此前 key 传空串给 fact_lookup，canonical key 归一后为空串，
    永远匹配不到任何事实 → 有事实的实体也返回空列表。
    """
    r = client.get("/facts", params={"entity": "用户"}, headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["key"] for i in items] == ["city"], items
    assert items[0]["value"] == "上海"
    # 不存在的实体仍返回空（不 404）
    r2 = client.get("/facts", params={"entity": "不存在"}, headers=AUTH)
    assert r2.status_code == 200 and r2.json()["items"] == []


def test_neighbors(tmp_path, client):
    r = client.get("/neighbors", params={"node": "用户"}, headers=AUTH)
    assert r.status_code == 200
    assert isinstance(r.json()["items"], list)


def test_entities_list_person(tmp_path, client):
    """GET /entities：默认按 kind=person 列 active 实体，附事实数降序。"""
    r = client.get("/entities", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "用户"
    assert data["items"][0]["kind"] == "person"
    assert data["items"][0]["fact_count"] == 1


def test_entities_kind_filter_empty(tmp_path, client):
    """kind 过滤无匹配时返回空（不 404）。"""
    r = client.get("/entities", params={"kind": "company"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"total": 0, "items": []}
