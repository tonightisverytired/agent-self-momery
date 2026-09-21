# -*- coding: utf-8 -*-
"""0.8.1 二轮 HyDE 查询改写回归：变体参与检索、失败回退、max 融合。"""
import pytest

from dnamemory import MemorySystem, RecallQuery
from dnamemory.embeddings import EmbeddingResult
from dnamemory.hyde import DeepSeekHyDERewriter
from dnamemory.retrieval import _merged_max


class FakeRewriter:
    def __init__(self, variants=None, fail=False):
        self.variants = variants or []
        self.fail = fail
        self.calls = 0

    def rewrite(self, text):
        self.calls += 1
        if self.fail:
            raise RuntimeError("LLM down")
        return list(self.variants)


def test_merged_max():
    a = {1: 0.5, 2: 0.3}
    b = {1: 0.8, 3: 0.9}
    assert _merged_max([a, b]) == {1: 0.8, 2: 0.3, 3: 0.9}
    assert _merged_max([]) == {}


def test_recall_with_rewrite_variant_hits():
    """原文查不到的节点，由改写变体命中（词面路即可验证）。"""
    mem = MemorySystem(query_rewriter=FakeRewriter(["喜欢吃火锅和烧烤"]))
    mem.add_event("火锅烧烤聚餐", None, kind="food", value_score=0.8)
    hits = mem.recall(RecallQuery(text="今晚吃了什么"), k=5, mode="triple")
    assert any(h.name == "火锅烧烤聚餐" for h in hits)
    assert mem._last_alt_texts == ("喜欢吃火锅和烧烤",)
    mem.close()


def test_rewrite_failure_falls_back():
    mem = MemorySystem(query_rewriter=FakeRewriter(fail=True))
    mem.add_event("预算讨论会", None, kind="meeting", value_score=0.8)
    hits = mem.recall(RecallQuery(text="预算讨论会"), k=5, mode="triple")
    assert hits and hits[0].name == "预算讨论会"
    assert mem._last_alt_texts == ()
    mem.close()


def test_no_rewriter_no_alt():
    mem = MemorySystem()
    mem.add_event("预算讨论会", None, kind="meeting", value_score=0.8)
    hits = mem.recall(RecallQuery(text="预算讨论会"), k=5, mode="triple")
    assert hits and mem._last_alt_texts == ()
    mem.close()


def test_llm_used_flag_in_trace():
    mem = MemorySystem(query_rewriter=FakeRewriter(["变体"]))
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"))
    assert ctx.trace.llm_used is True
    mem.close()


def test_deepseek_rewriter_parse_and_cache(monkeypatch):
    """不联网：monkeypatch _http 层，验证两行解析、过滤原文与缓存。"""

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {
                "content": "1. 用户喜欢吃火锅\n2. 火锅 聚餐 喜欢"}}]}

    class _FakeHttp:
        calls = 0

        def post(self, *a, **kw):
            _FakeHttp.calls += 1
            return _Resp()

    rw = DeepSeekHyDERewriter.__new__(DeepSeekHyDERewriter)
    rw.model = "deepseek-chat"
    rw.timeout, rw.max_retries, rw.max_tokens = 10, 0, 300
    rw.thinking = "disabled"
    rw.max_variants = 2
    rw._cache = {}
    rw._requests = _FakeHttp()
    rw._base_url = "http://stub"
    rw._api_key = "stub"
    out = rw.rewrite("用户喜欢吃什么")
    assert out == ["用户喜欢吃火锅", "火锅 聚餐 喜欢"]
    assert rw.rewrite("用户喜欢吃什么") == out      # 缓存命中
    assert _FakeHttp.calls == 1

    class _FailHttp:
        def post(self, *a, **kw):
            raise RuntimeError("network down")

    rw._requests = _FailHttp()
    rw._cache.clear()
    assert rw.rewrite("新问题") == []               # 失败回退空列表
