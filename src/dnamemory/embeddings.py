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


def _split_chunks(text, size, overlap):
    """按字符切块（步进 size-overlap）；短文本原样单块。"""
    if len(text) <= size:
        return [text]
    chunks, i = [], 0
    step = max(1, size - overlap)
    while i < len(text):
        chunks.append(text[i:i + size])
        i += step
    return chunks


def _mean_pool_dense(vecs):
    """多块稠密向量均值池化后 L2 归一。"""
    import numpy
    arr = numpy.asarray([list(v) for v in vecs], dtype=float)
    m = arr.mean(axis=0)
    n = numpy.linalg.norm(m)
    if n == 0:
        return [float(x) for x in m]
    return [float(x) for x in (m / n)]


def _max_merge_sparse(dicts):
    """多块稀疏权重按 token 取 max 合并。"""
    merged = {}
    for d in dicts:
        for k, v in d.items():
            fv = float(v)
            if k not in merged or fv > merged[k]:
                merged[k] = fv
    return merged

class BGEM3Embedder:
    """bge-m3（BAAI）中文友好，稠密 + 稀疏双输出。

    首次使用时自动从 HuggingFace 下载 BAAI/bge-m3；模型权重较大（约 2.3GB），
    建议一次性加载后复用。核心库不强制依赖，缺 FlagEmbedding 时给出安装提示。
    """

    def __init__(self, model_name: str = "BAAI/bge-m3",
                 use_fp16: bool = False, device: str | None = None,
                 max_length: int = 8192, chunk_chars: int = 1200,
                 chunk_overlap: int = 150, **kwargs):
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
        self.chunk_chars = chunk_chars
        self.chunk_overlap = chunk_overlap
        local_path = self._resolve_local_path(model_name)
        self._model = BGEM3FlagModel(local_path, use_fp16=use_fp16,
                                     device=device, **kwargs)

    @staticmethod
    def _resolve_local_path(model_name: str) -> str:
        """优先用本地缓存目录加载，避免每次初始化都联网检查 chat template；model_name 是已存在的本地目录时直接使用。"""
        import os
        if os.path.isdir(model_name):
            return model_name
        from huggingface_hub import snapshot_download
        try:
            return snapshot_download(model_name, local_files_only=True)
        except Exception:  # 首次使用：允许联网下载
            return snapshot_download(model_name)

    def _results(self, texts: list[str]) -> list[EmbeddingResult]:
        if not texts:
            return []
        # 长文本分块（0.8.1）：超过 chunk_chars 的文本切块编码，
        # 稠密均值池化 + 稀疏按 token 取 max，整段语义不被头部截断
        groups = [_split_chunks(t, self.chunk_chars, self.chunk_overlap)
                  for t in texts]
        flat = [c for g in groups for c in g]
        out = self._model.encode(
            flat,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
            max_length=self.max_length,
        )
        dense = out.get("dense_vecs")
        sparse = out.get("lexical_weights")
        if dense is None:
            raise EmbeddingError("E011 bge-m3 未返回稠密向量")
        results = []
        idx = 0
        for g in groups:
            k = len(g)
            vec = _mean_pool_dense(dense[idx:idx + k])
            weights = None
            if sparse is not None:
                merged = _max_merge_sparse(sparse[idx:idx + k])
                weights = {str(kk): float(vv) for kk, vv in merged.items()}
            results.append(EmbeddingResult(
                dense=vec, sparse=weights, dim=len(vec),
                model=self.model_name))
            idx += k
        return results

    def embed(self, text: str) -> EmbeddingResult:
        return self._results([text])[0]

    def embed_batch(self, texts: list[str]) -> list[EmbeddingResult]:
        return self._results(texts)
