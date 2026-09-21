# -*- coding: utf-8 -*-
"""0.8.1 分块嵌入（长文本 chunk + 均值池化 + 稀疏 max 合并）回归。"""
import pytest

from dnamemory.embeddings import (BGEM3Embedder, _max_merge_sparse,
                                  _mean_pool_dense, _split_chunks)


def test_split_chunks_short_text_single():
    assert _split_chunks("短文本", 1200, 150) == ["短文本"]


def test_split_chunks_overlap_and_cover():
    text = "x" * 2500
    chunks = _split_chunks(text, 1200, 150)
    assert len(chunks) == 3            # 1200 + 1200 + 100（步进 1050）
    assert chunks[0] == text[:1200]
    assert chunks[1] == text[1050:2250]
    assert chunks[2] == text[2100:]
    assert "".join(c[150:] if i else c for i, c in enumerate(chunks)) == text


def test_mean_pool_dense_normalized():
    out = _mean_pool_dense([[1.0, 0.0], [0.0, 1.0]])
    assert out == pytest.approx([2 ** -0.5, 2 ** -0.5])
    assert _mean_pool_dense([[0.0, 0.0]]) == [0.0, 0.0]


def test_max_merge_sparse():
    merged = _max_merge_sparse([{"a": 0.3, "b": 0.5}, {"a": 0.9, "c": 0.2}])
    assert merged == {"a": 0.9, "b": 0.5, "c": 0.2}


class _StubModel:
    """按块内容长度返回可区分向量，验证分块-池化管线。"""

    def encode(self, texts, **kw):
        dense = [[float(len(t)), 1.0] for t in texts]
        sparse = [{f"tok{len(t)}": 0.5} for t in texts]
        return {"dense_vecs": dense, "lexical_weights": sparse}


def _stub_embedder():
    emb = BGEM3Embedder.__new__(BGEM3Embedder)   # 跳过 __init__（不装模型）
    emb.model_name = "stub/bge-m3"
    emb.max_length = 8192
    emb.chunk_chars = 10
    emb.chunk_overlap = 2
    emb._model = _StubModel()
    return emb


def test_chunked_results_pool_per_text():
    emb = _stub_embedder()
    res = emb.embed_batch(["abc", "x" * 25])
    assert len(res) == 2
    # 短文本单块：[3,1] 归一化
    assert res[0].dense == pytest.approx([3 / 10 ** 0.5, 1 / 10 ** 0.5])
    assert res[0].sparse == {"tok3": 0.5}
    # 25 字 → 步进 8：4 块（10/10/9/1），均值 [7.5, 1.0] 归一化
    import math
    n = math.hypot(7.5, 1.0)
    assert res[1].dense == pytest.approx([7.5 / n, 1.0 / n])
    assert res[1].sparse == {"tok10": 0.5, "tok9": 0.5, "tok1": 0.5}


def test_chunked_sparse_max_not_sum():
    emb = _stub_embedder()
    res = emb.embed("x" * 21)   # 3 块（10/10/5）：tok10 出现两次不叠加
    assert res.sparse == {"tok10": 0.5, "tok5": 0.5}
