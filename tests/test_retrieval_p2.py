# -*- coding: utf-8 -*-
"""检索补强验收用例：rerank 回退语义、稀疏第四路 quad、实体消解、
写入端点模糊匹配。"""
from datetime import datetime

import pytest

from dnamemory import MemorySystem, RecallQuery
from dnamemory.embeddings import EmbeddingResult
from dnamemory.models import ExtractedMemory

START = datetime(2026, 8, 3)


class FakeReranker:
    model_name = "fake/reranker"

    def __init__(self, scores=None, raise_error=False):
        self.scores = scores
        self.raise_error = raise_error

    def rerank(self, query, texts):
        if self.raise_error:
            raise RuntimeError("reranker down")
        if self.scores is not None:
            return self.scores[:len(texts)]
        return list(range(len(texts), 0, -1))


class SparseEmbedder:
    model_name = "fake/sparse"

    def __init__(self, vectors):
        self.vectors = vectors

    def embed(self, text):
        dense, sparse = self.vectors.get(text, ([0.0, 0.0, 0.0, 0.0], None))
        return EmbeddingResult(dense=dense, sparse=sparse, dim=len(dense),
                               model=self.model_name)

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


def _build_rerank_mem(reranker):
    mem = MemorySystem(reranker=reranker)
    mem.add_entity("项目A", "project")
    ids = []
    for i, name in enumerate(["讨论预算", "讨论排期", "讨论方案"]):
        eid = mem.add_event(name, START, kind="meeting", value_score=0.6)
        mem.add_edge(eid, "项目A", "discusses", 0.8, 0.9, valid_at=START)
        ids.append(eid)
    return mem, ids


def test_rerank_reorders_and_falls_back():
    reranker = FakeReranker(scores=[0.1, 0.9, 0.5])
    mem, ids = _build_rerank_mem(reranker)
    q = RecallQuery(topic=["项目A"], text="项目A")
    hits = mem.recall(q, k=3, mode="triple", node_types=("event",))
    assert [h.node_id for h in hits] == [ids[1], ids[2], ids[0]]

    bad = FakeReranker(raise_error=True)
    mem2, ids2 = _build_rerank_mem(bad)
    hits2 = mem2.recall(RecallQuery(topic=["项目A"], text="项目A"), k=3,
                        mode="triple", node_types=("event",))
    assert [h.node_id for h in hits2] == ids2  # 回退原序
    mem.close()
    mem2.close()


def test_rerank_nan_and_small_top_n():
    import math
    reranker = FakeReranker(scores=[float("nan"), 0.9, 0.1])
    mem, ids = _build_rerank_mem(reranker)
    q = RecallQuery(topic=["项目A"], text="项目A")
    hits = mem.recall(q, k=3, mode="triple", node_types=("event",),
                      rerank_top_n=4)
    assert hits[0].node_id == ids[1]  # nan 视为 -inf

    mem2, ids2 = _build_rerank_mem(FakeReranker())
    hits2 = mem2.recall(q, k=3, mode="triple", node_types=("event",),
                        rerank_top_n=2)  # <= k：不重排
    assert [h.node_id for h in hits2] == ids2
    assert math  # keep import used
    mem.close()
    mem2.close()


def test_quad_sparse_path_adds_hits_and_degrades_gracefully():
    vectors = {
        "项目预算会议": ([0.9, 0.7, 0.0, 0.0], {"预算": 0.8, "会议": 0.5}),
        "周末爬山": ([0.0, 1.0, 0.0, 0.0], {"爬山": 1.0}),
        "买菜做饭": ([0.2, 0.1, 0.0, 0.0], {"买菜": 1.0, "做饭": 0.6}),
        "今天买菜做饭": ([0.0, 0.0, 0.2, 0.1], {"买菜": 1.0, "做饭": 0.6}),
    }
    emb = SparseEmbedder(vectors)
    mem = MemorySystem(embedder=emb)
    aid = mem.add_event("与张总讨论预算", START, kind="meeting")
    bid = mem.add_event("周末爬山", START, kind="life")
    cid = mem.add_event("买菜做饭", START, kind="life")
    # 直接写向量以控制 dense/sparse（默认 add 会走 embedder，上面文本不在表内）
    mem.store.set_node_vectors(aid, vectors["项目预算会议"][0],
                               vectors["项目预算会议"][1], "fake/sparse", 4)
    mem.store.set_node_vectors(bid, vectors["周末爬山"][0],
                               vectors["周末爬山"][1], "fake/sparse", 4)
    mem.store.set_node_vectors(cid, vectors["买菜做饭"][0],
                               vectors["买菜做饭"][1], "fake/sparse", 4)
    q = RecallQuery(text="今天买菜做饭")
    triple = {h.node_id for h in mem.recall(q, k=5, mode="triple",
                                            node_types=("event",))}
    quad = {h.node_id for h in mem.recall(q, k=5, mode="quad",
                                          node_types=("event",))}
    assert cid not in triple  # dense sim 低于 0.55
    assert cid in quad        # sparse sim 高于 0.25

    no_sparse = SparseEmbedder({"买菜做饭": ([0.9, 0.0, 0.0, 0.0], None)})
    mem2 = MemorySystem(embedder=no_sparse)
    eid = mem2.add_event("买菜做饭", START, kind="life")
    mem2.store.set_node_vectors(eid, [0.9, 0.0, 0.0, 0.0], None,
                                "fake/sparse", 4)
    hits = mem2.recall(RecallQuery(text="买菜做饭"), k=5, mode="quad",
                       node_types=("event",))
    assert eid in {h.node_id for h in hits}  # 无 sparse 不报错，稠密路正常
    mem.close()
    mem2.close()


def test_resolve_entities_merges_rewires_and_tombstones(tmp_path):
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    e1 = mem.add_entity("同事", "person")
    e2 = mem.add_entity("同事", "person")
    ev = mem.add_event("和同事吃饭", START, kind="life")
    mem.add_edge(ev, e2, "mentions", 0.6, 0.7, valid_at=START)
    mem.add_fact(e2, "note", "v1")
    mem.add_edge(e1, e2, "mentions", 0.5, 0.6, valid_at=START)  # 将成自边
    merged = mem.resolve_entities()
    assert merged == 1
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    assert nodes[e2].lifecycle == "tombstoned"
    edges = mem.store.fetch_edges()
    food_edge = next(e for e in edges if e.from_id == ev)
    assert food_edge.to_id == e1
    self_edge = next(e for e in edges
                     if e.from_id == e1 and e.to_id == e1)
    assert self_edge.lifecycle == "tombstoned"
    facts = [f for f in mem.store.fetch_facts() if f.key == "note"]
    assert facts and facts[0].node_id == e1
    assert len(mem.store.fetch_tombstones()) == 1
    mem.close()


def test_resolve_entities_skips_different_access_label():
    mem = MemorySystem()
    e1 = mem.add_entity("同事", "person")
    e2 = mem.add_entity("同事", "person", access_label="sensitive")
    merged = mem.resolve_entities()
    assert merged == 0
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    assert nodes[e1].lifecycle == "active"
    assert nodes[e2].lifecycle == "active"
    mem.close()


class FuzzyExtractor:
    def __init__(self, cands):
        self.cands = cands

    def extract_many(self, texts, meta=None, batch_size=20):
        return [self.cands]


def test_write_fuzzy_endpoint_match(tmp_path):
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    ex = FuzzyExtractor([
        ExtractedMemory(type="entity", name="张总", kind="person",
                        value="项目负责人"),
        ExtractedMemory(type="entity", name="项目管理系统", kind="project",
                        value="核心项目"),
        ExtractedMemory(type="edge", from_="张总", to="项目管理系统A",
                        name="张总-项目管理系统A", rel="participates",
                        confidence=0.9),
    ])
    res = mem.write_many(["张总负责项目A"], extractor=ex)
    assert res.accepted == 3
    assert not res.rejected
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    zhang = next(n for n in nodes.values() if n.name == "张总")
    pa = next(n for n in nodes.values() if n.name == "项目管理系统")
    edges = mem.store.fetch_edges()
    assert any(e.from_id == zhang.nid and e.to_id == pa.nid for e in edges)
    ops = [r[0] for r in mem.store.read(
        "SELECT op FROM audit_log ORDER BY id")]
    assert "endpoint_fuzzy_match" in ops
    mem.close()


def test_fuzzy_no_match_still_drops():
    mem = MemorySystem()
    ex = FuzzyExtractor([
        ExtractedMemory(type="entity", name="项目A", kind="project"),
        ExtractedMemory(type="edge", from_="张总", to="完全无关的名字",
                        name="张总-完全无关", rel="mentions"),
    ])
    res = mem.write_many(["x"], extractor=ex)
    assert res.accepted == 1
    assert any(t == "edge" and "端点不存在" in r for t, r in res.rejected)
    mem.close()
