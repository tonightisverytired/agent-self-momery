# -*- coding: utf-8 -*-
"""0.8.1 IA-4-T API 解释性暴露：/context 附 memory_score、evidence 全字段、
explain 版本链+audits+related.confidence、GET /audit 查询。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from server.api.app import create_app

AUTH = {"Authorization": "Bearer t"}
T0 = datetime(2026, 9, 1)

SCORE_KEYS = {"retrieval", "temporal", "validity", "source", "evidence",
              "coherence", "conflict_penalty", "final"}


@pytest.fixture
def client(tmp_path):
    # 先建库（app 实例构造时的 _name2id 快照需包含实体）
    _seed(tmp_path)
    app = create_app(path=str(tmp_path / "m.db"), token="t")
    with TestClient(app) as c:
        yield c


def _seed(tmp_path):
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"), clock=lambda: T0)
    u = mem.add_entity("用户", "person")
    f1 = mem.add_fact(u, "city", "南京", source="chat", confidence=0.8,
                      valid_at=T0 - timedelta(days=30))
    f2 = mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                      valid_at=T0)
    evid = mem.store.insert_evidence(
        "conversation", "用户说搬到了上海", "conv-1", "m-9", T0,
        "hash-abc", 5, {"channel": "wechat"}, T0)
    with mem.store.transaction() as conn:
        conn.execute("UPDATE facts SET evidence_ids=? WHERE id=?",
                     (f"[{evid}]", f2))
    mem.store.insert_memory_link(f1, f2, "fact", "supersedes", 0.9, T0, None)
    # 留下 supersede_fact + conflict_resolve 审计（f1 被 f2 替代）
    mem.resolve_conflicts()
    mem.close()


def _current_fid(client):
    r = client.get("/facts", params={"entity": "用户", "key": "city"},
                   headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and items[0]["value"] == "上海"
    return items[0]["id"]


def test_context_current_state_has_memory_score(client):
    r = client.post("/context", json={"text": "我现在住哪里",
                                      "include_history": True},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["current_state"], "current_state 应有当前事实"
    cur = data["current_state"][0]
    assert "memory_score" in cur
    assert SCORE_KEYS <= set(cur["memory_score"])
    assert cur["memory_score"]["final"] is not None
    # historical_changes 各项同样附分项分（南京已进历史）
    assert data["historical_changes"], "include_history 下应有历史版本"
    assert "memory_score" in data["historical_changes"][0]


def test_context_evidence_full_fields(client):
    r = client.post("/context", json={"text": "我现在住哪里",
                                      "include_evidence": True},
                    headers=AUTH)
    assert r.status_code == 200, r.text
    ev = [e for e in r.json()["evidence"] if e["source_ref"]
          == "用户说搬到了上海"]
    assert ev, "证据应随上下文返回"
    e = ev[0]
    assert e["content_hash"] == "hash-abc"
    assert e["trust_level"] == 5
    assert e["conversation_id"] == "conv-1"
    assert e["message_id"] == "m-9"
    assert e["observed_at"].startswith("2026-09-01")
    assert e["metadata"] == {"channel": "wechat"}


def test_explain_fact_versions_related_audits(client):
    fid = _current_fid(client)
    r = client.get(f"/memory/{fid}/explain", params={"kind": "fact"},
                   headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    # 版本链：同 (entity, key) 全部版本（南京 → 上海）
    assert len(data["versions"]) == 2
    assert [v["value"] for v in data["versions"]] == ["南京", "上海"]
    # 证据全字段
    assert data["evidence"][0]["content_hash"] == "hash-abc"
    # related 带 confidence
    assert data["related"] and data["related"][0]["confidence"] == 0.9
    # audits：该事实（胜者）上有 conflict_resolve 记录
    ops = [a["op"] for a in data["audits"]]
    assert "conflict_resolve" in ops


def test_audit_endpoint(client):
    # 全量（最新在前）
    r = client.get("/audit", headers=AUTH)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items
    for it in items:
        assert {"id", "op", "target_type", "target_id", "reason", "meta",
                "at"} <= set(it)
    ids = [it["id"] for it in items]
    assert ids == sorted(ids, reverse=True)
    # op 过滤：supersede_fact 必有一行（seed 里 resolve_conflicts 产生）
    r = client.get("/audit", params={"op": "supersede_fact"}, headers=AUTH)
    sup = r.json()["items"]
    assert sup and all(it["op"] == "supersede_fact" for it in sup)
    assert "source_rank" in sup[0]["reason"]
    # target 过滤：loser fid 上的审计
    loser = sup[0]["target_id"]
    r = client.get("/audit", params={"target_type": "fact",
                                     "target_id": loser}, headers=AUTH)
    rows = r.json()["items"]
    assert rows and all(it["target_id"] == loser for it in rows)
    # limit 生效
    r = client.get("/audit", params={"limit": 1}, headers=AUTH)
    assert len(r.json()["items"]) == 1
    # 无鉴权 → 401
    r = client.get("/audit")
    assert r.status_code == 401
