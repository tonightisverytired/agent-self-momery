# -*- coding: utf-8 -*-
"""SQLite 存储层（docs/09 C2）。"""
from __future__ import annotations

import sqlite3
import threading
import time
import json
from contextlib import contextmanager
from datetime import datetime

from .errors import StorageError
from .models import Edge, Fact, Node, Tombstone

DDL = """
CREATE TABLE IF NOT EXISTS nodes (
  id INTEGER PRIMARY KEY,
  node_type TEXT NOT NULL CHECK (node_type IN ('event','entity')),
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  ts TEXT,
  value_score REAL NOT NULL,
  protected INTEGER NOT NULL DEFAULT 0,
  access_label TEXT NOT NULL DEFAULT 'public',
  lifecycle TEXT NOT NULL DEFAULT 'active',
  life REAL NOT NULL,
  decay_rate REAL NOT NULL DEFAULT 0,
  last_access TEXT,
  created_at TEXT NOT NULL,
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_nodes_ts ON nodes(ts);
CREATE INDEX IF NOT EXISTS idx_nodes_life ON nodes(lifecycle);
CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY,
  from_id INTEGER NOT NULL REFERENCES nodes(id),
  to_id INTEGER NOT NULL REFERENCES nodes(id),
  relation_type TEXT NOT NULL,
  weight REAL NOT NULL,
  confidence REAL NOT NULL,
  valid_at TEXT NOT NULL,
  invalid_at TEXT,
  created_at TEXT NOT NULL,
  last_access TEXT,
  access_count INTEGER NOT NULL DEFAULT 0,
  lifecycle TEXT NOT NULL DEFAULT 'active',
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);
CREATE INDEX IF NOT EXISTS idx_edges_rel ON edges(relation_type);
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY,
  node_id INTEGER NOT NULL REFERENCES nodes(id),
  fact_key TEXT NOT NULL,
  fact_value TEXT NOT NULL,
  source TEXT NOT NULL,
  confidence REAL NOT NULL,
  valid_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  invalid_at TEXT,
  superseded_by INTEGER,
  tombstoned INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(node_id, fact_key);
CREATE TABLE IF NOT EXISTS versions (
  id INTEGER PRIMARY KEY,
  node_id INTEGER NOT NULL REFERENCES nodes(id),
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tombstones (
  id INTEGER PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id INTEGER NOT NULL,
  reason TEXT NOT NULL,
  tombstoned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  op TEXT NOT NULL,
  target_type TEXT,
  target_id INTEGER,
  reason TEXT,
  meta TEXT,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_vectors (
  node_id INTEGER PRIMARY KEY REFERENCES nodes(id),
  dense TEXT NOT NULL,
  sparse TEXT,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def _dt(v):
    return datetime.fromisoformat(v) if v else None


class SQLiteStore:
    def __init__(self, path=":memory:"):
        self.path = path
        self._memory = path == ":memory:"
        self._wlock = threading.RLock()  # 可重入：事务内幂等检查需嵌套加锁
        self._conn = sqlite3.connect(path, check_same_thread=False)
        if not self._memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(DDL)
        self._conn.commit()

    # ---------------- 读写 ----------------
    @contextmanager
    def transaction(self):
        with self._wlock:
            last_err = None
            for attempt in range(3):
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as e:
                    last_err = e
                    time.sleep(0.05 * (3 ** attempt))
            else:
                raise StorageError(f"E009 storage busy: {last_err}")
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def read(self, sql, params=()):
        if self._memory:
            with self._wlock:
                return self._conn.execute(sql, params).fetchall()
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def audit(self, op, target_type=None, target_id=None, reason=None,
              meta=None, at=None):
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log(op,target_type,target_id,reason,meta,at)"
                " VALUES(?,?,?,?,?,?)",
                (op, target_type, target_id, reason, meta,
                 (at or datetime.now()).isoformat()))

    # ---------------- 写入 ----------------
    def insert_node(self, node_type, kind, name, description, ts, value_score,
                    protected, access_label, life, decay_rate, created_at,
                    idempotency_key=None, conn=None):
        if idempotency_key:
            rows = self.read(
                "SELECT id FROM nodes WHERE idempotency_key=?", (idempotency_key,))
            if rows:
                return rows[0][0]
        params = (node_type, kind, name, description,
                  ts.isoformat() if ts else None, value_score,
                  int(protected), access_label, life, decay_rate,
                  created_at.isoformat(), idempotency_key)
        if conn is not None:
            return conn.execute(
                "INSERT INTO nodes(node_type,kind,name,description,ts,"
                "value_score,protected,access_label,life,decay_rate,"
                "created_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                params).lastrowid
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO nodes(node_type,kind,name,description,ts,"
                "value_score,protected,access_label,life,decay_rate,"
                "created_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                params)
            c.execute("INSERT INTO audit_log(op,target_type,target_id,at)"
                      " VALUES('write','node',?,?)",
                      (cur.lastrowid, created_at.isoformat()))
            return cur.lastrowid

    def insert_edge(self, from_id, to_id, rel, weight, confidence, valid_at,
                    invalid_at, created_at, idempotency_key=None, conn=None):
        if idempotency_key:
            rows = self.read(
                "SELECT id FROM edges WHERE idempotency_key=?", (idempotency_key,))
            if rows:
                return rows[0][0]
        params = (from_id, to_id, rel, weight, confidence,
                  valid_at.isoformat(), invalid_at.isoformat() if invalid_at else None,
                  created_at.isoformat(), idempotency_key)
        if conn is not None:
            return conn.execute(
                "INSERT INTO edges(from_id,to_id,relation_type,weight,confidence,"
                "valid_at,invalid_at,created_at,idempotency_key)"
                " VALUES(?,?,?,?,?,?,?,?,?)", params).lastrowid
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO edges(from_id,to_id,relation_type,weight,confidence,"
                "valid_at,invalid_at,created_at,idempotency_key)"
                " VALUES(?,?,?,?,?,?,?,?,?)", params)
            return cur.lastrowid

    def insert_fact(self, node_id, key, value, source, confidence, valid_at,
                    recorded_at, invalid_at=None, idempotency_key=None,
                    conn=None):
        if idempotency_key:
            rows = self.read(
                "SELECT id FROM facts WHERE idempotency_key=?", (idempotency_key,))
            if rows:
                return rows[0][0]
        params = (node_id, key, value, source, confidence,
                  valid_at.isoformat(), recorded_at.isoformat(),
                  invalid_at.isoformat() if invalid_at else None, idempotency_key)
        if conn is not None:
            return conn.execute(
                "INSERT INTO facts(node_id,fact_key,fact_value,source,confidence,"
                "valid_at,recorded_at,invalid_at,idempotency_key)"
                " VALUES(?,?,?,?,?,?,?,?,?)", params).lastrowid
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO facts(node_id,fact_key,fact_value,source,confidence,"
                "valid_at,recorded_at,invalid_at,idempotency_key)"
                " VALUES(?,?,?,?,?,?,?,?,?)", params)
            return cur.lastrowid

    def set_node_vectors(self, node_id, dense, sparse=None, model="",
                         dim=None, updated_at=None, conn=None):
        """写入稠密+稀疏向量；稠密向量缺失/为空直接报错（写入校验）。"""
        if not dense or not isinstance(dense, (list, tuple)):
            raise StorageError("E011 稠密向量缺失或为空，写入校验失败")
        dim = dim or len(dense)
        updated_at = updated_at or datetime.now()
        params = (node_id, json.dumps([float(x) for x in dense]),
                  json.dumps(sparse) if sparse else None,
                  model, dim, updated_at.isoformat())
        sql = (
            "INSERT INTO node_vectors(node_id,dense,sparse,model,dim,updated_at)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(node_id) DO UPDATE SET"
            " dense=excluded.dense, sparse=excluded.sparse,"
            " model=excluded.model, dim=excluded.dim,"
            " updated_at=excluded.updated_at")
        if conn is not None:
            conn.execute(sql, params)
            return
        with self.transaction() as c:
            c.execute(sql, params)

    def has_node_vector(self, node_id):
        rows = self.read("SELECT 1 FROM node_vectors WHERE node_id=?",
                         (node_id,))
        return bool(rows)

    def fetch_node_vectors(self):
        rows = self.read(
            "SELECT node_id, dense, sparse, model, dim, updated_at"
            " FROM node_vectors")
        return [(r[0], json.loads(r[1]), json.loads(r[2]) if r[2] else None,
                 r[3], r[4], _dt(r[5]))
                for r in rows]

    def add_version(self, node_id, version, content, at):
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO versions(node_id,version,content,created_at)"
                " VALUES(?,?,?,?)", (node_id, version, content, at.isoformat()))

    def add_tombstone(self, target_type, target_id, reason, at):
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO tombstones(target_type,target_id,reason,"
                "tombstoned_at) VALUES(?,?,?,?)",
                (target_type, target_id, reason, at.isoformat()))
            conn.execute("INSERT INTO audit_log(op,target_type,target_id,"
                         "reason,at) VALUES('tombstone',?,?,?,?)",
                         (target_type, target_id, reason, at.isoformat()))

    # ---------------- 更新 ----------------
    def update_lifecycle(self, node_id, lifecycle, at=None):
        with self.transaction() as conn:
            conn.execute("UPDATE nodes SET lifecycle=? WHERE id=?",
                         (lifecycle, node_id))
            conn.execute("INSERT INTO audit_log(op,target_type,target_id,"
                         "reason,at) VALUES(?,?,?,?,?)",
                         ("lifecycle", "node", node_id, lifecycle,
                          (at or datetime.now()).isoformat()))

    def update_life_decay(self, node_id, life, last_access):
        with self.transaction() as conn:
            conn.execute("UPDATE nodes SET life=?, last_access=? WHERE id=?",
                         (life, last_access.isoformat(), node_id))

    def update_life(self, node_id, life):
        with self.transaction() as conn:
            conn.execute("UPDATE nodes SET life=? WHERE id=?", (life, node_id))

    def supersede_fact(self, fid, by, at):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE facts SET invalid_at=?, superseded_by=? WHERE id=?",
                (at.isoformat(), by, fid))

    def tombstone_fact(self, fid, at):
        with self.transaction() as conn:
            conn.execute("UPDATE facts SET tombstoned=1 WHERE id=?", (fid,))
            conn.execute("INSERT INTO audit_log(op,target_type,target_id,"
                         "reason,at) VALUES('tombstone','fact',?,?,?)",
                         (fid, "retracted", at.isoformat()))

    # ---------------- 读取 ----------------
    def fetch_nodes(self):
        rows = self.read("SELECT * FROM nodes")
        return [Node(r[0], r[1], r[2], r[3], r[4], _dt(r[5]), r[6],
                     bool(r[7]), r[8], r[9], r[10], r[11], _dt(r[12]),
                     _dt(r[13]))
                for r in rows]

    def fetch_edges(self):
        rows = self.read("SELECT * FROM edges")
        return [Edge(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]), _dt(r[7]),
                     _dt(r[8]), r[10], r[11])
                for r in rows]

    def fetch_facts(self):
        rows = self.read("SELECT * FROM facts")
        return [Fact(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]), _dt(r[7]),
                     _dt(r[8]), r[9], bool(r[10]))
                for r in rows]

    def fetch_versions(self, node_id):
        return self.read("SELECT version, content, created_at FROM versions"
                         " WHERE node_id=? ORDER BY version", (node_id,))

    def fetch_tombstones(self):
        rows = self.read("SELECT * FROM tombstones")
        return [Tombstone(r[0], r[1], r[2], r[3], _dt(r[4])) for r in rows]

    def close(self):
        with self._wlock:
            self._conn.close()
