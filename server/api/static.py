# -*- coding: utf-8 -*-
"""静态后台托管（0.8.0 C-01）：StaticFiles + "/" 返回 SPA。

必须在 API 路由注册之后挂载（挂载遮蔽优先级低于已注册路由）。
web 目录用包内路径定位（wheel 内可靠）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def mount_web(app: FastAPI):
    """挂载 /static 静态资源与 / 首页（放在 API 路由之后）。"""
    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)),
                  name="web-static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")
