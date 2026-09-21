# -*- coding: utf-8 -*-
"""0.8.0 静态后台图谱：GET /graph 与 /neighbors 含边库回归。"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from server.api.app import create_app

AUTH = {"Authorization": "Bearer t"}


def _seed(tmp_path, with_edges=True):
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    u = mem.add_entity("用户", "person")
    ev = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
    ev2 = mem.add_event("换工作", datetime(2026, 8, 1), kind="work")
    if with_edges:
        mem.add_edge(u, ev, "participates", weight=0.9)
        mem.add_edge(ev, ev2, "precedes", weight=0.6)
    mem.close()
    return u, ev, ev2


@pytest.fixture
def client(tmp_path):
    _seed(tmp_path)
    app = create_app(path=str(tmp_path / "m.db"), token="t")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def empty_client(tmp_path):
    from dnamemory import MemorySystem
    MemorySystem(path=str(tmp_path / "m.db")).close()
    app = create_app(path=str(tmp_path / "m.db"), token="t")
    with TestClient(app) as c:
        yield c


def test_graph_empty(empty_client):
    r = empty_client.get("/graph", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert d["nodes"] == [] and d["edges"] == [] and d["recent_events"] == []
    assert d["counts"]["nodes"] == 0 and d["counts"]["edges"] == 0
    assert d["truncated"] is False
    assert d["center"]["matched"] is False


def test_graph_seeded(client):
    r = client.get("/graph", headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    types = {n["node_type"] for n in d["nodes"]}
    assert types == {"entity", "event"}
    assert d["counts"]["entities"] == 1 and d["counts"]["events"] == 2
    assert d["counts"]["edges"] == 2
    assert d["counts"]["rel_counts"]["participates"] == 1
    assert d["counts"]["rel_counts"]["precedes"] == 1
    # 边两端都在返回节点集内，且带 rel/weight
    ids = {n["id"] for n in d["nodes"]}
    for e in d["edges"]:
        assert e["from"] in ids and e["to"] in ids
        assert e["rel"] in ("participates", "precedes")
        assert e["weight"] > 0
    # degree 与边一致
    deg = {n["id"]: n["degree"] for n in d["nodes"]}
    assert deg == {1: 1, 2: 2, 3: 1}
    # recent_events 按 ts 降序
    names = [n["name"] for n in d["recent_events"]]
    assert names == ["换工作", "搬到上海"]
    assert d["truncated"] is False


def test_graph_limit_truncated(client):
    r = client.get("/graph", params={"limit": 1}, headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert d["truncated"] is True
    ids = {n["id"] for n in d["nodes"]}
    for e in d["edges"]:
        assert e["from"] in ids and e["to"] in ids
    # counts 仍是截断前全量
    assert d["counts"]["edges"] == 2


def test_graph_node_types_filter(client):
    r = client.get("/graph", params={"node_types": "entity"}, headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert all(n["node_type"] == "entity" for n in d["nodes"])
    assert d["edges"] == []  # 异类边被滤


def test_graph_center_hops(client):
    r = client.get("/graph", params={"center": "用户", "hops": 1},
                   headers=AUTH)
    assert r.status_code == 200
    d = r.json()
    assert d["center"]["matched"] is True
    names = {n["name"] for n in d["nodes"]}
    assert names == {"用户", "搬到上海"}  # 直达一跳；换工作被隔开
    # 未命中不 404
    r2 = client.get("/graph", params={"center": "不存在"}, headers=AUTH)
    assert r2.status_code == 200
    assert r2.json()["center"]["matched"] is False
    assert r2.json()["nodes"] == []


def test_graph_include_archived(tmp_path, client):
    # 归档一个节点后：默认不可见，include_archived=true 可见
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    mem.store.update_lifecycle(3, "archived")
    mem.close()
    r = client.get("/graph", headers=AUTH)
    names = {n["name"] for n in r.json()["nodes"]}
    assert "换工作" not in names
    r2 = client.get("/graph", params={"include_archived": "true"},
                    headers=AUTH)
    names2 = {n["name"] for n in r2.json()["nodes"]}
    assert "换工作" in names2


def test_neighbors_with_edges_regression(client):
    """含边库 /neighbors 原 500（元组误过 _hit_dict）的回归测试。"""
    r = client.get("/neighbors", params={"node": "用户"}, headers=AUTH)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["node"]["name"] == "搬到上海"
    assert it["node"]["node_type"] == "event"
    assert it["rel"] == "participates"
    assert it["direction"] == "out"
    assert it["weight"] == 0.9
    assert it["confidence"] > 0


def test_neighbors_rel_filter(client):
    r = client.get("/neighbors", params={"node": "搬到上海", "rel": "precedes"},
                   headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["node"]["name"] == "换工作"
    assert items[0]["direction"] == "out"


def test_context_conflicts_fact_objects(tmp_path):
    """回归：核心库 ctx.conflicts 产出 Fact 对象（非 dict）时
    /context 序列化不得 500（原 TypeError: 'Fact' not subscriptable）。"""
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    u = mem.add_entity("用户", "person")
    # 同源同置信同时有效的并列事实 → 查询端冲突态
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 1, 1))
    mem.add_fact(u, "city", "北京", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 1, 1))
    mem.close()
    app = create_app(path=str(tmp_path / "m.db"), token="t")
    with TestClient(app) as c:
        r = c.post("/context", headers=AUTH,
                   json={"text": "用户住哪里", "include_history": True})
        assert r.status_code == 200, r.text
        conflicts = r.json()["conflicts"]
        assert conflicts
        assert conflicts[0]["kind"] == "fact_conflict"
        assert conflicts[0]["key"] == "city"
        assert set(conflicts[0]["values"]) == {"上海", "北京"}


def test_neighbors_direction_in(client):
    r = client.get("/neighbors", params={"node": "搬到上海"}, headers=AUTH)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    dirs = {it["node"]["name"]: it["direction"] for it in items}
    assert dirs["用户"] == "in"
    assert dirs["换工作"] == "out"


def test_conflict_dicts_drops_single_value_groups():
    """序列化只做格式转换：单值组一律丢弃（回归：曾被按原始 key 重新分组
    并缺少 len<2 守卫，产出「只有一个值」的假冲突）。"""
    from dnamemory.models import ConflictGroup
    from server.api.serializers import _conflict_dicts
    assert _conflict_dicts([
        ConflictGroup(node_id=1, key="phone_brand", canonical_key="phone_brand",
                      fact_ids=[20], values=["华为"])]) == []
    out = _conflict_dicts([
        ConflictGroup(node_id=1, key="city", canonical_key="位置",
                      fact_ids=[1, 2], values=["上海", "北京"])])
    assert len(out) == 1
    assert out[0]["kind"] == "fact_conflict"
    assert out[0]["values"] == ["上海", "北京"]
    assert out[0]["fact_ids"] == [1, 2]
    # 治理接口的 dict 形态仍兼容
    assert _conflict_dicts([{"kind": "hard", "node_id": 1, "key": "city"}]) \
        == [{"kind": "hard", "node_id": 1, "key": "city"}]
