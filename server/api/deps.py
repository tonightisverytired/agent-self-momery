# -*- coding: utf-8 -*-
"""FastAPI 依赖（0.8.0 B-02）：Bearer 鉴权 + 内存实例注入。"""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import (HTTPAuthorizationCredentials, HTTPBearer)


def build_auth(token: str):
    """构造 require_auth 依赖：常量时间比较，401 体统一 {code,message}。"""
    expected_auth = f"Bearer {token}"
    security = HTTPBearer(auto_error=False)

    def require_auth(request: Request,
                     credentials: HTTPAuthorizationCredentials | None =
                     Depends(security)):
        provided = f"Bearer {credentials.credentials}" \
            if credentials is not None else ""
        if not secrets.compare_digest(provided, expected_auth):
            raise HTTPException(status_code=401,
                                detail={"code": "E401",
                                        "message": "Unauthorized"})

    return require_auth


def get_memory(request: Request):
    """从 app.state 取 MemorySystem 单例（create_app 工厂装配）。"""
    return request.app.state.memory
