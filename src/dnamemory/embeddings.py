# -*- coding: utf-8 -*-
"""Embedder 协议与 bge-m3 参考实现。

稠密向量用于相似度召回；bge-m3 同时输出词级稀疏权重（lexical_weights），
写入存储层备用（不做写入校验，best-effort）。
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Optional, Protocol

from .errors import EmbeddingError


@dataclass
class EmbeddingResult:
    dense: list[float]
    sparse: Optional[dict[str, float]] = None
    dim: int = 0
    model: str = ""


class Embedder(Protocol):
    """调用方注入的向量化回调，库本体不强制依赖 torch/FlagEmbedding。"""

    model_name: str

    def embed(self, text: str) -> EmbeddingResult: ...

    def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]: ...


class BGEM3Embedder:
    """bge-m3（BAAI）中文友好，稠密 + 稀疏双输出。

    首次使用时自动从 HuggingFace 下载 BAAI/bge-m3；模型权重较大（约 2.3GB），
    建议一次性加载后复用。核心库不强制依赖，缺 FlagEmbedding 时给出安装提示。
    """

    def __init__(self, model_name: str = "BAAI/bge-m3",
                 use_fp16: bool = False, device: str | None = None,
                 max_length: int = 8192, **kwargs):
        if not importlib.util.find_spec("FlagEmbedding"):
            raise ImportError(
                "需要 FlagEmbedding：pip install FlagEmbedding（依赖 torch）")
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "FlagEmbedding 导入失败，请确认 torch 已安装") from e
        self.model_name = model_name
        self.max_length = max_length
        local_path = self._resolve_local_path(model_name)
        self._model = BGEM3FlagModel(local_path, use_fp16=use_fp16,
                                     device=device, **kwargs)

    @staticmethod
    def _resolve_local_path(model_name: str) -> str:
        """优先用本地缓存目录加载，避免每次初始化都联网检查 chat template。"""
        from huggingface_hub import snapshot_download
        try:
            return snapshot_download(model_name, local_files_only=True)
        except Exception:  # 首次使用：允许联网下载
            return snapshot_download(model_name)

    def _results(self, texts: list[str]) -> list[EmbeddingResult]:
        if not texts:
            return []
        out = self._model.encode(
            texts,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
            max_length=self.max_length,
        )
        dense = out.get("dense_vecs")
        sparse = out.get("lexical_weights")
        if dense is None:
            raise EmbeddingError("E011 bge-m3 未返回稠密向量")
        n = len(texts)
        results = []
        for i in range(n):
            vec = [float(x) for x in dense[i]]
            weights = None
            if sparse is not None:
                weights = {str(k): float(v) for k, v in sparse[i].items()}
            results.append(EmbeddingResult(
                dense=vec, sparse=weights, dim=len(vec),
                model=self.model_name))
        return results

    def embed(self, text: str) -> EmbeddingResult:
        return self._results([text])[0]

    def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        return self._results(texts)
