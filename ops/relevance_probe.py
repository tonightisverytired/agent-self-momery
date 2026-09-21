# -*- coding: utf-8 -*-
"""检索相关性探针（0.8.0）：在**真实中文语料**上量召回与相关性。

与 `ops/gate_check.py` 的区别：门禁跑合成快照（查询词与节点名同构，
recall/MAP 恒 1.0），只能防退化、测不出真实相关性。本探针问三件事：

A. 事件自召回 hit@k  —— 用真实事件名当查询，目标事件是否进 top-k
B. 片段召回 hit@k    —— 只给事件名前 6 字（真实用户只说片段）
C. 答案进上下文      —— 「{实体}的{key}」类问题，值是否进 current_state 预算内

用法：
    py ops/relevance_probe.py                    # 词面代理（无向量）
    py ops/relevance_probe.py --bge-m3           # 加语义路（本地 bge-m3）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _modes(with_semantic):
    modes = ["time", "graph", "dual", "triple"]
    if with_semantic:
        modes += ["semantic", "quad"]
    return modes


def probe_retrieval(mem, nodes, modes, k, n, seed):
    """A/B：事件自召回与片段召回。返回 {mode: {full: [...], prefix: [...]}}。"""
    from dnamemory import RecallQuery
    out = {m: {"full": [], "prefix": []} for m in modes}
    for nd in nodes[:n]:
        name = (nd.name or "").strip()
        if not name:
            continue
        for mode in modes:
            for tag, text in (("full", name), ("prefix", name[:6])):
                try:
                    hits = mem.recall(RecallQuery(text=text), k=k, mode=mode,
                                      node_types=("event",))
                    ids = {h.node_id for h in hits}
                except Exception:  # noqa: BLE001  单条失败不阻断
                    ids = set()
                out[mode][tag].append(nd.nid in ids)
    return out


KEY_CN = {
    "location": "所在地", "city": "城市", "hometown": "家乡", "age": "年龄",
    "phone_brand": "手机品牌", "education": "学历", "major": "专业",
    "occupation": "职业", "job": "工作", "health_status": "健康状况",
    "habit": "习惯", "food_preference": "饮食偏好", "relationship_status":
    "感情状态", "has_child": "是否有孩子", "status": "状态",
    "financial_status": "财务状况", "appearance": "外貌",
    "drinking_ability": "酒量", "driving_license": "驾照",
}
# 有「查询词族」映射的 key → 用**自然问句**（系统设计支持的路径）
KEY_QUESTION = {
    "location": "{n}住在哪里", "city": "{n}住在哪里",
    "hometown": "{n}老家在哪", "住所": "{n}住址在哪",
}


def _question_for(name, key):
    tpl = KEY_QUESTION.get(key)
    return tpl.format(n=name) if tpl else f"{name}的{KEY_CN.get(key, key)}是什么"


def probe_answer_in_context(mem, facts, n, seed):
    """C：答案可得率——「{实体}的{key}」类问题，值是否进 current_state。

    分两组看：`family` = 有查询词族映射的 key（自然问句，设计支持的路径）；
    `generic` = 其余 key（只有「X 的 Y 是什么」这种朴素问法）。
    """
    from dnamemory import RecallQuery
    rows = []
    for f in facts:
        if len(rows) >= n:
            break
        nd = next((x for x in mem.store.fetch_nodes() if x.nid == f.node_id),
                  None)
        if nd is None or not (nd.name or "").strip():
            continue
        q = _question_for(nd.name, f.key)
        try:
            ctx = mem.recall_context(RecallQuery(text=q))
        except Exception as e:  # noqa: BLE001
            rows.append({"q": q, "ok": False, "err": str(e)[:60]})
            continue
        vals = [x.value for x in ctx.current_state]
        # 冲突组里的值也算「答案可得」：并列同源同置信交人工裁决是设计行为，
        # 此时事实不进 current_state 但确实被展示出来了
        conflict_vals = [v for g in ctx.conflicts for v in g.values]
        rows.append({
            "q": q, "key": f.key, "expect": f.value,
            "family": f.key in KEY_QUESTION,
            "in_current": f.value in vals,
            "ok": (f.value in vals) or (f.value in conflict_vals),
            "retrieved": bool(ctx.trace and ctx.trace.candidate_count),
            "blocks": sum(1 for v in (ctx.causes, ctx.impacts,
                                      ctx.impact_chains, ctx.patterns,
                                      ctx.temporal_chains) if v),
        })
    return rows


def _rate(flags):
    return round(sum(1 for x in flags if x) / len(flags), 3) if flags else 0.0


def main():
    _force_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "ops" / "data" / "lccc_memory.db"))
    ap.add_argument("--n", type=int, default=30, help="抽样条数")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--bge-m3", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "ops" / "data"
                                         / "relevance_probe_result.json"))
    ap.add_argument("--gate", action="store_true",
                    help="门禁模式：按 triple（无向量）结果判阈值，"
                         "任一不达标打印未达标项并以退出码 1 结束")
    ap.add_argument("--min-full", type=float, default=0.9,
                    help="triple 整名命中率下限（默认 0.9）")
    # 阈值按 0.8.1 实测定标（留回归余量）：prefix6 受反射摘要同名竞争
    # 上限约 0.43，答案可得率受 current_state 预算上限约 0.37
    ap.add_argument("--min-prefix", type=float, default=0.4,
                    help="triple 前6字命中率下限（默认 0.4）")
    ap.add_argument("--min-answer", type=float, default=0.7,
                    help="答案可得率下限（默认 0.7，0.8.1 实测 0.833）")
    args = ap.parse_args()
    if args.bge_m3:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

    from dnamemory import MemorySystem
    embedder = None
    if args.bge_m3:
        from dnamemory.embeddings import BGEM3Embedder
        embedder = BGEM3Embedder()
    mem = MemorySystem(path=args.db, embedder=embedder)

    nodes = [n for n in mem.store.fetch_nodes()
             if n.node_type == "event" and n.lifecycle == "active"
             and (n.name or "").strip()]
    random.Random(args.seed).shuffle(nodes)
    facts = [f for f in mem.store.fetch_facts()
             if not f.tombstoned and f.invalid_at is None
             and (f.value or "").strip()]
    random.Random(args.seed + 1).shuffle(facts)

    modes = _modes(args.bge_m3)
    retr = probe_retrieval(mem, nodes, modes, args.k, args.n, args.seed)
    ctx_rows = probe_answer_in_context(mem, facts, args.n, args.seed)

    report = {
        "db": args.db, "n": args.n, "k": args.k,
        "embedder": bool(embedder), "seed": args.seed,
        "corpus": {"nodes": len(mem.store.fetch_nodes()),
                   "facts": len(mem.store.fetch_facts())},
        "retrieval": {m: {"full_name": _rate(v["full"]),
                          "prefix6": _rate(v["prefix"]),
                          "n": len(v["full"])}
                      for m, v in retr.items()},
        "answer_in_context": {
            "n": len(ctx_rows),
            "rate": _rate([r["ok"] for r in ctx_rows]),
            "family_rate": _rate([r["ok"] for r in ctx_rows if r["family"]]),
            "family_n": sum(1 for r in ctx_rows if r["family"]),
            "generic_rate": _rate([r["ok"] for r in ctx_rows
                                   if not r["family"]]),
            "in_current_rate": _rate([r["in_current"] for r in ctx_rows
                                      if "in_current" in r]),
            "retrieved_rate": _rate([r["retrieved"] for r in ctx_rows
                                     if "retrieved" in r]),
            "rows": ctx_rows[:12],
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"语料: {args.db}  节点 {report['corpus']['nodes']} / "
          f"事实 {report['corpus']['facts']}  向量路: "
          f"{'开' if embedder else '关'}")
    print(f"检索命中率 hit@{args.k}（n={args.n}）:")
    print(f"  {'mode':<10} {'整名':>8} {'前6字':>8}")
    for m, v in report["retrieval"].items():
        print(f"  {m:<10} {v['full_name']:>8.3f} {v['prefix6']:>8.3f}")
    aic = report["answer_in_context"]
    print(f"答案可得（current_state 或冲突组）: {aic['rate']:.3f} (n={aic['n']})"
          f"  其中直接进 current_state: {aic['in_current_rate']:.3f}")
    print(f"  [有词族映射 {aic['family_rate']:.3f} (n={aic['family_n']}) / "
          f"其余 {aic['generic_rate']:.3f}]  "
          f"检索命中率 {aic['retrieved_rate']:.3f}")
    print(f"结果已保存: {args.out}")
    mem.close()

    if not args.gate:
        return 0
    # 门禁判定固定看 triple（无向量）：与 --bge-m3 无关，防词面路退化
    tri = report["retrieval"]["triple"]
    fails = []
    if tri["full_name"] < args.min_full:
        fails.append(f"triple 整名命中率 {tri['full_name']:.3f} "
                     f"< {args.min_full}")
    if tri["prefix6"] < args.min_prefix:
        fails.append(f"triple 前6字命中率 {tri['prefix6']:.3f} "
                     f"< {args.min_prefix}")
    if aic["rate"] < args.min_answer:
        fails.append(f"答案可得率 {aic['rate']:.3f} < {args.min_answer}")
    if fails:
        print("门禁未通过:")
        for f_ in fails:
            print(f"  - {f_}")
        return 1
    print(f"门禁通过: triple 整名 {tri['full_name']:.3f} / "
          f"前6字 {tri['prefix6']:.3f} / 答案可得 {aic['rate']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
