# -*- coding: utf-8 -*-
"""0.8.3 第一人称主体（主人）写入回归：
自我指代（我/自己/user）归一到 config.user_name；库中无主体时自动建
person 节点，第一人称事实/边/观点不再因「主体不存在」被丢弃；
事件名（自然语句）不被归一。"""
from datetime import datetime

from dnamemory import MemorySystem
from dnamemory.extract import DeepSeekExtractor
from dnamemory.models import ExtractedMemory, MemoryConfig

T0 = datetime(2026, 9, 21, 9, 0)


class Clock:
    def __call__(self):
        return T0


class FakeExtractor:
    def __init__(self, cands):
        self.cands = cands

    def extract(self, text, meta=None):
        return self.cands


def _write(mem, cands):
    return mem.write_text("x", extractor=FakeExtractor(cands))


def _user_node(mem, name="用户"):
    return [n for n in mem.store.fetch_nodes()
            if n.node_type == "entity" and n.name == name
            and n.lifecycle == "active"]


def test_principal_auto_created_and_fact_attached():
    mem = MemorySystem(clock=Clock())
    r = _write(mem, [ExtractedMemory(type="fact", from_="用户",
                                     key="favorite_drink", value="拿铁")])
    assert not r.rejected, r.rejected
    nodes = _user_node(mem)
    assert len(nodes) == 1 and nodes[0].kind == "person"
    fact = mem.store.fetch_facts()[0]
    assert fact.node_id == nodes[0].nid
    mem.close()


def test_alias_normalized_to_principal():
    mem = MemorySystem(clock=Clock())
    r = _write(mem, [ExtractedMemory(type="fact", from_="我",
                                     key="city", value="上海")])
    assert not r.rejected, r.rejected
    assert _user_node(mem), "「我」应归一到主人名并建主体"
    mem.close()


def test_belief_and_edge_with_principal():
    mem = MemorySystem(clock=Clock())
    r = _write(mem, [
        ExtractedMemory(type="entity", name="李明", kind="person"),
        ExtractedMemory(type="belief", name="用户",
                        proposition="上海生活成本高", polarity="negative"),
        ExtractedMemory(type="edge", from_="用户", to="李明",
                        rel="occurs_with"),
    ])
    assert not r.rejected, r.rejected
    beliefs = mem.store.fetch_beliefs()
    assert beliefs and beliefs[0].proposition == "上海生活成本高"
    edges = [e for e in mem.store.fetch_edges()]
    assert len(edges) == 1
    mem.close()


def test_non_principal_still_dropped():
    mem = MemorySystem(clock=Clock())
    r = _write(mem, [ExtractedMemory(type="fact", from_="张三",
                                     key="city", value="北京")])
    assert r.rejected and "张三" in r.rejected[0][1]
    mem.close()


def test_custom_user_name():
    mem = MemorySystem(clock=Clock(), config=MemoryConfig(user_name="王芳"))
    r = _write(mem, [ExtractedMemory(type="fact", from_="我",
                                     key="hobby", value="阅读")])
    assert not r.rejected, r.rejected
    assert _user_node(mem, "王芳"), "别名应归一到自定义主人名"
    assert not _user_node(mem, "用户")
    mem.close()


def test_event_name_not_normalized():
    mem = MemorySystem(clock=Clock())
    _write(mem, [ExtractedMemory(type="event", name="我昨天爬山",
                                 kind="life")])
    names = [n.name for n in mem.store.fetch_nodes()
             if n.node_type == "event"]
    assert names == ["我昨天爬山"], "事件名是自然语句，不得归一"
    mem.close()


def test_principal_created_once_across_writes():
    mem = MemorySystem(clock=Clock())
    _write(mem, [ExtractedMemory(type="fact", from_="用户",
                                 key="city", value="上海")])
    _write(mem, [ExtractedMemory(type="fact", from_="用户",
                                 key="hobby", value="阅读")])
    assert len(_user_node(mem)) == 1, "主体只建一次（实体消解复用）"
    mem.close()


def test_context_line_carries_user_name():
    ex = DeepSeekExtractor(api_key="k")
    line = ex._context_line({"today": "2026-09-21", "user_name": "王芳"})
    assert "王芳" in line
    assert "王芳" not in ex._context_line({"today": "2026-09-21"})
