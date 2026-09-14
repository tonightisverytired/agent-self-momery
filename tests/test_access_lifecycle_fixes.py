# -*- coding: utf-8 -*-
"""访问控制与生命周期过滤修复回归（2026-09-14）：
recall() 统一访问控制兜底（时间路/图谱路 dst）、step_day 零衰减节点、
neighbors 默认时间、后端返回未知 id 的兜底。"""
from datetime import datetime, timedelta

from dnamemory import MemorySystem, RecallFilters, RecallQuery
from dnamemory.retrieval import neighbors

START = datetime(2026, 3, 1)


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


def test_time_path_respects_access_and_lifecycle():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    mem.add_event("敏感会议", START, access_label="sensitive")
    mem.add_event("公开会议", START)
    mem.add_event("被遗忘的会议", START)
    mem.add_event("被合规删除的会议", START)
    mem.forget("被遗忘的会议", "retracted")            # tombstoned
    mem.forget("被合规删除的会议", "gdpr", force=True)  # deleted
    mem.add_event("将归档的会议", START, kind="transient",
                  life=1.0, decay_rate=1.0)
    mem.step_day()  # 将归档的会议 -> archived

    names = {h.name for h in mem.recall(RecallQuery(time=(START, 2)),
                                        mode="time")}
    assert names == {"公开会议"}

    # 组合模式（含时间路）同样兜底
    dual = {h.name for h in mem.recall(RecallQuery(time=(START, 2)),
                                       mode="triple")}
    assert dual == {"公开会议"}

    # 显式请求 sensitive 时可见，tombstoned/deleted 仍不可见
    visible = {h.name for h in mem.recall(
        RecallQuery(time=(START, 2)), mode="time",
        filters=RecallFilters(access_labels=("sensitive",)))}
    assert "敏感会议" in visible
    assert not {"被遗忘的会议", "被合规删除的会议"} & visible

    # include_archived 时 archived 可见，deleted 仍不可见
    with_archived = {h.name for h in mem.recall(
        RecallQuery(time=(START, 2)), mode="time",
        filters=RecallFilters(include_archived=True))}
    assert "将归档的会议" in with_archived
    assert "被合规删除的会议" not in with_archived
    mem.close()


def test_graph_path_filters_dst_events():
    mem = MemorySystem(clock=Clock())
    mem.add_entity("张总", "person")
    mem.add_entity("机密项目", "project", access_label="sensitive")
    mem.add_event("公开会议", START)
    mem.add_event("秘密谈话", START, access_label="sensitive")
    mem.add_event("被遗忘的谈话", START)
    mem.add_edge("张总", "公开会议", "participates", 0.8, 0.9,
                 valid_at=START)
    mem.add_edge("张总", "秘密谈话", "participates", 0.8, 0.9,
                 valid_at=START)
    mem.add_edge("机密项目", "秘密谈话", "participates", 0.8, 0.9,
                 valid_at=START)
    mem.add_edge("张总", "被遗忘的谈话", "participates", 0.8, 0.9,
                 valid_at=START)
    mem.forget("被遗忘的谈话", "retracted")
    # 默认过滤：公开实体种子经边到达的 sensitive/tombstoned 事件不得返回
    names = {h.name for h in mem.recall(RecallQuery(topic=["张总"]),
                                        mode="graph")}
    assert "公开会议" in names
    assert "秘密谈话" not in names
    assert "被遗忘的谈话" not in names

    # 显式请求 sensitive：sensitive 实体种子可到 sensitive 事件，
    # 但公开实体种子（被过滤）与 tombstoned 事件仍不可见
    visible = {h.name for h in mem.recall(
        RecallQuery(topic=["张总", "机密项目"]), mode="graph",
        filters=RecallFilters(access_labels=("sensitive",)))}
    assert "秘密谈话" in visible
    assert "公开会议" not in visible
    assert "被遗忘的谈话" not in visible
    mem.close()


def test_step_day_keeps_entities_active():
    clock = Clock()
    mem = MemorySystem(clock=clock)
    mem.add_entity("张总", "person")
    mem.add_entity("项目A", "project")
    eid = mem.add_event("与张总讨论项目A预算", START, kind="meeting")
    mem.add_edge(eid, "张总", "discusses", 0.8, 0.9, valid_at=START)
    for _ in range(10):
        clock.now += timedelta(days=1)
        mem.step_day()
    nodes = {n.name: n for n in mem.store.fetch_nodes()}
    assert nodes["张总"].lifecycle == "active"
    assert nodes["项目A"].lifecycle == "active"
    hits = mem.recall(RecallQuery(topic=["张总"]), mode="graph",
                      node_types=("event",))
    assert {h.name for h in hits} == {"与张总讨论项目A预算"}
    mem.close()


def test_neighbors_default_now_filters_invalid_at():
    mem = MemorySystem(clock=Clock())
    mem.add_entity("张总", "person")
    mem.add_entity("项目A", "project")
    mem.add_entity("项目B", "project")
    mem.add_edge("张总", "项目A", "participates", 0.8, 0.9,
                 valid_at=START - timedelta(days=30),
                 invalid_at=START - timedelta(days=1))
    mem.add_edge("张总", "项目B", "participates", 0.8, 0.9,
                 valid_at=START)
    # now=None 时用当前时间，带过期 invalid_at 的边被过滤且不抛 TypeError
    out = neighbors(mem.store, "张总")
    assert {n.name for n, _, _ in out} == {"项目B"}
    mem.close()


def test_recall_skips_unknown_ids_from_backend():
    class FakeTimeBackend:
        def time_window(self, t0, tol_days):
            return {999999: 0.5}

        def close(self):
            pass

    mem = MemorySystem(clock=Clock(), time_backend=FakeTimeBackend())
    mem.add_event("公开会议", START)
    assert mem.recall(RecallQuery(time=(START, 2)), mode="time") == []
    mem.close()
