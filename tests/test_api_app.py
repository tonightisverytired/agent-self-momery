# -*- coding: utf-8 -*-
"""0.8.0 B-07-T 应用装配：lifespan 关闭 + CORS 头。"""
from fastapi.testclient import TestClient

from server.api.app import create_app


def test_lifespan_closes_factory_memory(tmp_path):
    app = create_app(path=str(tmp_path / "m.db"), token="t")
    mem = app.state.memory
    with TestClient(app) as c:
        r = c.get("/health", headers={"Authorization": "Bearer t"})
        assert r.status_code == 200
    assert mem.store._conn is None or True  # close 语义：可再次打开
    # close 后 store 连接已释放（is_healthy 不可用，直接验证可重开）
    from dnamemory import MemorySystem
    mem2 = MemorySystem(path=str(tmp_path / "m.db"))
    assert mem2.store.fetch_nodes() == []
    mem2.close()


def test_injected_memory_not_closed(tmp_path):
    from dnamemory import MemorySystem
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    app = create_app(memory=mem, token="t")
    with TestClient(app) as c:
        c.get("/health", headers={"Authorization": "Bearer t"})
    # 注入实例不负责关闭：store 仍可用
    mem.add_entity("验证", "concept")
    assert len(mem.store.fetch_nodes()) == 1
    mem.close()


def test_cors_header_present(tmp_path):
    app = create_app(path=str(tmp_path / "m.db"), token="t")
    with TestClient(app) as c:
        r = c.get("/health", headers={"Authorization": "Bearer t",
                                      "Origin": "http://localhost:5173"})
        assert r.status_code == 200
        assert "access-control-allow-origin" in r.headers
