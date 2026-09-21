# -*- coding: utf-8 -*-
"""M1 全量回归：指标门禁（S1/S2）、生命周期（S3）、并发（S9）、
迁移回归（实验 7）。"""
import random
import threading
from datetime import datetime, timedelta

from dnamemory import MemorySystem, RecallQuery

START = datetime(2026, 3, 1)
PERSON = {"张总", "李工", "王总"}


class Clock:
    def __init__(self, t=START):
        self.now = t

    def __call__(self):
        return self.now


def build_corpus(mem):
    topics = [
        ("张总", "person", "项目负责人"), ("李工", "person", "后端工程师"),
        ("项目A", "project", "核心交付项目"), ("项目B", "project", "创新项目"),
        ("微服务", "concept", "分布式架构"), ("单体架构", "concept", "一体化架构"),
        ("Kubernetes", "skill", "容器编排"), ("预算", "concept", "资金计划"),
        ("供应商B", "vendor", "云服务商"), ("技术选型", "concept", "方案对比"),
        ("部署流程", "skill", "上线步骤"), ("供应商C", "vendor", "监控工具商"),
    ]
    for name, kind, desc in topics:
        mem.add_entity(name, kind, desc)
    for a, b, rel in [("张总", "项目A", "participates"),
                      ("李工", "项目A", "participates"),
                      ("项目A", "微服务", "depends_on"),
                      ("项目A", "Kubernetes", "depends_on"),
                      ("微服务", "单体架构", "similar_to")]:
        mem.add_edge(a, b, rel, weight=0.8, confidence=0.9)
    day = 0
    templates = [
        ("与张总讨论{proj}预算问题", ["张总", "预算", "项目A"], "meeting", "transient", 0.45),
        ("与李工进行{tech}技术选型", ["李工", "技术选型", "微服务"], "decision", "core", 0.85),
        ("{proj}例会：同步{sup}进展", ["供应商B", "项目A"], "meeting", "transient", 0.5),
        ("部署{stack}集群到生产环境", ["Kubernetes", "部署流程"], "deploy", "core", 0.9),
        ("评估微服务与单体架构架构差异", ["微服务", "单体架构"], "decision", "core", 0.8),
        ("与张总复盘{proj}延期原因", ["项目A", "张总"], "meeting", "transient", 0.55),
    ]
    prev = None
    for i in range(150):
        day += 1 if i % 3 == 0 else 2
        tmpl, atoms, kind, etype, imp = random.choice(templates)
        proj = random.choice(["项目A", "项目B"])
        summary = tmpl.format(proj=proj,
                              tech=random.choice(["微服务", "Kubernetes"]),
                              sup=random.choice(["供应商B", "供应商C"]),
                              stack="Kubernetes")
        core = etype == "core"
        eid = mem.add_event(summary, START + timedelta(days=day), kind=kind,
                            value_score=imp, life=150.0 if core else 50.0,
                            decay_rate=0.02 if core else 0.28)
        for atom in atoms:
            rel = "participates" if atom in PERSON else "discusses"
            mem.add_edge(eid, atom, rel, weight=0.9 if core else 0.5,
                         confidence=0.8, valid_at=START + timedelta(days=day))
        if prev is not None:
            mem.add_edge(prev, eid, "precedes", weight=0.5, confidence=0.9,
                         valid_at=START + timedelta(days=day))
        prev = eid


def active_truth_events(mem, node_name):
    nid = mem._name2id[node_name]
    by_id = {n.nid: n for n in mem.store.fetch_nodes()}
    now = mem.clock()
    out = set()
    for e in mem.store.fetch_edges():
        if e.lifecycle != "active" or (e.invalid_at and e.invalid_at <= now):
            continue
        if e.from_id == nid:
            out.add(e.to_id)
        if e.to_id == nid:
            out.add(e.from_id)
    return {n for n in out if by_id[n].node_type == "event"}


def test_regression_metrics_gates():
    random.seed(42)
    mem = MemorySystem()
    build_corpus(mem)
    event_days = sorted({(n.ts - START).days for n in mem.store.fetch_nodes()
                         if n.node_type == "event"})
    topics = ["项目A", "张总", "微服务", "Kubernetes", "预算"]
    dataset = []
    for _ in range(120):
        anchor = random.choice(event_days) + random.randint(-1, 1)
        t0 = START + timedelta(days=anchor)
        truth = {n.nid for n in mem.store.fetch_nodes()
                 if n.node_type == "event" and abs((n.ts - t0).days) <= 1}
        dataset.append((RecallQuery(time=(t0, 2),
                                    topic=[random.choice(topics)]), truth, "A"))
    for _ in range(120):
        topic = random.choice(topics)
        dataset.append((RecallQuery(topic=[topic], text=topic),
                        active_truth_events(mem, topic), "B"))
    dual, _ = mem.evaluate(dataset, k=5, mode="dual")
    triple, _ = mem.evaluate(dataset, k=5, mode="triple")
    assert dual["recall"] >= 0.95
    assert dual["map"] >= 0.90
    assert triple["map"] >= dual["map"]
    mem.close()


def test_regression_noise_gate():
    """门禁口径：5 种子均值。"""
    recalls = []
    for seed in (42, 1, 7, 123, 2026):
        random.seed(seed)
        mem = MemorySystem()
        build_corpus(mem)
        event_days = sorted(
            {(n.ts - START).days for n in mem.store.fetch_nodes()
             if n.node_type == "event"})
        dataset = []
        for _ in range(600):
            anchor = random.choice(event_days)
            t0 = START + timedelta(days=anchor)
            q = RecallQuery(time=(t0, 2),
                            topic=[random.choice(["项目A", "张总", "微服务"])])
            # 与模拟基线保持同一 RNG 序列（text 抽样虽不被 dual 使用，但影响后续随机流）
            random.choice(["项目A", "张总", "微服务"])
            if random.random() < 0.35:
                q = RecallQuery(
                    time=(q.time[0] + timedelta(days=random.choice(
                        [-25, -18, 12, 20])), 2), topic=q.topic)
            if random.random() < 0.35:
                q = RecallQuery(
                    time=q.time,
                    topic=[random.choice(["供应商C", "单体架构", "李工"])])
            truth = {n.nid for n in mem.store.fetch_nodes()
                     if n.node_type == "event"
                     and abs((n.ts - t0).days) <= 2}
            dataset.append((q, truth, "noise"))
        agg, _ = mem.evaluate(dataset, k=5, mode="dual")
        recalls.append(agg["recall"])
        mem.close()
    mean = sum(recalls) / len(recalls)
    assert mean >= 0.65, f"5 种子均值 {mean:.3f} < 0.65"


def test_regression_lifecycle_gate(tmp_path):
    random.seed(42)
    clock = Clock()
    mem = MemorySystem(path=str(tmp_path / "m.db"), clock=clock)
    core_ids, trans_ids = [], []
    for i in range(60):
        if i < 20:
            core_ids.append(mem.add_event(
                f"核心{i}", clock() + timedelta(days=i), kind="core",
                value_score=0.85, life=150.0, decay_rate=0.02))
        else:
            trans_ids.append(mem.add_event(
                f"临时{i}", clock() + timedelta(days=i), kind="transient",
                value_score=0.3, life=50.0, decay_rate=0.28))
    pref = mem.add_entity("咖啡", "preference", protected=True)
    mem.add_fact("咖啡", "drink", "拿铁", source="profile", confidence=0.95)
    for _ in range(300):
        clock.now += timedelta(days=1)
        mem.step_day()
        for eid in core_ids:
            if random.random() < 0.08:
                mem.access(eid)
        for eid in trans_ids:
            if random.random() < 0.005:
                mem.access(eid)
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    assert sum(1 for eid in core_ids if nodes[eid].lifecycle != "active") == 0
    assert sum(1 for eid in trans_ids if nodes[eid].lifecycle != "active") == 40
    assert nodes[pref].lifecycle == "active"
    mem.close()


def test_regression_concurrency(tmp_path):
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    build_corpus(mem)
    errors = []

    def reader():
        try:
            for _ in range(20):
                mem.recall(RecallQuery(topic=["项目A"]), k=5,
                           node_types=("event",))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def writer():
        try:
            for i in range(20):
                mem.add_fact("项目A", f"note{i}", f"v{i}",
                             idempotency_key=f"note:{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    threads.append(threading.Thread(target=writer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    facts = len([f for f in mem.store.fetch_facts() if f.key.startswith("note")])
    assert not errors
    assert facts == 20
    mem.close()


def test_regression_legacy_base_pair_migration(tmp_path):
    random.seed(7)
    mem = MemorySystem(path=str(tmp_path / "m.db"))
    names = ["张总", "李工", "项目A", "项目B", "微服务", "Kubernetes",
             "预算", "供应商B"]
    legacy = []
    day = 0
    for i in range(60):
        day += 1 if i % 3 else 2
        core = i % 4 == 0
        legacy.append({"ts": START + timedelta(days=day),
                       "summary": f"旧事件{i}",
                       "entities": [(random.choice(names), "关联",
                                     3 if core else 2),
                                    (random.choice(names), "关联", 2)],
                       "etype": "core" if core else "transient",
                       "importance": 0.85 if core else 0.35})
    for le in legacy:
        core = le["etype"] == "core"
        eid = mem.add_event(le["summary"], le["ts"],
                            kind="core" if core else "transient",
                            value_score=le["importance"],
                            life=150.0 if core else 50.0,
                            decay_rate=0.02 if core else 0.28)
        for name, _rel, strength in le["entities"]:
            mem.add_entity(name, "entity", name)
            if name in PERSON:
                rel = "participates"
            elif "流程" in name or "技能" in name:
                rel = "step_of" if core else "mentions"
            else:
                rel = "discusses" if core else "mentions"
            mem.add_edge(eid, name, rel,
                         weight=0.9 if strength == 3 else 0.5,
                         confidence=0.8, valid_at=le["ts"])
    by_id = {n.nid: n for n in mem.store.fetch_nodes()}
    for topic in names:
        nid = mem._name2id[topic]
        old = set()
        for e in mem.store.fetch_edges():
            if e.from_id == nid:
                old.add(e.to_id)
            if e.to_id == nid:
                old.add(e.from_id)
        old = {n for n in old if by_id[n].node_type == "event"}
        if not old:
            continue
        new = {h.node_id for h in mem.recall(
            RecallQuery(topic=[topic]), k=200, mode="graph",
            node_types=("event",))}
        assert old.issubset(new), f"迁移降级: {topic}"
    mem.close()


# ---------------- S0-00-T 版本 0.7.0 ----------------
def test_version_070():
    import tomllib
    from pathlib import Path

    import dnamemory
    assert dnamemory.__version__ == "0.8.3"
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["version"] == "0.8.3"
