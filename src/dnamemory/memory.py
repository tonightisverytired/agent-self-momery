# -*- coding: utf-8 -*-
"""MemorySystem 门面（公共 API v1）。"""
from __future__ import annotations

import difflib
import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime
from functools import lru_cache

from . import governance
from .errors import (EmbeddingError, EvidenceNotFoundError, NotFoundError,
                     ValidationError)
from .models import (BELIEF_POLARITIES, EVIDENCE_SOURCE_TYPES,
                     IMPACT_DIRECTIONS, IMPACT_VALENCES, INTENT_STATUSES,
                     TRIGGER_TYPES, ExtractedMemory, MemoryConfig, MemoryHit,
                     RecallFilters, RecallQuery, WriteResult)
from .retrieval import neighbors, recall
from .storage import build_store


@lru_cache(maxsize=4096)
def _bigram_set(s):
    """bigram 集缓存（0.8.2 性能）：_bigram_overlap 在同一查询下对
    同一 query/值串反复建集，规模库单次 recall_context 达数千次。"""
    return frozenset(s[i:i + 2] for i in range(len(s) - 1))


def _conflict_key(c):
    """冲突条目归一成 (node_id, key)：兼容 coherence 的 dict 与
    FactResolver 的 ConflictGroup（0.8.2：供冲突键集预建）。"""
    if isinstance(c, dict):
        return (c.get("node_id"), c.get("key"))
    return (getattr(c, "node_id", None), getattr(c, "key", None))


class MemorySystem:
    def __init__(self, path=":memory:", config=None, clock=None,
                 embedder=None, extractor=None, judge=None, reranker=None,
                 time_backend=None, graph_backend=None, store=None, dsn=None,
                 query_rewriter=None):
        """store 与 dsn 均可选：store 注入现成存储实例（测试/高级用法），
        dsn 提供 PostgreSQL 连接串（默认留空则用 path 指定的 SQLite）。
        query_rewriter：可选 HyDE 查询改写器（hyde.QueryRewriter 协议），
        改写失败自动回退原文检索，不阻断。"""
        if store is not None and dsn:
            raise ValidationError("E001 store 与 dsn 不可同时提供")
        self.store = store or build_store(path=path, dsn=dsn)
        self.config = config or MemoryConfig()
        self.clock = clock or (lambda: datetime.now())
        self.embedder = embedder
        self.extractor = extractor
        self.judge = judge
        self.reranker = reranker
        self.query_rewriter = query_rewriter
        self._last_alt_texts: tuple = ()
        self.time_backend = time_backend
        self.graph_backend = graph_backend
        self._name2id = {
            n.name: n.nid for n in self.store.fetch_nodes()
            if n.lifecycle not in ("deleted", "tombstoned")}

    def _resolve(self, ref):
        if isinstance(ref, int):
            return ref
        nid = self._name2id.get(ref)
        if nid is None:
            raise NotFoundError("E006 目标不存在")
        return nid

    # ---------------- 写入 ----------------
    def add_entity(self, name, kind="concept", description="",
                   protected=False, access_label="public",
                   evidence_ids=None, idempotency_key=None):
        nid = self.store.insert_node(
            "entity", kind, name, description, None, 0.7, protected,
            access_label, 0.0, 0.0, self.clock(), evidence_ids=evidence_ids,
            idempotency_key=idempotency_key)
        self._name2id[name] = nid
        self._embed_and_store([(nid, f"{name} {description}".strip())])
        return nid

    def add_event(self, name, ts, kind="meeting", value_score=0.5,
                  protected=False, access_label="public", life=None,
                  decay_rate=None, evidence_ids=None, idempotency_key=None):
        if life is None or decay_rate is None:
            d = self.config.event_defaults.get(
                kind, self.config.event_defaults["transient"])
            life = life if life is not None else d.life
            decay_rate = decay_rate if decay_rate is not None else d.decay_rate
        nid = self.store.insert_node(
            "event", kind, name, "", ts, value_score, protected,
            access_label, life, decay_rate, self.clock(),
            evidence_ids=evidence_ids, idempotency_key=idempotency_key)
        self._name2id[name] = nid
        self._embed_and_store([(nid, name)])
        return nid

    def _embed_and_store(self, nodes):
        """批量计算稠密+稀疏向量并写入；稠密必须写成功，稀疏 best-effort。"""
        if not nodes or self.embedder is None:
            return
        pending = [(nid, text) for nid, text in nodes
                   if not self.store.has_node_vector(nid)]
        if not pending:
            return
        results = self.embedder.embed_batch([t for _, t in pending])
        if len(results) != len(pending):
            raise EmbeddingError(
                f"E011 embed_batch 返回数量不符：{len(results)} != "
                f"{len(pending)}")
        now = self.clock()
        for (nid, _), res in zip(pending, results):
            self.store.set_node_vectors(
                nid, res.dense, res.sparse,
                res.model or getattr(self.embedder, "model_name", ""),
                res.dim or len(res.dense), now)

    def add_edge(self, a, b, rel, weight=0.5, confidence=0.8,
                 valid_at=None, invalid_at=None, idempotency_key=None):
        if rel not in self.config.relations:
            raise ValidationError("E001 未知关系类型")
        if not (0 <= weight <= 1 and 0 <= confidence <= 1):
            raise ValidationError("E002 数值域越界")
        now = self.clock()
        va = valid_at or now
        if invalid_at is not None and invalid_at <= va:
            raise ValidationError("E003 双时态顺序错误")
        return self.store.insert_edge(
            self._resolve(a), self._resolve(b), rel, weight, confidence,
            va, invalid_at, now, idempotency_key=idempotency_key)

    def add_fact(self, entity, key, value, source="chat", confidence=0.7,
                 valid_at=None, invalid_at=None,
                 explicit_confirmation=False, evidence_ids=None,
                 idempotency_key=None):
        if not (0 <= confidence <= 1):
            raise ValidationError("E002 数值域越界")
        now = self.clock()
        va = valid_at or now
        if invalid_at is not None and invalid_at <= va:
            raise ValidationError("E003 双时态顺序错误")
        fid = self.store.insert_fact(
            self._resolve(entity), key, value, source, confidence,
            va, now, invalid_at=invalid_at,
            explicit_confirmation=explicit_confirmation,
            evidence_ids=evidence_ids, idempotency_key=idempotency_key)
        self._rule_triggers_fact(fid, entity, key, now)
        return fid

    def _rule_triggers_fact(self, fid, entity, key, now, conn=None):
        """0.7.0 P2：fact 写入后的确定性触达路径（entity + bridge）。

        幂等键防重；失败不影响主写入（触达路径是增强，不是事实）。
        """
        from .resolve import canonical_fact_key
        ent_name = None
        if isinstance(entity, str):
            ent_name = entity
        else:
            for n in self.store.fetch_nodes():
                if n.nid == entity:
                    ent_name = n.name
                    break
        if not ent_name:
            return
        try:
            # 保存点：触发失败只回滚触发本身。PG 下没有保存点的话，这一句
            # 报错会把外层事务置为 aborted，整批记忆跟着一起回滚。
            with self.store.savepoint(conn) as sp:
                self.store.insert_trigger(
                    fid, "fact", "entity", ent_name, source="rule",
                    confidence=0.9, created_at=now,
                    idempotency_key=f"trg:fact:{fid}:entity:{ent_name}",
                    conn=sp)
                ck = canonical_fact_key(key, self.config)
                for alias in sorted(self.config.key_aliases.get(ck, ())):
                    if alias != key:
                        self.store.insert_trigger(
                            fid, "fact", "bridge", alias, source="rule",
                            confidence=0.7, created_at=now,
                            idempotency_key=f"trg:fact:{fid}:bridge:{alias}",
                            conn=sp)
        except Exception:  # noqa: BLE001 trigger 失败不影响主写入
            return

    def _resolve_subject(self, subject):
        """belief/intent 主体消解：int id 直过；名字精确/模糊匹配实体。"""
        if isinstance(subject, int):
            return subject, False
        nid = self._name2id.get(subject)
        if nid is not None:
            return nid, False
        entity_local = {n.name: n.nid for n in self.store.fetch_nodes()
                        if n.node_type == "entity"}
        nid = self._fuzzy_entity_id(entity_local, subject)
        if nid is None:
            raise NotFoundError(f"E006 主体不存在: {subject}")
        return nid, True

    def add_belief(self, subject, proposition, polarity="neutral",
                   confidence=0.7, source="chat", valid_at=None,
                   invalid_at=None, access_label="public",
                   evidence_ids=None, idempotency_key=None):
        """结构化写入观点（设计稿 §6.2 非 LLM 路径）。"""
        nid, fuzzy = self._resolve_subject(subject)
        bid = self.store.insert_belief(
            nid, proposition, polarity, confidence, source,
            valid_at, invalid_at, None, "active", access_label,
            evidence_ids or [], self.clock(),
            idempotency_key=idempotency_key)
        if fuzzy:
            self.store.audit(
                "endpoint_fuzzy_match", "belief", bid, "fuzzy",
                meta=json.dumps({"endpoint": subject, "matched": nid},
                                ensure_ascii=False))
        self._rule_triggers_state(bid, "belief", subject, proposition,
                                  self.clock())
        return bid

    def add_intent(self, subject, proposition, status="active",
                   confidence=0.7, source="chat", valid_at=None,
                   invalid_at=None, access_label="public",
                   evidence_ids=None, idempotency_key=None):
        """结构化写入意图（设计稿 §6.2 非 LLM 路径）。"""
        nid, fuzzy = self._resolve_subject(subject)
        iid = self.store.insert_intent(
            nid, proposition, status, confidence, source,
            valid_at, invalid_at, "active", access_label,
            evidence_ids or [], self.clock(),
            idempotency_key=idempotency_key)
        if fuzzy:
            self.store.audit(
                "endpoint_fuzzy_match", "intent", iid, "fuzzy",
                meta=json.dumps({"endpoint": subject, "matched": nid},
                                ensure_ascii=False))
        self._rule_triggers_state(iid, "intent", subject, proposition,
                                  self.clock())
        return iid

    def _rule_triggers_state(self, mid, mtype, subject, text, now,
                             conn=None):
        """belief/intent 的确定性触达路径：entity（主体名）+ scene
        （命题截断 12 字）。幂等；失败不影响主写入。"""
        ent_name = subject if isinstance(subject, str) else None
        if ent_name is None:
            for n in self.store.fetch_nodes():
                if n.nid == subject:
                    ent_name = n.name
                    break
        if not ent_name:
            return
        try:
            with self.store.savepoint(conn) as sp:
                self.store.insert_trigger(
                    mid, mtype, "entity", ent_name, source="rule",
                    confidence=0.9, created_at=now,
                    idempotency_key=f"trg:{mtype}:{mid}:entity:{ent_name}",
                    conn=sp)
                scene = (text or "")[:12]
                if scene:
                    self.store.insert_trigger(
                        mid, mtype, "scene", scene, source="rule",
                        confidence=0.7, created_at=now,
                        idempotency_key=f"trg:{mtype}:{mid}:scene:{scene}",
                        conn=sp)
        except Exception:  # noqa: BLE001 trigger 失败不影响主写入
            return

    def add_evidence(self, source_type, source_ref="", conversation_id=None,
                     message_id=None, observed_at=None, content_hash=None,
                     trust_level=None, access_label="public", metadata=None,
                     idempotency_key=None):
        """结构化写入证据（设计稿 §6.2 非 LLM 路径）。"""
        return self.store.insert_evidence(
            source_type, source_ref, conversation_id, message_id,
            observed_at, content_hash, trust_level, metadata or {},
            self.clock(), access_label=access_label,
            idempotency_key=idempotency_key)

    def add_impact(self, subject, dimension, direction, valence, magnitude,
                   kind="objective", evaluator="agent", description="",
                   cause_event=None, source="chat", confidence=0.7,
                   valid_at=None, invalid_at=None, access_label="public",
                   evidence_ids=None, idempotency_key=None):
        """结构化写入影响（0.7.0 P1，设计稿 §6.2 非 LLM 路径）。

        cause_event：事件名或 id；找不到 → cause_event_id=None
        （不丢影响数据，audit impact_cause_unresolved）。
        """
        nid, fuzzy = self._resolve_subject(subject)
        cause_id = None
        if cause_event is not None:
            if isinstance(cause_event, int):
                if any(n.nid == cause_event for n in self.store.fetch_nodes()):
                    cause_id = cause_event
            else:
                for n in self.store.fetch_nodes():
                    if n.node_type == "event" and n.name == cause_event:
                        cause_id = n.nid
                        break
        iid = self.store.insert_impact(
            nid, dimension, direction, valence, magnitude,
            kind=kind, evaluator=evaluator, description=description,
            cause_event_id=cause_id, source=source, confidence=confidence,
            valid_at=valid_at, invalid_at=invalid_at,
            access_label=access_label, evidence_ids=evidence_ids or [],
            created_at=self.clock(), idempotency_key=idempotency_key)
        if fuzzy:
            self.store.audit(
                "endpoint_fuzzy_match", "impact", iid, "fuzzy",
                meta=json.dumps({"endpoint": subject, "matched": nid},
                                ensure_ascii=False))
        if cause_event is not None and cause_id is None:
            self.store.audit(
                "impact_cause_unresolved", "impact", iid, "cause_missing",
                meta=json.dumps({"cause": str(cause_event)},
                                ensure_ascii=False))
        return iid

    def write_text(self, text, meta=None, extractor=None):
        ex = extractor or self.extractor
        if ex is None:
            raise ValidationError("E010 抽取器未注入")
        # 拷贝后注入内部键 _raw_text（批级证据合成的原文依据），
        # 不污染调用方 dict；下划线前缀避免与用户键冲突。
        meta = dict(meta or {})
        meta["_raw_text"] = text
        # 自动注入当前日期：抽取器据此把「昨天/上周一」等相对时间换算成
        # 具体日期填入 ts；调用方显式传入的 today 优先（离线注入可复现）。
        meta.setdefault("today", self.clock().date().isoformat())
        cands = ex.extract(text, meta)
        ids, dropped = self._write_candidates(cands, meta)
        return WriteResult(accepted=len(ids), rejected=dropped, ids=ids)

    def write_many(self, texts, meta=None, extractor=None, batch_size=20):
        """批量拆解写入：支持 Extractor.extract_many 分批调用，失败批次回滚。"""
        ex = extractor or self.extractor
        if ex is None:
            raise ValidationError("E010 抽取器未注入")
        texts = list(texts)
        meta = dict(meta or {})
        meta.setdefault("today", self.clock().date().isoformat())
        if hasattr(ex, "extract_many"):
            batches = ex.extract_many(texts, meta, batch_size=batch_size)
        else:
            batches = [ex.extract(t, meta) for t in texts]
        # 每批 meta 注入该批原文摘要：先按 batch_size 对齐 extract_many 的
        # 标准分批；批数不符（如逐条兜底抽取器）则按批数均分兜底。
        chunks = [texts[i:i + batch_size]
                  for i in range(0, len(texts), batch_size)]
        if len(chunks) != len(batches):
            per = max(1, math.ceil(len(texts) / max(len(batches), 1)))
            chunks = [texts[i:i + per] for i in range(0, len(texts), per)]
        total_ids, rejected = [], []
        for i, cands in enumerate(batches):
            batch_meta = dict(meta or {})
            chunk = chunks[i] if i < len(chunks) else []
            if chunk:
                batch_meta["_raw_text"] = "|".join(t[:40] for t in chunk)
            ids, dropped = self._write_candidates(cands, batch_meta)
            total_ids.extend(ids)
            rejected.extend(dropped)
        return WriteResult(accepted=len(total_ids), rejected=rejected,
                           ids=total_ids)

    def _write_candidates(self, cands, meta=None):
        ids = []
        dropped = []
        embed_queue = []
        local = dict(self._name2id)
        entity_local = {}
        for n in self.store.fetch_nodes():
            # 写入侧实体消解的规范锚点：跳过墓碑/删除节点；同名取最低 id
            # （setdefault 先到先得），让事实/边/观点都向规范节点收敛
            if n.node_type == "entity" \
                    and n.lifecycle not in ("tombstoned", "deleted"):
                entity_local.setdefault(n.name, n.nid)
        # 实体名字一律拨正到规范节点：_name2id 是后者覆盖（指向最后建的
        # 重复节点），不拨正的话事实/边会绕过消解继续落到重复节点上
        local.update(entity_local)
        with self.store.transaction() as conn:
            meta = meta or {}
            rec = meta.get("recorded_at")
            if isinstance(rec, str):
                rec = datetime.fromisoformat(rec)
            elif not isinstance(rec, datetime):
                rec = self.clock()
            now = rec
            # 第一遍：evidence 候选先落表，收集本批证据 id 供后续绑定
            batch_evidence = []
            for c in cands:
                if c.type != "evidence":
                    continue
                try:
                    self._validate_candidate(c)
                except ValidationError as e:
                    dropped.append((c.type, str(e)))
                    continue
                eid = self.store.insert_evidence(
                    c.source_type, c.source_ref or "", c.conversation_id,
                    c.message_id, c.ts or now, c.content_hash, c.trust_level,
                    {}, now, idempotency_key=c.idempotency_key, conn=conn)
                ids.append(eid)
                batch_evidence.append(eid)
            # 批级证据自动闭环（0.8.1 IA-1）：抽取器几乎不产生 evidence
            # 候选，批内无显式证据且存在其他候选时，用原文合成一条批级
            # 证据并绑定批内全部候选；幂等键防同一文本重放产生重复。
            rest = [c for c in cands if c.type != "evidence"]
            if not batch_evidence and rest:
                eid = self._auto_batch_evidence(meta, rest, now, conn)
                ids.append(eid)
                batch_evidence.append(eid)
            # 第二遍：其余候选，evidence_ids 绑定本批证据
            for c in cands:
                if c.type == "evidence":
                    continue
                try:
                    self._validate_candidate(c)
                    if c.type in ("event", "entity") and not c.name:
                        dropped.append((c.type, "缺少 name"))
                        continue
                    if c.type == "fact" and (not c.key or c.value is None):
                        dropped.append((c.type, "fact 缺少 key/value"))
                        continue
                except ValidationError as e:
                    dropped.append((c.type, str(e)))
                    continue
                ev_ids = (c.evidence_ids or []) + batch_evidence

                if c.type == "event":
                    # 未提及时间 → ts 保持 None，不回填"当天时间戳"；
                    # 真实环境时间记录在 created_at/recorded_at。
                    nid = self.store.insert_node(
                        "event", c.kind or "meeting", c.name, "", c.ts,
                        c.value_score if c.value_score is not None else 0.5,
                        c.protected, "public",
                        self.config.event_defaults.get(
                            c.kind or "meeting",
                            self.config.event_defaults["transient"]).life,
                        self.config.event_defaults.get(
                            c.kind or "meeting",
                            self.config.event_defaults["transient"]).decay_rate,
                        now, evidence_ids=ev_ids,
                        idempotency_key=c.idempotency_key, conn=conn)
                    ids.append(nid)
                    local[c.name] = nid
                    embed_queue.append((nid, c.name))
                elif c.type == "entity":
                    existing = entity_local.get(c.name)
                    if existing is not None:
                        # 实体消解治本：同名实体复用规范节点，不再重复建点
                        # （此前每批注入都给「李明」新建节点：1715 节点/554 名）。
                        # 旧节点描述为空且候选带描述时回填；向量保持名字级不重建。
                        nid = existing
                        if c.value:
                            conn.execute(
                                "UPDATE nodes SET description=? WHERE id=? "
                                "AND (description IS NULL OR description='')",
                                (c.value, nid))
                        ids.append(nid)
                        local[c.name] = nid
                        continue
                    nid = self.store.insert_node(
                        "entity", c.kind or "concept", c.name,
                        c.value or "", None, 0.7, c.protected, "public",
                        0.0, 0.0, now, evidence_ids=ev_ids,
                        idempotency_key=c.idempotency_key, conn=conn)
                    ids.append(nid)
                    local[c.name] = nid
                    entity_local[c.name] = nid
                    embed_queue.append(
                        (nid, f"{c.name} {c.value or ''}".strip()))
                elif c.type == "fact":
                    target = c.from_ or c.name
                    nid = local.get(target)
                    fuzzy = None
                    if nid is None:
                        fuzzy = self._fuzzy_entity_id(entity_local, target)
                        nid = fuzzy
                    if nid is None:
                        dropped.append((c.type, f"实体不存在: {target}"))
                        continue
                    if fuzzy is not None:
                        conn.execute(
                            "INSERT INTO audit_log(op,target_type,target_id,"
                            "reason,meta,at) VALUES(?,?,?,?,?,?)",
                            ("endpoint_fuzzy_match", "fact", nid, "fuzzy",
                             json.dumps({"endpoint": target,
                                         "matched": nid}, ensure_ascii=False),
                             now.isoformat()))
                    fid = self.store.insert_fact(
                        nid, c.key, c.value, c.source, c.confidence,
                        c.ts or now, now, evidence_ids=ev_ids,
                        idempotency_key=c.idempotency_key, conn=conn)
                    ids.append(fid)
                    self._rule_triggers_fact(fid, target, c.key, now,
                                             conn=conn)
                elif c.type == "edge":
                    frm = local.get(c.from_)
                    to = local.get(c.to)
                    frm_fuzzy = None
                    to_fuzzy = None
                    if frm is None:
                        frm_fuzzy = self._fuzzy_entity_id(entity_local,
                                                          c.from_)
                        frm = frm_fuzzy
                    if to is None:
                        to_fuzzy = self._fuzzy_entity_id(entity_local, c.to)
                        to = to_fuzzy
                    if frm is None or to is None:
                        dropped.append(
                            (c.type, f"端点不存在: {c.from_} -> {c.to}"))
                        continue
                    if frm_fuzzy is not None or to_fuzzy is not None:
                        conn.execute(
                            "INSERT INTO audit_log(op,target_type,target_id,"
                            "reason,meta,at) VALUES(?,?,?,?,?,?)",
                            ("endpoint_fuzzy_match", "edge", None, "fuzzy",
                             json.dumps(
                                 {"from": c.from_, "to": c.to,
                                  "from_matched": frm,
                                  "to_matched": to},
                                 ensure_ascii=False),
                             now.isoformat()))
                    ids.append(self.store.insert_edge(
                        frm, to, c.rel, 0.6, c.confidence, c.ts or now, None,
                        now, idempotency_key=c.idempotency_key, conn=conn))
                elif c.type in ("belief", "intent"):
                    target = c.name
                    nid = local.get(target)
                    fuzzy = None
                    if nid is None:
                        fuzzy = self._fuzzy_entity_id(entity_local, target)
                        nid = fuzzy
                    if nid is None:
                        dropped.append((c.type, f"主体不存在: {target}"))
                        continue
                    if fuzzy is not None:
                        conn.execute(
                            "INSERT INTO audit_log(op,target_type,target_id,"
                            "reason,meta,at) VALUES(?,?,?,?,?,?)",
                            ("endpoint_fuzzy_match", c.type, nid, "fuzzy",
                             json.dumps({"endpoint": target,
                                         "matched": nid},
                                        ensure_ascii=False),
                             now.isoformat()))
                    if c.type == "belief":
                        bid = self.store.insert_belief(
                            nid, c.proposition or c.name,
                            c.polarity or "neutral", c.confidence, c.source,
                            c.ts, None, None, "active", "public", ev_ids,
                            now, idempotency_key=c.idempotency_key, conn=conn)
                        ids.append(bid)
                        self._rule_triggers_state(
                            bid, "belief", target,
                            c.proposition or c.name, now, conn=conn)
                    else:
                        iid = self.store.insert_intent(
                            nid, c.proposition or c.name,
                            c.status or "active", c.confidence, c.source,
                            c.ts, None, "active", "public", ev_ids,
                            now, idempotency_key=c.idempotency_key, conn=conn)
                        ids.append(iid)
                        self._rule_triggers_state(
                            iid, "intent", target,
                            c.proposition or c.name, now, conn=conn)
                elif c.type == "impact":
                    # 主体消解复用 belief/intent 模式；cause 事件名消解
                    # 到 event 节点，找不到 → None + audit（不丢数据）。
                    target = c.name
                    nid = local.get(target)
                    fuzzy = None
                    if nid is None:
                        fuzzy = self._fuzzy_entity_id(entity_local, target)
                        nid = fuzzy
                    if nid is None:
                        dropped.append((c.type, f"主体不存在: {target}"))
                        continue
                    if fuzzy is not None:
                        conn.execute(
                            "INSERT INTO audit_log(op,target_type,target_id,"
                            "reason,meta,at) VALUES(?,?,?,?,?,?)",
                            ("endpoint_fuzzy_match", "impact", nid, "fuzzy",
                             json.dumps({"endpoint": target,
                                         "matched": nid},
                                        ensure_ascii=False),
                             now.isoformat()))
                    cause_id = None
                    if c.cause:
                        for n in self.store.fetch_nodes():
                            if n.node_type == "event" and n.name == c.cause:
                                cause_id = n.nid
                                break
                        if cause_id is None:
                            conn.execute(
                                "INSERT INTO audit_log(op,target_type,"
                                "target_id,reason,meta,at)"
                                " VALUES(?,?,?,?,?,?)",
                                ("impact_cause_unresolved", "impact", nid,
                                 "cause_missing",
                                 json.dumps({"cause": c.cause},
                                            ensure_ascii=False),
                                 now.isoformat()))
                    iid = self.store.insert_impact(
                        nid, c.dimension, c.direction, c.valence,
                        c.magnitude if c.magnitude is not None else 0.5,
                        kind=c.impact_kind or "objective",
                        evaluator=c.evaluator or "agent",
                        description=c.description or "",
                        cause_event_id=cause_id, source=c.source,
                        confidence=c.confidence,
                        valid_at=c.ts, access_label="public",
                        evidence_ids=ev_ids, created_at=now,
                        idempotency_key=c.idempotency_key, conn=conn)
                    ids.append(iid)
                elif c.type == "trigger":
                    # LLM 提议的触达路径：target 名按类型消解到记忆 id；
                    # 找不到 → 丢 trigger 不丢主记忆。落库走确定性校验。
                    ttype = c.trigger_target_type or "event"
                    tgt_id = None
                    if ttype == "event":
                        tgt_id = local.get(c.name)
                        if tgt_id is None:
                            for n in self.store.fetch_nodes():
                                if n.node_type == "event" \
                                        and n.name == c.name:
                                    tgt_id = n.nid
                                    break
                    elif ttype == "belief":
                        for b in self.store.fetch_beliefs():
                            if b.proposition == c.name:
                                tgt_id = b.id
                                break
                    elif ttype == "intent":
                        for i in self.store.fetch_intents():
                            if i.proposition == c.name:
                                tgt_id = i.id
                                break
                    elif ttype == "impact":
                        for im in self.store.fetch_impacts():
                            if im.description == c.name:
                                tgt_id = im.id
                                break
                    if tgt_id is None:
                        dropped.append(
                            (c.type, f"trigger target 未消解: {c.name}"))
                        continue
                    tid = self.store.insert_trigger(
                        tgt_id, ttype, c.trigger_type, c.trigger_text,
                        source="llm", confidence=c.confidence,
                        created_at=now,
                        idempotency_key=(f"trg:{ttype}:{tgt_id}:"
                                         f"{c.trigger_type}:"
                                         f"{c.trigger_text}"), conn=conn)
                    ids.append(tid)
        # 刷新名称索引
        self._name2id.update(local)
        self._embed_and_store(embed_queue)
        return ids, dropped

    def _auto_batch_evidence(self, meta, cands, now, conn):
        """合成批级证据（IA-1）：字段取自 meta/_raw_text，与调用方事务
        同 conn；幂等键 auto:{sha1[:12]} 保证同一文本重放不重复落证据。"""
        raw = meta.get("_raw_text") or ""
        source_type = meta.get("source_type")
        if source_type not in EVIDENCE_SOURCE_TYPES:
            source_type = "conversation"
        source_ref = meta.get("source_ref") or raw[:200]
        content_hash = (hashlib.sha1(raw.encode("utf-8")).hexdigest()
                        if raw else None)
        if raw:
            basis = raw
        else:
            # 无原文时用 meta 稳定摘要 + 候选摘要兜底，保证确定性
            basis = (repr(sorted((k, repr(v)) for k, v in meta.items()))
                     + repr(sorted(f"{c.type}:{c.name}:{c.key or ''}"
                                   for c in cands)))
        digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
        eid = self.store.insert_evidence(
            source_type, source_ref, meta.get("conversation_id"),
            meta.get("message_id"), now, content_hash,
            meta.get("trust_level"), {}, now,
            idempotency_key=f"auto:{digest}", conn=conn)
        conn.execute(
            "INSERT INTO audit_log(op,target_type,target_id,reason,meta,at)"
            " VALUES(?,?,?,?,?,?)",
            ("auto_evidence", "evidence", eid, "batch",
             json.dumps({"batch_size": len(cands),
                         "content_hash": content_hash}, ensure_ascii=False),
             now.isoformat()))
        return eid

    def _validate_candidate(self, c: ExtractedMemory):
        if c.type not in ("event", "entity", "fact", "edge",
                          "belief", "intent", "evidence", "impact",
                          "trigger"):
            raise ValidationError("E001 未知候选类型")
        if c.type == "edge":
            if c.rel not in self.config.relations:
                raise ValidationError("E001 未知关系类型")
            if c.from_ is None or c.to is None:
                raise ValidationError("E001 edge 缺少端点")
        if c.type in ("belief", "intent"):
            if not (c.name or c.proposition):
                raise ValidationError(
                    f"E001 {c.type} 缺少命题（name 或 proposition）")
            if c.type == "belief" and c.polarity is not None \
                    and c.polarity not in BELIEF_POLARITIES:
                raise ValidationError(f"E001 非法 polarity: {c.polarity}")
            if c.type == "intent" and c.status is not None \
                    and c.status not in INTENT_STATUSES:
                raise ValidationError(f"E001 非法 status: {c.status}")
        if c.type == "evidence":
            if not c.source_type \
                    or c.source_type not in EVIDENCE_SOURCE_TYPES:
                raise ValidationError(
                    f"E001 非法 source_type: {c.source_type}")
        if c.type == "impact":
            if not (c.dimension and c.direction and c.valence):
                raise ValidationError(
                    "E001 impact 缺少 dimension/direction/valence")
            if c.direction not in IMPACT_DIRECTIONS:
                raise ValidationError(
                    f"E001 非法 direction: {c.direction}")
            if c.valence not in IMPACT_VALENCES:
                raise ValidationError(f"E001 非法 valence: {c.valence}")
            if c.magnitude is not None and not (0 <= c.magnitude <= 1):
                raise ValidationError("E002 magnitude 必须在 [0,1]")
        if c.type == "trigger":
            if c.trigger_type not in TRIGGER_TYPES:
                raise ValidationError(
                    f"E001 非法 trigger_type: {c.trigger_type}")
            if not (c.trigger_text and c.name):
                raise ValidationError(
                    "E001 trigger 缺少 target 名或 trigger_text")

    # ---------------- 检索 ----------------
    def recall(self, query, filters=None, k=8, mode="triple", max_hops=None,
               tol_days=None, node_types=None, rerank_top_n=20):
        if filters is None:
            filters = RecallFilters(node_types=tuple(node_types)
                                    if node_types else None)
        elif node_types:
            filters = RecallFilters(
                node_types=tuple(node_types),
                kinds=filters.kinds, time_range=filters.time_range,
                include_archived=filters.include_archived,
                access_labels=filters.access_labels)
        # HyDE 查询改写（0.8.1 二轮）：失败/无改写器 → 仅原文
        self._last_alt_texts = ()
        if self.query_rewriter is not None and query.text:
            try:
                alt = tuple(self.query_rewriter.rewrite(query.text) or ())
                self._last_alt_texts = alt
            except Exception:  # noqa: BLE001 改写失败回退原文检索
                self._last_alt_texts = ()
        hits = recall(self.store, self.config, query, filters,
                      # reranker 生效时多取候选到 rerank_top_n：否则 top-k
                      # 截断在重排之前发生，重排只能改变组内次序（0.8.1 修复
                      # reranker 对 hit@k 集合无影响的问题）
                      max(k, rerank_top_n) if (self.reranker and query.text)
                      else k,
                      mode, self.clock(), embedder=self.embedder,
                      max_hops=max_hops, tol_days=tol_days,
                      time_backend=self.time_backend,
                      graph_backend=self.graph_backend,
                      alt_texts=self._last_alt_texts or None)
        return self._maybe_rerank(query, hits, k, rerank_top_n)

    def recall_context(self, query, query_type=None, query_time=None,
                       include_history=False, include_beliefs=True,
                       include_evidence=True):
        """0.5.0 记忆 Runtime 入口（设计稿 §7.2/§16.1）。

        召回候选 → 四类扩展 → 状态解析（确定性）→ 时间链 → 证据校验
        → 跨维度一致性 → 结构化 MemoryContext。`recall()`/`MemoryHit`
        行为零变化；MemoryScore 分项仅在本管线计算。
        """
        from .coherence import CrossDimensionCoherence
        from .context import ContextBuilder, expand, validate
        from .models import MemoryScore
        from .resolve import MemoryStateResolver, query_router
        from .temporal import TemporalChainBuilder

        qt = query_type or query_router(query.text)
        now = query_time or self.clock()
        # 1) 候选召回（既有四路 + 访问控制兜底）
        hits = self.recall(query, k=40, mode="triple")
        nodes = {n.nid: n for n in self.store.fetch_nodes()}
        cand_nodes = [nodes[h.node_id] for h in hits
                      if h.node_id in nodes]
        # 1.5) Trigger 关联召回（0.7.0 P2，文档 §9）：触达路径命中
        # → 还原记忆对象与 descriptive 候选合并；Trigger 只负责
        # 「找到」，访问/生命周期裁决仍由后续层执行。
        trig_objs, n_trig = self._trigger_candidates(query.text)
        cand_nodes = list(cand_nodes)
        for obj in trig_objs:
            if getattr(obj, "node_type", None) is not None:
                if obj not in cand_nodes:
                    cand_nodes.append(obj)
        # 实体锚定进候选 + 主体偏好（0.8.2，docs §6.2 T5）：查询提及的
        # 节点名对应的节点保证进候选——k=40 的 RRF 竞争会把 person 锚挤
        # 出候选池，使其全部事实失去主体锚定（answer:profile/social 失败
        # 根因）；prefer_nids 让 current_state 预算优先给被问主体的事实，
        # 防多主体下同族事实灌榜淹没答案。person 名 ≥2 字即可，其它 kind
        # 须 ≥3 字防泛词误锚；最多 5 个（按名长降序，最具体优先）。
        q_text = (query.text or "").strip()
        prefer_nids = set()
        if q_text:
            mentioned = [n for n in nodes.values()
                         if n.name and n.name in q_text
                         and (len(n.name) >= 3
                              or (n.kind == "person" and len(n.name) >= 2))]
            mentioned.sort(key=lambda n: -len(n.name))
            mentioned = mentioned[:5]
            prefer_nids = {n.nid for n in mentioned}
            hit_nids = {h.node_id for h in hits}
            for n in mentioned:
                if n.nid in hit_nids:
                    continue
                hits.append(MemoryHit(
                    node_id=n.nid, node_type=n.node_type, name=n.name,
                    ts=n.ts, score=0.5, sources=("anchor",),
                    path_scores={"lexical": {"score": 0.5, "rank": 0}}))
                cand_nodes.append(n)
        # 2) 候选扩展（Entity/FactVersion/TemporalNeighbor/Evidence）
        expanded = expand(self.store, cand_nodes + trig_objs, limit=200)
        # 3) 状态解析（fact/belief/intent 确定性裁决）
        resolver = MemoryStateResolver(self.store, self.config)
        state = resolver.resolve(expanded, query_time=now, query_type=qt)
        # 历史/变化/时间线类查询自动携带历史（设计稿 §13.2）
        if not include_history and qt not in ("history", "change",
                                              "why_change", "timeline"):
            state.historical_facts = []
        if not include_beliefs:
            state.beliefs, state.changes = [], []
        # 3.5) 查询在场地：只收推理类区块（causes/impacts/impact_chains/
        # patterns）。状态层（fact/belief/intent）保持全库确定性裁决——
        # 「我现在住哪里」这类问句与事实无词面重叠，靠相关性收口会把
        # 正确答案一起滤掉（docs §30）。
        notes = []
        if state.degraded:
            dims = "、".join(sorted({d["dimension"] for d in state.degraded}))
            notes.append(
                f"部分维度解析失败（{dims}），本次上下文不完整；"
                f"首个错误：{state.degraded[0]['error']}")
        nids, scope_fids, scope_events = self._query_scope(
            state, hits, trig_objs, query.text, qt)
        if not nids:
            notes.append("未命中相关记忆：下面是库中当前状态，"
                         "不是对本次问题的回答")
        # 时间锚点落空明示（0.8.2）：查询含时间表达（显式时间/「上周」等
        # 已被识别）但时间路零命中时，沉默地退回其它路径会让用户以为
        # 「上周的记忆就是这些」——必须显式告知时间窗内无记忆
        else:
            from .retrieval import _extract_monthday, _extract_query_time
            has_time_anchor = bool(query.time) \
                or _extract_query_time(query.text, now) is not None \
                or _extract_monthday(query.text) is not None
            time_hit = any("time" in (h.path_scores or {}) for h in hits)
            if has_time_anchor and not time_hit:
                notes.append("查询涉及的时间范围内没有记忆"
                             "（时间表达已识别，但该时间窗内无事件）")
        state.impacts = [i for i in state.impacts
                         if i.subject_id in nids]
        state.patterns = [p for p in state.patterns
                          if p.subject_id in nids]
        # 4) 时间链（links + edges 参与关系推导）。无在场地对象时不做链推导：
        # 否则会拿状态层的全库事实（147 条）拼出一条与问题无关的巨链
        builder = TemporalChainBuilder(self.config)
        flat = state.current_facts + state.historical_facts + state.events
        chains = builder.build(flat, links=self.store.fetch_memory_links(),
                               edges=self.store.fetch_edges()) if nids else []
        # 5) 证据校验：inferred 不得以 Fact 身份进 current_state
        all_ids = set()
        for m in flat + state.beliefs + state.intents + state.impacts:
            for eid in (getattr(m, "evidence_ids", None) or []):
                all_ids.add(eid)
        ev_rows = self.store.get_evidence(list(all_ids))
        # 证据访问控制：sensitive 证据默认不进上下文（设计稿 §11.2 可访问性）
        from .context import _evidence_allowed
        from .models import RecallFilters as _RF
        ev_rows = [e for e in ev_rows if _evidence_allowed(e, _RF())]
        by_id = {e.id: e for e in ev_rows}
        grounded_current = []
        for f in state.current_facts:
            evs = [by_id[e] for e in (f.evidence_ids or []) if e in by_id]
            v = validate(f, evs, config=self.config)
            if v.inferred:
                state.historical_facts.append(f)
                notes.append(
                    f"fact {f.fid}（{f.key}={f.value}）为推断记忆，"
                    "已移出当前状态")
                continue
            # 证据校验的降置信必须落地：原实现只读 v.inferred，把
            # 「无证据 ×0.6 / 不可信 ×0.85」整段算完就丢掉，于是无证据
            # 事实在 current_state 里与有证据事实完全等价
            if v.confidence < f.confidence:
                # copy-on-write（0.8.2）：fetch_facts 走快照缓存，版本内
                # 对象跨查询共享，不得原地改写
                f = replace(f, confidence=v.confidence)
            grounded_current.append(f)
        state.current_facts = grounded_current
        # 变化类查询的因果/abstain 判定放在 causes 区块填充之后（见下）
        cause_links = [l for l in self.store.fetch_memory_links()
                       if l.relation == "caused_by"]
        if include_evidence:
            state.evidence = ev_rows
        # 6) 一致性
        coherence = CrossDimensionCoherence(self.config)
        coh = coherence.check(state)
        # 0.8.1 IA-4：一致性解释（中文说明）追加进 notes——原先生成后即
        # 丢弃，调用方看不到「为什么判冲突」；去重并限 3 条防爆量。
        # 0.8.2：只收查询在场地的冲突解释——全库一致性检查会对无关主体
        # 报冲突，原样透出会把「实体 7253 的 award 并列」这类噪声塞进
        # 无关问题的响应
        added_expl = 0
        for c, expl in zip(coh.conflicts or [], coh.explanations or []):
            if added_expl >= 3:
                break
            c_nid = c.get("node_id") if isinstance(c, dict) \
                else getattr(c, "node_id", None)
            if c_nid is not None and nids and c_nid not in nids:
                continue
            if expl and expl not in notes:
                # 裸节点 id 换成节点名（「实体 7274」→「张小红」），
                # 并列值去重显示（LLM 层重复事实会打出 打篮球×2 式噪声）
                cn = nodes.get(c_nid)
                if cn is not None and cn.name:
                    expl = expl.replace(f"实体 {c_nid}", f"「{cn.name}」", 1)
                notes.append(expl)
                added_expl += 1
        # 7) 上下文组装（current_state 按状态层分项分排序；
        #    0.7.0 S0-02：分项含查询相关性 → 相关事实置顶）
        from .context import _mem_key
        hit_by_nid = {}
        for h in hits:
            if h.node_id is None:
                continue
            # 相关性锚定用词面路原始分（0.15~1.0 有区分度）；RRF 融合分
            # 量级 ~0.01，会被 _relevance 的 key 族/value 项彻底淹没
            lex = (h.path_scores or {}).get("lexical") or {}
            sc = lex.get("score", h.score)
            hit_by_nid[h.node_id] = max(hit_by_nid.get(h.node_id, 0.0), sc)
        scored = {}
        # 身份/冲突判定预算成集合（0.8.2 性能）：原先每条事实都全扫
        # current/historical 列表与 conflicts 列表——规模库单次查询
        # 内层循环达千万次
        validity_ids = ({id(f) for f in state.current_facts},
                        {id(f) for f in state.historical_facts},
                        {_conflict_key(c) for c in state.conflicts})
        for f in state.current_facts + state.historical_facts:
            scored[_mem_key(f)] = self._memory_score(
                f, state, coh, hit_by_nid, query_text=query.text,
                validity_ids=validity_ids)
        ctx = ContextBuilder(self.config).build(
            state, query_type=qt, chains=chains, notes=notes, scores=scored,
            prefer_nids=prefer_nids)
        # 0.8.1 IA-4：单记忆分项分透出（key=str(fid)，口径见 MemoryContext
        # docstring），序列化层按 fid 匹配到 current_state 各项
        ctx.score_by_item = {str(k[1]): sc for k, sc in scored.items()}
        # 影响链区块（0.7.0 P2，文档 §8.2）：Event→Impact→Belief→Intent。
        # 链上可能涉及历史观点/意图（当前观点会被 BeliefResolver 挤出
        # current），从 caused_by 链两端按 source_dim 收集补齐——补进来的
        # 观点/意图仍须属于本次在场地（否则全库扫描会把无关对象拖进链里）。
        from .impact_chain import ImpactChainBuilder
        _links_all = self.store.fetch_memory_links()
        _beliefs_all = {b.id: b for b in self.store.fetch_beliefs()}
        _intents_all = {i.id: i for i in self.store.fetch_intents()}
        _extra = []
        for _l in _links_all:
            if _l.relation != "caused_by":
                continue
            _dim = getattr(_l, "source_dim", "") or _l.source_type
            if _dim == "belief" and _l.source_id in _beliefs_all:
                _extra.append(_beliefs_all[_l.source_id])
            elif _dim == "intent" and _l.source_id in _intents_all:
                _extra.append(_intents_all[_l.source_id])
            if _l.source_type == "belief" and _l.target_id in _beliefs_all:
                _extra.append(_beliefs_all[_l.target_id])
            elif _l.source_type == "intent" and _l.target_id in _intents_all:
                _extra.append(_intents_all[_l.target_id])
        _extra = [m for m in _extra
                  if getattr(m, "subject_id", None) in nids]
        # 记忆集合取「本次上下文实际展示的内容」：链条解释的是展示出来的
        # 事实/事件，不是全库（否则 503 条全库因果会整批进链）
        ic_memories = (list(ctx.current_state) + list(ctx.historical_changes)
                       + list(ctx.recent_events) + state.impacts
                       + [b for b in state.beliefs if b.subject_id in nids]
                       + [i for i in state.intents if i.subject_id in nids]
                       + _extra)
        ic_chains = ImpactChainBuilder(self.config).build(
            ic_memories, links=_links_all) if nids else []
        # 单点「链」不是链：无在场地对象时会从状态层事实拼出无意义的一点链
        ic_chains = [ch for ch in ic_chains if len(ch.nodes) >= 2]
        ctx.impact_chains = ic_chains[:ContextBuilder.DEFAULT_BUDGETS[
            "impact_chains"]]
        # 因果链区块（规则推导已落库的 caused_by；§10/0.4.1 Change Reason）：
        # 只解释本次上下文里**展示出来的事实**（或候选带出的事件），并受
        # causes 预算约束——原实现取全库 caused_by（实测 503 条无上限）
        if cause_links and nids:
            ev_nodes = {n.nid: n for n in self.store.fetch_nodes()}
            facts_by_id = {f.fid: f for f in self.store.fetch_facts()}
            shown_fids = {f.fid for f in ctx.current_state} \
                | {f.fid for f in ctx.historical_changes}
            rows = [
                {"cause": ev_nodes.get(l.source_id),
                 "caused": facts_by_id.get(l.target_id),
                 "confidence": l.confidence}
                for l in cause_links
                if l.source_id in ev_nodes and l.target_id in facts_by_id
                and (l.target_id in shown_fids
                     or l.source_id in scope_events)
            ]
            rows.sort(key=lambda r: (-(r["confidence"] or 0.0),
                                     r["caused"].fid))
            ctx.causes = rows[:ContextBuilder.DEFAULT_BUDGETS["causes"]]
        # 变化类查询：本次上下文里没有可解释的因果且无证据 → abstain
        # （不编造原因，§8 案例 E）
        if qt in ("why_change", "change") and not ctx.causes:
            relevant = state.historical_facts + list(state.events)
            grounded = any((getattr(m, "evidence_ids", None) or [])
                           for m in relevant)
            if relevant and not grounded:
                ctx.notes.append("已确认变化事实；未发现足够证据证明变化原因")
        # 8) 分项评分（仅本管线）
        ctx.score = self._state_score(ctx, coh, hits)
        # 9) 可观测性 Trace（§26，仅内存对象不持久化）
        from .models import RecallTrace
        ctx.trace = RecallTrace(
            query=query.text or "",
            query_type=qt,
            retrieval_mode="triple",
            candidate_count=len(hits),
            rrf_candidates=len(hits),
            resolved_count=(len(state.current_facts)
                            + len(state.historical_facts)
                            + len(state.events)),
            current_state_count=len(state.current_facts),
            history_count=len(state.historical_facts),
            belief_count=len(state.beliefs),
            chain_count=len(chains),
            conflict_count=len(coh.conflicts),
            evidence_count=len(ev_rows),
            # 最终上下文条数：含全部 16 区块（原实现漏了 causes/impacts/
            # impact_chains/patterns/temporal_chains，503 条膨胀看不出来）
            final_context_count=(len(ctx.current_state)
                                 + len(ctx.historical_changes)
                                 + len(ctx.recent_events)
                                 + len(ctx.beliefs) + len(ctx.intents)
                                 + len(ctx.evidence) + len(ctx.causes)
                                 + len(ctx.impacts)
                                 + len(ctx.impact_chains)
                                 + len(ctx.patterns)
                                 + len(ctx.temporal_chains)),
            impact_count=len(state.impacts),
            impact_chain_count=len(getattr(ctx, "impact_chains", [])),
            trigger_count=n_trig, pattern_count=len(state.patterns),
            degraded_count=len(state.degraded),
            chain_node_count=sum(len(ch.nodes)
                                 for ch in (ctx.temporal_chains or [])),
            llm_used=bool(self._last_alt_texts), fallback_used=False)
        return ctx

    @staticmethod
    def _bigram_overlap(a, b):
        """字符 bigram 重叠率（中文无需分词；任一为空返回 0）。"""
        a, b = (a or "").strip(), (b or "").strip()
        if not a or not b:
            return 0.0
        if a in b or b in a:
            return 1.0
        ba, bb = _bigram_set(a), _bigram_set(b)
        if not ba or not bb:
            return 0.0
        return len(ba & bb) / min(len(ba), len(bb))

    def _trigger_candidates(self, query_text):
        """0.7.0 P2：trigger 确定性命中 → 还原记忆对象。

        精确子串优先；否则 bigram 重叠率 ≥ trigger_match_threshold
        （单字 trigger 只精确）。命中对象仍须过访问/生命周期裁决
        （调用方后续层执行），此处不旁路。返回 (对象列表, 命中数)。
        """
        from .retrieval import _access_allowed
        from .models import RecallFilters as _RF
        q = (query_text or "").strip()
        if not q:
            return [], 0
        filters = _RF()
        matched = []
        for t in self.store.fetch_triggers():
            text = t.text
            hit = text in q or q in text
            if not hit and len(text) >= 2 and len(q) >= 2:
                hit = self._bigram_overlap(q, text) \
                    >= self.config.trigger_match_threshold
            if not hit:
                continue
            obj = None
            if t.memory_type == "event":
                for n in self.store.fetch_nodes():
                    if n.nid == t.memory_id and n.node_type == "event" \
                            and _access_allowed(n, filters):
                        obj = n
                        break
            elif t.memory_type == "fact":
                for f in self.store.fetch_facts():
                    if f.fid == t.memory_id and not f.tombstoned:
                        obj = f
                        break
            elif t.memory_type == "belief":
                for b in self.store.fetch_beliefs():
                    if b.id == t.memory_id and b.lifecycle == "active":
                        obj = b
                        break
            elif t.memory_type == "intent":
                for i in self.store.fetch_intents():
                    if i.id == t.memory_id and i.lifecycle == "active":
                        obj = i
                        break
            elif t.memory_type == "impact":
                for im in self.store.fetch_impacts():
                    if im.id == t.memory_id and im.lifecycle == "active":
                        obj = im
                        break
            if obj is not None:
                matched.append(obj)
        return matched, len(matched)

    def _key_in_family(self, key, canonical):
        """查询侧族成员判定（0.8.2）：canonical 本名 / 裁决级同义
        （key_aliases）/ 查询级成员（key_query_aliases）/「族_实例」
        前缀（如 关系_李明 → 关系族，此前缀规则仅查询侧生效）。"""
        from .resolve import canonical_fact_key
        if key == canonical:
            return True
        if canonical_fact_key(key, self.config) == canonical:
            return True
        qa = self.config.key_query_aliases.get(canonical, ())
        if key in qa:
            return True
        if isinstance(key, str) and "_" in key:
            prefix = key.split("_", 1)[0]
            if prefix == canonical or prefix in qa:
                return True
        return False

    def _query_families(self, q):
        """查询文本命中的 key 族集（按查询串缓存：原先每条事实都重扫
        全部族 × 族词 any()，规模库单次查询 18052 次调用、内层 genexpr
        逾百万次）。"""
        cache = getattr(self, "_qf_cache", None)
        if cache is not None and cache[0] == q:
            return cache[1]
        fams = frozenset(
            canonical for canonical, words
            in self.config.key_query_words.items()
            if any(w in q for w in words))
        self._qf_cache = (q, fams)
        return fams

    def _relevance(self, m, hit_scores, query_text=None):
        """查询相关性（S0-02 口径，唯一实现）：

        无族命中时：max(实体命中分, 0.6×value bigram 重叠, key 弱信号)。
        0.8.2 族命中分层：主体锚定（节点命中分>0）+ key 族/直配命中
        = 1.0 顶格（「问的就是这个主体的这个属性」）；仅族命中 = 0.3
        弱相关——多主体库下同族事实成百上千，顶格灌榜会把真正被问的
        主体的事实挤出预算（PerLTQA answer:profile/social 失败根因）。
        既用于 MemoryScore 的 retrieval 分项，也用于推理类区块的收口
        判据。
        """
        relevance = hit_scores.get(getattr(m, "node_id", None), 0.0)
        key = getattr(m, "key", None)
        if not query_text:
            return relevance
        q = query_text.strip()
        # key 族查询词命中（如 query 含「住」→ 位置族事实）；0.8.2 族
        # 成员判定走查询侧（key_query_aliases + 「族_实例」前缀），
        # 不影响裁决分组（canonical_fact_key 只并真同义 key）；命中族
        # 集按查询预算（_query_families），不逐事实重扫族词表
        qk = 0.0
        if key is not None:
            for canonical in self._query_families(q):
                if self._key_in_family(key, canonical):
                    qk = 1.0
                    break
        # key 直配（0.8.1）：查询含 key 原文（≥2 字，「饮食偏好是什么」）
        # ；否则按 key 与查询的 bigram 重叠给弱信号——单字 key 不
        # 直配（任意含该字的查询都会误命中）
        if key:
            k = str(key)
            if len(k) >= 2 and k.lower() in q.lower():
                qk = 1.0
            else:
                qk = max(qk, 0.8 * self._bigram_overlap(q, k))
        value_term = 0.6 * self._bigram_overlap(q, getattr(m, "value", ""))
        if qk >= 1.0:
            return 1.0 if relevance > 0 else max(0.3, value_term)
        return max(relevance, qk, value_term)

    def _query_scope(self, state, hits, trig_objs, query_text, query_type):
        """本次查询的「在场地」（0.8.0）：只给推理类区块收口用。

        组成：检索/Trigger 命中的节点与主体 + 与查询词面相关的事实 +
        状态类问题（现在/以前/为什么变）下上下文事实所属的主体。返回
        (node_ids, fact_ids, event_ids)。

        原则（docs/系统设计与架构设计.md §30「相关性不等于状态」）：
        相关性只用来收窄 causes/impacts/impact_chains/patterns 这类
        **解释性** 区块，绝不用来决定谁当前有效——状态层仍全库裁决。
        """
        hit_scores = {}
        for h in hits:
            if h.node_id is not None:
                hit_scores[h.node_id] = max(
                    hit_scores.get(h.node_id, 0.0), h.score)
        nids, fids, events = set(), set(), set()
        nids.update(hit_scores)
        for o in trig_objs:
            if getattr(o, "node_type", None) is not None:    # event 节点
                nids.add(getattr(o, "nid", None))
                events.add(getattr(o, "nid", None))
            else:                                            # fact/belief/…
                nids.add(getattr(o, "subject_id", None))
                if getattr(o, "fid", None) is not None:
                    fids.add(o.fid)
                    nids.add(getattr(o, "node_id", None))
        facts = state.current_facts + state.historical_facts
        for f in facts:
            if self._relevance(f, hit_scores, query_text) > 0:
                fids.add(f.fid)
                nids.add(f.node_id)
        # 状态类问题问的是主体本身 → 主体的状态面与上下文事实一体在场
        if query_type in ("current_state", "history", "timeline",
                          "change", "why_change"):
            for f in facts:
                fids.add(f.fid)
                nids.add(f.node_id)
        for n in state.events:
            events.add(getattr(n, "nid", None))
            nids.add(getattr(n, "nid", None))
        return nids - {None}, fids, events - {None}

    def _memory_score(self, m, state, coh, hit_scores, query_text=None,
                      validity_ids=None):
        """单记忆分项评分（设计稿 §23）：retrieval/validity/source/
        evidence/coherence/conflict_penalty，final 供排序使用。

        0.7.0 S0-02：retrieval_score = 查询相关性（见 _relevance）；
        无查询文本且无命中时退化为 0（0.6.0 排序不变）。
        validity_ids：可选 (current_ids, historical_ids[, conflict_keys])
        预建集合三元组，缺省时按列表/逐项扫描（兼容直接调用方）。
        """
        from .models import MemoryScore
        nid = getattr(m, "node_id", None)
        key = getattr(m, "key", None)
        if validity_ids is None:
            validity_ids = ({id(f) for f in state.current_facts},
                            {id(f) for f in state.historical_facts},
                            {_conflict_key(c) for c in state.conflicts})
        if len(validity_ids) > 2 and validity_ids[2] is not None:
            in_conflict = (nid, key) in validity_ids[2]
        else:
            in_conflict = any(_conflict_key(c) == (nid, key)
                              for c in state.conflicts)
        is_current = id(m) in validity_ids[0]
        is_history = id(m) in validity_ids[1]
        validity = 1.0 if is_current else (0.5 if is_history else 0.0)
        source = self.config.source_rank.get(getattr(m, "source", ""), 0)
        relevance = self._relevance(m, hit_scores, query_text)
        return MemoryScore(
            retrieval_score=round(min(1.0, relevance), 4),
            temporal_score=0.0,
            validity_score=validity,
            source_score=min(1.0, round(source / 5.0, 4)),
            evidence_score=1.0 if getattr(m, "evidence_ids", None) else 0.0,
            coherence_score=1.0 if coh.consistent else 0.0,
            conflict_penalty=1.0 if in_conflict else 0.0)

    def _state_score(self, ctx, coh, hits):
        from .models import MemoryScore
        mems = (ctx.current_state + ctx.historical_changes
                + ctx.beliefs + ctx.intents)
        retr = (sum(h.score for h in hits) / len(hits)) if hits else 0.0
        total = len(mems)
        grounded = sum(1 for m in mems
                       if getattr(m, "evidence_ids", None))
        source = sum(
            self.config.source_rank.get(getattr(m, "source", ""), 0)
            for m in mems) / (3.0 * max(total, 1))
        return MemoryScore(
            retrieval_score=round(retr, 4),
            temporal_score=1.0 if ctx.temporal_chains else 0.0,
            # 两个分项同源：都按**本次上下文里实际展示的冲突组**算
            # （原先 validity 用 ctx.conflicts、penalty 用 coh.conflicts，
            # 口径不一致；罚分再封顶 1.0，与其它分项同量纲，避免真冲突
            # 一多（lccc 实测 6 组）把总分压成负数）
            validity_score=max(0.0, 1.0 - 0.2 * len(ctx.conflicts)),
            source_score=round(min(1.0, source), 4),
            evidence_score=round(grounded / total, 4) if total else 0.0,
            coherence_score=1.0 if coh.consistent else 0.0,
            conflict_penalty=min(1.0, 0.2 * len(ctx.conflicts)))

    def _maybe_rerank(self, query, hits, k, rerank_top_n):
        """可选重排：失败/异常一律回退原排序，绝不阻断召回。"""
        reranker = self.reranker
        if reranker is None or not query.text or not hits \
                or rerank_top_n <= k:
            return hits
        candidates = hits[:rerank_top_n]
        try:
            by_id = {n.nid: n for n in self.store.fetch_nodes()}
            texts = [by_id[h.node_id].name if h.node_id in by_id else h.name
                     for h in candidates]
            scores = reranker.rerank(query.text, texts)
        except Exception:  # noqa: BLE001 重排失败回退
            return hits
        if len(scores) != len(candidates):
            return hits
        scored = []
        for rank, (hit, raw) in enumerate(zip(candidates, scores)):
            try:
                score = float(raw)
            except (TypeError, ValueError):
                score = float("-inf")
            if math.isnan(score) or math.isinf(score):
                score = float("-inf")
            scored.append((score, rank, hit))
        scored.sort(key=lambda x: (-x[0], x[1], x[2].node_id))
        return [item[2] for item in scored[:k]]

    @staticmethod
    def _fuzzy_entity_id(local, name, threshold=0.88):
        """写入端点消解：精确匹配失败时按相似度复用既有实体。"""
        if not name:
            return None
        n = str(name).strip()
        if not n:
            return None
        if n in local:
            return local[n]
        best = None
        best_ratio = 0.0
        for existing in local:
            ratio = difflib.SequenceMatcher(None, n, existing).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = existing
        if best is not None and best_ratio >= threshold:
            return local[best]
        return None

    def resolve_entities(self, merge_similar=False, min_similarity=0.85,
                         force=False):
        """合并重复实体（默认仅同名）；返回合并数量。

        - 保留最低 id 节点；边/事实/**状态行**重挂；重复节点墓碑 + 审计；
        - 不同 access_label 的同名实体不合并；
        - `protected` 实体需要 `force=True` 才合并（与 forget 一致）；
        - 重挂后出现自边时该边 tombstoned；
        - 全程单事务，部分失败整体回滚。
        """
        def norm(name):
            return " ".join(str(name).strip().split())

        entities = [n for n in self.store.fetch_nodes()
                    if n.node_type == "entity"]
        groups = {}
        for n in entities:
            groups.setdefault(norm(n.name), []).append(n)

        if merge_similar:
            names = list(groups)
            parent = {name: name for name in names}

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    if difflib.SequenceMatcher(
                            None, names[i], names[j]).ratio() >= min_similarity:
                        union(names[i], names[j])
            merged_groups = {}
            for name in names:
                root = find(name)
                merged_groups.setdefault(root, []).extend(groups[name])
            groups = merged_groups

        now = self.clock()
        merged = 0
        with self.store.transaction() as conn:
            for members in groups.values():
                if len(members) < 2:
                    continue
                members.sort(key=lambda n: n.nid)
                canonical = members[0]
                for dup in members[1:]:
                    if dup.access_label != canonical.access_label:
                        continue  # 保守：不同敏感级不合并
                    if dup.protected and not force:
                        # 与 forget 一致：protected 节点不得被静默删除
                        continue
                    self._merge_entity(dup, canonical, now, conn)
                    merged += 1
        # 重建名字索引时必须排除墓碑节点：否则同名时「id 大者胜」会让
        # `_resolve()` 指向刚被墓碑的 dup，后续 add_fact 挂上去静默丢失
        self._name2id = {
            n.name: n.nid for n in self.store.fetch_nodes()
            if n.lifecycle not in ("deleted", "tombstoned")}
        return merged

    def _merge_entity(self, dup, canonical, now, conn):
        for e in self.store.fetch_edges():
            if e.lifecycle != "active":
                continue
            if e.from_id == dup.nid or e.to_id == dup.nid:
                self.store.rewire_edge(e.eid, dup.nid, canonical.nid,
                                       conn=conn)
                row = conn.execute(
                    "SELECT from_id, to_id FROM edges WHERE id=?",
                    (e.eid,)).fetchone()
                if row and row[0] == row[1]:
                    self.store.update_edge_lifecycle(
                        e.eid, "tombstoned", now, conn=conn)
        for f in self.store.fetch_facts():
            if f.node_id == dup.nid and not f.tombstoned:
                self.store.rewire_fact(f.fid, dup.nid, canonical.nid,
                                       conn=conn)
        # 状态行（观点/意图/影响/模式）也要重挂：只重挂 edges/facts 的话
        # 它们会指向被墓碑的 dup，成为检索不到的孤儿
        self.store.rewire_subject_rows(dup.nid, canonical.nid, conn=conn)
        self.store.update_lifecycle(dup.nid, "tombstoned", now, conn=conn)
        self.store.add_tombstone("node", dup.nid, "entity_merge", now,
                                 conn=conn)

    def neighbors(self, node_name, rel=None):
        return neighbors(self.store, node_name, rel, self.clock())

    def fact_lookup(self, entity_name, key):
        from .resolve import canonical_fact_key
        nid = self._name2id.get(entity_name)
        if nid is None:
            return []
        now = self.clock()
        node = next((n for n in self.store.fetch_nodes() if n.nid == nid), None)
        if node is None or node.lifecycle in ("tombstoned", "deleted"):
            return []
        ckey = canonical_fact_key(key, self.config)
        return [f for f in self.store.fetch_facts()
                if f.node_id == nid
                and canonical_fact_key(f.key, self.config) == ckey
                and not f.tombstoned
                and (f.invalid_at is None or f.invalid_at > now)]

    def fact_history(self, entity_name, key):
        """事实版本链：同一 (entity, canonical_key) 的全部事实，按
        valid_at 升序（0.7.0 起同义 key 归一化视角）。

        updates_to 收敛链由 facts.superseded_by 表达。
        """
        from .resolve import canonical_fact_key
        nid = self._name2id.get(entity_name)
        if nid is None:
            return []
        ckey = canonical_fact_key(key, self.config)
        facts = [f for f in self.store.fetch_facts()
                 if f.node_id == nid
                 and canonical_fact_key(f.key, self.config) == ckey]
        facts.sort(key=lambda f: (f.valid_at, f.fid))
        return facts

    # ---------------- 记忆历史 / 时间线 / 解释（0.5.0） ----------------
    def memory_history(self, entity_id, dimension="fact"):
        """按维度回实体历史（设计稿 §16.2）。

        fact/belief/intent/event 各自按时间序返回完整序列。
        """
        nid = self._resolve(entity_id)
        now = self.clock()
        if dimension == "fact":
            facts = [f for f in self.store.fetch_facts()
                     if f.node_id == nid and not f.tombstoned]
            facts.sort(key=lambda f: (f.valid_at, f.fid))
            return facts
        if dimension in ("belief", "intent"):
            from .resolve import BeliefResolver, IntentResolver
            res = (BeliefResolver(self.config) if dimension == "belief"
                   else IntentResolver(self.config)).resolve(self.store, now)
            return [m for m in res["history"] + res["current"]
                    if m.subject_id == nid]
        if dimension == "event":
            nodes = {n.nid: n for n in self.store.fetch_nodes()}
            events = []
            for e in self.store.fetch_edges():
                other = None
                if e.from_id == nid:
                    other = nodes.get(e.to_id)
                elif e.to_id == nid:
                    other = nodes.get(e.from_id)
                if other is not None and other.node_type == "event":
                    events.append(other)
            events.sort(key=lambda n: (n.ts or datetime.min, n.nid))
            return events
        raise ValidationError(f"E001 未知维度: {dimension}")

    def timeline(self, entity_id=None, start=None, end=None, kinds=None):
        """实体时间线：关联事件 + 事实版本，按稳定时间排序成链（§16.3）。"""
        from .temporal import TemporalChainBuilder
        nid = self._resolve(entity_id) if entity_id else None
        items = []
        nodes = {n.nid: n for n in self.store.fetch_nodes()}
        if nid is not None:
            for e in self.store.fetch_edges():
                other = None
                if e.from_id == nid:
                    other = nodes.get(e.to_id)
                elif e.to_id == nid:
                    other = nodes.get(e.from_id)
                if other is not None and other.node_type == "event":
                    items.append(other)
            for f in self.store.fetch_facts():
                if f.node_id == nid and not f.tombstoned:
                    items.append(f)
        else:
            items = [n for n in nodes.values() if n.node_type == "event"]
        if start or end:
            kept = []
            for it in items:
                t = getattr(it, "ts", None) or getattr(it, "valid_at", None)
                if t is None:
                    continue
                if start and t < start:
                    continue
                if end and t > end:
                    continue
                kept.append(it)
            items = kept
        chains = TemporalChainBuilder(self.config).build(
            items, links=self.store.fetch_memory_links(),
            edges=self.store.fetch_edges())
        return chains[0].nodes if chains else []

    def explain(self, memory_id, kind=None):
        """解释一条记忆：Memory + Evidence + Version + Source + Related
        + Audits（设计稿 §16.4；0.8.1 IA-4 扩展）。无证据 → E013。

        kind：node/fact/belief/intent，跨表 id 空间重叠时用于消歧。
        versions 口径：
        - node：versions 表快照行（(version, content, created_at) 元组）；
        - fact：同 (node_id, canonical key) 全部事实版本按时间升序
          （与 fact_history 同口径，Fact 对象列表）；
        - belief/intent：同主体演化时间线（复用 BeliefResolver /
          IntentResolver 的 history+current，不造新表）。
        related 各项带 confidence（memory_links 行字段）；
        audits 为该对象的 audit_log 记录（最新在前）。
        """
        target, kind = self._locate_memory(memory_id, kind=kind)
        eids = getattr(target, "evidence_ids", None) or []
        if not eids:
            raise EvidenceNotFoundError(
                "E013 该记忆没有绑定证据，无法解释来源")
        # 证据访问控制：与 recall_context 同一口径（sensitive 默认隐藏，
        # 设计稿 §11.2）。原实现直接返回，解释接口会吐出敏感证据行
        from .context import _evidence_allowed
        from .models import RecallFilters
        evidence = [e for e in self.store.get_evidence(eids)
                    if _evidence_allowed(e, RecallFilters())]
        versions = self._explain_versions(target, kind, memory_id)
        related = []
        for l in self.store.memory_links_of(memory_id):
            if l.target_id == memory_id:
                related.append({"relation": l.relation,
                                "other_id": l.source_id,
                                "source_type": l.source_type,
                                "confidence": l.confidence})
            elif kind != "node":
                related.append({"relation": l.relation,
                                "other_id": l.target_id,
                                "source_type": l.source_type,
                                "confidence": l.confidence})
        audits = self.store.fetch_audit_logs(target_type=kind,
                                             target_id=memory_id)
        return {"memory": target, "evidence": evidence,
                "versions": versions, "source": getattr(target, "source", ""),
                "related": related, "audits": audits}

    def _explain_versions(self, target, kind, memory_id):
        """explain() 的版本链装配（各 kind 口径见 explain docstring）。"""
        if kind == "node":
            return self.store.fetch_versions(memory_id)
        if kind == "fact":
            from .resolve import canonical_fact_key
            ckey = canonical_fact_key(target.key, self.config)
            facts = [f for f in self.store.fetch_facts()
                     if f.node_id == target.node_id
                     and canonical_fact_key(f.key, self.config) == ckey]
            facts.sort(key=lambda f: (f.valid_at or datetime.min, f.fid))
            return facts
        now = self.clock()
        if kind == "belief":
            from .resolve import BeliefResolver
            res = BeliefResolver(self.config).resolve(self.store, now)
            chain = [b for b in res["history"] + res["current"]
                     if b.subject_id == target.subject_id]
            chain.sort(key=lambda b: (b.valid_at or b.created_at
                                      or datetime.min, b.id))
            return chain
        from .resolve import IntentResolver
        res = IntentResolver(self.config).resolve(self.store, now)
        chain = [i for i in res["history"] + res["current"]
                 if i.subject_id == target.subject_id]
        chain.sort(key=lambda i: (i.valid_at or i.created_at
                                  or datetime.min, i.id))
        return chain

    def _locate_memory(self, memory_id, kind=None):
        """定位记忆对象。

        已删除/墓碑对象及其下属状态行一律不可定位：否则 `explain()` 会讲出
        一个「已被合规删除」的对象（连带它的证据与版本），与删除承诺矛盾。
        """
        if kind not in (None, "node", "fact", "belief", "intent"):
            raise ValidationError(f"E001 未知 kind: {kind}")
        nodes = self.store.fetch_nodes()
        hidden = {n.nid for n in nodes
                  if n.lifecycle in ("deleted", "tombstoned")}
        if kind in (None, "node"):
            for n in nodes:
                if n.nid == memory_id and n.nid not in hidden:
                    return n, "node"
        if kind in (None, "fact"):
            for f in self.store.fetch_facts():
                if f.fid == memory_id and not f.tombstoned \
                        and f.node_id not in hidden:
                    return f, "fact"
        if kind in (None, "belief"):
            for b in self.store.fetch_beliefs():
                if b.id == memory_id and b.lifecycle == "active" \
                        and b.subject_id not in hidden:
                    return b, "belief"
        if kind in (None, "intent"):
            for i in self.store.fetch_intents():
                if i.id == memory_id and i.lifecycle == "active" \
                        and i.subject_id not in hidden:
                    return i, "intent"
        raise NotFoundError("E006 目标不存在")

    # ---------------- 生命周期 ----------------
    def step_day(self):
        return governance.step_day(self.store, self.config, self.clock())

    def access(self, node_id):
        return governance.access(self.store, self.config, node_id, self.clock())

    def forget(self, target, reason, force=False):
        return governance.forget(self.store, self.config,
                                 self._resolve(target), reason, force,
                                 self.clock())

    def restore(self, node_id):
        return governance.restore(self.store, node_id, self.clock(),
                                  config=self.config)

    def version_of(self, node_id):
        return self.store.fetch_versions(node_id)

    # ---------------- 矛盾治理 ----------------
    def resolve_conflicts(self):
        return governance.resolve_conflicts(self.store, self.config,
                                            self.clock())

    def confirm(self, decision, choice_value):
        return governance.confirm(self.store, decision, choice_value,
                                  self.clock())

    # ---------------- 反射压缩 ----------------
    def reflect_monthly(self, year, month, extractor=None):
        return governance.reflect_monthly(
            self.store, self.config, year, month, self.clock(),
            extractor=extractor)

    def extract_patterns(self):
        """0.7.0 P3：手动触发个人模式提取（幂等；reflect 时也会挂接）。"""
        return governance.extract_patterns(self.store, self.config,
                                           now=self.clock())

    def compress_stable(self):
        """跨月稳定记忆聚合（0.6.0 R-02）：返回新生成的 stable 节点数。"""
        return governance.compress_stable(
            self.store, self.config, self.clock())

    # ---------------- 评测与关闭 ----------------
    def evaluate(self, dataset, k=5, mode="dual"):
        from .metrics import evaluate
        return evaluate(self, dataset, k=k, mode=mode)

    def close(self):
        for backend in (self.time_backend, self.graph_backend):
            close = getattr(backend, "close", None)
            if close is not None:
                close()
        self.store.close()
