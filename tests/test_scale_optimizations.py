# -*- coding: utf-8 -*-
"""规模化检索优化回归：邻接缓存失效、时间路 SQL 预筛、图谱路邻接语义、ANN top-k 边界。"""
from datetime import datetime, timedelta

from dnamemory import MemorySystem, RecallQuery
from dnamemory.embeddings import EmbeddingResult
from dnamemory.retrieval import _graph_path, _time_path

START = datetime(2026, 3, 1)


def test_adjacency_cache_invalidates_on_write():
    mem = MemorySystem()
    ent = mem.add_entity("项目A", "project")
    e1 = mem.add_event("事件一", START, kind="meeting", value_score=0.6)
    e2 = mem.add_event("事件二", START, kind="meeting", value_score=0.6)
    mem.add_edge(e1, ent, "discusses", 0.7, 0.8, valid_at=START)
    now = mem.clock()
    adj1 = mem.store.adjacency(now)
    assert not any(x[0] == e2 for x in adj1.get(e1, []))
    mem.add_edge(e2, ent, "discusses", 0.7, 0.8, valid_at=START)
    adj2 = mem.store.adjacency(now)
    assert any(x[0] == e1 for x in adj2[ent])
    assert any(x[0] == e2 for x in adj2[ent])
    assert mem.store.adjacency(now) is adj2  # 版本不变时命中缓存
    mem.close()


def test_time_path_sql_prefilter_matches_manual():
    mem = MemorySystem()
    offsets = [0, 1, 2, 3, -1, -2, -3, 2.9, -2.9]
    expected = {}
    for i, off in enumerate(offsets):
        ts = START + timedelta(days=off, hours=6)
        nid = mem.add_event(f"事件{i}", ts, kind="life", value_score=0.5)
        diff = abs((ts - START).days)
        if diff <= 2:
            expected[nid] = 1.0 / (1.0 + diff)
    got = _time_path(mem.store, START, 2)
    assert got == expected
    mem.close()


def test_graph_path_adjacency_keeps_semantics():
    mem = MemorySystem()
    mem.add_entity("项目A", "project")
    e1 = mem.add_event("事件一", START, kind="meeting", value_score=0.9)
    e2 = mem.add_event("事件二", START, kind="meeting", value_score=0.6)
    mem.add_edge(e1, "项目A", "discusses", 0.8, 0.9, valid_at=START)
    mem.add_edge(e2, e1, "precedes", 0.5, 0.8, valid_at=START)
    from dnamemory.models import RecallFilters
    filters = RecallFilters()
    out = _graph_path(mem.store, ["项目A"], 2, None, filters, mem.clock())
    # e1 经项目A直达：1/1*0.8*0.9*0.7=0.504；e2 经 e1 一跳：1/2*0.5*0.8*0.9=0.18
    assert abs(out[e1] - 0.504) < 1e-9
    assert abs(out[e2] - 0.18) < 1e-9
    mem.close()


class AnnEmbedder:
    model_name = "fake/ann"

    def __init__(self):
        self.vectors = {
            "与张总讨论预算": [1.0, 0.0, 0.0, 0.0],
            "周末爬山": [0.0, 1.0, 0.0, 0.0],
            "买菜做饭": [0.0, 0.0, 1.0, 0.0],
            "项目预算会议": [0.9, 0.7, 0.2, 0.0],
        }

    def embed(self, text):
        dense = self.vectors.get(text, [0.0, 0.0, 0.0, 0.0])
        return EmbeddingResult(dense=dense, sparse=None, dim=4,
                               model=self.model_name)

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


def test_dense_ann_k_bounded_by_config():
    emb = AnnEmbedder()
    mem = MemorySystem(embedder=emb)
    for name in ("与张总讨论预算", "周末爬山", "买菜做饭"):
        mem.add_event(name, START, kind="life", value_score=0.5)
    captured = []
    mem.store._vec = True
    mem.store.search_vectors = (
        lambda qv, model, dim, k: (captured.append(k), None)[1])
    mem.recall(RecallQuery(text="项目预算会议"), k=5, mode="triple",
               node_types=("event",))
    # N=3 < dense_ann_top_k，ann_k 应等于 N（全量）
    assert captured and captured[0] == 3
    mem.close()
