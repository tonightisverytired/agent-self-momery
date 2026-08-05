# -*- coding: utf-8 -*-
"""后端对比验证：SQLite（当前） vs Postgres vs Neo4j。

对比对象：
- 时间链（time chain）：时间窗召回 —— SQLite 索引扫描 vs Postgres 索引扫描 vs Neo4j 属性索引；
- 空间链（graph chain）：实体种子多跳遍历 —— Python BFS（当前实现） vs Postgres 递归 CTE vs Neo4j Cypher。

运行（需先启动 docker 容器）：
  docker run -d --name dnamemory-pg -e POSTGRES_PASSWORD=dnatest -e POSTGRES_USER=dnatest -e POSTGRES_DB=dnamemory -p 5433:5432 postgres:15-alpine
  docker run -d --name dnamemory-neo4j -e NEO4J_AUTH=neo4j/dnatest2026 -p 7687:7687 -p 7474:7474 neo4j:2025.03.0-community-bullseye
  py tools/backend_compare.py --events 5000 --entities 1000
输出：simulation/backend_compare_result.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PG_DSN = "postgresql://dnatest:dnatest@127.0.0.1:5433/dnamemory"
NEO4J_URI = "bolt://127.0.0.1:7687"
NEO4J_AUTH = ("neo4j", "dnatest2026")
MAX_HOPS = 2
TOL_DAYS = 2


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def gen_data(seed, n_events, n_entities, edges_per_event, extra_ee):
    rng = random.Random(seed)
    base = datetime(2026, 1, 1)
    entities = [{"id": i, "name": f"实体{i}"}
                for i in range(1, n_entities + 1)]
    events = []
    for i in range(1, n_events + 1):
        eid = n_entities + i
        events.append({
            "id": eid, "name": f"事件{i}",
            "ts": base + timedelta(days=rng.randint(0, 364),
                                   hours=rng.randint(0, 23)),
            "value": round(rng.uniform(0.3, 1.0), 3),
        })
    edges = []
    for ev in events:
        k = min(rng.randint(2, edges_per_event), n_entities)
        for ent in rng.sample(range(1, n_entities + 1), k):
            edges.append((ev["id"], ent, "mentions",
                          round(rng.uniform(0.5, 0.9), 3), 0.8))
    for _ in range(extra_ee):
        a, b = rng.sample(range(1, n_entities + 1), 2)
        edges.append((a, b, "similar_to", 0.7, 0.8))
    return entities, events, edges


def load_sqlite(path, entities, events, edges):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, node_type TEXT, name TEXT,
                           ts TEXT, value REAL);
        CREATE TABLE edges(from_id INT, to_id INT, rel TEXT,
                           weight REAL, confidence REAL);
        CREATE INDEX idx_nodes_ts ON nodes(ts);
        CREATE INDEX idx_edges_from ON edges(from_id);
        CREATE INDEX idx_edges_to ON edges(to_id);
    """)
    conn.executemany(
        "INSERT INTO nodes(id,node_type,name,ts,value) VALUES(?,?,?,?,?)",
        [(e["id"], "entity", e["name"], None, 0.7) for e in entities] +
        [(e["id"], "event", e["name"], e["ts"].isoformat(), e["value"])
         for e in events])
    conn.executemany(
        "INSERT INTO edges(from_id,to_id,rel,weight,confidence) "
        "VALUES(?,?,?,?,?)", edges)
    conn.commit()
    return conn


def load_postgres(entities, events, edges):
    import psycopg
    conn = psycopg.connect(PG_DSN)
    conn.execute("DROP TABLE IF EXISTS edges")
    conn.execute("DROP TABLE IF EXISTS nodes")
    conn.execute("""
        CREATE TABLE nodes(id BIGINT PRIMARY KEY, node_type TEXT, name TEXT,
                           ts TIMESTAMPTZ, value DOUBLE PRECISION);
        CREATE TABLE edges(from_id BIGINT, to_id BIGINT, rel TEXT,
                           weight DOUBLE PRECISION, confidence DOUBLE PRECISION);
        CREATE INDEX idx_nodes_ts ON nodes(ts);
        CREATE INDEX idx_edges_from ON edges(from_id);
        CREATE INDEX idx_edges_to ON edges(to_id);
    """)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO nodes(id,node_type,name,ts,value) VALUES(%s,%s,%s,%s,%s)",
            [(e["id"], "entity", e["name"], None, 0.7) for e in entities] +
            [(e["id"], "event", e["name"], e["ts"], e["value"])
             for e in events])
        cur.executemany(
            "INSERT INTO edges(from_id,to_id,rel,weight,confidence) "
            "VALUES(%s,%s,%s,%s,%s)", edges)
    conn.commit()
    return conn


def load_neo4j(entities, events, edges):
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    n_entities = len(entities)
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
        s.run("CREATE CONSTRAINT entity_id IF NOT EXISTS "
              "FOR (n:Entity) REQUIRE n.id IS UNIQUE")
        s.run("CREATE CONSTRAINT event_id IF NOT EXISTS "
              "FOR (n:Event) REQUIRE n.id IS UNIQUE")
        s.run("CREATE INDEX event_ts IF NOT EXISTS FOR (n:Event) ON (n.ts)")
        s.run("UNWIND $rows AS r MERGE (n:Entity {id:r.id}) "
              "SET n.name=r.name, n.value_score=r.value",
              rows=[{"id": e["id"], "name": e["name"], "value": 0.7}
                    for e in entities])
        s.run("UNWIND $rows AS r MERGE (n:Event {id:r.id}) "
              "SET n.name=r.name, n.value_score=r.value, n.ts=r.ts",
              rows=[{"id": e["id"], "name": e["name"],
                     "value": e["value"],
                     "ts": int(e["ts"].timestamp() * 1000)} for e in events])
        ee_rows = [{"from_id": a, "to_id": b, "rel": rel,
                    "weight": w, "confidence": c}
                   for a, b, rel, w, c in edges
                   if a <= n_entities and b <= n_entities]
        ev_rows = [{"from_id": a, "to_id": b, "rel": rel,
                    "weight": w, "confidence": c}
                   for a, b, rel, w, c in edges
                   if a > n_entities or b > n_entities]
        if ee_rows:
            s.run("UNWIND $rows AS r MATCH (a:Entity {id:r.from_id}) "
                  "MATCH (b:Entity {id:r.to_id}) "
                  "CREATE (a)-[:REL {rel:r.rel, weight:r.weight, "
                  "confidence:r.confidence}]->(b)",
                  rows=ee_rows)
        if ev_rows:
            s.run("UNWIND $rows AS r MATCH (a:Event {id:r.from_id}) "
                  "MATCH (b:Entity {id:r.to_id}) "
                  "CREATE (a)-[:REL {rel:r.rel, weight:r.weight, "
                  "confidence:r.confidence}]->(b)",
                  rows=ev_rows)
    return driver


def bench_time_sqlite(conn, queries):
    lat = []
    results = []
    for lo, hi in queries:
        t0 = time.perf_counter()
        rows = conn.execute(
            "SELECT id FROM nodes WHERE node_type='event' AND ts>=? AND ts<=?",
            (lo.isoformat(), hi.isoformat())).fetchall()
        lat.append((time.perf_counter() - t0) * 1000)
        results.append({r[0] for r in rows})
    return lat, results


def bench_time_postgres(conn, queries):
    lat = []
    results = []
    for lo, hi in queries:
        t0 = time.perf_counter()
        rows = conn.execute(
            "SELECT id FROM nodes WHERE node_type='event' AND ts>=%s AND ts<=%s",
            (lo, hi)).fetchall()
        lat.append((time.perf_counter() - t0) * 1000)
        results.append({r[0] for r in rows})
    return lat, results


def bench_time_neo4j(driver, queries):
    from neo4j.exceptions import Neo4jError
    lat = []
    results = []
    with driver.session() as s:
        for lo, hi in queries:
            t0 = time.perf_counter()
            recs = s.run(
                "MATCH (e:Event) WHERE e.ts >= $lo AND e.ts <= $hi "
                "RETURN e.id AS id", lo=int(lo.timestamp() * 1000),
                hi=int(hi.timestamp() * 1000))
            rows = [r["id"] for r in recs]
            lat.append((time.perf_counter() - t0) * 1000)
            results.append(set(rows))
    return lat, results


def bfs_python(conn, seed, max_hops=MAX_HOPS):
    edges = conn.execute("SELECT from_id,to_id FROM edges").fetchall()
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    frontier = [(seed, 0)]
    seen = {seed}
    out = set()
    while frontier:
        nid, hop = frontier.pop(0)
        if hop >= max_hops:
            continue
        for nb in adj.get(nid, []):
            if nb not in seen:
                seen.add(nb)
                frontier.append((nb, hop + 1))
                out.add(nb)
    ev = conn.execute(
        "SELECT id FROM nodes WHERE node_type='event'").fetchall()
    evset = {r[0] for r in ev}
    return out & evset


def bench_graph_sqlite(conn, seeds, hops=MAX_HOPS):
    lat, results = [], []
    for seed in seeds:
        t0 = time.perf_counter()
        results.append(bfs_python(conn, seed, max_hops=hops))
        lat.append((time.perf_counter() - t0) * 1000)
    return lat, results


def bench_graph_postgres(conn, seeds, hops=MAX_HOPS):
    lat, results = [], []
    for seed in seeds:
        t0 = time.perf_counter()
        rows = conn.execute("""
            WITH RECURSIVE reach(id, hop) AS (
              SELECT %s::bigint, 0
              UNION ALL
              SELECT CASE WHEN e.from_id=r.id THEN e.to_id ELSE e.from_id END,
                     r.hop+1
              FROM edges e JOIN reach r
                ON e.from_id=r.id OR e.to_id=r.id
              WHERE r.hop < %s
            )
            SELECT DISTINCT id FROM reach
            WHERE hop > 0 AND id IN (SELECT id FROM nodes WHERE node_type='event')
        """, (seed, hops)).fetchall()
        results.append({r[0] for r in rows})
        lat.append((time.perf_counter() - t0) * 1000)
    return lat, results


def bench_graph_neo4j(driver, seeds, hops=MAX_HOPS):
    lat, results = [], []
    with driver.session() as s:
        for seed in seeds:
            t0 = time.perf_counter()
            recs = s.run(
                f"MATCH (s:Entity {{id:$seed}})-[*1..{hops}]-(ev:Event) "
                "RETURN DISTINCT ev.id AS id", seed=seed)
            rows = [r["id"] for r in recs]
            results.append(set(rows))
            lat.append((time.perf_counter() - t0) * 1000)
    return lat, results


def stats(lat):
    return {
        "n": len(lat), "mean_ms": round(statistics.fmean(lat), 3),
        "p50_ms": round(statistics.median(lat), 3),
        "p95_ms": round(sorted(lat)[int(len(lat) * 0.95) - 1], 3),
    }


def main():
    _force_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--events", type=int, default=5000)
    parser.add_argument("--entities", type=int, default=1000)
    parser.add_argument("--edges-per-event", type=int, default=3)
    parser.add_argument("--extra-ee", type=int, default=2000)
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--hops", type=int, default=MAX_HOPS)
    parser.add_argument("--sqlite", default=os.path.join(
        ROOT, ".pytest_tmp", "backend_compare.db"))
    args = parser.parse_args()

    print("生成合成数据...")
    entities, events, edges = gen_data(
        args.seed, args.events, args.entities, args.edges_per_event,
        args.extra_ee)
    print(f"entities={len(entities)} events={len(events)} edges={len(edges)}")

    rng = random.Random(args.seed)
    time_queries = []
    for ev in rng.sample(events, min(args.queries, len(events))):
        lo = ev["ts"] - timedelta(days=TOL_DAYS)
        hi = ev["ts"] + timedelta(days=TOL_DAYS)
        time_queries.append((lo, hi))
    graph_seeds = rng.sample([e["id"] for e in entities],
                             min(args.queries, len(entities)))

    report = {
        "scale": {"entities": len(entities), "events": len(events),
                  "edges": len(edges)},
        "config": {"max_hops": args.hops, "tol_days": TOL_DAYS,
                   "queries": len(time_queries)},
        "time_chain": {}, "graph_chain": {},
    }

    # SQLite（当前实现）
    os.makedirs(os.path.dirname(args.sqlite), exist_ok=True)
    conn = load_sqlite(args.sqlite, entities, events, edges)
    bench_time_sqlite(conn, time_queries[:1])
    bench_graph_sqlite(conn, graph_seeds[:1], hops=args.hops)
    lat, res = bench_time_sqlite(conn, time_queries)
    report["time_chain"]["sqlite"] = stats(lat)
    sqlite_time_sets = res
    lat, res = bench_graph_sqlite(conn, graph_seeds, hops=args.hops)
    report["graph_chain"]["sqlite_python_bfs"] = stats(lat)
    sqlite_graph_sets = res
    conn.close()

    # Postgres
    import psycopg
    pg = load_postgres(entities, events, edges)
    bench_time_postgres(pg, time_queries[:1])
    bench_graph_postgres(pg, graph_seeds[:1], hops=args.hops)
    lat, res = bench_time_postgres(pg, time_queries)
    report["time_chain"]["postgres"] = stats(lat)
    pg_time_ok = all(a == b for a, b in zip(sqlite_time_sets, res))
    lat, res = bench_graph_postgres(pg, graph_seeds, hops=args.hops)
    report["graph_chain"]["postgres_recursive_cte"] = stats(lat)
    pg_graph_ok = all(a == b for a, b in zip(sqlite_graph_sets, res))
    pg.close()

    # Neo4j
    from neo4j import GraphDatabase
    driver = load_neo4j(entities, events, edges)
    bench_time_neo4j(driver, time_queries[:1])
    bench_graph_neo4j(driver, graph_seeds[:1], hops=args.hops)
    lat, res = bench_time_neo4j(driver, time_queries)
    report["time_chain"]["neo4j"] = stats(lat)
    neo4j_time_ok = all(a == b for a, b in zip(sqlite_time_sets, res))
    lat, res = bench_graph_neo4j(driver, graph_seeds, hops=args.hops)
    report["graph_chain"]["neo4j_cypher"] = stats(lat)
    neo4j_graph_ok = all(a == b for a, b in zip(sqlite_graph_sets, res))
    driver.close()

    report["correctness"] = {
        "time": {"postgres_vs_sqlite": pg_time_ok,
                 "neo4j_vs_sqlite": neo4j_time_ok},
        "graph": {"postgres_vs_sqlite": pg_graph_ok,
                  "neo4j_vs_sqlite": neo4j_graph_ok},
    }

    out = os.path.join(ROOT, "simulation", "backend_compare_result.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"结果已保存: {out}")


if __name__ == "__main__":
    main()
