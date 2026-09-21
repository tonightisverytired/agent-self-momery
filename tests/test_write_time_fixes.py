# -*- coding: utf-8 -*-
"""0.8.3 写入侧时间修复回归：
write_text/write_many 自动注入 today（相对时间换算的前提）；
ChainedExtractor LLM 优先 + 失败/零产出兜底；
_time_path 对 ts=NULL 的事件回退 created_at（写入时间锚点）。"""
from datetime import datetime, timedelta

from dnamemory import MemorySystem
from dnamemory.extract import ChainedExtractor, FallbackExtractor
from dnamemory.models import ExtractedMemory
from dnamemory.retrieval import _time_path

START = datetime(2026, 9, 21, 9, 0)


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


class SpyExtractor:
    def __init__(self):
        self.metas = []

    def extract(self, text, meta=None):
        self.metas.append(dict(meta or {}))
        return []

    def extract_many(self, texts, meta=None, batch_size=20):
        self.metas.append(dict(meta or {}))
        return [[] for _ in texts]


def test_write_text_injects_today():
    mem = MemorySystem(clock=Clock())
    spy = SpyExtractor()
    mem.write_text("随便记一笔", extractor=spy)
    assert spy.metas[0]["today"] == "2026-09-21"
    mem.close()


def test_write_text_keeps_caller_today():
    mem = MemorySystem(clock=Clock())
    spy = SpyExtractor()
    mem.write_text("离线注入", meta={"today": "2020-01-01"}, extractor=spy)
    assert spy.metas[0]["today"] == "2020-01-01"   # 显式传入优先（可复现）
    mem.close()


def test_write_many_injects_today():
    mem = MemorySystem(clock=Clock())
    spy = SpyExtractor()
    mem.write_many(["a", "b"], extractor=spy)
    assert spy.metas[0]["today"] == "2026-09-21"
    mem.close()


def _cand():
    return [ExtractedMemory(type="event", name="开会", kind="meeting")]


class BoomExtractor:
    def extract(self, text, meta=None):
        raise RuntimeError("API down")

    def extract_many(self, texts, meta=None, batch_size=20):
        raise RuntimeError("API down")


class EmptyExtractor:
    def extract(self, text, meta=None):
        return []

    def extract_many(self, texts, meta=None, batch_size=20):
        return [[] for _ in texts]


class OkExtractor:
    def extract(self, text, meta=None):
        return _cand()

    def extract_many(self, texts, meta=None, batch_size=20):
        return [_cand()]


def test_chained_prefers_primary():
    ex = ChainedExtractor(OkExtractor(), FallbackExtractor())
    assert ex.extract("x")[0].name == "开会"
    batches = ex.extract_many(["x"])
    assert batches[0][0].name == "开会"


def test_chained_falls_back_on_error():
    ex = ChainedExtractor(BoomExtractor(), FallbackExtractor())
    got = ex.extract("今天喝了咖啡")
    assert got[0].type == "event" and got[0].name == "今天喝了咖啡"
    batches = ex.extract_many(["a", "b"])
    assert len(batches) == 2 and all(b[0].kind == "chat" for b in batches)


def test_chained_falls_back_on_empty():
    ex = ChainedExtractor(EmptyExtractor(), FallbackExtractor())
    assert ex.extract("x")[0].kind == "chat"
    batches = ex.extract_many(["a"])
    assert batches[0][0].kind == "chat"


def test_time_path_falls_back_to_created_at():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    nid = mem.add_event("无时间事件", None, kind="chat")  # created_at=START
    other = mem.add_event("异窗事件", None, kind="chat")
    with mem.store.transaction() as conn:  # 模拟异窗写入时间
        conn.execute("UPDATE nodes SET created_at=? WHERE id=?",
                     ((START + timedelta(days=30)).isoformat(), other))
    got = _time_path(mem.store, START, 2)
    assert nid in got           # ts=NULL 由 created_at 锚定
    assert other not in got     # 窗口外不误命中
    mem.close()
