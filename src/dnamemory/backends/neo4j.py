# -*- coding: utf-8 -*-
"""Neo4j 空间链后端（阶段 2，可选）。

- 查询只读：数据由 sync_memory_to_neo4j 预置；
- 分语义与 retrieval._graph_path 一致（Cypher 按路径最大分折算）。
"""
from __future__ import annotations

from typing import Optional

from ..errors import StorageError


class Neo4jGraphBackend:
    """空间链后端：`traverse(seed_ids, max_hops, rel_filter)` 返回可达节点分。"""

    def __init__(self, uri: str, auth=None):
        try:
            from neo4j import GraphDatabase
        except ImportError as e:  # pragma: no cover
            raise StorageError("E009 需要 neo4j 驱动：pip install neo4j") from e
        try:
            self._driver = GraphDatabase.driver(uri, auth=auth)
            self._driver.verify_connectivity()
        except Exception as e:
            raise StorageError(f"E009 Neo4j 连接失败: {e}") from e

    def traverse(self, seed_ids: list[int], max_hops: int,
                 rel_filter: Optional[str] = None) -> dict[int, float]:
        if not seed_ids:
            return {}
        try:
            with self._driver.session() as s:
                # 1) 种子节点（hop=0）+ 可达节点（最短 hop，与 _graph_path 的 BFS reach 一致）
                seed_rows = s.run(
                    "MATCH (s:Entity) WHERE s.id IN $seeds "
                    "RETURN s.id AS id, s.value_score AS value",
                    seeds=list(seed_ids)).data()
                rows = s.run(
                    "MATCH (s:Entity) WHERE s.id IN $seeds "
                    f"MATCH p=(s)-[rs*1..{int(max_hops)}]-(n) "
                    "WITH n, min(length(p)) AS hop "
                    "RETURN n.id AS id, hop, n.value_score AS value, "
                    "(n:Event) AS is_event",
                    seeds=list(seed_ids)).data()
                if not rows:
                    return {}
                reach = {int(r["id"]): 0 for r in seed_rows}
                values = {int(r["id"]): r["value"] for r in seed_rows}
                is_event = {int(r["id"]): False for r in seed_rows}
                for r in rows:
                    nid = int(r["id"])
                    hop = int(r["hop"])
                    if nid not in reach or hop < reach[nid]:
                        reach[nid] = hop
                    values.setdefault(nid, r["value"])
                    is_event.setdefault(nid, bool(r["is_event"]))
                # 2) 可达节点邻接子图（含可达节点一跳外的邻居，边界与 _graph_path 一致）
                ids = list(reach)
                erows = s.run(
                    "MATCH (a)-[r:REL]->(b) WHERE a.id IN $ids OR b.id IN $ids "
                    "RETURN a.id AS a, (a:Event) AS ae, "
                    "b.id AS b, (b:Event) AS be, "
                    "r.rel AS rel, r.weight AS w, r.confidence AS c",
                    ids=ids).data()
        except Exception as e:
            raise StorageError(f"E009 Neo4j 图谱查询失败: {e}") from e

        # 3) 用与 retrieval._graph_path 相同的打分逻辑（保证逐位一致）
        out: dict[int, float] = {}
        for r in erows:
            a, b = int(r["a"]), int(r["b"])
            if a not in reach and b not in reach:
                continue
            if rel_filter and r["rel"] != rel_filter:
                continue
            w, c = float(r["w"]), float(r["c"])
            for src, dst in ((a, b), (b, a)):
                if src not in reach or dst == src:
                    continue
                hop = reach[src]
                score = (1.0 / (hop + 1)) * w * c
                dst_is_event = r["be"] if src == a else r["ae"]
                if dst_is_event:
                    score *= values.get(src, 0.0)
                if score > out.get(dst, 0.0):
                    out[dst] = score
        return out

    def close(self):
        try:
            self._driver.close()
        except Exception:  # noqa: BLE001
            pass


def sync_memory_to_neo4j(mem, uri: str, auth=None):
    """把 MemorySystem 的 nodes/edges 预置到 Neo4j（查询只读用）。"""
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(uri, auth=auth)
    try:
        nodes = mem.store.fetch_nodes()
        type_of = {n.nid: n.node_type for n in nodes}
        edges = [e for e in mem.store.fetch_edges()
                 if e.lifecycle == "active"]
        with driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n")
            s.run("CREATE CONSTRAINT entity_id IF NOT EXISTS "
                  "FOR (n:Entity) REQUIRE n.id IS UNIQUE")
            s.run("CREATE CONSTRAINT event_id IF NOT EXISTS "
                  "FOR (n:Event) REQUIRE n.id IS UNIQUE")
            entities = [n for n in nodes if n.node_type == "entity"]
            events = [n for n in nodes if n.node_type == "event"]
            if entities:
                s.run("UNWIND $rows AS r MERGE (n:Entity {id:r.id}) "
                      "SET n.name=r.name, n.value_score=r.value",
                      rows=[{"id": n.nid, "name": n.name,
                             "value": n.value_score} for n in entities])
            if events:
                s.run("UNWIND $rows AS r MERGE (n:Event {id:r.id}) "
                      "SET n.name=r.name, n.value_score=r.value, n.ts=r.ts",
                      rows=[{"id": n.nid, "name": n.name,
                             "value": n.value_score,
                             "ts": int(n.ts.timestamp() * 1000)
                             if n.ts else None} for n in events])
            ee, ev, ve = [], [], []
            for e in edges:
                row = {"from_id": e.from_id, "to_id": e.to_id,
                       "rel": e.rel, "weight": e.weight,
                       "confidence": e.confidence}
                ft, tt = type_of.get(e.from_id), type_of.get(e.to_id)
                if ft == "entity" and tt == "entity":
                    ee.append(row)
                elif ft == "event" and tt == "entity":
                    ev.append(row)
                elif ft == "entity" and tt == "event":
                    ve.append(row)
            merge_sql = (
                "UNWIND $rows AS r MATCH (a:{fl} {{id:r.from_id}}) "
                "MATCH (b:{tl} {{id:r.to_id}}) "
                "CREATE (a)-[:REL {{rel:r.rel, weight:r.weight, "
                "confidence:r.confidence}}]->(b)")
            if ee:
                s.run(merge_sql.format(fl="Entity", tl="Entity"), rows=ee)
            if ev:
                s.run(merge_sql.format(fl="Event", tl="Entity"), rows=ev)
            if ve:
                s.run(merge_sql.format(fl="Entity", tl="Event"), rows=ve)
    finally:
        driver.close()
