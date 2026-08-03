# -*- coding: utf-8 -*-
"""MemorySystem 门面（公共 API v1）。"""
from __future__ import annotations

import difflib
import json
import math
from datetime import datetime

from . import governance
from .errors import EmbeddingError, NotFoundError, ValidationError
from .models import (ExtractedMemory, MemoryConfig, RecallFilters, RecallQuery,
                     WriteResult)
from .retrieval import neighbors, recall
from .store import SQLiteStore


class MemorySystem:
    def __init__(self, path=":memory:", config=None, clock=None,
                 embedder=None, extractor=None, judge=None, reranker=None):
        self.store = SQLiteStore(path)
        self.config = config or MemoryConfig()
        self.clock = clock or (lambda: datetime.now())
        self.embedder = embedder
        self.extractor = extractor
        self.judge = judge
        self.reranker = reranker
        self._name2id = {n.name: n.nid for n in self.store.fetch_nodes()}

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
                   idempotency_key=None):
        nid = self.store.insert_node(
            "entity", kind, name, description, None, 0.7, protected,
            access_label, 0.0, 0.0, self.clock(),
            idempotency_key=idempotency_key)
        self._name2id[name] = nid
        self._embed_and_store([(nid, f"{name} {description}".strip())])
        return nid

    def add_event(self, name, ts, kind="meeting", value_score=0.5,
                  protected=False, access_label="public", life=None,
                  decay_rate=None, idempotency_key=None):
        if life is None or decay_rate is None:
            d = self.config.event_defaults.get(
                kind, self.config.event_defaults["transient"])
            life = life if life is not None else d.life
            decay_rate = decay_rate if decay_rate is not None else d.decay_rate
        nid = self.store.insert_node(
            "event", kind, name, "", ts, value_score, protected,
            access_label, life, decay_rate, self.clock(),
            idempotency_key=idempotency_key)
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
                 valid_at=None, invalid_at=None, idempotency_key=None):
        if not (0 <= confidence <= 1):
            raise ValidationError("E002 数值域越界")
        now = self.clock()
        va = valid_at or now
        if invalid_at is not None and invalid_at <= va:
            raise ValidationError("E003 双时态顺序错误")
        return self.store.insert_fact(
            self._resolve(entity), key, value, source, confidence,
            va, now, invalid_at=invalid_at, idempotency_key=idempotency_key)

    def write_text(self, text, meta=None, extractor=None):
        ex = extractor or self.extractor
        if ex is None:
            raise ValidationError("E010 抽取器未注入")
        cands = ex.extract(text, meta or {})
        ids, dropped = self._write_candidates(cands, meta or {})
        return WriteResult(accepted=len(ids), rejected=dropped, ids=ids)

    def write_many(self, texts, meta=None, extractor=None, batch_size=20):
        """批量拆解写入：支持 Extractor.extract_many 分批调用，失败批次回滚。"""
        ex = extractor or self.extractor
        if ex is None:
            raise ValidationError("E010 抽取器未注入")
        if hasattr(ex, "extract_many"):
            batches = ex.extract_many(list(texts), meta or {}, batch_size=batch_size)
        else:
            batches = [ex.extract(t, meta or {}) for t in texts]
        total_ids, rejected = [], []
        for cands in batches:
            ids, dropped = self._write_candidates(cands, meta or {})
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
            if n.node_type == "entity":
                entity_local[n.name] = n.nid
        with self.store.transaction() as conn:
            meta = meta or {}
            rec = meta.get("recorded_at")
            if isinstance(rec, str):
                rec = datetime.fromisoformat(rec)
            elif not isinstance(rec, datetime):
                rec = self.clock()
            now = rec
            for c in cands:
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
                        now, idempotency_key=c.idempotency_key, conn=conn)
                    ids.append(nid)
                    local[c.name] = nid
                    embed_queue.append((nid, c.name))
                elif c.type == "entity":
                    nid = self.store.insert_node(
                        "entity", c.kind or "concept", c.name,
                        c.value or "", None, 0.7, c.protected, "public",
                        0.0, 0.0, now, idempotency_key=c.idempotency_key,
                        conn=conn)
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
                    ids.append(self.store.insert_fact(
                        nid, c.key, c.value, c.source, c.confidence,
                        c.ts or now, now,
                        idempotency_key=c.idempotency_key, conn=conn))
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
        # 刷新名称索引
        self._name2id.update(local)
        self._embed_and_store(embed_queue)
        return ids, dropped

    def _validate_candidate(self, c: ExtractedMemory):
        if c.type not in ("event", "entity", "fact", "edge"):
            raise ValidationError("E001 未知候选类型")
        if c.type == "edge":
            if c.rel not in self.config.relations:
                raise ValidationError("E001 未知关系类型")
            if c.from_ is None or c.to is None:
                raise ValidationError("E001 edge 缺少端点")

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
        hits = recall(self.store, self.config, query, filters, k, mode,
                      self.clock(), embedder=self.embedder,
                      max_hops=max_hops, tol_days=tol_days)
        return self._maybe_rerank(query, hits, k, rerank_top_n)

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

    def resolve_entities(self, merge_similar=False, min_similarity=0.85):
        """合并重复实体（默认仅同名）；返回合并数量。

        - 保留最低 id 节点；边/事实重挂；重复节点墓碑 + 审计；
        - 不同 access_label 的同名实体不合并；
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
                    self._merge_entity(dup, canonical, now, conn)
                    merged += 1
        self._name2id = {n.name: n.nid for n in self.store.fetch_nodes()}
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
        self.store.update_lifecycle(dup.nid, "tombstoned", now, conn=conn)
        self.store.add_tombstone("node", dup.nid, "entity_merge", now,
                                 conn=conn)

    def neighbors(self, node_name, rel=None):
        return neighbors(self.store, node_name, rel, self.clock())

    def fact_lookup(self, entity_name, key):
        nid = self._name2id.get(entity_name)
        if nid is None:
            return []
        now = self.clock()
        node = next((n for n in self.store.fetch_nodes() if n.nid == nid), None)
        if node is None or node.lifecycle in ("tombstoned", "deleted"):
            return []
        return [f for f in self.store.fetch_facts()
                if f.node_id == nid and f.key == key and not f.tombstoned
                and (f.invalid_at is None or f.invalid_at > now)]

    def fact_history(self, entity_name, key):
        """事实版本链：同一 (entity, key) 的全部事实，按 valid_at 升序。

        updates_to 收敛链由 facts.superseded_by 表达。
        """
        nid = self._name2id.get(entity_name)
        if nid is None:
            return []
        facts = [f for f in self.store.fetch_facts()
                 if f.node_id == nid and f.key == key]
        facts.sort(key=lambda f: (f.valid_at, f.fid))
        return facts

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
        return governance.restore(self.store, node_id, self.clock())

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

    # ---------------- 评测与关闭 ----------------
    def evaluate(self, dataset, k=5, mode="dual"):
        from .metrics import evaluate
        return evaluate(self, dataset, k=k, mode=mode)

    def close(self):
        self.store.close()
