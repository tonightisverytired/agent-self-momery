# -*- coding: utf-8 -*-
"""0.8.1 时间感知：查询文本时间锚点提取 + recall 接线回归。"""
from datetime import datetime

from dnamemory import MemorySystem, RecallQuery
from dnamemory.models import MemoryConfig
from dnamemory.retrieval import _extract_monthday, _extract_query_time

NOW = datetime(2026, 9, 20, 12, 0)


def test_extract_explicit_cn_date():
    t0, tol = _extract_query_time("2022年5月12日那天做了什么", NOW)
    assert (t0, tol) == (datetime(2022, 5, 12), 0)


def test_extract_iso_and_slash_date():
    assert _extract_query_time("2026-09-01 的会议", NOW)[0] \
        == datetime(2026, 9, 1)
    assert _extract_query_time("2026/09/02 出发", NOW)[0] \
        == datetime(2026, 9, 2)


def test_extract_relative_words():
    assert _extract_query_time("昨天吃了什么", NOW)[0] \
        == datetime(2026, 9, 19, 12, 0)
    assert _extract_query_time("上周发生了什么", NOW)[1] == 4
    assert _extract_query_time("本周计划", NOW)[1] == 3


def test_no_false_positive():
    assert _extract_query_time("看牙的计划安排在什么时候", NOW) is None
    assert _extract_query_time("k=5 的 top 结果", NOW) is None
    # 无年份的月日不锚定具体年份（跨年误判代价大）
    assert _extract_query_time("5月12日去了哪里", NOW) is None


def test_extract_monthday():
    assert _extract_monthday("5月12日去了哪里") == "05-12"
    assert _extract_monthday("去年5月12日做了什么") == "05-12"
    assert _extract_monthday("2022年5月12日") is None   # 带年份走显式日期
    assert _extract_monthday("13月40日") is None       # 越界拒绝
    assert _extract_monthday("今天天气如何") is None


def test_recall_monthday_cross_year():
    mem = MemorySystem(config=MemoryConfig())
    mem.add_event("生日聚餐", datetime(2022, 5, 12, 18, 0), kind="life")
    mem.add_event("无关事件", datetime(2026, 1, 1), kind="life")
    hits = mem.recall(RecallQuery(text="5月12日那天吃了什么"), k=5,
                      mode="triple")
    by_name = {h.name: h for h in hits}
    assert "生日聚餐" in by_name            # 2022 年的事件照样命中
    assert "time" in by_name["生日聚餐"].sources
    mem.close()


def test_recall_time_path_fires_from_text():
    mem = MemorySystem(config=MemoryConfig())
    mem.add_event("预算讨论会", datetime(2026, 8, 3, 10, 0), kind="meeting")
    mem.add_event("无关事件", datetime(2026, 1, 1), kind="life")
    hits = mem.recall(RecallQuery(text="2026年8月3日开了什么会"), k=5,
                      mode="triple")
    by_name = {h.name: h for h in hits}
    assert "预算讨论会" in by_name
    assert "time" in by_name["预算讨论会"].sources
    mem.close()


def test_time_aware_disabled_by_config():
    mem = MemorySystem(config=MemoryConfig(time_aware_text=False))
    mem.add_event("预算讨论会", datetime(2026, 8, 3, 10, 0), kind="meeting")
    hits = mem.recall(RecallQuery(text="2026年8月3日"), k=5, mode="triple")
    for h in hits:
        assert "time" not in h.sources
    mem.close()
