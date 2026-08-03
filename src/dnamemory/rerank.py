# -*- coding: utf-8 -*-
"""Reranker 协议与 bge-reranker 参考实现。

重排是可选能力：未注入 reranker 时召回行为与现状完全一致；
reranker 抛错/分数异常时由调用方回退原排序（见 memory.recall）。
"""
from __future__ import annotations

import importlib.util
from typing import Protocol


class Reranker(Protocol):
    """调用方注入的重排回调：对候选文本打分，分数越大越靠前。"""

    model_name: str

    def rerank(self, query: str, texts: list[str]) -> list[float]: ...


class BGEReranker:
    """BAAI/bge-reranker-v2-m3（FlagReranker 惰性加载，本地缓存优先）。"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3",
                 use_fp16: bool = False, device: str | None = None,
                 **kwargs):
        if not importlib.util.find_spec("FlagEmbedding"):
            raise ImportError(
                "需要 FlagEmbedding：pip install FlagEmbedding（依赖 torch）")
        try:
            from FlagEmbedding import FlagReranker
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "FlagEmbedding 导入失败，请确认 torch 已安装") from e
        self.model_name = model_name
        from .embeddings import BGEM3Embedder
        local_path = BGEM3Embedder._resolve_local_path(model_name)
        self._model = FlagReranker(local_path, use_fp16=use_fp16,
                                   device=device, **kwargs)

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        scores = self._model.compute_score([[query, t] for t in texts],
                                           normalize=True)
        if isinstance(scores, (int, float)):
            scores = [scores]
        return [float(s) for s in scores]
