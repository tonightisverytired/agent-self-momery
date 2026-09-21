# -*- coding: utf-8 -*-
"""SQLite 存储层。"""
from __future__ import annotations

import sqlite3
import threading
import time
import json
import re
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache

from .errors import StorageError, ValidationError
from .models import (BELIEF_POLARITIES, EVIDENCE_SOURCE_TYPES,
                     IMPACT_DIRECTIONS, IMPACT_KINDS, IMPACT_VALENCES,
                     INTENT_STATUSES, MEMORY_LINK_RELATIONS, PATTERN_TYPES,
                     TRIGGER_TYPES, Belief, Edge, Evidence, Fact, Impact,
                     Intent, MemoryLink, Node, Pattern, Tombstone, Trigger,
                     to_naive)

LEGAL_LIFECYCLE = frozenset({
    ("active", "active"), ("active", "archived"), ("active", "tombstoned"),
    ("active", "deleted"),
    ("archived", "archived"), ("archived", "active"),
    ("archived", "tombstoned"), ("archived", "deleted"),
    ("tombstoned", "tombstoned"), ("tombstoned", "active"),
    ("tombstoned", "deleted"),
    ("deleted", "deleted"),
})

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
  idempotency_key TEXT UNIQUE,
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL DEFAULT ''
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
  idempotency_key TEXT UNIQUE,
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  explicit_confirmation INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS vector_index_meta (
  model TEXT PRIMARY KEY,
  dim INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS beliefs (
  id INTEGER PRIMARY KEY,
  subject_id INTEGER NOT NULL REFERENCES nodes(id),
  proposition TEXT NOT NULL,
  polarity TEXT NOT NULL DEFAULT 'neutral',
  confidence REAL NOT NULL,
  source TEXT NOT NULL DEFAULT 'chat',
  valid_at TEXT,
  invalid_at TEXT,
  superseded_by INTEGER,
  lifecycle TEXT NOT NULL DEFAULT 'active',
  access_label TEXT NOT NULL DEFAULT 'public',
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_beliefs_subject ON beliefs(subject_id);
CREATE TABLE IF NOT EXISTS intents (
  id INTEGER PRIMARY KEY,
  subject_id INTEGER NOT NULL REFERENCES nodes(id),
  proposition TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  confidence REAL NOT NULL DEFAULT 0.7,
  source TEXT NOT NULL DEFAULT 'chat',
  valid_at TEXT,
  invalid_at TEXT,
  lifecycle TEXT NOT NULL DEFAULT 'active',
  access_label TEXT NOT NULL DEFAULT 'public',
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_intents_subject ON intents(subject_id);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_ref TEXT NOT NULL DEFAULT '',
  conversation_id TEXT,
  message_id TEXT,
  observed_at TEXT,
  content_hash TEXT,
  trust_level REAL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  access_label TEXT NOT NULL DEFAULT 'public'
);
CREATE TABLE IF NOT EXISTS memory_links (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL,
  target_id INTEGER NOT NULL,
  source_type TEXT NOT NULL,
  relation TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.7,
  valid_at TEXT,
  invalid_at TEXT,
  idempotency_key TEXT UNIQUE,
  source_dim TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_memory_links_source ON memory_links(source_id);
CREATE INDEX IF NOT EXISTS idx_memory_links_target ON memory_links(target_id);
CREATE TABLE IF NOT EXISTS impacts (
  id INTEGER PRIMARY KEY,
  subject_id INTEGER NOT NULL REFERENCES nodes(id),
  dimension TEXT NOT NULL,
  direction TEXT NOT NULL,
  valence TEXT NOT NULL,
  magnitude REAL NOT NULL DEFAULT 0.5,
  kind TEXT NOT NULL DEFAULT 'objective',
  evaluator TEXT NOT NULL DEFAULT 'agent',
  description TEXT NOT NULL DEFAULT '',
  cause_event_id INTEGER,
  source TEXT NOT NULL DEFAULT 'chat',
  confidence REAL NOT NULL DEFAULT 0.7,
  valid_at TEXT,
  invalid_at TEXT,
  superseded_by INTEGER,
  lifecycle TEXT NOT NULL DEFAULT 'active',
  access_label TEXT NOT NULL DEFAULT 'public',
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_impacts_subject_dim
  ON impacts(subject_id, dimension);
CREATE TABLE IF NOT EXISTS triggers (
  id INTEGER PRIMARY KEY,
  memory_id INTEGER NOT NULL,
  memory_type TEXT NOT NULL,
  trigger_type TEXT NOT NULL,
  text TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'rule',
  confidence REAL NOT NULL DEFAULT 0.7,
  created_at TEXT NOT NULL,
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_triggers_text ON triggers(text);
CREATE INDEX IF NOT EXISTS idx_triggers_mem
  ON triggers(memory_type, memory_id);
CREATE TABLE IF NOT EXISTS patterns (
  id INTEGER PRIMARY KEY,
  pattern_type TEXT NOT NULL,
  subject_id INTEGER NOT NULL REFERENCES nodes(id),
  proposition TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.7,
  support INTEGER NOT NULL DEFAULT 1,
  window_days INTEGER,
  source TEXT NOT NULL DEFAULT 'inferred',
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  valid_at TEXT,
  created_at TEXT NOT NULL,
  idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_patterns_subject_type
  ON patterns(subject_id, pattern_type);
"""

# 0.5.0 向后兼容迁移：旧库缺列补列（幂等；新列追加在表尾，
# 与新库 DDL 的列顺序保持一致，保证 SELECT * 位置解析一致）。
_MIGRATIONS = (
    ("nodes", "evidence_ids", "TEXT NOT NULL DEFAULT '[]'"),
    ("nodes", "source", "TEXT NOT NULL DEFAULT ''"),
    ("facts", "evidence_ids", "TEXT NOT NULL DEFAULT '[]'"),
    ("facts", "explicit_confirmation", "INTEGER NOT NULL DEFAULT 0"),
    ("evidence", "access_label", "TEXT NOT NULL DEFAULT 'public'"),
    ("memory_links", "source_dim", "TEXT NOT NULL DEFAULT ''"),
)


@lru_cache(maxsize=65536)
def _dt_str(s):
    # 同一时间串在库内跨行大量重复（一次 recall_context 曾解析 211
    # 万次）；datetime 不可变，共享安全
    return to_naive(s)


def _dt(v):
    # 读边界统一归一：库里的历史行可能带时区偏移（aware），混进链路会
    # 让 `aware > naive` 直接抛 TypeError
    if isinstance(v, str):
        return _dt_str(v)
    return to_naive(v)


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
        self._migrate_legacy()
        self._conn.commit()
        self._vec = None
        try:
            import sqlite_vec
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._vec = sqlite_vec
        except Exception:  # noqa: BLE001 无向量索引时走 numpy 降级
            self._vec = None
        self._data_version = 0
        self._adj_cache = None
        self._adj_key = None
        self._snap = {}
        self._snap_key = None

    def _db_version(self):
        """库级持久版号（PRAGMA user_version）：写事务随提交自增。
        多实例共享同一文件库时，内存计数器感知不到他实例的写入，
        缓存失效必须以库级版号为准（0.8.2 test_api_graph 归档可见性
        回归的修复）。走 read() 取连接：文件库每次新建连接（线程安全），
        内存库走 _wlock——直连 self._conn 会在读写并发时与写事务争用
        同一连接对象（test_regression_concurrency 的 NoneType 回归）。
        """
        return self.read("PRAGMA user_version")[0][0]

    def _snapshot(self, name, loader):
        """读快照缓存（0.8.2 性能）：按库级版号失效，版本内多次
        fetch 共享同一解析结果——recall_context 单次查询曾重复全表解析
        nodes×7 / edges×10。返回对象跨调用共享，调用方不得原地修改
        （已知的降置信改写走 copy-on-write，见 memory.py 证据校验段）。
        """
        v = self._db_version()
        if self._snap_key != v:
            self._snap = {}
            self._snap_key = v
        if name not in self._snap:
            self._snap[name] = loader()
        return self._snap[name]

    # ---------------- 读写 ----------------
    def _migrate_legacy(self):
        """0.5.0 兼容迁移：旧库缺列补列（幂等，重复执行无副作用）。"""
        for table, col, ddl in _MIGRATIONS:
            try:
                cols = {r[1] for r in self._conn.execute(
                    f"PRAGMA table_info({table})")}
            except Exception:  # noqa: BLE001 表不存在时跳过
                continue
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

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
                # 库级版号随写事务提交自增（同事务原子生效），供读快照/
                # 邻接缓存跨实例失效
                v = self._conn.execute("PRAGMA user_version").fetchone()[0]
                self._conn.execute(f"PRAGMA user_version = {v + 1}")
                self._conn.commit()
                self._data_version += 1
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

    @contextmanager
    def savepoint(self, conn=None):
        """局部失败隔离：块内异常只回滚到保存点，不污染外层事务。

        `conn` 为 None 时退化为独立事务（失败只回滚自身）。存在的意义是
        PostgreSQL：事务内任一句报错都会把整个事务置为 aborted，之后连
        COMMIT 都会静默变成 ROLLBACK——`_rule_triggers_*` 这类「允许失败」
        的增强写入必须用保存点兜住，否则一次触发路径失败会吞掉整批记忆。
        """
        if conn is None:
            with self.transaction() as c:
                yield c
            return
        conn.execute("SAVEPOINT dnamemory_sp")
        try:
            yield conn
            conn.execute("RELEASE SAVEPOINT dnamemory_sp")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT dnamemory_sp")
            raise

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
                    evidence_ids=None, idempotency_key=None, conn=None):
        if idempotency_key:
            rows = self.read(
                "SELECT id FROM nodes WHERE idempotency_key=?", (idempotency_key,))
            if rows:
                return rows[0][0]
        params = (node_type, kind, name, description,
                  ts.isoformat() if ts else None, value_score,
                  int(protected), access_label, life, decay_rate,
                  created_at.isoformat(), json.dumps(evidence_ids or []),
                  idempotency_key)
        if conn is not None:
            return conn.execute(
                "INSERT INTO nodes(node_type,kind,name,description,ts,"
                "value_score,protected,access_label,life,decay_rate,"
                "created_at,evidence_ids,idempotency_key)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                params).lastrowid
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO nodes(node_type,kind,name,description,ts,"
                "value_score,protected,access_label,life,decay_rate,"
                "created_at,evidence_ids,idempotency_key)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    recorded_at, invalid_at=None, evidence_ids=None,
                    explicit_confirmation=False, idempotency_key=None,
                    conn=None):
        if idempotency_key:
            rows = self.read(
                "SELECT id FROM facts WHERE idempotency_key=?", (idempotency_key,))
            if rows:
                return rows[0][0]
        params = (node_id, key, value, source, confidence,
                  valid_at.isoformat(), recorded_at.isoformat(),
                  invalid_at.isoformat() if invalid_at else None,
                  json.dumps(evidence_ids or []),
                  int(explicit_confirmation), idempotency_key)
        if conn is not None:
            return conn.execute(
                "INSERT INTO facts(node_id,fact_key,fact_value,source,confidence,"
                "valid_at,recorded_at,invalid_at,evidence_ids,"
                "explicit_confirmation,idempotency_key)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)", params).lastrowid
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO facts(node_id,fact_key,fact_value,source,confidence,"
                "valid_at,recorded_at,invalid_at,evidence_ids,"
                "explicit_confirmation,idempotency_key)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)", params)
            return cur.lastrowid

    def insert_belief(self, subject_id, proposition, polarity, confidence,
                      source, valid_at, invalid_at, superseded_by, lifecycle,
                      access_label, evidence_ids, created_at,
                      idempotency_key=None, conn=None):
        if polarity not in BELIEF_POLARITIES:
            raise ValidationError(f"E001 非法 polarity: {polarity}")
        if idempotency_key:
            rows = self.read(
                "SELECT id FROM beliefs WHERE idempotency_key=?",
                (idempotency_key,))
            if rows:
                return rows[0][0]
        params = (subject_id, proposition, polarity, confidence, source,
                  valid_at.isoformat() if valid_at else None,
                  invalid_at.isoformat() if invalid_at else None,
                  superseded_by, lifecycle, access_label,
                  json.dumps(evidence_ids or []),
                  created_at.isoformat(), idempotency_key)
        sql = ("INSERT INTO beliefs(subject_id,proposition,polarity,"
               "confidence,source,valid_at,invalid_at,superseded_by,"
               "lifecycle,access_label,evidence_ids,created_at,"
               "idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)")
        audit = ("INSERT INTO audit_log(op,target_type,target_id,at)"
                 " VALUES('write_belief','belief',?,?)")
        if conn is not None:
            cur = conn.execute(sql, params)
            conn.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid
        with self.transaction() as c:
            cur = c.execute(sql, params)
            c.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid

    def insert_intent(self, subject_id, proposition, status, confidence,
                      source, valid_at, invalid_at, lifecycle, access_label,
                      evidence_ids, created_at, idempotency_key=None,
                      conn=None):
        if status not in INTENT_STATUSES:
            raise ValidationError(f"E001 非法 status: {status}")
        if idempotency_key:
            rows = self.read(
                "SELECT id FROM intents WHERE idempotency_key=?",
                (idempotency_key,))
            if rows:
                return rows[0][0]
        params = (subject_id, proposition, status, confidence, source,
                  valid_at.isoformat() if valid_at else None,
                  invalid_at.isoformat() if invalid_at else None,
                  lifecycle, access_label, json.dumps(evidence_ids or []),
                  created_at.isoformat(), idempotency_key)
        sql = ("INSERT INTO intents(subject_id,proposition,status,"
               "confidence,source,valid_at,invalid_at,lifecycle,"
               "access_label,evidence_ids,created_at,idempotency_key)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)")
        audit = ("INSERT INTO audit_log(op,target_type,target_id,at)"
                 " VALUES('write_intent','intent',?,?)")
        if conn is not None:
            cur = conn.execute(sql, params)
            conn.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid
        with self.transaction() as c:
            cur = c.execute(sql, params)
            c.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid

    def insert_evidence(self, source_type, source_ref, conversation_id,
                        message_id, observed_at, content_hash, trust_level,
                        metadata, created_at, access_label="public",
                        idempotency_key=None, conn=None):
        if source_type not in EVIDENCE_SOURCE_TYPES:
            raise ValidationError(
                f"E001 非法 source_type: {source_type}")
        if idempotency_key:
            rows = self.read(
                "SELECT id FROM evidence WHERE idempotency_key=?",
                (idempotency_key,))
            if rows:
                return rows[0][0]
        params = (source_type, source_ref, conversation_id, message_id,
                  observed_at.isoformat() if observed_at else None,
                  content_hash, trust_level,
                  json.dumps(metadata or {}, ensure_ascii=False),
                  created_at.isoformat(), idempotency_key, access_label)
        sql = ("INSERT INTO evidence(source_type,source_ref,conversation_id,"
               "message_id,observed_at,content_hash,trust_level,metadata,"
               "created_at,idempotency_key,access_label)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?)")
        audit = ("INSERT INTO audit_log(op,target_type,target_id,at)"
                 " VALUES('write_evidence','evidence',?,?)")
        if conn is not None:
            cur = conn.execute(sql, params)
            conn.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid
        with self.transaction() as c:
            cur = c.execute(sql, params)
            c.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid

    def insert_memory_link(self, source_id, target_id, source_type, relation,
                           confidence, valid_at, invalid_at,
                           idempotency_key=None, conn=None, source_dim=""):
        if relation not in MEMORY_LINK_RELATIONS:
            raise ValidationError(f"E001 非法 relation: {relation}")
        if idempotency_key:
            rows = self.read(
                "SELECT id FROM memory_links WHERE idempotency_key=?",
                (idempotency_key,))
            if rows:
                return rows[0][0]
        params = (source_id, target_id, source_type, relation, confidence,
                  valid_at.isoformat() if valid_at else None,
                  invalid_at.isoformat() if invalid_at else None,
                  idempotency_key, source_dim)
        sql = ("INSERT INTO memory_links(source_id,target_id,source_type,"
               "relation,confidence,valid_at,invalid_at,idempotency_key,"
               "source_dim)"
               " VALUES(?,?,?,?,?,?,?,?,?)")
        audit = ("INSERT INTO audit_log(op,target_type,target_id,at)"
                 " VALUES('write_memory_link','memory_link',?,?)")
        if conn is not None:
            cur = conn.execute(sql, params)
            conn.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid
        with self.transaction() as c:
            cur = c.execute(sql, params)
            c.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid

    def insert_impact(self, subject_id, dimension, direction, valence,
                      magnitude, kind="objective", evaluator="agent",
                      description="", cause_event_id=None, source="chat",
                      confidence=0.7, valid_at=None, invalid_at=None,
                      superseded_by=None, lifecycle="active",
                      access_label="public", evidence_ids=None,
                      created_at=None, idempotency_key=None, conn=None):
        """0.7.0 P1：impact 行写入（仿 insert_evidence 模板：枚举校验
        → 幂等键查重 → INSERT → audit_log → conn 双路径）。"""
        if direction not in IMPACT_DIRECTIONS:
            raise ValidationError(f"E001 非法 direction: {direction}")
        if valence not in IMPACT_VALENCES:
            raise ValidationError(f"E001 非法 valence: {valence}")
        if kind not in IMPACT_KINDS:
            raise ValidationError(f"E001 非法 kind: {kind}")
        if not (0.0 <= magnitude <= 1.0):
            raise ValidationError("E002 magnitude 必须在 [0,1]")
        if idempotency_key:
            rows = self.read(
                "SELECT id FROM impacts WHERE idempotency_key=?",
                (idempotency_key,))
            if rows:
                return rows[0][0]
        created_at = created_at or datetime.now()
        params = (subject_id, dimension, direction, valence, magnitude,
                  kind, evaluator, description, cause_event_id, source,
                  confidence,
                  valid_at.isoformat() if valid_at else None,
                  invalid_at.isoformat() if invalid_at else None,
                  superseded_by, lifecycle, access_label,
                  json.dumps(evidence_ids or [], ensure_ascii=False),
                  created_at.isoformat(), idempotency_key)
        sql = ("INSERT INTO impacts(subject_id,dimension,direction,valence,"
               "magnitude,kind,evaluator,description,cause_event_id,source,"
               "confidence,valid_at,invalid_at,superseded_by,lifecycle,"
               "access_label,evidence_ids,created_at,idempotency_key)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        audit = ("INSERT INTO audit_log(op,target_type,target_id,at)"
                 " VALUES('write_impact','impact',?,?)")
        if conn is not None:
            cur = conn.execute(sql, params)
            conn.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid
        with self.transaction() as c:
            cur = c.execute(sql, params)
            c.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid

    def insert_pattern(self, subject_id, pattern_type, proposition,
                       confidence=0.7, support=1, window_days=None,
                       source="inferred", evidence_ids=None, valid_at=None,
                       created_at=None, idempotency_key=None, conn=None):
        """0.7.0 P3：个人模式行写入。source 强制 inferred（Pattern
        是总结/推断，不得伪装原始事实）；幂等键按 proposition 哈希。"""
        import hashlib
        if pattern_type not in PATTERN_TYPES:
            raise ValidationError(f"E001 非法 pattern_type: {pattern_type}")
        if source != "inferred":
            source = "inferred"
        if idempotency_key is None:
            h = hashlib.sha1(proposition.encode("utf-8")).hexdigest()[:12]
            idempotency_key = f"pat:{pattern_type}:{subject_id}:{h}"
        rows = self.read(
            "SELECT id FROM patterns WHERE idempotency_key=?",
            (idempotency_key,))
        if rows:
            return rows[0][0]
        created_at = created_at or datetime.now()
        params = (pattern_type, subject_id, proposition, confidence,
                  support, window_days, source,
                  json.dumps(evidence_ids or [], ensure_ascii=False),
                  valid_at.isoformat() if valid_at else None,
                  created_at.isoformat(), idempotency_key)
        sql = ("INSERT INTO patterns(pattern_type,subject_id,proposition,"
               "confidence,support,window_days,source,evidence_ids,valid_at,"
               "created_at,idempotency_key)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?)")
        audit = ("INSERT INTO audit_log(op,target_type,target_id,at)"
                 " VALUES('write_pattern','pattern',?,?)")
        if conn is not None:
            cur = conn.execute(sql, params)
            conn.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid
        with self.transaction() as c:
            cur = c.execute(sql, params)
            c.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid

    def insert_trigger(self, memory_id, memory_type, trigger_type, text,
                       source="rule", confidence=0.7, created_at=None,
                       idempotency_key=None, conn=None):
        """0.7.0 P2：触达路径写入（仿 insert_memory_link 模板）。"""
        if trigger_type not in TRIGGER_TYPES:
            raise ValidationError(f"E001 非法 trigger_type: {trigger_type}")
        if memory_type not in ("event", "fact", "belief", "intent",
                               "impact"):
            raise ValidationError(f"E001 非法 memory_type: {memory_type}")
        if idempotency_key:
            rows = self.read(
                "SELECT id FROM triggers WHERE idempotency_key=?",
                (idempotency_key,))
            if rows:
                return rows[0][0]
        created_at = created_at or datetime.now()
        params = (memory_id, memory_type, trigger_type, text, source,
                  confidence, created_at.isoformat(), idempotency_key)
        sql = ("INSERT INTO triggers(memory_id,memory_type,trigger_type,"
               "text,source,confidence,created_at,idempotency_key)"
               " VALUES(?,?,?,?,?,?,?,?)")
        audit = ("INSERT INTO audit_log(op,target_type,target_id,at)"
                 " VALUES('write_trigger','trigger',?,?)")
        if conn is not None:
            cur = conn.execute(sql, params)
            conn.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
            return cur.lastrowid
        with self.transaction() as c:
            cur = c.execute(sql, params)
            c.execute(audit, (cur.lastrowid, datetime.now().isoformat()))
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
        self._sync_vec_after_write(node_id, dense, model,
                                   dim or len(dense))

    # ---------------- 向量索引（sqlite-vec，纯加速层） ----------------

    @staticmethod
    def _norm_vec(dense):
        import numpy
        v = numpy.asarray([float(x) for x in dense], dtype=float)
        norm = numpy.linalg.norm(v)
        if norm == 0:
            return None
        return (v / norm).tolist()

    def _vec_table(self, model, dim):
        slug = re.sub(r"[^0-9A-Za-z]+", "_", model)[:60]
        return f"node_vectors_ann_{slug}_{int(dim)}"

    def _vec_meta(self, model, dim):
        rows = self.read(
            "SELECT model, dim FROM vector_index_meta WHERE model=?",
            (model,))
        return bool(rows) and rows[0][1] == int(dim)

    def rebuild_vector_index(self, model, dim):
        """全量重建 ANN 投影；模型/维度变更或索引异常时调用。"""
        if self._vec is None or not model:
            return False
        dim = int(dim)
        table = self._vec_table(model, dim)
        try:
            with self.transaction() as conn:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
                conn.execute(
                    f"CREATE VIRTUAL TABLE {table} USING vec0("
                    f"node_id INTEGER PRIMARY KEY, dense FLOAT[{dim}] "
                    f"distance_metric=Cosine)")
                for nid, dense, _sparse, m, d, _at in \
                        self.fetch_node_vectors():
                    if m != model or d != dim:
                        continue
                    vec = self._norm_vec(dense)
                    if vec is None:
                        continue
                    conn.execute(
                        f"INSERT INTO {table}(node_id, dense) VALUES(?,?)",
                        (nid, json.dumps(vec)))
                conn.execute(
                    "INSERT INTO vector_index_meta(model,dim,updated_at) "
                    "VALUES(?,?,?) ON CONFLICT(model) DO UPDATE SET "
                    "dim=excluded.dim, updated_at=excluded.updated_at",
                    (model, dim, datetime.now().isoformat()))
            return True
        except Exception:  # noqa: BLE001 索引失败不阻断主流程
            return False

    def _sync_vec_after_write(self, node_id, dense, model, dim):
        if self._vec is None or not model:
            return
        dim = int(dim)
        try:
            if not self._vec_meta(model, dim):
                self.rebuild_vector_index(model, dim)
                return
            vec = self._norm_vec(dense)
            if vec is None:
                return
            table = self._vec_table(model, dim)
            with self.transaction() as c:
                c.execute(f"DELETE FROM {table} WHERE node_id=?",
                          (node_id,))
                c.execute(f"INSERT INTO {table}(node_id, dense) "
                          "VALUES(?,?)", (node_id, json.dumps(vec)))
        except Exception:  # noqa: BLE001 增量失败则整体重建
            self.rebuild_vector_index(model, dim)

    def search_vectors(self, query_vec, model, dim, k=None):
        """ANN 余弦检索；索引不可用时返回 None（调用方必须回退 numpy）。"""
        if self._vec is None or not model:
            return None
        dim = int(dim)
        if not self._vec_meta(model, dim):
            return None
        vec = self._norm_vec(query_vec)
        if vec is None:
            return None
        table = self._vec_table(model, dim)
        limit = int(k) if k is not None else 2 ** 31 - 1
        try:
            with self._wlock:
                rows = self._conn.execute(
                    f"SELECT node_id, distance FROM {table} "
                    "WHERE dense MATCH ? AND k = "
                    f"{limit}", (json.dumps(vec),)).fetchall()
            return [(r[0], float(r[1])) for r in rows]
        except Exception:  # noqa: BLE001 查询失败回退 numpy
            return None

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

    def add_tombstone(self, target_type, target_id, reason, at, conn=None):
        sql = ("INSERT INTO tombstones(target_type,target_id,reason,"
               "tombstoned_at) VALUES(?,?,?,?)",
               (target_type, target_id, reason, at.isoformat()))
        audit = ("INSERT INTO audit_log(op,target_type,target_id,reason,at)"
                 " VALUES('tombstone',?,?,?,?)",
                 (target_type, target_id, reason, at.isoformat()))
        if conn is not None:
            conn.execute(sql[0], sql[1])
            conn.execute(audit[0], audit[1])
            return
        with self.transaction() as c:
            c.execute(sql[0], sql[1])
            c.execute(audit[0], audit[1])

    # ---------------- 更新 ----------------
    def update_lifecycle(self, node_id, lifecycle, at=None, conn=None):
        rows = self.read("SELECT lifecycle FROM nodes WHERE id=?", (node_id,))
        if not rows:
            raise StorageError("E006 目标不存在")
        current = rows[0][0]
        if (current, lifecycle) not in LEGAL_LIFECYCLE:
            raise ValidationError(
                f"E001 非法生命周期迁移: {current} -> {lifecycle}")
        if current == lifecycle:
            return
        at = at or datetime.now()
        update = ("UPDATE nodes SET lifecycle=? WHERE id=?",
                  (lifecycle, node_id))
        audit = ("INSERT INTO audit_log(op,target_type,target_id,reason,at)"
                 " VALUES('lifecycle','node',?,?,?)",
                 (node_id, lifecycle, at.isoformat()))
        if conn is not None:
            conn.execute(update[0], update[1])
            conn.execute(audit[0], audit[1])
            return
        with self.transaction() as c:
            c.execute(update[0], update[1])
            c.execute(audit[0], audit[1])

    def update_edge_lifecycle(self, edge_id, lifecycle, at=None, conn=None):
        at = at or datetime.now()
        update = ("UPDATE edges SET lifecycle=? WHERE id=?",
                  (lifecycle, edge_id))
        audit = ("INSERT INTO audit_log(op,target_type,target_id,reason,at)"
                 " VALUES('edge_lifecycle','edge',?,?,?)",
                 (edge_id, lifecycle, at.isoformat()))
        if conn is not None:
            conn.execute(update[0], update[1])
            conn.execute(audit[0], audit[1])
            return
        with self.transaction() as c:
            c.execute(update[0], update[1])
            c.execute(audit[0], audit[1])

    def rewire_edge(self, edge_id, old_nid, new_nid, conn=None):
        """实体合并：把边端点从 old_nid 重挂到 new_nid（同事务用 conn）。"""
        update = (
            "UPDATE edges SET from_id = CASE WHEN from_id=? THEN ? "
            "ELSE from_id END, to_id = CASE WHEN to_id=? THEN ? "
            "ELSE to_id END WHERE id=?",
            (old_nid, new_nid, old_nid, new_nid, edge_id))
        audit = (
            "INSERT INTO audit_log(op,target_type,target_id,reason,meta,at)"
            " VALUES('entity_merge','edge',?,?,?,?)",
            (edge_id, "rewire",
             json.dumps({"old": old_nid, "new": new_nid}, ensure_ascii=False),
             datetime.now().isoformat()))
        if conn is not None:
            conn.execute(update[0], update[1])
            conn.execute(audit[0], audit[1])
            return
        with self.transaction() as c:
            c.execute(update[0], update[1])
            c.execute(audit[0], audit[1])

    def rewire_fact(self, fid, old_nid, new_nid, conn=None):
        """实体合并：把 fact 从 old_nid 重挂到 new_nid（同事务用 conn）。"""
        update = ("UPDATE facts SET node_id=? WHERE id=? AND node_id=?",
                  (new_nid, fid, old_nid))
        audit = (
            "INSERT INTO audit_log(op,target_type,target_id,reason,meta,at)"
            " VALUES('entity_merge','fact',?,?,?,?)",
            (fid, "rewire",
             json.dumps({"old": old_nid, "new": new_nid}, ensure_ascii=False),
             datetime.now().isoformat()))
        if conn is not None:
            conn.execute(update[0], update[1])
            conn.execute(audit[0], audit[1])
            return
        with self.transaction() as c:
            c.execute(update[0], update[1])
            c.execute(audit[0], audit[1])

    def update_life_decay(self, node_id, life, last_access):
        with self.transaction() as conn:
            conn.execute("UPDATE nodes SET life=?, last_access=? WHERE id=?",
                         (life, last_access.isoformat(), node_id))

    def update_life(self, node_id, life):
        with self.transaction() as conn:
            conn.execute("UPDATE nodes SET life=? WHERE id=?", (life, node_id))

    def supersede_fact(self, fid, by, at, reason=None, conn=None):
        """事实替代（0.8.1 IA-4 起写审计）：UPDATE 与 audit_log 同事务。

        reason：裁决依据简述；调用方（治理/人工确认）能提供就透传，
        否则落最小描述 ``superseded_by={winner_fid}``。
        """
        update = ("UPDATE facts SET invalid_at=?, superseded_by=? WHERE id=?",
                  (at.isoformat(), by, fid))
        audit = ("INSERT INTO audit_log(op,target_type,target_id,reason,meta,"
                 "at) VALUES('supersede_fact','fact',?,?,?,?)",
                 (fid, reason or f"superseded_by={by}",
                  json.dumps({"superseded_by": by}, ensure_ascii=False),
                  at.isoformat()))
        if conn is not None:
            conn.execute(update[0], update[1])
            conn.execute(audit[0], audit[1])
            return
        with self.transaction() as c:
            c.execute(update[0], update[1])
            c.execute(audit[0], audit[1])

    def tombstone_fact(self, fid, at, conn=None):
        if conn is not None:
            conn.execute("UPDATE facts SET tombstoned=1 WHERE id=?", (fid,))
            conn.execute("INSERT INTO audit_log(op,target_type,target_id,"
                         "reason,at) VALUES('tombstone','fact',?,?,?)",
                         (fid, "retracted", at.isoformat()))
            return
        with self.transaction() as c:
            c.execute("UPDATE facts SET tombstoned=1 WHERE id=?", (fid,))
            c.execute("INSERT INTO audit_log(op,target_type,target_id,"
                      "reason,at) VALUES('tombstone','fact',?,?,?)",
                      (fid, "retracted", at.isoformat()))

    def rewire_subject_rows(self, from_nid, to_nid, conn=None):
        """实体合并：把状态行（belief/intent/impact/pattern）的主体改挂到
        规范实体上。原实现只重挂 edges/facts，状态行仍指向被墓碑的 dup
        → 观点/意图/影响/模式变成指向墓碑节点的孤儿。返回 {表名: 行数}。"""
        counts = {}

        def _run(c):
            counts.clear()
            for table in ("beliefs", "intents", "impacts", "patterns"):
                counts[table] = c.execute(
                    f"UPDATE {table} SET subject_id=? WHERE subject_id=?",
                    (to_nid, from_nid)).rowcount

        if conn is not None:
            _run(conn)
        else:
            with self.transaction() as c:
                _run(c)
        return counts

    # ---------------- 合规删除级联 ----------------

    def cascade_forget(self, node_id, at, evidence_ids=(), conn=None):
        """`forget(force=True)` 的级联：主体名下的派生数据一并处理。

        - beliefs / intents / impacts → `lifecycle='tombstoned'`
        - patterns / versions / node_vectors → 删除（纯派生数据）
        - triggers / memory_links → 删除与该主体对象相关的行
        - evidence（调用方传入的 ids）→ `access_label='sensitive'`
          （默认过滤器隐藏；"物理数据保留"策略下不做物理删除）

        返回 {表名: 影响行数}。原实现只做 node + facts + active edges，
        于是主体的观点/意图/影响/模式/证据仍可被 explain/queries 读到，
        与对外宣称的「合规删除」不符。
        """
        counts = {}

        def _run(c):
            counts.clear()
            own = [r[0] for r in c.execute(
                "SELECT id FROM facts WHERE node_id=?", (node_id,))]
            own += [r[0] for r in c.execute(
                "SELECT id FROM beliefs WHERE subject_id=?", (node_id,))]
            own += [r[0] for r in c.execute(
                "SELECT id FROM intents WHERE subject_id=?", (node_id,))]
            own += [r[0] for r in c.execute(
                "SELECT id FROM impacts WHERE subject_id=?", (node_id,))]
            for table in ("beliefs", "intents", "impacts"):
                counts[table] = c.execute(
                    f"UPDATE {table} SET lifecycle='tombstoned' "
                    "WHERE subject_id=? AND lifecycle<>'tombstoned'",
                    (node_id,)).rowcount
            for table, col in (("patterns", "subject_id"),
                               ("versions", "node_id"),
                               ("node_vectors", "node_id")):
                counts[table] = c.execute(
                    f"DELETE FROM {table} WHERE {col}=?", (node_id,)).rowcount
            ids = tuple([node_id] + own)
            if ids:
                ph = ",".join("?" * len(ids))
                counts["triggers"] = c.execute(
                    f"DELETE FROM triggers WHERE memory_id IN ({ph})",
                    ids).rowcount
                counts["memory_links"] = c.execute(
                    f"DELETE FROM memory_links WHERE source_id IN ({ph}) "
                    f"OR target_id IN ({ph})", ids + ids).rowcount
            if evidence_ids:
                eph = ",".join("?" * len(evidence_ids))
                counts["evidence"] = c.execute(
                    "UPDATE evidence SET access_label='sensitive' "
                    f"WHERE id IN ({eph}) AND access_label<>'sensitive'",
                    tuple(evidence_ids)).rowcount
            # 审计写在**同一连接**里：另起事务会与外层事务冲突
            c.execute("INSERT INTO audit_log(op,target_type,target_id,"
                      "reason,at) VALUES('cascade_forget','node',?,?,?)",
                      (node_id, f"tables={sorted(counts)}", at.isoformat()))

        if conn is not None:
            _run(conn)
        else:
            with self.transaction() as c:
                _run(c)
        return counts

    # ---------------- 读取 ----------------
    def fetch_nodes(self):
        def _load():
            rows = self.read("SELECT * FROM nodes")
            return [Node(r[0], r[1], r[2], r[3], r[4], _dt(r[5]), r[6],
                         bool(r[7]), r[8], r[9], r[10], r[11], _dt(r[12]),
                         _dt(r[13]),
                         json.loads(r[15] or "[]") if len(r) > 15 else [],
                         r[16] or "" if len(r) > 16 else "")
                    for r in rows]
        return self._snapshot("nodes", _load)

    def fetch_edges(self):
        def _load():
            rows = self.read("SELECT * FROM edges")
            return [Edge(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]),
                         _dt(r[7]), _dt(r[8]), r[10], r[11])
                    for r in rows]
        return self._snapshot("edges", _load)

    def adjacency(self):
        """惰性无向邻接表：随库级版号自动重建，O(E) 只发生一次。

        invalid_at 过滤下沉到查询时：边元组末尾携带 invalid_at，
        由调用方按当前时间过滤，保证缓存跨查询真正命中。
        """
        v = self._db_version()
        if self._adj_key != v:
            adj = {}
            for e in self.fetch_edges():
                if e.lifecycle != "active":
                    continue
                adj.setdefault(e.from_id, []).append(
                    (e.to_id, e.weight, e.confidence, e.rel, e.invalid_at))
                adj.setdefault(e.to_id, []).append(
                    (e.from_id, e.weight, e.confidence, e.rel, e.invalid_at))
            self._adj_cache = adj
            self._adj_key = v
        return self._adj_cache

    def fetch_facts(self):
        def _load():
            rows = self.read("SELECT * FROM facts")
            return [Fact(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]),
                         _dt(r[7]), _dt(r[8]), r[9], bool(r[10]),
                         json.loads(r[12] or "[]") if len(r) > 12 else [],
                         bool(r[13]) if len(r) > 13 else False)
                    for r in rows]
        return self._snapshot("facts", _load)

    def fetch_beliefs(self):
        def _load():
            rows = self.read("SELECT * FROM beliefs")
            return [Belief(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]),
                           _dt(r[7]), r[8], r[9], r[10],
                           json.loads(r[11] or "[]"), _dt(r[12]))
                    for r in rows]
        return self._snapshot("beliefs", _load)

    def fetch_intents(self):
        def _load():
            rows = self.read("SELECT * FROM intents")
            return [Intent(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]),
                           _dt(r[7]), r[8], r[9], json.loads(r[10] or "[]"),
                           _dt(r[11]))
                    for r in rows]
        return self._snapshot("intents", _load)

    def fetch_evidence(self):
        def _load():
            rows = self.read("SELECT * FROM evidence")
            return [Evidence(r[0], r[1], r[2], r[3], r[4], _dt(r[5]), r[6],
                             r[7], json.loads(r[8] or "{}"), _dt(r[9]),
                             r[11] or "public" if len(r) > 11 else "public")
                    for r in rows]
        return self._snapshot("evidence", _load)

    def get_evidence(self, ids):
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.read(f"SELECT * FROM evidence WHERE id IN ({marks})",
                         tuple(ids))
        return [Evidence(r[0], r[1], r[2], r[3], r[4], _dt(r[5]), r[6],
                         r[7], json.loads(r[8] or "{}"), _dt(r[9]),
                         r[11] or "public" if len(r) > 11 else "public")
                for r in rows]

    def fetch_impacts(self):
        def _load():
            rows = self.read("SELECT * FROM impacts")
            return [Impact(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                           r[8], r[9], r[10], r[11], _dt(r[12]), _dt(r[13]),
                           r[14], r[15], r[16],
                           json.loads(r[17] or "[]"), _dt(r[18]))
                    for r in rows]
        return self._snapshot("impacts", _load)

    def fetch_triggers(self):
        def _load():
            rows = self.read("SELECT * FROM triggers")
            return [Trigger(r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                            _dt(r[7]))
                    for r in rows]
        return self._snapshot("triggers", _load)

    def fetch_patterns(self):
        def _load():
            rows = self.read("SELECT * FROM patterns")
            return [Pattern(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                            json.loads(r[8] or "[]"), _dt(r[9]), _dt(r[10]))
                    for r in rows]
        return self._snapshot("patterns", _load)

    def fetch_memory_links(self):
        def _load():
            rows = self.read("SELECT * FROM memory_links")
            return [MemoryLink(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]),
                               _dt(r[7]), r[9] if len(r) > 9 else "")
                    for r in rows]
        return self._snapshot("memory_links", _load)

    def memory_links_of(self, memory_id):
        rows = self.read(
            "SELECT * FROM memory_links WHERE source_id=? OR target_id=?",
            (memory_id, memory_id))
        return [MemoryLink(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]),
                           _dt(r[7]), r[9] if len(r) > 9 else "")
                for r in rows]

    def fetch_versions(self, node_id):
        return self.read("SELECT version, content, created_at FROM versions"
                         " WHERE node_id=? ORDER BY version", (node_id,))

    def fetch_audit_logs(self, target_type=None, target_id=None, op=None,
                         limit=50):
        """审计日志只读查询（0.8.1 IA-4）：按对象/操作过滤，id 倒序
        （最新在前）。返回 dict 列表（meta 已按 JSON 解析，解析失败留
        原始字符串），供 explain() 与 GET /audit 共用。"""
        sql = ("SELECT id,op,target_type,target_id,reason,meta,at"
               " FROM audit_log")
        conds, params = [], []
        if target_type is not None:
            conds.append("target_type=?")
            params.append(target_type)
        if target_id is not None:
            conds.append("target_id=?")
            params.append(target_id)
        if op is not None:
            conds.append("op=?")
            params.append(op)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self.read(sql, tuple(params))
        out = []
        for r in rows:
            meta = r[5]
            if meta:
                try:
                    meta = json.loads(meta)
                except (TypeError, ValueError):
                    pass  # 非 JSON 的历史行原样返回
            out.append({"id": r[0], "op": r[1], "target_type": r[2],
                        "target_id": r[3], "reason": r[4], "meta": meta,
                        "at": r[6]})
        return out

    def fetch_tombstones(self):
        def _load():
            rows = self.read("SELECT * FROM tombstones")
            return [Tombstone(r[0], r[1], r[2], r[3], _dt(r[4]))
                    for r in rows]
        return self._snapshot("tombstones", _load)

    def close(self):
        with self._wlock:
            self._conn.close()
