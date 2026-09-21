# -*- coding: utf-8 -*-
"""M3 语义路回归：bge-m3 适配器协议、稠密/稀疏双向量写入、
稠密写入校验、稠密路召回排序。"""
from datetime import datetime

import pytest

from dnamemory import MemorySystem, RecallQuery
from dnamemory.embeddings import EmbeddingResult
from dnamemory.errors import StorageError
from dnamemory.models import ExtractedMemory


class FakeEmbedder:
    model_name = "fake/bge-m3"

    def __init__(self, vectors=None, fail_dense=False):
        self.vectors = vectors or {}
        self.fail_dense = fail_dense

    def _vec(self, text):
        return self.vectors.get(text, [0.0, 0.0, 0.0, 0.0])

    def embed(self, text):
        dense = [] if self.fail_dense else self._vec(text)
        return EmbeddingResult(dense=dense, sparse={"token": 0.8},
                               dim=len(dense), model=self.model_name)

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


class FakeExtractor:
    def __init__(self, cands):
        self.cands = cands

    def extract_many(self, texts, meta=None, batch_size=20):
        return [self.cands]


def test_write_stores_dense_and_sparse(tmp_path):
    emb = FakeEmbedder(vectors={
        "开会讨论预算": [1.0, 0.0, 0.0, 0.0],
        "咖啡 喜欢拿铁": [0.0, 1.0, 0.0, 0.0],
    })
    mem = MemorySystem(path=str(tmp_path / "m.db"), embedder=emb)
    mem.add_event("开会讨论预算", datetime(2026, 8, 3), kind="meeting")
    mem.add_entity("咖啡", "preference", description="喜欢拿铁")
    rows = mem.store.fetch_node_vectors()
    assert len(rows) == 2
    by_id = {r[0]: r for r in rows}
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    assert all(by_id[nid][1] for nid in by_id)  # dense 非空
    assert all(by_id[nid][2] == {"token": 0.8} for nid in by_id)  # sparse 已存
    assert all(r[3] == "fake/bge-m3" for r in rows)
    assert all(r[4] == 4 for r in rows)
    assert set(by_id) == set(nodes)
    mem.close()


def test_write_many_embeds_extracted_nodes(tmp_path):
    emb = FakeEmbedder(vectors={
        "今天开会": [1.0, 0.0, 0.0, 0.0],
        "张总 项目负责人": [0.0, 1.0, 0.0, 0.0],
    })
    ex = FakeExtractor([
        ExtractedMemory(type="event", name="今天开会", kind="meeting",
                        ts=datetime(2026, 8, 3), value_score=0.6),
        ExtractedMemory(type="entity", name="张总", kind="person",
                        value="项目负责人"),
    ])
    mem = MemorySystem(path=str(tmp_path / "m.db"), embedder=emb)
    res = mem.write_many(["今天开会，张总负责"], extractor=ex)
    assert res.accepted == 3  # 2 节点 + 1 条批级自动证据（IA-1）
    assert len(mem.store.fetch_node_vectors()) == 2
    mem.close()


def test_dense_write_validation_fails_on_empty(tmp_path):
    emb = FakeEmbedder(fail_dense=True)
    mem = MemorySystem(path=str(tmp_path / "m.db"), embedder=emb)
    with pytest.raises(StorageError) as ei:
        mem.add_event("无向量事件", datetime(2026, 8, 3))
    assert "E011" in str(ei.value)
    mem.close()


def test_recall_dense_path_ranks_semantic_hit():
    vectors = {
        "与张总讨论预算": [1.0, 0.0, 0.0, 0.0],
        "周末爬山": [0.0, 1.0, 0.0, 0.0],
        "买菜做饭": [0.0, 0.0, 1.0, 0.0],
        "项目预算会议": [0.9, 0.7, 0.2, 0.0],
    }
    mem = MemorySystem(embedder=FakeEmbedder(vectors=vectors))
    aid = mem.add_event("与张总讨论预算", datetime(2026, 8, 3),
                        kind="meeting", value_score=0.5)
    bid = mem.add_event("周末爬山", datetime(2026, 8, 4),
                        kind="life", value_score=0.5)
    cid = mem.add_event("买菜做饭", datetime(2026, 8, 5),
                        kind="life", value_score=0.5)
    hits = mem.recall(RecallQuery(text="项目预算会议"), k=5, mode="triple",
                      node_types=("event",))
    assert hits and hits[0].node_id == aid
    assert {h.node_id for h in hits[:2]} == {aid, bid}
    # sim=0.2 < dense_min_sim=0.55，稠密路应排除
    assert cid not in {h.node_id for h in hits}
