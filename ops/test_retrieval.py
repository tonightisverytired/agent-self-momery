# -*- coding: utf-8 -*-
"""注入库检索测试与评估。

对 LCCC 注入库（simulation/lccc_memory.db）做三组评估：
1. 事实检索：从库内抽取事实构造查询 → recall 多路径 + RRF，统计命中率
2. 上下文管线：6 类 query_type 的 recall_context，统计区块非空率与
   RecallTrace 各字段（定位「没召回/解析错/链断/证据不足」）
3. 可解释性：fact/summary/stable 的 explain 追溯成功率
向量模型一律本地缓存（HF_HUB_OFFLINE=1）。
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dnamemory import MemorySystem, RecallQuery  # noqa: E402
from dnamemory.embeddings import BGEM3Embedder  # noqa: E402
from dnamemory.errors import EvidenceNotFoundError  # noqa: E402

DB_PATH = ROOT / "ops" / "data" / "lccc_memory.db"
REPORT_PATH = ROOT / "ops" / "data" / "retrieval_report.json"
N_FACT_QUERIES = 30
N_CONTEXT_PER_TYPE = 5
SEED = 20260916


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def fact_retrieval(mem, rng):
    facts = [f for f in mem.store.fetch_facts() if not f.tombstoned]
    sample = rng.sample(facts, min(N_FACT_QUERIES, len(facts)))
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    results = []
    for f in sample:
        ent = nodes.get(f.node_id)
        q = f"{ent.name if ent else ''} {f.key} {f.value}".strip()
        try:
            hits = mem.recall(RecallQuery(text=q), k=8, mode="triple")
        except Exception as e:  # noqa: BLE001
            results.append({"query": q, "ok": False, "error": str(e)[:120]})
            continue
        names = [h.name for h in hits]
        # 严格口径：目标实体节点被召回
        strict = ent is not None and ent.name in names
        # 宽松口径：任一召回记忆与实体名或事实值词面相关（≥2 字重叠）
        def _overlap(a, b):
            a, b = (a or "").strip(), (b or "").strip()
            if not a or not b:
                return False
            if a in b or b in a:
                return True
            return any(a[i:i + 2] in b for i in range(len(a) - 1))
        relaxed = any(_overlap(h, ent.name if ent else "") or
                      _overlap(h, f.value or "") for h in names)
        results.append({"query": q, "entity": ent.name if ent else None,
                        "key": f.key, "value": f.value,
                        "strict_ok": strict, "relaxed_ok": relaxed,
                        "hits": [h.name for h in hits[:5]]})
    n = max(1, len(results))
    return {"total": len(results),
            "strict_hit": sum(1 for r in results if r["strict_ok"]),
            "strict_rate": round(
                sum(1 for r in results if r["strict_ok"]) / n, 4),
            "relaxed_hit": sum(1 for r in results if r["relaxed_ok"]),
            "relaxed_rate": round(
                sum(1 for r in results if r["relaxed_ok"]) / n, 4),
            "details": results}


def context_probe(mem, rng):
    """6 类 query_type 上下文管线探针（用库内真实实体/事实值构造查询）。"""
    nodes = [n for n in mem.store.fetch_nodes() if n.node_type == "entity"]
    facts = [f for f in mem.store.fetch_facts() if not f.tombstoned]
    ents = rng.sample(nodes, min(6, len(nodes))) if nodes else []
    fact_sample = rng.sample(facts, min(6, len(facts))) if facts else []
    queries = {
        "current_state": [f"{e.name} 现在怎么样" for e in ents],
        "history": [f"{e.name} 之前的情况" for e in ents],
        "timeline": [f"{e.name} 的时间线" for e in ents],
        "change": [f"{e.name} 发生了什么变化" for e in ents],
        "why_change": [f"为什么 {f.key} 变成了 {f.value}" for f in fact_sample],
        "semantic_recall": [f.value for f in fact_sample],
    }
    per_type = {}
    for qtype, qs in queries.items():
        rows = []
        for q in qs[:N_CONTEXT_PER_TYPE]:
            try:
                ctx = mem.recall_context(RecallQuery(text=q),
                                         query_type=qtype)
                tr = ctx.trace
                rows.append({
                    "query": q,
                    "current_state": len(ctx.current_state),
                    "historical_changes": len(ctx.historical_changes),
                    "recent_events": len(ctx.recent_events),
                    "beliefs": len(ctx.beliefs),
                    "intents": len(ctx.intents),
                    "temporal_chains": len(ctx.temporal_chains),
                    "evidence": len(ctx.evidence),
                    "conflicts": len(ctx.conflicts),
                    "causes": len(ctx.causes),
                    "notes": len(ctx.notes),
                    "trace": tr.to_dict() if tr is not None else None,
                })
            except Exception as e:  # noqa: BLE001
                rows.append({"query": q, "error": str(e)[:150]})
        ok = [r for r in rows if "error" not in r]
        nonempty = sum(1 for r in ok if any(
            r[k] for k in ("current_state", "historical_changes",
                           "recent_events", "beliefs", "intents",
                           "temporal_chains", "causes")))
        per_type[qtype] = {
            "probes": len(rows), "errors": len(rows) - len(ok),
            "nonempty_rate": round(nonempty / max(1, len(ok)), 4),
            "rows": rows,
        }
    return per_type


def explain_probe(mem, rng):
    """explain 追溯：无证据目标抛 E013 属预期（正确 abstain，不追溯编造）。"""
    results = []

    def run(target, mtype, mid):
        try:
            ex = mem.explain(mid, kind=mtype)
            results.append({"target": target, "ok": True,
                            "evidence": len(ex.get("evidence", [])),
                            "related": len(ex.get("related", []))})
        except EvidenceNotFoundError:
            results.append({"target": target, "ok": False,
                            "abstain": True, "error": "E013(无证据,预期)"})
        except Exception as e:  # noqa: BLE001
            results.append({"target": target, "ok": False, "abstain": False,
                            "error": type(e).__name__})

    facts = [f for f in mem.store.fetch_facts() if not f.tombstoned]
    for f in rng.sample(facts, min(5, len(facts))):
        run(f"fact:{f.fid} ({f.key}={f.value})", "fact", f.fid)
    summaries = [n for n in mem.store.fetch_nodes() if n.kind == "summary"]
    if summaries:
        run(f"summary:{summaries[0].nid}", "node", summaries[0].nid)
    stables = [n for n in mem.store.fetch_nodes() if n.kind == "stable"]
    if stables:
        run(f"stable:{stables[0].nid}", "node", stables[0].nid)
    ok = sum(1 for r in results if r["ok"])
    abstain = sum(1 for r in results if r.get("abstain"))
    total = len(results)
    # 有效追溯率 = 成功 / (成功 + 预期拒答)；异常才算失败
    effective = ok + abstain
    return {"total": total, "ok": ok, "expected_abstain": abstain,
            "abnormal": total - effective,
            "effective_rate": round(effective / max(1, total), 4),
            "details": results}


def main():
    _force_utf8()
    if not DB_PATH.exists():
        print(f"[retrieval] 缺少注入库 {DB_PATH}，先跑 ingest_lccc.py")
        sys.exit(1)
    rng = random.Random(SEED)
    print("[retrieval] 加载本地 bge-m3（离线）...")
    embedder = BGEM3Embedder()
    mem = MemorySystem(str(DB_PATH), embedder=embedder)

    print("[retrieval] 1/3 事实检索（recall triple + RRF）...")
    fr = fact_retrieval(mem, rng)
    print(f"[retrieval]   严格命中率 {fr['strict_rate']}"
          f"（{fr['strict_hit']}/{fr['total']}）；"
          f"词面相关率 {fr['relaxed_rate']}"
          f"（{fr['relaxed_hit']}/{fr['total']}）")

    print("[retrieval] 2/3 上下文管线（6 类 query_type）...")
    cp = context_probe(mem, rng)
    for qtype, d in cp.items():
        print(f"[retrieval]   {qtype:>16}: 非空率 {d['nonempty_rate']}"
              f"（{d['probes']} 探针, {d['errors']} 错误）")

    print("[retrieval] 3/3 explain 追溯...")
    ep = explain_probe(mem, rng)
    print(f"[retrieval]   有效追溯率 {ep['effective_rate']}"
          f"（成功 {ep['ok']} + 预期拒答 {ep['expected_abstain']}"
          f"/ 总 {ep['total']}）")

    report = {"fact_retrieval": fr, "context_probe": cp, "explain": ep,
              "db": str(DB_PATH)}
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"[retrieval] 报告已写入 {REPORT_PATH}")
    mem.close()


if __name__ == "__main__":
    main()
