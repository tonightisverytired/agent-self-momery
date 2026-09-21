# -*- coding: utf-8 -*-
"""0.8.3 写入侧实体消解（治本）回归：
entity 候选同名复用规范节点（最低 id、跳过墓碑），不再每批新建；
描述回填不覆盖；事实/边向规范节点收敛。"""
from datetime import datetime

from dnamemory import MemorySystem
from dnamemory.models import ExtractedMemory

T0 = datetime(2026, 9, 21, 9, 0)


class Clock:
    def __call__(self):
        return T0


class FakeExtractor:
    def __init__(self, cands):
        self.cands = cands

    def extract(self, text, meta=None):
        return self.cands


def _person(name, desc=None):
    return ExtractedMemory(type="entity", name=name, kind="person",
                           value=desc)


def _ents(mem, name):
    return [n for n in mem.store.fetch_nodes()
            if n.node_type == "entity" and n.name == name
            and n.lifecycle not in ("tombstoned", "deleted")]


def test_entity_candidate_reuses_existing():
    mem = MemorySystem(clock=Clock())
    old = mem.add_entity("李明", "person")
    mem.write_text("x", extractor=FakeExtractor([
        _person("李明"),
        ExtractedMemory(type="fact", from_="李明", key="hobby",
                        value="篮球"),
    ]))
    assert len(_ents(mem, "李明")) == 1, "同名实体候选不得再建节点"
    fact = [f for f in mem.store.fetch_facts() if f.key == "hobby"][0]
    assert fact.node_id == old, "事实必须落规范节点"
    mem.close()


def test_same_batch_entity_dedup():
    mem = MemorySystem(clock=Clock())
    mem.write_text("x", extractor=FakeExtractor([
        _person("王芳", "同事"), _person("王芳", "大学同学"),
    ]))
    nodes = _ents(mem, "王芳")
    assert len(nodes) == 1, "同批同名候选只建一个节点"
    assert nodes[0].description == "同事", "先到的描述落库"
    mem.close()


def test_tombstoned_entity_not_reused():
    mem = MemorySystem(clock=Clock())
    old = mem.add_entity("张伟", "person")
    mem.forget("张伟", "retracted")
    mem.write_text("x", extractor=FakeExtractor([_person("张伟")]))
    nodes = _ents(mem, "张伟")
    assert len(nodes) == 1 and nodes[0].nid != old, \
        "墓碑节点不得复用，应新建"
    mem.close()


def test_description_backfill_not_overwrite():
    mem = MemorySystem(clock=Clock())
    a = mem.add_entity("陈静", "person")            # 描述为空
    mem.write_text("x", extractor=FakeExtractor([_person("陈静", "护士")]))
    node = next(n for n in mem.store.fetch_nodes() if n.nid == a)
    assert node.description == "护士", "空描述应回填"
    mem.write_text("y", extractor=FakeExtractor([_person("陈静", "医生")]))
    node = next(n for n in mem.store.fetch_nodes() if n.nid == a)
    assert node.description == "护士", "已有描述不得覆盖"
    mem.close()


def test_facts_converge_to_lowest_id():
    mem = MemorySystem(clock=Clock())
    a = mem.add_entity("刘洋", "person")
    b = mem.add_entity("刘洋", "person")            # 存量重复（显式 API 仍可建）
    assert a != b
    mem.write_text("x", extractor=FakeExtractor([
        ExtractedMemory(type="fact", from_="刘洋", key="city",
                        value="上海"),
    ]))
    fact = [f for f in mem.store.fetch_facts() if f.key == "city"][0]
    assert fact.node_id == a, "存量重复时事实向最低 id 规范节点收敛"
    mem.close()
