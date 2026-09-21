# -*- coding: utf-8 -*-
"""dnamemory-server 启动入口（0.8.0）。

运行（需先安装 dnamemory[server]）：
  dnamemory-server --path data/mvp_memory.db --token <token>
环境变量默认：DNAMEMORY_DB / DNAMEMORY_TOKEN / DNAMEMORY_HOST /
DNAMEMORY_PORT（命令行参数优先）。
"""
from __future__ import annotations

import argparse
import os

from dnamemory.extract import FallbackExtractor
from dnamemory.settings import apply_config

from server.api.app import create_app


def main():
    parser = argparse.ArgumentParser(description="dnamemory FastAPI 服务")
    parser.add_argument("--path", default=None,
                        help="SQLite 库路径（默认 DNAMEMORY_DB 或 :memory:）")
    parser.add_argument("--dsn", default=None,
                        help="PostgreSQL DSN（默认 DNAMEMORY_DSN；"
                             "提供后改用 PG 存储，忽略 --path）")
    parser.add_argument("--token", default=None,
                        help="Bearer token（默认 DNAMEMORY_TOKEN）")
    parser.add_argument("--host", default=None,
                        help="监听地址（默认 DNAMEMORY_HOST 或 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None,
                        help="监听端口（默认 DNAMEMORY_PORT 或 8000）")
    parser.add_argument("--fallback-extractor", action="store_true",
                        help="注入确定性兜底抽取器，/write 开箱可测")
    parser.add_argument("--bge-m3", action="store_true",
                        help="加载本地缓存的 bge-m3 模型，启用真实语义路"
                             "（DNAMEMORY_BGE_PATH 可指定本地目录）")
    parser.add_argument("--reranker", action="store_true",
                        help="加载 bge-reranker-v2-m3 交叉编码器重排"
                             "（DNAMEMORY_RERANKER_PATH 可指定本地目录）")
    parser.add_argument("--config", default=None,
                        help="配置文件路径（默认 ./dnamemory.env 或 "
                             "DNAMEMORY_CONFIG）")
    args = parser.parse_args()
    # 先注入配置文件的键（不覆盖已存在的环境变量），再读各项默认值
    try:
        applied = apply_config(args.config, strict=True)
    except FileNotFoundError as e:
        parser.error(str(e))
    if applied:
        # flush：输出被重定向到文件/管道时是块缓冲，不 flush 会晚于 uvicorn 日志
        print(f"[config] 已从配置文件注入 {len(applied)} 项: "
              f"{', '.join(sorted(applied))}", flush=True)
    path = args.path or os.environ.get("DNAMEMORY_DB") or ":memory:"
    dsn = args.dsn or os.environ.get("DNAMEMORY_DSN")
    token = args.token or os.environ.get("DNAMEMORY_TOKEN")
    host = args.host or os.environ.get("DNAMEMORY_HOST") or "127.0.0.1"
    port = args.port or int(os.environ.get("DNAMEMORY_PORT", "8000"))
    if not token:
        parser.error("需要 --token 或环境变量 DNAMEMORY_TOKEN")
    import uvicorn
    extractor = FallbackExtractor() if args.fallback_extractor else None
    embedder = None
    if args.bge_m3:
        from dnamemory.embeddings import BGEM3Embedder
        # 本地目录直通（os.path.isdir）或 HF 缓存/下载；生产建议用
        # DNAMEMORY_BGE_PATH 指向本地模型目录
        embedder = BGEM3Embedder(model_name=os.environ.get(
            "DNAMEMORY_BGE_PATH", "BAAI/bge-m3"))
    reranker = None
    if args.reranker:
        from dnamemory.rerank import BGEReranker
        reranker = BGEReranker(model_name=os.environ.get(
            "DNAMEMORY_RERANKER_PATH", "BAAI/bge-reranker-v2-m3"))
    app = create_app(path=path, token=token, extractor=extractor,
                     embedder=embedder, reranker=reranker, dsn=dsn)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
