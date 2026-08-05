# -*- coding: utf-8 -*-
"""P3 向量索引验收用例：sqlite-vec 重建/增量/对拍/降级、Postgres 占位。

未安装 sqlite-vec 时索引相关用例自动跳过（降级链由其余用例覆盖）。
"""
from datetime import datetime

import pytest

from dnamemory import MemorySystem, RecallQuery
from dnamemory.embeddings import EmbeddingResult

START = datetime(2026, 8, 3)


class FakeEmbedder:
    model_name = "fake/bge-m3"

    def __init__(self, vectors):
        self.vectors = vectors

    def embed(self, text):
        dense = self.vectors.get(text, [0.0, 0.0, 0.0, 0.0])
        return EmbeddingResult(dense=dense, sparse={"tok": 0.5}, dim=4,
                               model=self.model_name)

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


def _build(emb):
    mem = MemorySystem(embedder=emb)
    aid = mem.add_event("与张总讨论预算", START, kind="meeting",
                        value_score=0.5)
    bid = mem.add_event("周末爬山", START, kind="life", value_score=0.5)
    cid = mem.add_event("买菜做饭", START, kind="life", value_score=0.5)
    mem.store.rebuild_vector_index(emb.model_name, 4)
    return mem, aid, bid, cid


def test_index_rebuild_and_search():
    emb = FakeEmbedder({"与张总讨论预算": [1.0, 0.0, 0.0, 0.0],
                        "周末爬山": [0.0, 1.0, 0.0, 0.0],
                        "买菜做饭": [0.0, 0.0, 1.0, 0.0]})
    mem, aid, bid, _ = _build(emb)
    if mem.store._vec is None:
        pytest.skip("sqlite-vec 未安装")
    rows = mem.store.search_vectors([1.0, 0.0, 0.0, 0.0],
                                    emb.model_name, 4, k=10)
    assert rows is not None
    by_id = dict(rows)
    assert aid in by_id
    assert abs((1.0 - by_id[aid]) - 1.0) < 1e-4  # 余弦距离 ≈ 0
    mem.close()


def test_dense_path_index_matches_numpy_topk():
    vectors = {
        "与张总讨论预算": [1.0, 0.0, 0.0, 0.0],
        "周末爬山": [0.0, 1.0, 0.0, 0.0],
        "买菜做饭": [0.0, 0.0, 1.0, 0.0],
        "项目预算会议": [0.9, 0.7, 0.2, 0.0],
    }
    emb = FakeEmbedder(vectors)
    mem, aid, bid, cid = _build(emb)
    mem2, aid2, bid2, cid2 = _build(emb)
    if mem.store._vec is None:
        pytest.skip("sqlite-vec 未安装")
    mem2.store._vec = None  # 强制 numpy 路径
    q = RecallQuery(text="项目预算会议")
    idx = [h.node_id for h in mem.recall(q, k=5, mode="triple",
                                         node_types=("event",))]
    npy = [h.node_id for h in mem2.recall(q, k=5, mode="triple",
                                          node_types=("event",))]
    assert set(idx) == {aid, bid}
    assert set(npy) == {aid2, bid2}
    assert idx == npy
    assert cid not in idx and cid2 not in npy
    mem.close()
    mem2.close()


def test_incremental_sync_updates_index():
    emb = FakeEmbedder({"与张总讨论预算": [1.0, 0.0, 0.0, 0.0],
                        "周末爬山": [0.0, 1.0, 0.0, 0.0],
                        "买菜做饭": [0.0, 0.0, 1.0, 0.0]})
    mem, aid, _, _ = _build(emb)
    if mem.store._vec is None:
        pytest.skip("sqlite-vec 未安装")
    mem.store.set_node_vectors(aid, [0.0, 1.0, 0.0, 0.0],
                               {"tok": 0.5}, emb.model_name, 4)
    rows = mem.store.search_vectors([0.0, 1.0, 0.0, 0.0],
                                    emb.model_name, 4, k=10)
    by_id = dict(rows)
    assert abs(by_id[aid]) < 1e-4
    mem.close()


def test_model_change_triggers_rebuild():
    emb = FakeEmbedder({"与张总讨论预算": [1.0, 0.0, 0.0, 0.0],
                        "周末爬山": [0.0, 1.0, 0.0, 0.0],
                        "买菜做饭": [0.0, 0.0, 1.0, 0.0]})
    mem, aid, _, _ = _build(emb)
    if mem.store._vec is None:
        pytest.skip("sqlite-vec 未安装")
    other = "other/model"
    mem.store.set_node_vectors(aid, [1.0, 0.0, 0.0, 0.0],
                               None, other, 4)
    old_rows = mem.store.search_vectors([1.0, 0.0, 0.0, 0.0],
                                        emb.model_name, 4, k=10)
    assert old_rows is not None and dict(old_rows)[aid] < 1e-4
    rows = mem.store.search_vectors([1.0, 0.0, 0.0, 0.0],
                                    other, 4, k=10)
    assert rows is not None and dict(rows)[aid] < 1e-4
    mem.close()


def test_fallback_when_vec_disabled_still_recalls():
    vectors = {
        "与张总讨论预算": [1.0, 0.0, 0.0, 0.0],
        "周末爬山": [0.0, 1.0, 0.0, 0.0],
        "项目预算会议": [0.9, 0.7, 0.2, 0.0],
    }
    emb = FakeEmbedder(vectors)
    mem, aid, _, _ = _build(emb)
    mem.store._vec = None  # 模拟 sqlite-vec 不可用
    hits = mem.recall(RecallQuery(text="项目预算会议"), k=5, mode="triple",
                      node_types=("event",))
    assert hits and hits[0].node_id == aid
    mem.close()
