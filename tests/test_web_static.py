# -*- coding: utf-8 -*-
"""0.8.0 C-01-T/C-02-T 静态托管与页面骨架 + 六页路由契约回归。"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from server.api.app import create_app

AUTH = {"Authorization": "Bearer t"}
WEB_DIR = Path(__file__).resolve().parent.parent / "server" / "web"


def _client(tmp_path):
    app = create_app(path=str(tmp_path / "m.db"), token="t")
    return TestClient(app)


def test_index_html(tmp_path):
    with _client(tmp_path) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        # 六区块容器 id
        for block in ("token-bar", "dashboard", "browser", "query-console",
                      "write-form", "govern-console"):
            assert f'id="{block}"' in r.text, block


def test_static_assets(tmp_path):
    with _client(tmp_path) as c:
        r1 = c.get("/static/app.js")
        assert r1.status_code == 200
        r2 = c.get("/static/style.css")
        assert r2.status_code == 200
        # 六页视图脚本（含子目录，wheel 打包易漏）
        r3 = c.get("/static/views/graph.js")
        assert r3.status_code == 200


def test_view_route_contract():
    """nav 视图名 ↔ window.VIEWS 键 ↔ section data-view 三方一致。

    回归：六页改造时 route() 用 $(viewName) 直取 id，而实际 id 是
    query-console/write-form/govern-console，叠加 .view{display:none} 后
    查询/写入/治理三页永久空白。此断言防同类错配复发。
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    nav = set(re.findall(r'href="#/([a-z]+)"', html))
    sections = set()
    for tag in re.findall(r"<section[^>]*>", html):
        m = re.search(r'data-view="([a-z]+)"', tag)
        if m:
            sections.add(m.group(1))
    views = set()
    for js in (WEB_DIR / "views").glob("*.js"):
        views |= set(re.findall(r"window\.VIEWS\.([a-z]+)\s*=",
                                js.read_text(encoding="utf-8")))

    assert nav, "nav 未解析到视图名"
    assert nav == views, f"nav {nav} != VIEWS {views}"
    assert nav == sections, f"nav {nav} != section data-view {sections}"
    # route() 必须按 data-view 定位，不得退回 $(viewName) 直取 id
    assert "data-view" in app_js


def test_ctx_blocks_contract(tmp_path):
    """查询台 16 区块：前端 CTX_BLOCKS ↔ /context 实际响应键 一一对应。

    回归：区块清单曾与响应脱节（标题写 16 个、实际渲染 15 个），且每块只有
    英文键名、看不出是什么意思。此断言锁三件事：数量 16、键名与响应一致、
    每块都带中文名与一行释义。
    """
    js = (WEB_DIR / "views" / "query.js").read_text(encoding="utf-8")
    body = js.split("const CTX_BLOCKS = [", 1)[1].split("\n  ];", 1)[0]
    keys = re.findall(r'key:\s*"([a-z_]+)"', body)
    cns = re.findall(r'cn:\s*"([^"]+)"', body)
    hints = re.findall(r'hint:\s*"([^"]+)"', body)
    groups = re.findall(r'group:\s*"([a-z]+)"', body)

    assert len(keys) == 16, f"上下文区块应为 16 个，实际 {len(keys)}"
    assert len(keys) == len(set(keys)), "区块 key 重复"
    assert len(cns) == len(hints) == 16, "每个区块都要有中文名与释义"
    assert all(any("一" <= ch <= "鿿" for ch in t) for t in cns), \
        "中文名不得退回英文键名"
    assert set(groups) == {"answer", "stance", "reason", "quality"}, groups

    with _client(tmp_path) as c:
        ctx = c.post("/context", headers=AUTH,
                     json={"text": "测试"}).json()
    assert set(keys) == set(ctx), (
        f"前端区块 {set(keys) ^ set(ctx)} 与 /context 响应键不一致")


def test_health_version(tmp_path):
    """顶栏版本徽标数据源（前端从 /health 取，不硬编码）。"""
    with _client(tmp_path) as c:
        body = c.get("/health", headers=AUTH).json()
        assert body["ok"] is True
        assert body["version"]


def test_api_not_shadowed(tmp_path):
    with _client(tmp_path) as c:
        r = c.get("/health", headers=AUTH)
        assert r.status_code == 200
        r2 = c.get("/stats", headers=AUTH)
        assert r2.status_code == 200
