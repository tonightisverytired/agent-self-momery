# -*- coding: utf-8 -*-
"""
长期个人智能体记忆系统模拟 v2（带类型关联边模型）
===================================================
依据 docs/00-长期个人智能体记忆系统方案.md 实现：
  - nodes：事件/实体统一异构节点（价值分、保护区、生命周期状态）
  - edges：带类型关联边（relation_type, weight, confidence, 双时态）
  - facts：双时态事实（valid_at/recorded_at/invalid_at + 来源可信级）
  - versions / tombstones：版本审计与显式撤回
  - 检索：时间路 + 图谱路 + 语义路 三路召回，RRF 融合
  - 治理：价值优先遗忘、可信源矛盾策略、TTL 过期、保护区不衰减
  - 迁移：旧"碱基对"(strength 2/3) → 新关联边的映射与回归验证

"碱基对"概念已废弃；本文件为其替代形态的可运行验证。
运行：py simulation/dna_helix_memory_sim.py
"""

import math
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

random.seed(42)
START = datetime(2026, 3, 1)

RELATIONS = [
    "participates", "discusses", "mentions", "occurs_with", "precedes",
    "causes", "depends_on", "similar_to", "part_of", "prefers",
    "updates_to", "contradicts", "summarizes", "step_of",
]
SOURCE_RANK = {"profile": 3, "agent": 2, "chat": 1}
PERSON_NAMES = {"张总", "李工", "王总"}


@dataclass
class Node:
    nid: int
    node_type: str            # event | entity
    kind: str                 # meeting/decision/deploy/chat | person/project/concept/skill/preference/vendor
    name: str
    ts: datetime              # 仅事件
    description: str
    value_score: float
    protected: bool
    lifecycle: str            # active/archived/tombstoned/deleted
    life: float               # 生命周期预算（protected 节点为 inf）
    decay_per_day: float
    access_label: str
    last_access: datetime = None


@dataclass
class Edge:
    eid: int
    from_id: int
    to_id: int
    rel: str
    weight: float
    confidence: float
    valid_at: datetime
    invalid_at: datetime
    created_at: datetime
    last_access: datetime = None
    access_count: int = 0
    lifecycle: str = "active"


@dataclass
class Fact:
    fid: int
    node_id: int
    key: str
    value: str
    source: str
    confidence: float
    valid_at: datetime
    recorded_at: datetime
    invalid_at: datetime = None
    superseded_by: int = None
    tombstoned: bool = False


@dataclass
class Tombstone:
    tid: int
    target_type: str
    target_id: int
    reason: str
    at: datetime


class MemorySystem:
    """带类型关联边记忆系统（v2，碱基对已废弃）。"""

    def __init__(self, start=START):
        self.nodes = {}
        self.edges = {}
        self.facts = {}
        self.tombstones = {}
        self.name2id = {}
        self.clock = start
        self.next_nid = 1
        self.next_eid = 1
        self.next_fid = 1
        self.next_tid = 1
        self.audit = []

    # ---------------- 写入 ----------------
    def _add_node(self, node_type, name, kind, ts, description, value_score,
                  protected, life, decay_per_day, access_label="public"):
        n = Node(self.next_nid, node_type, kind, name, ts, description,
                 value_score, protected, "active", life, decay_per_day,
                 access_label)
        self.nodes[n.nid] = n
        if name not in self.name2id:
            self.name2id[name] = n.nid
        self.next_nid += 1
        return n.nid

    def add_entity(self, name, kind="concept", description="",
                   value_score=0.7, protected=False, access_label="public"):
        if name in self.name2id:
            return self.name2id[name]
        return self._add_node("entity", name, kind, None, description,
                              value_score, protected, float("inf"), 0.0,
                              access_label)

    def add_event(self, name, ts, kind="meeting", description="",
                  value_score=0.5, protected=False, life=50.0,
                  decay_per_day=0.28, access_label="public"):
        return self._add_node("event", name, kind, ts, description,
                              value_score, protected, life, decay_per_day,
                              access_label)

    def add_edge(self, a, b, rel, weight=0.5, confidence=0.8,
                 valid_at=None, invalid_at=None):
        assert rel in RELATIONS, f"未知关系类型: {rel}"
        aid = self.name2id.get(a, a)
        bid = self.name2id.get(b, b)
        assert aid in self.nodes and bid in self.nodes, "节点不存在"
        e = Edge(self.next_eid, aid, bid, rel, weight, confidence,
                 valid_at or self.clock, invalid_at, self.clock)
        self.edges[e.eid] = e
        self.next_eid += 1
        return e.eid

    def add_fact(self, entity_name, key, value, source="chat",
                 confidence=0.7, valid_at=None, recorded_at=None,
                 invalid_at=None):
        nid = self.name2id[entity_name]
        f = Fact(self.next_fid, nid, key, value, source, confidence,
                 valid_at or self.clock, recorded_at or self.clock, invalid_at)
        self.facts[f.fid] = f
        self.next_fid += 1
        return f.fid

    def version(self, nid, content):
        self.audit.append(("version", nid, self.clock, content))

    def tombstone(self, target_type, target_id, reason):
        if target_type == "node":
            self.nodes[target_id].lifecycle = "tombstoned"
        elif target_type == "edge":
            self.edges[target_id].lifecycle = "tombstoned"
        elif target_type == "fact":
            self.facts[target_id].tombstoned = True
        self.tombstones[self.next_tid] = Tombstone(
            self.next_tid, target_type, target_id, reason, self.clock)
        self.next_tid += 1
        self.audit.append(("tombstone", target_type, target_id, self.clock))

    # ---------------- 检索 ----------------
    def _active_edges(self):
        return [e for e in self.edges.values()
                if e.lifecycle == "active"
                and (e.invalid_at is None or e.invalid_at > self.clock)]

    def _time_path(self, t0, tol_days):
        out = {}
        for n in self.nodes.values():
            if n.node_type != "event" or n.lifecycle != "active" or n.ts is None:
                continue
            diff = abs((n.ts - t0).days)
            if diff <= tol_days:
                out[n.nid] = 1.0 / (1.0 + diff)
        return out

    def _graph_path(self, topics, max_hops=2, rel_filter=None):
        seeds = []
        for n in self.nodes.values():
            if n.node_type != "entity" or n.lifecycle != "active":
                continue
            if any(t.lower() in n.name.lower() or t.lower() in n.description.lower()
                   for t in topics):
                seeds.append((n.nid, 0))
        if not seeds:
            return {}
        reach = {}
        frontier = list(seeds)
        visited = set()
        while frontier:
            nid, hop = frontier.pop(0)
            if nid in visited or hop > max_hops:
                continue
            visited.add(nid)
            reach[nid] = hop
            for e in self._active_edges():
                if rel_filter and e.rel != rel_filter:
                    continue
                if e.from_id == nid and e.to_id not in visited:
                    frontier.append((e.to_id, hop + 1))
                elif e.to_id == nid and e.from_id not in visited:
                    frontier.append((e.from_id, hop + 1))
        out = {}
        for e in self._active_edges():
            for src, dst in ((e.from_id, e.to_id), (e.to_id, e.from_id)):
                if src in reach and self.nodes[dst].node_type == "event":
                    hop = reach[src]
                    ent = self.nodes[src]
                    sc = (1.0 / (hop + 1)) * e.weight * e.confidence * ent.value_score
                    out[dst] = max(out.get(dst, 0.0), sc)
        # 实体邻居也参与召回（支持 prefers/part_of 等实体级关系查询）
        for e in self._active_edges():
            for src, dst in ((e.from_id, e.to_id), (e.to_id, e.from_id)):
                if (src in reach and self.nodes[dst].node_type == "entity"
                        and dst != src):
                    hop = reach[src]
                    sc = (1.0 / (hop + 1)) * e.weight * e.confidence
                    out[dst] = max(out.get(dst, 0.0), sc)
        return out

    def _semantic_path(self, text):
        tokens = re.findall(r"[0-9a-zA-Z\u4e00-\u9fff]+", text or "")
        tokens = [t.lower() for t in tokens]
        if not tokens:
            return {}
        out = {}
        for n in self.nodes.values():
            if n.lifecycle != "active":
                continue
            hay = (n.name + " " + n.description).lower()
            hits = sum(1 for t in tokens if t in hay)
            if hits:
                out[n.nid] = (hits / len(tokens)) * n.value_score
        return out

    def neighbors(self, node_name, rel=None):
        """角色/关系过滤：返回 (邻居节点, 边, 方向)。"""
        nid = self.name2id.get(node_name)
        if nid is None:
            return []
        out = []
        for e in self._active_edges():
            if rel and e.rel != rel:
                continue
            if e.from_id == nid:
                out.append((self.nodes[e.to_id], e, "out"))
            elif e.to_id == nid:
                out.append((self.nodes[e.from_id], e, "in"))
        return out

    def recall(self, query, k=8, mode="triple", max_hops=2, tol_days=2,
               node_types=None):
        """query: {text, time:(t0,tol), topic:[...], relation:{node,rel_type}}
        返回 [(nid, score, [来源路径])]，按分数降序。"""
        paths = {}
        if mode in ("time", "dual", "triple") and query.get("time"):
            t0, tol = query["time"]
            paths["time"] = self._time_path(t0, tol or tol_days)
        if mode in ("graph", "dual", "triple") and query.get("topic"):
            rel = None
            if query.get("relation"):
                rel = query["relation"].get("rel_type")
            paths["graph"] = self._graph_path(query["topic"], max_hops, rel)
        if mode in ("semantic", "triple") and query.get("text"):
            paths["semantic"] = self._semantic_path(query["text"])

        # 分数加权 RRF：score(n) = Σ_path path_score(n) / (60 + rank(n))
        # 相比纯 RRF，低分噪声（如被污染的时间提示、过宽的语义命中）自然衰减
        scores = {}
        for pname, ps in paths.items():
            ranked = sorted(ps.items(), key=lambda x: -x[1])
            for rk, (nid, sc) in enumerate(ranked):
                scores[nid] = scores.get(nid, 0.0) + sc / (60.0 + rk)

        if query.get("relation"):
            rel = query["relation"].get("rel_type")
            allowed = {nb.nid for nb, _, _ in self.neighbors(
                query["relation"]["node"], rel)}
            scores = {nid: sc for nid, sc in scores.items() if nid in allowed}

        if node_types:
            scores = {nid: sc for nid, sc in scores.items()
                      if self.nodes[nid].node_type in node_types}

        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return [(nid, sc, [p for p, ps in paths.items() if nid in ps])
                for nid, sc in ranked[:k]]

    # ---------------- 生命周期 ----------------
    def access(self, nid):
        n = self.nodes[nid]
        n.last_access = self.clock
        if n.protected:
            return
        n.life -= 0.2
        n.life += 3.0 + 6.0 * min(n.value_score, 1.0)

    def step_day(self):
        self.clock += timedelta(days=1)
        for n in self.nodes.values():
            if n.lifecycle != "active" or n.protected:
                continue
            n.life -= n.decay_per_day
            if n.life <= 0:
                n.lifecycle = "archived"  # 受控遗忘：降权不删除
                self.audit.append(("archive", n.nid, self.clock))

    def alive_ratio(self, kind=None, node_type="event"):
        total = alive = 0
        for n in self.nodes.values():
            if n.node_type != node_type:
                continue
            if kind and n.kind != kind:
                continue
            total += 1
            if n.lifecycle == "active":
                alive += 1
        return alive / total if total else 0.0

    def fact_lookup(self, entity_name, key):
        nid = self.name2id.get(entity_name)
        if nid is None:
            return []
        return [f for f in self.facts.values()
                if f.node_id == nid and f.key == key and not f.tombstoned
                and (f.invalid_at is None or f.invalid_at > self.clock)]

    # ---------------- 矛盾策略 ----------------
    def resolve_conflicts(self):
        groups = {}
        for f in self.facts.values():
            if f.tombstoned:
                continue
            groups.setdefault((f.node_id, f.key), []).append(f)
        decisions = []
        for (nid, key), fs in groups.items():
            active = [f for f in fs
                      if f.invalid_at is None or f.invalid_at > self.clock]
            if len(active) < 2:
                continue
            ordered = sorted(
                active,
                key=lambda f: (SOURCE_RANK[f.source], f.confidence, f.valid_at),
                reverse=True)
            top, second = ordered[0], ordered[1]
            same_rank = SOURCE_RANK[top.source] == SOURCE_RANK[second.source]
            if same_rank and top.confidence >= 0.8 and second.confidence >= 0.8:
                decisions.append(("confirm", nid, key, top, second))
            else:
                for f in active:
                    if f is not top:
                        f.superseded_by = top.fid
                        f.invalid_at = self.clock
                decisions.append(("resolved", nid, key, top, second))
        return decisions


# --------------------------------------------------------------------------
# 语料构建（v2：带类型关联边）
# --------------------------------------------------------------------------
def build_corpus(mem):
    topics = [
        ("张总", "person", "项目负责人，负责预算审批"),
        ("李工", "person", "后端工程师，负责技术选型"),
        ("项目A", "project", "2026 年核心交付项目"),
        ("项目B", "project", "探索型创新项目"),
        ("微服务", "concept", "分布式架构风格"),
        ("单体架构", "concept", "传统一体化架构"),
        ("Kubernetes", "skill", "容器编排平台"),
        ("预算", "concept", "项目资金计划"),
        ("供应商B", "vendor", "云服务供应商"),
        ("技术选型", "concept", "方案对比与选择"),
        ("部署流程", "skill", "上线操作步骤"),
        ("供应商C", "vendor", "监控工具供应商"),
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
        ("与张总讨论{proj}预算问题", ["张总", "预算", "项目A"], "meeting",
         "transient", 0.45),
        ("与李工进行{tech}技术选型", ["李工", "技术选型", "微服务"], "decision",
         "core", 0.85),
        ("{proj}例会：同步{sup}进展", ["供应商B", "项目A"], "meeting",
         "transient", 0.5),
        ("部署{stack}集群到生产环境", ["Kubernetes", "部署流程"], "deploy",
         "core", 0.9),
        ("评估{arch1}与{arch2}架构差异", ["微服务", "单体架构"], "decision",
         "core", 0.8),
        ("与张总复盘{proj}延期原因", ["项目A", "张总"], "meeting",
         "transient", 0.55),
    ]
    eids = []
    for i in range(150):
        day += (1 if i % 3 == 0 else 2)
        tmpl, atoms, kind, etype, imp = random.choice(templates)
        proj = random.choice(["项目A", "项目B"])
        summary = tmpl.format(
            proj=proj,
            tech=random.choice(["微服务", "Kubernetes"]),
            sup=random.choice(["供应商B", "供应商C"]),
            arch1="微服务", arch2="单体架构", stack="Kubernetes")
        core = etype == "core"
        eid = mem.add_event(summary, START + timedelta(days=day), kind=kind,
                            value_score=imp, life=150.0 if core else 50.0,
                            decay_per_day=0.02 if core else 0.28)
        for atom in atoms:
            rel = "participates" if atom in PERSON_NAMES else "discusses"
            w = 0.9 if core else 0.5
            mem.add_edge(eid, atom, rel, weight=w, confidence=0.8,
                         valid_at=START + timedelta(days=day))
        if eids:
            mem.add_edge(eids[-1], eid, "precedes", weight=0.5,
                         confidence=0.9, valid_at=START + timedelta(days=day))
        eids.append(eid)
    return eids


# --------------------------------------------------------------------------
# 实验 1：三路召回率（时间 / 图谱 / 双路 / 三路）
# --------------------------------------------------------------------------
def experiment_recall():
    print("=" * 76)
    print("实验 1：召回率对比（时间路 / 图谱路 / 双路 / 三路 RRF）")
    print("=" * 76)
    mem = MemorySystem()
    build_corpus(mem)
    event_days = sorted({(n.ts - START).days for n in mem.nodes.values()
                         if n.node_type == "event"})
    max_day = event_days[-1]
    topics = ["项目A", "张总", "微服务", "Kubernetes", "预算"]

    queries = []
    for _ in range(120):
        anchor = random.choice(event_days) + random.randint(-1, 1)
        t0 = START + timedelta(days=anchor)
        truth = {n.nid for n in mem.nodes.values()
                 if n.node_type == "event" and abs((n.ts - t0).days) <= 1}
        q = {"time": (t0, 2), "topic": [random.choice(topics)]}
        queries.append((q, truth))
    for _ in range(120):
        topic = random.choice(topics)
        nid = mem.name2id[topic]
        truth = {e.to_id for e in mem._active_edges() if e.from_id == nid}
        truth |= {e.from_id for e in mem._active_edges() if e.to_id == nid}
        truth = {n for n in truth if mem.nodes[n].node_type == "event"}
        q = {"topic": [topic], "text": topic}
        queries.append((q, truth))

    n = len(queries)
    stats = {}
    for mode in ("time", "graph", "dual", "triple"):
        hit = 0
        for q, truth in queries:
            got = {nid for nid, _, _ in mem.recall(
                q, k=5, mode=mode, node_types=["event"])}
            hit += 1 if got & truth else 0
        stats[mode] = hit / n

    for mode in ("time", "graph", "dual", "triple"):
        print(f"  {mode:>8} 召回率 = {stats[mode]:.3f}")
    print(f"  双路相对最佳单路提升 = "
          f"{(stats['dual'] - max(stats['time'], stats['graph'])) * 100:+.1f}%")
    print(f"  三路相对双路提升 = {(stats['triple'] - stats['dual']) * 100:+.1f}%")
    return stats


# --------------------------------------------------------------------------
# 实验 2：抗遗忘噪声（5 种子均值，消除抽样波动）
# --------------------------------------------------------------------------
def run_noise_once(trials=600):
    mem = MemorySystem()
    build_corpus(mem)
    event_days = sorted({(n.ts - START).days for n in mem.nodes.values()
                         if n.node_type == "event"})
    single = dual = triple = 0
    for _ in range(trials):
        anchor = random.choice(event_days)
        q = {"time": (START + timedelta(days=anchor), 2),
             "topic": [random.choice(["项目A", "张总", "微服务"])],
             "text": random.choice(["项目A", "张总", "微服务"])}
        if random.random() < 0.35:
            q["time"] = (q["time"][0] + timedelta(days=random.choice(
                [-25, -18, 12, 20])), 2)
        if random.random() < 0.35:
            q["topic"] = [random.choice(["供应商C", "单体架构", "李工"])]
            q["text"] = q["topic"][0]
        truth = {n.nid for n in mem.nodes.values()
                 if n.node_type == "event"
                 and abs((n.ts - (START + timedelta(days=anchor))).days) <= 2}
        got1 = {nid for nid, _, _ in mem.recall(
            q, k=5, mode="graph", node_types=["event"])}
        got2 = {nid for nid, _, _ in mem.recall(
            q, k=5, mode="dual", node_types=["event"])}
        got3 = {nid for nid, _, _ in mem.recall(
            q, k=5, mode="triple", node_types=["event"])}
        single += 1 if got1 & truth else 0
        dual += 1 if got2 & truth else 0
        triple += 1 if got3 & truth else 0
    return single / trials, dual / trials, triple / trials


def experiment_noise():
    print()
    print("=" * 76)
    print("实验 2：抗遗忘噪声（时间/主题各有 35% 被污染，5 种子 × 600 查询）")
    print("=" * 76)
    state = random.getstate()
    rows = []
    for s in (42, 1, 7, 123, 2026):
        random.seed(s)
        a, b, c = run_noise_once()
        rows.append((s, a, b, c))
        print(f"  seed={s:>5}: 单维={a:.3f} 双维={b:.3f} 三维={c:.3f}")
    random.setstate(state)
    mean_dual = sum(r[2] for r in rows) / len(rows)
    print(f"  5 种子均值: 双维={mean_dual:.3f}"
          f"（min={min(r[2] for r in rows):.3f}, "
          f"max={max(r[2] for r in rows):.3f}）")
    return mean_dual


# --------------------------------------------------------------------------
# 实验 3：生命周期（价值优先遗忘 + 保护区）
# --------------------------------------------------------------------------
def experiment_lifecycle():
    print()
    print("=" * 76)
    print("实验 3：生命周期 300 天（核心 vs 临时 vs 保护区偏好）")
    print("=" * 76)
    mem = MemorySystem()
    core_ids, trans_ids = [], []
    for i in range(60):
        if i < 20:
            eid = mem.add_event(f"核心知识{i}", START + timedelta(days=i),
                                kind="core", value_score=0.85,
                                life=150.0, decay_per_day=0.02)
            core_ids.append(eid)
        else:
            eid = mem.add_event(f"临时会话{i}", START + timedelta(days=i),
                                kind="transient", value_score=0.3,
                                life=50.0, decay_per_day=0.28)
            trans_ids.append(eid)
    pref = mem.add_entity("咖啡", "preference", "用户偏好", protected=True)
    mem.add_fact("咖啡", "favorite_drink", "拿铁", source="profile",
                 confidence=0.95)

    series = []
    for day in range(1, 301):
        mem.step_day()
        for eid in core_ids:
            if random.random() < 0.08:
                mem.access(eid)
        for eid in trans_ids:
            if random.random() < 0.005:
                mem.access(eid)
        if day % 30 == 0:
            series.append((day, mem.alive_ratio(kind="core"),
                           mem.alive_ratio(kind="transient")))

    print(f"{'天数':>6} {'核心记忆存活率':>14} {'临时记忆存活率':>14}")
    for d, c, t in series:
        print(f"{d:>6} {c:>14.2%} {t:>14.2%}")
    archived_core = sum(1 for eid in core_ids
                        if mem.nodes[eid].lifecycle == "archived")
    archived_trans = sum(1 for eid in trans_ids
                         if mem.nodes[eid].lifecycle == "archived")
    print(f"核心记忆归档: {archived_core}/20；临时记忆归档: {archived_trans}/40")
    print(f"保护区偏好 '咖啡/拿铁' 存活: "
          f"{mem.nodes[pref].lifecycle == 'active'}"
          f"，facts 有效: {all(f.invalid_at is None for f in mem.facts.values())}")
    return series


# --------------------------------------------------------------------------
# 实验 4：角色/关系过滤查询
# --------------------------------------------------------------------------
def experiment_relation_filter():
    print()
    print("=" * 76)
    print("实验 4：角色/关系过滤（participates / discusses / prefers）")
    print("=" * 76)
    mem = MemorySystem()
    for name, kind, desc in [("张总", "person", ""), ("李工", "person", ""),
                             ("项目A", "project", ""), ("项目B", "project", ""),
                             ("咖啡", "preference", ""), ("拿铁", "drink", "")]:
        mem.add_entity(name, kind, desc, protected=(kind == "preference"))
    e1 = mem.add_event("预算会", START, value_score=0.6, life=50.0)
    e2 = mem.add_event("技术选型会", START + timedelta(days=2), value_score=0.7,
                       life=50.0)
    e3 = mem.add_event("部署会", START + timedelta(days=4), value_score=0.6,
                       life=50.0)
    mem.add_edge(e1, "张总", "participates", 0.8, 0.9, valid_at=START)
    mem.add_edge(e1, "项目A", "discusses", 0.8, 0.9, valid_at=START)
    mem.add_edge(e2, "张总", "participates", 0.8, 0.9,
                 valid_at=START + timedelta(days=2))
    mem.add_edge(e2, "项目B", "discusses", 0.8, 0.9,
                 valid_at=START + timedelta(days=2))
    mem.add_edge(e3, "李工", "participates", 0.8, 0.9,
                 valid_at=START + timedelta(days=4))
    mem.add_edge(e3, "项目A", "discusses", 0.8, 0.9,
                 valid_at=START + timedelta(days=4))
    mem.add_edge("咖啡", "拿铁", "prefers", 0.9, 0.95, valid_at=START)
    mem.add_fact("咖啡", "favorite_drink", "拿铁", source="profile", confidence=0.95)

    print("张总 participates 的邻居:")
    for nb, e, _ in mem.neighbors("张总", "participates"):
        print(f"  - {nb.name} (事件 e{nb.nid}, weight={e.weight:.1f})")
    ev_names = {n.nid: n.name for n in mem.nodes.values()}
    projects = set()
    for nb, _, _ in mem.neighbors("张总", "participates"):
        for nb2, e2_, _ in mem.neighbors(nb.name, "discusses"):
            if nb2.node_type == "entity":
                projects.add(nb2.name)
    print(f"张总参与讨论的项目: {sorted(projects)}")
    hits = mem.recall({"topic": ["张总"],
                       "relation": {"node": "张总", "rel_type": "participates"}},
                      mode="triple", k=5)
    print(f"带 participates 过滤的 recall 命中: "
          f"{[ev_names[nid] for nid, _, _ in hits]}")
    hits2 = mem.recall({"topic": ["咖啡"],
                        "relation": {"node": "咖啡", "rel_type": "prefers"}},
                       mode="triple", k=5)
    print(f"prefers 偏好查询命中: {[mem.nodes[nid].name for nid, _, _ in hits2]}")


# --------------------------------------------------------------------------
# 实验 5：双时态矛盾（可信源优先，而非新者胜）
# --------------------------------------------------------------------------
def experiment_conflict():
    print()
    print("=" * 76)
    print("实验 5：双时态矛盾策略（profile 档案 vs chat 闲聊）")
    print("=" * 76)
    mem = MemorySystem()
    mem.add_entity("居住地", "preference", "用户居住城市", protected=True)
    mem.add_fact("居住地", "address", "上海", source="profile",
                 confidence=0.95, valid_at=START)
    # 更晚但低可信的闲聊："Actually we moved to Munich last month."（模拟）
    mem.add_fact("居住地", "address", "慕尼黑", source="chat",
                 confidence=0.6, valid_at=START + timedelta(days=30),
                 recorded_at=START + timedelta(days=30))
    naive = "慕尼黑"  # 旧"时间戳新者胜"的答案
    decisions = mem.resolve_conflicts()
    for d in decisions:
        print(f"策略输出: {d[0]} | key={d[2]} | "
              f"候选: {[f.value for f in [d[3], d[4]]]}")
    active = [f.value for f in mem.facts.values()
              if not f.tombstoned and (f.invalid_at is None
                                       or f.invalid_at > mem.clock)]
    print(f"活跃事实: {active}（旧策略会答: {naive}）")
    print(f"审计 superseded: {[f.superseded_by for f in mem.facts.values()]}")

    # 同源双高置信 → 确认
    mem2 = MemorySystem()
    mem2.add_entity("口味", "preference", "口味", protected=True)
    mem2.add_fact("口味", "taste", "辣的", source="chat", confidence=0.9)
    mem2.add_fact("口味", "taste", "甜的", source="chat", confidence=0.9)
    ds = mem2.resolve_conflicts()
    print(f"同源高置信冲突策略: {ds[0][0] if ds else '无'}（应为 confirm）")


# --------------------------------------------------------------------------
# 实验 6：TTL 过期 + 显式撤回墓碑
# --------------------------------------------------------------------------
def experiment_tombstone_ttl():
    print()
    print("=" * 76)
    print("实验 6：TTL 过期与显式撤回（墓碑）")
    print("=" * 76)
    mem = MemorySystem()
    mem.add_entity("促销活动", "concept", "限时促销")
    mem.add_fact("促销活动", "price", "85 折", source="agent", confidence=0.8,
                 valid_at=START, invalid_at=START + timedelta(days=90))
    mem.add_entity("旧项目", "project", "已完结项目")
    fid = mem.add_fact("旧项目", "status", "进行中", source="chat",
                       confidence=0.8)

    mem.clock += timedelta(days=91)
    expired = [f.value for f in mem.facts.values()
               if f.invalid_at is not None and f.invalid_at <= mem.clock]
    active_price = [f.value for f in mem.fact_lookup("促销活动", "price")]
    print(f"TTL 过期事实: {expired}；过期后活跃事实: {active_price}（应排除）")
    hits = mem.recall({"topic": ["促销"]}, mode="triple", k=5)
    print(f"过期后 '促销' 召回: {[mem.nodes[nid].name for nid, _, _ in hits]}"
          f"（实体可检索，过期事实不出现）")

    mem.tombstone("fact", fid, "retracted")
    hits2 = mem.recall({"topic": ["旧项目"]}, mode="triple", k=5)
    print(f"撤回后 '旧项目' 召回: {[mem.nodes[nid].name for nid, _, _ in hits2]}")
    print(f"撤回后 '旧项目/status' 活跃事实: "
          f"{[f.value for f in mem.fact_lookup('旧项目', 'status')]}")
    print(f"墓碑记录数: {len(mem.tombstones)}（审计保留，检索彻底过滤）")


# --------------------------------------------------------------------------
# 实验 7：旧"碱基对"→ 关联边迁移回归
# --------------------------------------------------------------------------
def default_rel(name, core):
    if name in PERSON_NAMES:
        return "participates"
    if "流程" in name or "技能" in name:
        return "step_of" if core else "mentions"
    return "discusses" if core else "mentions"


def migrate_legacy_base_pairs(legacy_events):
    """旧格式 {ts, summary, entities:[(name, rel, strength)], etype, importance}
    按映射规则转新模型：strength 3→weight 0.9，2→0.5；事件间 precedes。"""
    mem = MemorySystem()
    mappings = []
    prev = None
    for le in legacy_events:
        core = le["etype"] == "core"
        eid = mem.add_event(le["summary"], le["ts"],
                            kind="core" if core else "transient",
                            value_score=le["importance"],
                            life=150.0 if core else 50.0,
                            decay_per_day=0.02 if core else 0.28)
        for name, _rel, strength in le["entities"]:
            mem.add_entity(name, "实体", name)
            w = 0.9 if strength == 3 else 0.5
            rel = default_rel(name, core)
            mem.add_edge(eid, name, rel, weight=w, confidence=0.8,
                         valid_at=le["ts"])
            mappings.append((le["summary"], name, strength, rel, w))
        if prev is not None:
            mem.add_edge(prev, eid, "precedes", weight=0.5, confidence=0.9,
                         valid_at=le["ts"])
        prev = eid
    return mem, mappings


def legacy_lookup(mem, topic):
    """旧模型语义：碱基对集合遍历，返回与实体直接相连的事件。"""
    nid = mem.name2id.get(topic)
    if nid is None:
        return set()
    return {e.to_id for e in mem._active_edges() if e.from_id == nid} | \
           {e.from_id for e in mem._active_edges() if e.to_id == nid}


# --------------------------------------------------------------------------
# 实验 8：指标集（Recall@k / MRR / MAP@k / NDCG@k）+ MAP 可行性
# --------------------------------------------------------------------------
def average_precision(ranked_ids, truth, k):
    if not truth:
        return 0.0
    hits = 0
    ap = 0.0
    for i, nid in enumerate(ranked_ids[:k]):
        if nid in truth:
            hits += 1
            ap += hits / (i + 1.0)
    return ap / min(len(truth), k)


def ndcg_at_k(ranked_ids, truth, k):
    grades = {nid: 1 for nid in truth}
    dcg = sum(grades.get(nid, 0) / math.log2(i + 2)
              for i, nid in enumerate(ranked_ids[:k]))
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def mrr_at_k(ranked_ids, truth, k):
    for i, nid in enumerate(ranked_ids[:k]):
        if nid in truth:
            return 1.0 / (i + 1.0)
    return 0.0


def bootstrap_ci(samples, n_iter=200, alpha=0.05):
    state = random.getstate()
    means = []
    n = len(samples)
    for _ in range(n_iter):
        idx = [random.randrange(n) for _ in range(n)]
        means.append(sum(samples[i] for i in idx) / n)
    random.setstate(state)
    means.sort()
    lo = means[int(n_iter * alpha / 2)]
    hi = means[int(n_iter * (1 - alpha / 2)) - 1]
    return lo, hi


def build_metric_queries(mem, topics, max_day, event_days):
    queries = []
    for _ in range(120):  # A 类：时间锚定
        anchor = random.choice(event_days) + random.randint(-1, 1)
        t0 = START + timedelta(days=anchor)
        truth = {n.nid for n in mem.nodes.values()
                 if n.node_type == "event" and abs((n.ts - t0).days) <= 1}
        queries.append({"label": "A", "q": {"time": (t0, 2),
                                            "topic": [random.choice(topics)]},
                        "truth": truth})
    for _ in range(120):  # B 类：主题锚定
        topic = random.choice(topics)
        nid = mem.name2id[topic]
        truth = {e.to_id for e in mem._active_edges() if e.from_id == nid}
        truth |= {e.from_id for e in mem._active_edges() if e.to_id == nid}
        truth = {n for n in truth if mem.nodes[n].node_type == "event"}
        queries.append({"label": "B", "q": {"topic": [topic], "text": topic},
                        "truth": truth})
    return queries


def experiment_metrics():
    print()
    print("=" * 76)
    print("实验 8：指标集 + MAP 可行性（240 查询，5 档 k）")
    print("=" * 76)
    mem = MemorySystem()
    build_corpus(mem)
    event_days = sorted({(n.ts - START).days for n in mem.nodes.values()
                         if n.node_type == "event"})
    max_day = event_days[-1]
    topics = ["项目A", "张总", "微服务", "Kubernetes", "预算"]
    queries = build_metric_queries(mem, topics, max_day, event_days)

    modes = ("time", "graph", "dual", "triple")
    agg = {m: {"recall": 0.0, "precision": 0.0, "mrr": 0.0, "map": 0.0,
               "ndcg": 0.0} for m in modes}
    ap_by_type = {m: {"A": [], "B": []} for m in modes}

    for item in queries:
        truth = item["truth"]
        for m in modes:
            ids = [nid for nid, _, _ in mem.recall(
                item["q"], k=5, mode=m, node_types=["event"])]
            hits = set(ids) & truth
            agg[m]["recall"] += 1.0 if hits else 0.0
            agg[m]["precision"] += len(hits) / 5.0
            agg[m]["mrr"] += mrr_at_k(ids, truth, 5)
            agg[m]["map"] += average_precision(ids, truth, 5)
            agg[m]["ndcg"] += ndcg_at_k(ids, truth, 5)
            ap_by_type[m][item["label"]].append(average_precision(ids, truth, 5))

    n = len(queries)
    print(f"\n{'路径':<8} {'Recall@5':>9} {'Precision@5':>11} {'MRR':>7} "
          f"{'MAP@5':>7} {'NDCG@5':>8}")
    for m in modes:
        for key in agg[m]:
            agg[m][key] /= n
        print(f"{m:<8} {agg[m]['recall']:>9.3f} {agg[m]['precision']:>11.3f} "
              f"{agg[m]['mrr']:>7.3f} {agg[m]['map']:>7.3f} "
              f"{agg[m]['ndcg']:>8.3f}")

    print("\n按查询类型分层的 MAP@5（证明分层报告的必要性）:")
    for m in modes:
        ma = sum(ap_by_type[m]["A"]) / len(ap_by_type[m]["A"])
        mb = sum(ap_by_type[m]["B"]) / len(ap_by_type[m]["B"])
        print(f"  {m:<8} A 时间锚定={ma:.3f}  B 主题锚定={mb:.3f}")

    lo, hi = bootstrap_ci(ap_by_type["dual"]["A"] + ap_by_type["dual"]["B"])
    print(f"\n双路 MAP@5 bootstrap 95% CI: [{lo:.3f}, {hi:.3f}]")

    # 噪声切片：污染后的 MAP（可行性：噪声下排序质量是否仍可用）
    noise_aps, noise_recall = [], 0
    for _ in range(120):
        anchor = random.choice(event_days)
        q = {"time": (START + timedelta(days=anchor), 2),
             "topic": [random.choice(["项目A", "张总", "微服务"])]}
        if random.random() < 0.35:
            q["time"] = (q["time"][0] + timedelta(days=random.choice(
                [-25, -18, 12, 20])), 2)
        if random.random() < 0.35:
            q["topic"] = [random.choice(["供应商C", "单体架构", "李工"])]
        truth = {n.nid for n in mem.nodes.values()
                 if n.node_type == "event"
                 and abs((n.ts - (START + timedelta(days=anchor))).days) <= 2}
        ids = [nid for nid, _, _ in mem.recall(q, k=5, mode="dual",
                                               node_types=["event"])]
        noise_aps.append(average_precision(ids, truth, 5))
        noise_recall += 1 if set(ids) & truth else 0
    print(f"噪声切片（120 查询，35% 污染）双路: "
          f"MAP@5={sum(noise_aps) / 120:.3f}, "
          f"Recall@5={noise_recall / 120:.3f}")

    print("\nMAP 可行性要点（详见 docs/05-MAP可行性探究.md）:")
    print("  1. 模拟中真值确定性可得 → MAP 可精确计算；生产需人工/LLM 标注")
    print("  2. 分层报告必要：A/B 类 MAP 差异显著，混合值会掩盖类型缺陷")
    print("  3. 空真值查询需剔除并单独统计覆盖率，避免分母污染")
    print("  4. MAP 需与 Recall@k 配对使用，防'排序好但漏召回'")
    return agg, ap_by_type


def experiment_migration():
    print()
    print("=" * 76)
    print("实验 7：旧碱基对 → 关联边迁移回归（新结果 ⊇ 旧结果）")
    print("=" * 76)
    random.seed(7)
    legacy = []
    day = 0
    names = ["张总", "李工", "项目A", "项目B", "微服务", "Kubernetes",
             "预算", "供应商B"]
    for i in range(60):
        day += 1 if i % 3 else 2
        core = i % 4 == 0
        ents = [(random.choice(names), "关联", 3 if core else 2),
                (random.choice(names), "关联", 2)]
        legacy.append({"ts": START + timedelta(days=day),
                       "summary": f"旧事件{i}",
                       "entities": ents,
                       "etype": "core" if core else "transient",
                       "importance": 0.85 if core else 0.35})
    mem, mappings = migrate_legacy_base_pairs(legacy)
    print(f"迁移边数: {len(mappings)}（期望 {sum(len(l['entities']) for l in legacy)}）")
    s3 = sum(1 for _, _, s, _, _ in mappings if s == 3)
    s2 = sum(1 for _, _, s, _, _ in mappings if s == 2)
    print(f"映射分布: strength=3 → weight 0.9: {s3} 条；"
          f"strength=2 → weight 0.5: {s2} 条")

    degraded = 0
    total = 0
    for topic in names:
        old_set = legacy_lookup(mem, topic)
        old_set = {n for n in old_set if mem.nodes[n].node_type == "event"}
        if not old_set:
            continue
        new_set = {nid for nid, _, _ in mem.recall({"topic": [topic]},
                                                   mode="graph", k=200)}
        total += 1
        if not new_set.issuperset(old_set):
            degraded += 1
            print(f"  [降级] topic={topic}: 旧 {len(old_set)} 条, 新 {len(new_set)} 条")
    print(f"迁移回归: {total} 个主题查询，降级 {degraded} 个"
          f"（{'通过' if degraded == 0 else '失败'}）")
    return degraded == 0


# --------------------------------------------------------------------------
def main():
    print("长期个人智能体记忆系统模拟 v2（带类型关联边，碱基对已废弃）")
    print("方案依据: docs/00-长期个人智能体记忆系统方案.md")
    stats = experiment_recall()
    d1 = experiment_noise()
    experiment_lifecycle()
    experiment_relation_filter()
    experiment_conflict()
    experiment_tombstone_ttl()
    agg, _ = experiment_metrics()
    ok_mig = experiment_migration()

    print()
    print("=" * 76)
    print("验收清单")
    print("=" * 76)
    checks = [
        ("双路召回 ≥ 0.933", stats["dual"] >= 0.933, f"{stats['dual']:.3f}"),
        ("带噪双路均值 ≥ 0.663", d1 >= 0.663, f"{d1:.3f}"),
        ("三路召回 ≥ 双路", stats["triple"] >= stats["dual"],
         f"{stats['triple']:.3f} vs {stats['dual']:.3f}"),
        ("双路 MRR ≥ 0.80", agg["dual"]["mrr"] >= 0.80,
         f"{agg['dual']['mrr']:.3f}"),
        ("双路 MAP@5 ≥ 0.75", agg["dual"]["map"] >= 0.75,
         f"{agg['dual']['map']:.3f}"),
        ("双路 NDCG@5 ≥ 0.80", agg["dual"]["ndcg"] >= 0.80,
         f"{agg['dual']['ndcg']:.3f}"),
        ("迁移回归无降级", ok_mig, "0 降级" if ok_mig else "有降级"),
    ]
    for label, ok, val in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {val}")


if __name__ == "__main__":
    main()
