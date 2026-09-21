# -*- coding: utf-8 -*-
"""0.8.0 B-04-T 结构化写入端点全能力。"""
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


def _post(client, url, **kw):
    r = client.post(url, headers=AUTH, **kw)
    assert r.status_code == 200, r.text
    return r.json()


def test_entities_and_events(tmp_path, client):
    eid = _post(client, "/entities", json={"name": "用户", "kind": "person"})
    assert eid["id"] > 0
    ev = _post(client, "/events",
               json={"name": "搬到上海", "ts": "2026-07-01T09:00:00",
                     "kind": "life"})
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    names = {n.name for n in mem.store.fetch_nodes()}
    assert {"用户", "搬到上海"} <= names
    mem.close()


def test_facts(tmp_path, client):
    _post(client, "/entities", json={"name": "用户", "kind": "person"})
    fid = _post(client, "/facts", json={"entity": "用户", "key": "city",
                                        "value": "上海", "source": "profile",
                                        "confidence": 0.9})
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    f = mem.store.fetch_facts()[0]
    assert f.fid == fid["id"] and f.value == "上海"
    mem.close()


def test_beliefs_intents(tmp_path, client):
    _post(client, "/entities", json={"name": "用户", "kind": "person"})
    _post(client, "/beliefs", json={"subject": "用户",
                                    "proposition": "上海成本高",
                                    "polarity": "negative"})
    _post(client, "/intents", json={"subject": "用户",
                                    "proposition": "考虑离开上海",
                                    "status": "active"})
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    assert mem.store.fetch_beliefs()[0].proposition == "上海成本高"
    assert mem.store.fetch_intents()[0].proposition == "考虑离开上海"
    mem.close()


def test_impacts_with_cause(tmp_path, client):
    _post(client, "/entities", json={"name": "用户", "kind": "person"})
    _post(client, "/events", json={"name": "换工作",
                                   "ts": "2026-07-01T09:00:00"})
    r = _post(client, "/impacts", json={
        "subject": "用户", "dimension": "income", "direction": "increase",
        "valence": "positive", "magnitude": 0.7, "kind": "objective",
        "evaluator": "user", "cause_event": "换工作"})
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    im = mem.store.fetch_impacts()[0]
    assert im.id == r["id"]
    assert im.cause_event_id is not None
    mem.close()


def test_evidence_and_edges(tmp_path, client):
    _post(client, "/entities", json={"name": "用户", "kind": "person"})
    _post(client, "/entities", json={"name": "咖啡", "kind": "preference"})
    _post(client, "/evidence", json={"source_type": "conversation",
                                     "source_ref": "conv-1"})
    _post(client, "/edges", json={"a": "用户", "b": "咖啡",
                                  "rel": "prefers"})
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    assert len(mem.store.fetch_evidence()) == 1
    assert len(mem.store.fetch_edges()) == 1
    mem.close()


def test_write_requires_extractor(client):
    r = client.post("/write", json={"text": "你好"}, headers=AUTH)
    assert r.status_code == 400
    assert "E010" in r.json()["message"]


def test_batch_write(tmp_path, client):
    r = client.post("/batch", json={"texts": ["a", "b", "c"]},
                    headers=AUTH)
    assert r.status_code == 400  # 无 extractor → E010 语义


# ---------------- 0.8.1 IA-1：批级证据自动闭环 + evidence_ids 透传 ----------------
@pytest.fixture
def client_fb(tmp_path):
    from dnamemory.extract import FallbackExtractor
    app = create_app(path=str(tmp_path / "m.db"), token="t",
                     extractor=FallbackExtractor())
    with TestClient(app) as c:
        yield c


def test_write_with_meta_auto_evidence(tmp_path, client_fb):
    """POST /write 带 meta：批内无 evidence 候选 → 自动合成批级证据。"""
    r = _post(client_fb, "/write",
              json={"text": "用户搬到了上海",
                    "meta": {"source_type": "inferred",
                             "conversation_id": "conv-1"}})
    assert r["accepted"] == 2  # 事件候选 + 自动证据
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    evs = mem.store.fetch_evidence()
    assert len(evs) == 1
    assert evs[0].source_type == "inferred"
    assert evs[0].conversation_id == "conv-1"
    assert evs[0].source_ref == "用户搬到了上海"
    node = [n for n in mem.store.fetch_nodes()
            if n.node_type == "event"][0]
    assert node.evidence_ids == [evs[0].id]
    # 重放不重复
    _post(client_fb, "/write", json={"text": "用户搬到了上海"})
    assert len(mem.store.fetch_evidence()) == 1
    mem.close()


def test_structured_write_evidence_ids(tmp_path, client):
    """POST /entities /events /facts 透传 evidence_ids。"""
    eid = _post(client, "/evidence", json={"source_type": "conversation",
                                           "source_ref": "conv-1"})["id"]
    nid1 = _post(client, "/entities", json={"name": "用户", "kind": "person",
                                            "evidence_ids": [eid]})["id"]
    nid2 = _post(client, "/events", json={"name": "搬到上海",
                                          "evidence_ids": [eid]})["id"]
    fid = _post(client, "/facts", json={"entity": "用户", "key": "city",
                                        "value": "上海",
                                        "evidence_ids": [eid]})["id"]
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    assert nodes[nid1].evidence_ids == [eid]
    assert nodes[nid2].evidence_ids == [eid]
    assert mem.store.fetch_facts()[0].evidence_ids == [eid]
    assert fid > 0
    mem.close()
