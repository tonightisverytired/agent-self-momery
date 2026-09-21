# -*- coding: utf-8 -*-
"""PostgreSQL 存储层（存储后端 P4）。

与 ``store.SQLiteStore`` **同签名同语义**的可选实现：默认路径仍是 SQLite，
只有显式提供 DSN 时才构造本类（见 ``storage.build_store``）。

设计要点：

- **显式列名**：``SQLiteStore`` 用 ``SELECT *`` + 位置下标构造 model；本类改为
  显式列名（顺序与 SQLite DDL 一致），因此不存在「两份列序契约」——列名写错会直接
  报错，而不是静默错位。
- **占位符适配**：项目里全部 SQL 用 ``?``；``_q()`` 统一替换成 psycopg3 的 ``%s``
  （带缓存）。因此 ``read()`` / ``transaction()`` 能直接支撑散落在 retrieval /
  temporal / governance / memory 中的既有裸 SQL，无需改动那些调用点。
- **事务**：单连接 ``autocommit=True`` + 进程内 ``RLock`` + 显式 ``BEGIN`` +
  ``pg_advisory_xact_lock``。前者等价于 SQLite 的单连接串行语义，advisory lock 让
  跨进程（api / mcp / cli 同连一库）的写也串行化——这是 ``BEGIN IMMEDIATE`` 的对应物。
- **幂等**：``INSERT ... ON CONFLICT(idempotency_key) DO NOTHING RETURNING id``
  + 同事务回查，消除 read-then-insert 在并发下的唯一键冲突。
- **时间**：仍是 TEXT ISO-8601（与 SQLite 逐字段一致，``_dt()`` 与时间窗字典序
  比较全部复用）。
- **向量**：pgvector。``dense`` 为 ``vector(1024)`` 的 ANN 列（仅 ``VECTOR_DIM``
  维度投影），``dense_json`` 保留原始 JSON 供 numpy 降级与逐字节对拍。

约束：本模块的 SQL 常量里**禁止出现字面量 ``%`` 与字面量 ``?``**——
``_q()`` 只做 ``?`` → ``%s`` 替换，字面量 ``%`` 需写成 ``%%``。
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from .errors import StorageError, ValidationError
from .models import (BELIEF_POLARITIES, EVIDENCE_SOURCE_TYPES,
                     IMPACT_DIRECTIONS, IMPACT_KINDS, IMPACT_VALENCES,
                     INTENT_STATUSES, MEMORY_LINK_RELATIONS, PATTERN_TYPES,
                     TRIGGER_TYPES, Belief, Edge, Evidence, Fact, Impact,
                     Intent, MemoryLink, Node, Pattern, Tombstone, Trigger,
                     to_naive)
from .pg_schema import DDL_STATEMENTS, VECTOR_DIM

LEGAL_LIFECYCLE = frozenset({
    ("active", "active"), ("active", "archived"), ("active", "tombstoned"),
    ("active", "deleted"),
    ("archived", "archived"), ("archived", "active"),
    ("archived", "tombstoned"), ("archived", "deleted"),
    ("tombstoned", "tombstoned"), ("tombstoned", "active"),
    ("tombstoned", "deleted"),
    ("deleted", "deleted"),
})

# 全局写锁 key（advisory lock）：同一库的所有写事务串行化
ADVISORY_LOCK_KEY = 918273645

# 邻接缓存 TTL：advisory/版本号已能精确失效，TTL 只兜底「有人绕过本类直接改库」
ADJ_CACHE_TTL_S = 2.0

# 每张表的显式列名（顺序与 SQLite DDL 一致，供 SELECT 与位置下标构造 model）
COLS = {
    "nodes": ("id", "node_type", "kind", "name", "description", "ts",
              "value_score", "protected", "access_label", "lifecycle",
              "life", "decay_rate", "last_access", "created_at",
              "idempotency_key", "evidence_ids", "source"),
    "edges": ("id", "from_id", "to_id", "relation_type", "weight",
              "confidence", "valid_at", "invalid_at", "created_at",
              "last_access", "access_count", "lifecycle", "idempotency_key"),
    "facts": ("id", "node_id", "fact_key", "fact_value", "source",
              "confidence", "valid_at", "recorded_at", "invalid_at",
              "superseded_by", "tombstoned", "idempotency_key",
              "evidence_ids", "explicit_confirmation"),
    "beliefs": ("id", "subject_id", "proposition", "polarity", "confidence",
                "source", "valid_at", "invalid_at", "superseded_by",
                "lifecycle", "access_label", "evidence_ids", "created_at",
                "idempotency_key"),
    "intents": ("id", "subject_id", "proposition", "status", "confidence",
                "source", "valid_at", "invalid_at", "lifecycle",
                "access_label", "evidence_ids", "created_at",
                "idempotency_key"),
    "evidence": ("id", "source_type", "source_ref", "conversation_id",
                 "message_id", "observed_at", "content_hash", "trust_level",
                 "metadata", "created_at", "idempotency_key", "access_label"),
    "memory_links": ("id", "source_id", "target_id", "source_type",
                     "relation", "confidence", "valid_at", "invalid_at",
                     "idempotency_key", "source_dim"),
    "impacts": ("id", "subject_id", "dimension", "direction", "valence",
                "magnitude", "kind", "evaluator", "description",
                "cause_event_id", "source", "confidence", "valid_at",
                "invalid_at", "superseded_by", "lifecycle", "access_label",
                "evidence_ids", "created_at", "idempotency_key"),
    "triggers": ("id", "memory_id", "memory_type", "trigger_type", "text",
                 "source", "confidence", "created_at", "idempotency_key"),
    "patterns": ("id", "pattern_type", "subject_id", "proposition",
                 "confidence", "support", "window_days", "source",
                 "evidence_ids", "valid_at", "created_at", "idempotency_key"),
    "tombstones": ("id", "target_type", "target_id", "reason",
                   "tombstoned_at"),
    "versions": ("id", "node_id", "version", "content", "created_at"),
    "audit_log": ("id", "op", "target_type", "target_id", "reason", "meta",
                  "at"),
}


def _dt(v):
    return to_naive(v)


def _vec_literal(vec) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]"


class _Cursor:
    """cursor 包装：把 SQLite 风格的 ``?`` 占位符适配成 psycopg3 的 ``%s``。

    ``transaction()`` 会把本对象交给调用方（memory / governance 等仍按既有的
    ``conn.execute("... ?", params)`` 写法使用），因此这里必须支持 ``?``。
    """

    __slots__ = ("_cur", "_q")

    def __init__(self, cur, q):
        self._cur = cur
        self._q = q

    def execute(self, sql, params=()):
        self._cur.execute(self._q(sql), params)
        return self

    def executemany(self, sql, seq):
        self._cur.executemany(self._q(sql), seq)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount


class PGStore:
    """PostgreSQL 存储实现（与 SQLiteStore 同接口）。"""

    def __init__(self, dsn: str):
        try:
            import psycopg
        except ImportError as e:  # pragma: no cover
            raise StorageError(
                "E009 使用 PostgreSQL 后端需要安装 psycopg："
                "pip install \"dnamemory[pg]\"") from e
        self.dsn = dsn
        self._psycopg = psycopg
        self._wlock = threading.RLock()
        self._tx_depth = 0
        self._cache: dict[str, str] = {}
        self._cur_ef = 0
        try:
            self._conn = psycopg.connect(dsn, autocommit=True,
                                         connect_timeout=10)
        except Exception as e:  # noqa: BLE001
            raise StorageError(f"E009 无法连接 PostgreSQL: {e}") from e
        self.ensure_schema()
        self._vec = self._probe_vector()
        self._data_version = 0
        self._adj_cache = None
        self._adj_key = None
        self._adj_expiry = 0.0

    # ---------------- 连接与适配 ----------------

    def _q(self, sql: str) -> str:
        cached = self._cache.get(sql)
        if cached is None:
            cached = self._cache[sql] = sql.replace("?", "%s")
        return cached

    def _assert_schema_compatible(self):
        """若 nodes 表已存在但列不齐，说明连到了别的 schema（如时间链后端的精简库）。

        必须在执行 DDL **之前**判断：``CREATE TABLE IF NOT EXISTS`` 不会补列，
        而随后的 ``CREATE INDEX ... ON nodes(lifecycle)`` 会以 psycopg 的
        ``UndefinedColumn`` 报错——那是难以诊断的形态，这里换成明确的 E009。
        """
        rows = self._conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'nodes' AND table_schema = current_schema()"
        ).fetchall()
        present = {r[0] for r in rows}
        if not present:
            return  # 全新库：交给 DDL 建全
        missing = [c for c in COLS["nodes"] if c not in present]
        if missing:
            raise StorageError(
                f"E009 目标库的 nodes 表缺少列 {missing}；该库不是 dnamemory "
                "主存储 schema（可能连到了时间链后端的精简库）。")

    def ensure_schema(self):
        """建表（幂等）：逐条执行 DDL_STATEMENTS（psycopg3 不支持多语句一次执行）。"""
        with self._wlock:
            self._assert_schema_compatible()
            for stmt in DDL_STATEMENTS:
                self._conn.execute(stmt)

    def _probe_vector(self):
        """pgvector 可用则返回真值 sentinel，否则 None（retrieval 据此走 numpy 降级）。"""
        try:
            row = self.read("SELECT 1 FROM pg_extension WHERE extname = ?",
                            ("vector",))
            return True if row else None
        except Exception:  # noqa: BLE001
            return None

    @contextmanager
    def transaction(self):
        with self._wlock:
            if self._tx_depth:
                raise StorageError("E009 PostgreSQL 事务不可嵌套（用 conn= 下传）")
            # BEGIN 与加锁也放进保护：加锁失败（statement_timeout / 连接被
            # kill）时必须回滚，否则连接停在打开的事务里而 `_tx_depth` 仍是 0，
            # 下一次 BEGIN 只发 WARNING（psycopg 不抛），静默加入旧事务，
            # 其它进程的 advisory lock 会永久阻塞。
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("SELECT pg_advisory_xact_lock(%s)",
                                   (ADVISORY_LOCK_KEY,))
            except Exception:
                self._rollback_quiet()
                raise
            self._tx_depth = 1
            try:
                with self._conn.cursor() as raw:
                    yield _Cursor(raw, self._q)
                self._assert_not_aborted()
                self._conn.execute("COMMIT")
                self._data_version += 1
                self._adj_expiry = 0.0
            except Exception:
                self._rollback_quiet()
                raise
            finally:
                self._tx_depth = 0

    def _rollback_quiet(self):
        try:
            self._conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 已经不在事务里
            pass

    def _assert_not_aborted(self):
        """COMMIT 前识别「已中止」的事务。

        块内异常被调用方吞掉（如 `_rule_triggers_*`）时，PostgreSQL 把事务
        标记为 aborted，此时 `COMMIT` **不报错**、只返回 ROLLBACK 标签，调用
        方以为写入成功而库里一行没有。这里显式把它变成异常。
        """
        from psycopg.pq import TransactionStatus
        if self._conn.info.transaction_status == TransactionStatus.INERROR:
            raise StorageError(
                "E009 事务已中止：块内有语句失败但异常被吞掉，本次写入整体回滚")

    def read(self, sql, params=()):
        """执行 SQL 并返回全部行。

        与 SQLiteStore.read 一致：**既可用于读也可用于写**（autocommit 连接下
        写入立即生效），调用方既有写法无需改动。
        """
        with self._wlock:
            with self._conn.cursor() as cur:
                cur.execute(self._q(sql), params)
                try:
                    return cur.fetchall()
                except Exception:  # noqa: BLE001 非查询语句无结果集
                    return []

    @contextmanager
    def savepoint(self, conn=None):
        """局部失败隔离：块内异常只回滚到保存点，不污染外层事务。

        PostgreSQL 下这一步是**必需**的：事务内任一句报错都会把事务置为
        aborted，之后 COMMIT 静默变 ROLLBACK——「允许失败」的增强写入
        （如 `_rule_triggers_*`）必须用保存点兜住。
        `conn` 为 None 时退化为独立事务。
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
              meta=None, at=None, conn=None):
        sql = ("INSERT INTO audit_log(op,target_type,target_id,reason,meta,at)"
               " VALUES(?,?,?,?,?,?)")
        params = (op, target_type, target_id, reason, meta,
                  (at or datetime.now()).isoformat())
        if conn is not None:
            conn.execute(sql, params)
            return
        with self.transaction() as c:
            c.execute(sql, params)

    # ---------------- 写入（内部工具） ----------------

    def _exec_insert(self, cur, sql, params, table, idem_key):
        """执行 INSERT，返回 (id, is_new)。

        带幂等键时用 ``ON CONFLICT DO NOTHING RETURNING id``：命中冲突（并发或
        重放）则回查既有行并返回 ``is_new=False``，调用方据此跳过 audit——与
        SQLiteStore 的「幂等命中直接返回、不写审计」语义一致。
        """
        if idem_key:
            cur.execute(sql + " ON CONFLICT (idempotency_key) DO NOTHING"
                              " RETURNING id", params)
            row = cur.fetchone()
            if row:
                return int(row[0]), True
            row = cur.execute(
                f"SELECT id FROM {table} WHERE idempotency_key = ?",
                (idem_key,)).fetchone()
            return (int(row[0]) if row else None), False
        row = cur.execute(sql + " RETURNING id", params).fetchone()
        return int(row[0]), True

    def _audit_write(self, cur, op, target_type, target_id, at_iso):
        cur.execute("INSERT INTO audit_log(op,target_type,target_id,at)"
                    " VALUES(?,?,?,?)", (op, target_type, target_id, at_iso))

    def _insert_with_audit(self, conn, table, sql, params, op, target_type,
                           idem_key):
        """插入 + 审计：`conn` 下传时复用调用方事务，否则自建事务。

        回归：此前 7 个 insert_* 声明了 `conn=` 却忽略它、直接开自己的事务，
        而调用方（`memory._write_candidates` / `governance`）恰恰在事务里
        下传 `conn=` → PG 上必撞 `E009 事务不可嵌套（用 conn= 下传）`，
        整批写入回滚（SQLite 无此问题，所以只在 PG 上暴露）。
        """
        at = datetime.now().isoformat()
        if conn is not None:
            rid, is_new = self._exec_insert(conn, sql, params, table, idem_key)
            if is_new:
                self._audit_write(conn, op, target_type, rid, at)
            return rid
        with self.transaction() as c:
            rid, is_new = self._exec_insert(c, sql, params, table, idem_key)
            if is_new:
                self._audit_write(c, op, target_type, rid, at)
            return rid

    # ---------------- 写入 ----------------

    def insert_node(self, node_type, kind, name, description, ts, value_score,
                    protected, access_label, life, decay_rate, created_at,
                    evidence_ids=None, idempotency_key=None, conn=None):
        params = (node_type, kind, name, description,
                  ts.isoformat() if ts else None, value_score,
                  bool(protected), access_label, life, decay_rate,
                  created_at.isoformat(), json.dumps(evidence_ids or []),
                  idempotency_key)
        sql = ("INSERT INTO nodes(node_type,kind,name,description,ts,"
               "value_score,protected,access_label,life,decay_rate,"
               "created_at,evidence_ids,idempotency_key)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)")
        if conn is not None:
            return self._exec_insert(conn, sql, params, "nodes",
                                     idempotency_key)[0]
        with self.transaction() as c:
            nid, is_new = self._exec_insert(c, sql, params, "nodes",
                                            idempotency_key)
            if is_new:
                self._audit_write(c, "write", "node", nid,
                                  created_at.isoformat())
            return nid

    def insert_edge(self, from_id, to_id, rel, weight, confidence, valid_at,
                    invalid_at, created_at, idempotency_key=None, conn=None):
        params = (from_id, to_id, rel, weight, confidence,
                  valid_at.isoformat(),
                  invalid_at.isoformat() if invalid_at else None,
                  created_at.isoformat(), idempotency_key)
        sql = ("INSERT INTO edges(from_id,to_id,relation_type,weight,"
               "confidence,valid_at,invalid_at,created_at,idempotency_key)"
               " VALUES(?,?,?,?,?,?,?,?,?)")
        if conn is not None:
            eid, _ = self._exec_insert(conn, sql, params, "edges",
                                       idempotency_key)
            return eid
        with self.transaction() as c:
            eid, _ = self._exec_insert(c, sql, params, "edges",
                                       idempotency_key)
            # 邻接表只依赖 edges：写边时 bump 版本号，供跨进程缓存失效
            c.execute("UPDATE data_version SET version = version + 1"
                      " WHERE scope = 'edges'")
            return eid

    def insert_fact(self, node_id, key, value, source, confidence, valid_at,
                    recorded_at, invalid_at=None, evidence_ids=None,
                    explicit_confirmation=False, idempotency_key=None,
                    conn=None):
        params = (node_id, key, value, source, confidence,
                  valid_at.isoformat(), recorded_at.isoformat(),
                  invalid_at.isoformat() if invalid_at else None,
                  json.dumps(evidence_ids or []),
                  bool(explicit_confirmation), idempotency_key)
        sql = ("INSERT INTO facts(node_id,fact_key,fact_value,source,"
               "confidence,valid_at,recorded_at,invalid_at,evidence_ids,"
               "explicit_confirmation,idempotency_key)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?)")
        if conn is not None:
            return self._exec_insert(conn, sql, params, "facts",
                                     idempotency_key)[0]
        with self.transaction() as c:
            return self._exec_insert(c, sql, params, "facts",
                                     idempotency_key)[0]

    def insert_belief(self, subject_id, proposition, polarity, confidence,
                      source, valid_at, invalid_at, superseded_by, lifecycle,
                      access_label, evidence_ids, created_at,
                      idempotency_key=None, conn=None):
        if polarity not in BELIEF_POLARITIES:
            raise ValidationError(f"E001 非法 polarity: {polarity}")
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
        return self._insert_with_audit(conn, "beliefs", sql, params,
                                       "write_belief", "belief",
                                       idempotency_key)

    def insert_intent(self, subject_id, proposition, status, confidence,
                      source, valid_at, invalid_at, lifecycle, access_label,
                      evidence_ids, created_at, idempotency_key=None,
                      conn=None):
        if status not in INTENT_STATUSES:
            raise ValidationError(f"E001 非法 status: {status}")
        params = (subject_id, proposition, status, confidence, source,
                  valid_at.isoformat() if valid_at else None,
                  invalid_at.isoformat() if invalid_at else None,
                  lifecycle, access_label, json.dumps(evidence_ids or []),
                  created_at.isoformat(), idempotency_key)
        sql = ("INSERT INTO intents(subject_id,proposition,status,"
               "confidence,source,valid_at,invalid_at,lifecycle,"
               "access_label,evidence_ids,created_at,idempotency_key)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)")
        return self._insert_with_audit(conn, "intents", sql, params,
                                       "write_intent", "intent",
                                       idempotency_key)

    def insert_evidence(self, source_type, source_ref, conversation_id,
                        message_id, observed_at, content_hash, trust_level,
                        metadata, created_at, access_label="public",
                        idempotency_key=None, conn=None):
        if source_type not in EVIDENCE_SOURCE_TYPES:
            raise ValidationError(f"E001 非法 source_type: {source_type}")
        params = (source_type, source_ref, conversation_id, message_id,
                  observed_at.isoformat() if observed_at else None,
                  content_hash, trust_level,
                  json.dumps(metadata or {}, ensure_ascii=False),
                  created_at.isoformat(), idempotency_key, access_label)
        sql = ("INSERT INTO evidence(source_type,source_ref,conversation_id,"
               "message_id,observed_at,content_hash,trust_level,metadata,"
               "created_at,idempotency_key,access_label)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?)")
        return self._insert_with_audit(conn, "evidence", sql, params,
                                       "write_evidence", "evidence",
                                       idempotency_key)

    def insert_memory_link(self, source_id, target_id, source_type, relation,
                           confidence, valid_at, invalid_at,
                           idempotency_key=None, conn=None, source_dim=""):
        if relation not in MEMORY_LINK_RELATIONS:
            raise ValidationError(f"E001 非法 relation: {relation}")
        params = (source_id, target_id, source_type, relation, confidence,
                  valid_at.isoformat() if valid_at else None,
                  invalid_at.isoformat() if invalid_at else None,
                  idempotency_key, source_dim)
        sql = ("INSERT INTO memory_links(source_id,target_id,source_type,"
               "relation,confidence,valid_at,invalid_at,idempotency_key,"
               "source_dim) VALUES(?,?,?,?,?,?,?,?,?)")
        return self._insert_with_audit(conn, "memory_links", sql, params,
                                       "write_memory_link", "memory_link",
                                       idempotency_key)

    def insert_impact(self, subject_id, dimension, direction, valence,
                      magnitude, kind="objective", evaluator="agent",
                      description="", cause_event_id=None, source="chat",
                      confidence=0.7, valid_at=None, invalid_at=None,
                      superseded_by=None, lifecycle="active",
                      access_label="public", evidence_ids=None,
                      created_at=None, idempotency_key=None, conn=None):
        if direction not in IMPACT_DIRECTIONS:
            raise ValidationError(f"E001 非法 direction: {direction}")
        if valence not in IMPACT_VALENCES:
            raise ValidationError(f"E001 非法 valence: {valence}")
        if kind not in IMPACT_KINDS:
            raise ValidationError(f"E001 非法 kind: {kind}")
        if not (0.0 <= magnitude <= 1.0):
            raise ValidationError("E002 magnitude 必须在 [0,1]")
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
        return self._insert_with_audit(conn, "impacts", sql, params,
                                       "write_impact", "impact",
                                       idempotency_key)

    def insert_pattern(self, subject_id, pattern_type, proposition,
                       confidence=0.7, support=1, window_days=None,
                       source="inferred", evidence_ids=None, valid_at=None,
                       created_at=None, idempotency_key=None, conn=None):
        import hashlib
        if pattern_type not in PATTERN_TYPES:
            raise ValidationError(f"E001 非法 pattern_type: {pattern_type}")
        if source != "inferred":
            source = "inferred"
        if idempotency_key is None:
            h = hashlib.sha1(proposition.encode("utf-8")).hexdigest()[:12]
            idempotency_key = f"pat:{pattern_type}:{subject_id}:{h}"
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
        return self._insert_with_audit(conn, "patterns", sql, params,
                                       "write_pattern", "pattern",
                                       idempotency_key)

    def insert_trigger(self, memory_id, memory_type, trigger_type, text,
                       source="rule", confidence=0.7, created_at=None,
                       idempotency_key=None, conn=None):
        if trigger_type not in TRIGGER_TYPES:
            raise ValidationError(f"E001 非法 trigger_type: {trigger_type}")
        if memory_type not in ("event", "fact", "belief", "intent", "impact"):
            raise ValidationError(f"E001 非法 memory_type: {memory_type}")
        created_at = created_at or datetime.now()
        params = (memory_id, memory_type, trigger_type, text, source,
                  confidence, created_at.isoformat(), idempotency_key)
        sql = ("INSERT INTO triggers(memory_id,memory_type,trigger_type,"
               "text,source,confidence,created_at,idempotency_key)"
               " VALUES(?,?,?,?,?,?,?,?)")
        return self._insert_with_audit(conn, "triggers", sql, params,
                                       "write_trigger", "trigger",
                                       idempotency_key)

    # ---------------- 向量 ----------------

    @staticmethod
    def _norm_vec(dense):
        import numpy
        v = numpy.asarray([float(x) for x in dense], dtype=float)
        norm = numpy.linalg.norm(v)
        if norm == 0:
            return None
        return (v / norm).tolist()

    def set_node_vectors(self, node_id, dense, sparse=None, model="",
                         dim=None, updated_at=None, conn=None):
        if not dense or not isinstance(dense, (list, tuple)):
            raise StorageError("E011 稠密向量缺失或为空，写入校验失败")
        dim = dim or len(dense)
        updated_at = updated_at or datetime.now()
        vec = self._norm_vec(dense)
        literal = _vec_literal(vec) if (vec and int(dim) == VECTOR_DIM) else None
        params = (node_id, literal, json.dumps([float(x) for x in dense]),
                  json.dumps(sparse) if sparse else None,
                  model, dim, updated_at.isoformat())
        sql = ("INSERT INTO node_vectors(node_id,dense,dense_json,sparse,"
               "model,dim,updated_at) VALUES(?,?::vector,?,?,?,?,?)"
               " ON CONFLICT(node_id) DO UPDATE SET"
               " dense=excluded.dense, dense_json=excluded.dense_json,"
               " sparse=excluded.sparse, model=excluded.model,"
               " dim=excluded.dim, updated_at=excluded.updated_at")
        if conn is not None:
            conn.execute(sql, params)
            return
        with self.transaction() as c:
            c.execute(sql, params)

    def rebuild_vector_index(self, model, dim):
        """全量回填 ANN 投影（pgvector 的 dense 列）。"""
        if self._vec is None or not model or int(dim) != VECTOR_DIM:
            return False
        dim = int(dim)
        try:
            rows = [r for r in self.fetch_node_vectors()
                    if r[3] == model and r[4] == dim]
            with self.transaction() as c:
                for nid, dense, _sp, _m, _d, _at in rows:
                    vec = self._norm_vec(dense)
                    if vec is None:
                        continue
                    c.execute("UPDATE node_vectors SET dense = ?::vector"
                              " WHERE node_id = ?", (_vec_literal(vec), nid))
                c.execute(
                    "INSERT INTO vector_index_meta(model,dim,updated_at)"
                    " VALUES(?,?,?) ON CONFLICT(model) DO UPDATE SET"
                    " dim=excluded.dim, updated_at=excluded.updated_at",
                    (model, dim, datetime.now().isoformat()))
            return True
        except Exception:  # noqa: BLE001 索引失败不阻断主流程
            return False

    def _vec_meta(self, model, dim):
        rows = self.read("SELECT model, dim FROM vector_index_meta"
                         " WHERE model = ?", (model,))
        return bool(rows) and rows[0][1] == int(dim)

    def _set_ef_search(self, k):
        """HNSW 的 ef_search 默认 40，而调用方会传 k=1000；不调会严重欠召回。"""
        ef = max(40, min(int(k), 1000))
        if ef != self._cur_ef:
            self._conn.execute(f"SET hnsw.ef_search = {ef}")
            self._cur_ef = ef

    def search_vectors(self, query_vec, model, dim, k=None):
        """ANN 余弦检索；不可用时返回 None（调用方回退 numpy 全量）。"""
        if self._vec is None or not model:
            return None
        dim = int(dim)
        if dim != VECTOR_DIM or not self._vec_meta(model, dim):
            return None
        vec = self._norm_vec(query_vec)
        if vec is None:
            return None
        limit = int(k) if k is not None else 10000
        self._set_ef_search(limit)
        literal = _vec_literal(vec)
        try:
            rows = self.read(
                "SELECT node_id, dense <=> ?::vector AS dist"
                " FROM node_vectors"
                " WHERE model = ? AND dim = ? AND dense IS NOT NULL"
                " ORDER BY dense <=> ?::vector LIMIT ?",
                (literal, model, dim, literal, limit))
            return [(int(r[0]), float(r[1])) for r in rows]
        except Exception:  # noqa: BLE001 查询失败回退 numpy
            return None

    def has_node_vector(self, node_id):
        return bool(self.read("SELECT 1 FROM node_vectors WHERE node_id = ?",
                              (node_id,)))

    def fetch_node_vectors(self):
        rows = self.read(
            "SELECT node_id, dense_json, sparse, model, dim, updated_at"
            " FROM node_vectors")
        return [(r[0], json.loads(r[1]), json.loads(r[2]) if r[2] else None,
                 r[3], r[4], _dt(r[5])) for r in rows]

    # ---------------- 版本 / 墓碑 ----------------

    def add_version(self, node_id, version, content, at):
        with self.transaction() as conn:
            conn.execute("INSERT INTO versions(node_id,version,content,"
                         "created_at) VALUES(?,?,?,?)",
                         (node_id, version, content, at.isoformat()))

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
        rows = self.read("SELECT lifecycle FROM nodes WHERE id = ?", (node_id,))
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
        """见 SQLiteStore.supersede_fact（UPDATE 与 audit_log 同事务）。"""
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
        sql = "UPDATE facts SET tombstoned=TRUE WHERE id=?"
        audit = ("INSERT INTO audit_log(op,target_type,target_id,reason,at)"
                 " VALUES('tombstone','fact',?,?,?)", (fid, "retracted",
                                                       at.isoformat()))
        if conn is not None:
            conn.execute(sql, (fid,))
            conn.execute(audit[0], audit[1])
            return
        with self.transaction() as c:
            c.execute(sql, (fid,))
            c.execute(audit[0], audit[1])

    def rewire_subject_rows(self, from_nid, to_nid, conn=None):
        """见 SQLiteStore.rewire_subject_rows（实体合并时重挂状态行主体）。"""
        counts = {}

        def _run(c):
            counts.clear()
            for table in ("beliefs", "intents", "impacts", "patterns"):
                counts[table] = c.execute(
                    f"UPDATE {table} SET subject_id=%s WHERE subject_id=%s",
                    (to_nid, from_nid)).rowcount

        if conn is not None:
            _run(conn)
        else:
            with self.transaction() as c:
                _run(c)
        return counts

    # ---------------- 合规删除级联 ----------------

    def cascade_forget(self, node_id, at, evidence_ids=(), conn=None):
        """见 SQLiteStore.cascade_forget（两后端同语义）。

        注意：本方法在事务里执行多条语句，PG 下**必须**下传 `conn`
        （调用方已在事务中时不能另起事务）。
        """
        counts = {}

        def _run(c):
            own = [r[0] for r in c.execute(
                "SELECT id FROM facts WHERE node_id=?", (node_id,))]
            own += [r[0] for r in c.execute(
                "SELECT id FROM beliefs WHERE subject_id=?", (node_id,))]
            own += [r[0] for r in c.execute(
                "SELECT id FROM intents WHERE subject_id=?", (node_id,))]
            own += [r[0] for r in c.execute(
                "SELECT id FROM impacts WHERE subject_id=?", (node_id,))]
            for table in ("beliefs", "intents", "impacts"):
                cur = c.execute(
                    f"UPDATE {table} SET lifecycle='tombstoned' "
                    "WHERE subject_id=? AND lifecycle<>'tombstoned'",
                    (node_id,))
                counts[table] = cur.rowcount
            for table, col in (("patterns", "subject_id"),
                               ("versions", "node_id"),
                               ("node_vectors", "node_id")):
                counts[table] = c.execute(
                    f"DELETE FROM {table} WHERE {col}=?", (node_id,)).rowcount
            ids = list([node_id] + own)
            if ids:
                ph = ",".join(["%s"] * len(ids))
                counts["triggers"] = c.execute(
                    f"DELETE FROM triggers WHERE memory_id IN ({ph})",
                    tuple(ids)).rowcount
                counts["memory_links"] = c.execute(
                    f"DELETE FROM memory_links WHERE source_id IN ({ph}) "
                    f"OR target_id IN ({ph})", tuple(ids + ids)).rowcount
            if evidence_ids:
                eph = ",".join(["%s"] * len(evidence_ids))
                counts["evidence"] = c.execute(
                    "UPDATE evidence SET access_label='sensitive' "
                    f"WHERE id IN ({eph}) AND access_label<>'sensitive'",
                    tuple(evidence_ids)).rowcount
            # 审计写在**同一连接**里：另起事务会与外层事务冲突（E009）
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

    def _select_all(self, table):
        return self.read(f"SELECT {','.join(COLS[table])} FROM {table}")

    def fetch_nodes(self):
        return [Node(r[0], r[1], r[2], r[3], r[4], _dt(r[5]), r[6],
                     bool(r[7]), r[8], r[9], r[10], r[11], _dt(r[12]),
                     _dt(r[13]), json.loads(r[15] or "[]"), r[16] or "")
                for r in self._select_all("nodes")]

    def fetch_edges(self):
        return [Edge(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]), _dt(r[7]),
                     _dt(r[8]), r[10], r[11])
                for r in self._select_all("edges")]

    def adjacency(self):
        """惰性无向邻接表。跨进程失效靠 data_version 表的版本号 + 短 TTL 兜底。"""
        try:
            rows = self.read("SELECT version FROM data_version"
                             " WHERE scope = 'edges'")
            version = int(rows[0][0]) if rows else 0
        except Exception:  # noqa: BLE001 版本表不可用时退回 TTL
            version = self._data_version
        stale = (self._adj_key != version
                 or time.monotonic() > self._adj_expiry)
        if stale or self._adj_cache is None:
            adj: dict = {}
            for e in self.fetch_edges():
                if e.lifecycle != "active":
                    continue
                adj.setdefault(e.from_id, []).append(
                    (e.to_id, e.weight, e.confidence, e.rel, e.invalid_at))
                adj.setdefault(e.to_id, []).append(
                    (e.from_id, e.weight, e.confidence, e.rel, e.invalid_at))
            self._adj_cache = adj
            self._adj_key = version
            self._adj_expiry = time.monotonic() + ADJ_CACHE_TTL_S
        return self._adj_cache

    def fetch_facts(self):
        return [Fact(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]), _dt(r[7]),
                     _dt(r[8]), r[9], bool(r[10]),
                     json.loads(r[12] or "[]"), bool(r[13]))
                for r in self._select_all("facts")]

    def fetch_beliefs(self):
        return [Belief(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]),
                       _dt(r[7]), r[8], r[9], r[10],
                       json.loads(r[11] or "[]"), _dt(r[12]))
                for r in self._select_all("beliefs")]

    def fetch_intents(self):
        return [Intent(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]),
                       _dt(r[7]), r[8], r[9], json.loads(r[10] or "[]"),
                       _dt(r[11]))
                for r in self._select_all("intents")]

    def _evidence_rows(self, rows):
        return [Evidence(r[0], r[1], r[2], r[3], r[4], _dt(r[5]), r[6],
                         r[7], json.loads(r[8] or "{}"), _dt(r[9]),
                         r[11] or "public")
                for r in rows]

    def fetch_evidence(self):
        return self._evidence_rows(self._select_all("evidence"))

    def get_evidence(self, ids):
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.read(
            f"SELECT {','.join(COLS['evidence'])} FROM evidence"
            f" WHERE id IN ({marks})", tuple(ids))
        return self._evidence_rows(rows)

    def fetch_impacts(self):
        return [Impact(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                       r[8], r[9], r[10], r[11], _dt(r[12]), _dt(r[13]),
                       r[14], r[15], r[16], json.loads(r[17] or "[]"),
                       _dt(r[18]))
                for r in self._select_all("impacts")]

    def fetch_triggers(self):
        return [Trigger(r[0], r[1], r[2], r[3], r[4], r[5], r[6], _dt(r[7]))
                for r in self._select_all("triggers")]

    def fetch_patterns(self):
        return [Pattern(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                        json.loads(r[8] or "[]"), _dt(r[9]), _dt(r[10]))
                for r in self._select_all("patterns")]

    def _link_rows(self, rows):
        return [MemoryLink(r[0], r[1], r[2], r[3], r[4], r[5], _dt(r[6]),
                           _dt(r[7]), r[9])
                for r in rows]

    def fetch_memory_links(self):
        return self._link_rows(self._select_all("memory_links"))

    def memory_links_of(self, memory_id):
        rows = self.read(
            f"SELECT {','.join(COLS['memory_links'])} FROM memory_links"
            " WHERE source_id = ? OR target_id = ?", (memory_id, memory_id))
        return self._link_rows(rows)

    def fetch_versions(self, node_id):
        return self.read("SELECT version, content, created_at FROM versions"
                         " WHERE node_id=? ORDER BY version", (node_id,))

    def fetch_audit_logs(self, target_type=None, target_id=None, op=None,
                         limit=50):
        """见 SQLiteStore.fetch_audit_logs（两后端同签名同语义）。"""
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
        return [Tombstone(r[0], r[1], r[2], r[3], _dt(r[4]))
                for r in self._select_all("tombstones")]

    def close(self):
        with self._wlock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 重复 close 幂等
                pass
