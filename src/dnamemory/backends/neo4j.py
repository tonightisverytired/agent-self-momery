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
        # 与 _graph_path 边界语义一致：可达节点（hop<=max_hops）的邻居也会被计分，
        # 因此 Cypher 路径上限取 max_hops+1。
        hops = int(max_hops) + 1
        query = (
            "MATCH (s:Entity) WHERE s.id IN $seeds\n"
            f"MATCH p=(s)-[rs*1..{hops}]-(n)\n"
            "WHERE n <> s\n"
        )
        if rel_filter:
            query += "AND ALL(x IN rs WHERE x.rel = $rel)\n"
        query += (
            "WITH n.id AS id, (n:Event) AS is_event, length(p) AS L,\n"
            "     relationships(p)[-1] AS r, nodes(p)[-2] AS prev\n"
            "RETURN id, max(CASE WHEN is_event "
            "THEN (1.0/L)*r.weight*r.confidence*prev.value_score "
            "ELSE (1.0/L)*r.weight*r.confidence END) AS score"
        )
        try:
            with self._driver.session() as s:
                params = {"seeds": list(seed_ids)}
                if rel_filter:
                    params["rel"] = rel_filter
                rows = s.run(query, **params)
                return {int(r["id"]): float(r["score"]) for r in rows}
        except Exception as e:
            raise StorageError(f"E009 Neo4j 图谱查询失败: {e}") from e

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
                "MERGE (a)-[x:REL {{rel:r.rel}}]->(b) "
                "SET x.weight=r.weight, x.confidence=r.confidence")
            if ee:
                s.run(merge_sql.format(fl="Entity", tl="Entity"), rows=ee)
            if ev:
                s.run(merge_sql.format(fl="Event", tl="Entity"), rows=ev)
            if ve:
                s.run(merge_sql.format(fl="Entity", tl="Event"), rows=ve)
    finally:
        driver.close()
