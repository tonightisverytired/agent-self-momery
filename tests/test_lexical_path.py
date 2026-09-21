# -*- coding: utf-8 -*-
"""IA-2 词面路验收用例：中文 bigram 包含度召回、单字回退、
lexical/semantic 双路并存、MemoryHit.path_scores 填充。"""
from datetime import datetime

import pytest

from dnamemory import MemorySystem, RecallQuery
from dnamemory.embeddings import EmbeddingResult
from dnamemory.models import MemoryHit

START = datetime(2026, 8, 3)


class FakeEmbedder:
    model_name = "fake/bge-m3"

    def __init__(self, vectors):
        self.vectors = vectors

    def embed(self, text):
        dense = self.vectors.get(text, [0.0, 0.0, 0.0, 0.0])
        return EmbeddingResult(dense=dense, sparse=None, dim=4,
                               model=self.model_name)

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


def _dentist_mem():
    """库里有「计划2026-08-04看牙」+ 无关干扰事件。"""
    mem = MemorySystem()
    mem.store.insert_node(
        "event", "life", "计划2026-08-04看牙", "看牙的计划安排",
        datetime(2026, 8, 4), 0.5, False, "public", 50.0, 0.28,
        datetime(2026, 8, 1))
    mem.add_event("周末爬山", START, kind="life", value_score=0.9)
    return mem


def test_chinese_natural_question_hits():
    """自然问句「看牙的计划安排在什么时候」应命中看牙事件。"""
    mem = _dentist_mem()
    hits = mem.recall(RecallQuery(text="看牙的计划安排在什么时候"),
                      k=5, mode="triple", node_types=("event",))
    names = [h.name for h in hits]
    assert "计划2026-08-04看牙" in names
    assert "周末爬山" not in names  # 零重叠干扰项被阈值过滤
    mem.close()


def test_single_char_query_falls_back_to_substring():
    mem = _dentist_mem()
    hits = mem.recall(RecallQuery(text="牙"), k=5, mode="triple",
                      node_types=("event",))
    assert [h.name for h in hits] == ["计划2026-08-04看牙"]
    # 短路 1.0 × (0.5 + 0.5×0.5)
    assert hits[0].path_scores["lexical"]["score"] == pytest.approx(0.75)
    # 单字且无子串命中 → 空
    assert mem.recall(RecallQuery(text="猫"), k=5, mode="triple",
                      node_types=("event",)) == []
    mem.close()


def test_lexical_path_name_without_embedder():
    mem = _dentist_mem()
    hits = mem.recall(RecallQuery(text="看牙"), k=5, mode="triple",
                      node_types=("event",))
    assert hits and hits[0].sources == ("lexical",)
    # semantic 模式无 embedder 时同样走词面路
    hits2 = mem.recall(RecallQuery(text="看牙"), k=5, mode="semantic",
                       node_types=("event",))
    assert hits2 and hits2[0].sources == ("lexical",)
    mem.close()


def test_lexical_and_dense_paths_coexist_with_embedder():
    vectors = {
        "与张总讨论预算": [1.0, 0.0, 0.0, 0.0],
        "周末爬山": [0.0, 1.0, 0.0, 0.0],
        "讨论预算周末爬山": [0.0, 1.0, 0.0, 0.0],
    }
    mem = MemorySystem(embedder=FakeEmbedder(vectors))
    aid = mem.add_event("与张总讨论预算", START, kind="meeting",
                        value_score=0.9)
    bid = mem.add_event("周末爬山", START, kind="life", value_score=0.9)
    hits = mem.recall(RecallQuery(text="讨论预算周末爬山"), k=5,
                      mode="triple", node_types=("event",))
    by_id = {h.node_id: h for h in hits}
    # 词面路命中两个，稠密路只命中周末爬山
    assert by_id[aid].sources == ("lexical",)
    assert by_id[bid].sources == ("lexical", "semantic")
    # 「周末爬山」名是查询子串 → 实体锚定保底 0.5 × (0.5 + 0.5×0.9)
    assert by_id[bid].path_scores["lexical"]["score"] \
        == pytest.approx(0.5 * 0.95)
    assert by_id[bid].path_scores["lexical"]["rank"] == 0
    # 「与张总讨论预算」纯 bigram 包含度：q 的 7 个 bigram 命中
    # 「讨论/论预/预算」3 个 × (0.5 + 0.5×0.9)
    assert by_id[aid].path_scores["lexical"]["score"] \
        == pytest.approx(3 / 7 * 0.95)
    assert by_id[aid].path_scores["lexical"]["rank"] == 1
    # 融合后周末爬山排第一
    assert hits[0].node_id == bid
    assert by_id[bid].path_scores["semantic"]["rank"] == 0
    assert "semantic" not in by_id[aid].path_scores
    mem.close()


def test_path_scores_filled_and_default_none():
    mem = _dentist_mem()
    hits = mem.recall(RecallQuery(text="看牙的计划安排在什么时候"),
                      k=5, mode="triple", node_types=("event",))
    hit = next(h for h in hits if h.name == "计划2026-08-04看牙")
    # description「看牙的计划安排」命中 q 的 6/11 bigram × (0.5 + 0.5×0.5)
    assert hit.path_scores == {
        "lexical": {"score": pytest.approx(6 / 11 * 0.75), "rank": 0}}
    mem.close()
    # 位置构造兼容：缺省 path_scores 为 None
    h = MemoryHit(1, "event", "x", None, 0.1, ("lexical",))
    assert h.path_scores is None
