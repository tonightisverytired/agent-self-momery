# -*- coding: utf-8 -*-
"""中文长期能力评测：temporal / multi_hop / update / abstain / paraphrase。

运行（验收方执行）：
  py tools/eval_longterm.py --bge-m3              # 自包含语料（内存库）
  py tools/eval_longterm.py --bge-m3 --mode quad
  py tools/eval_longterm.py --db user_memory.db   # 用自有库评测
输出：simulation/longterm_eval_result.json + 控制台表格
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from dnamemory import MemorySystem, RecallQuery  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


class CachingEmbedder:
    """评测用嵌入缓存：同一查询文本只算一次。"""

    def __init__(self, inner):
        self._inner = inner
        self.model_name = inner.model_name
        self._cache = {}

    def embed(self, text):
        if text not in self._cache:
            self._cache[text] = self._inner.embed(text)
        return self._cache[text]

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


def make_embedder():
    from dnamemory.embeddings import BGEM3Embedder
    return CachingEmbedder(BGEM3Embedder())


def load_items():
    with open(os.path.join(ROOT, "data", "eval_longterm.json"),
              encoding="utf-8") as f:
        return json.load(f)


def build_memory(data, db=None, embedder=None, reranker=None):
    """优先用指定库；否则用内嵌语料构建内存库（自包含可复现）。"""
    if db and os.path.exists(db):
        return MemorySystem(path=db, embedder=embedder, reranker=reranker)
    mem = MemorySystem(path=":memory:", embedder=embedder, reranker=reranker)
    corpus = data.get("corpus") or {}
    for ent in corpus.get("entities", []):
        mem.add_entity(ent["name"], ent.get("kind", "concept"),
                       ent.get("description", ""))
    for ev in corpus.get("events", []):
        ts = datetime.fromisoformat(ev["ts"]) if ev.get("ts") else None
        mem.add_event(ev["name"], ts, kind=ev.get("kind", "life"),
                      value_score=ev.get("value_score", 0.5))
    for edge in corpus.get("edges", []):
        try:
            mem.add_edge(edge["from"], edge["to"], edge.get("rel", "mentions"),
                         0.6, 0.8)
        except Exception:  # noqa: BLE001 孤立边不影响评测主路径
            continue
    return mem


def build_query(item):
    if item.get("time"):
        t0 = datetime.fromisoformat(item["time"])
        return RecallQuery(time=(t0, int(item.get("tol_days", 2))),
                           text=item.get("query"),
                           topic=item.get("topic"))
    if item.get("topic"):
        return RecallQuery(topic=item["topic"], text=item.get("query"))
    return RecallQuery(text=item.get("query"))


def score_item(mem, item, k=5, mode="triple"):
    hits = mem.recall(build_query(item), k=k, mode=mode,
                      node_types=("event",))
    names = {h.name for h in hits}
    expected = set(item.get("expected", []))
    if not expected:
        ok = len(hits) == 0  # abstain：无相关记忆即成功
    else:
        ok = bool(expected & names)
    return {
        "id": item["id"], "ability": item["ability"],
        "query": item.get("query", item.get("time", "")),
        "mode": mode, "ok": ok, "expected": sorted(expected),
        "hits": [h.name for h in hits[:5]],
    }


def main():
    _force_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None,
                        help="自有库路径（默认使用内嵌语料构建内存库）")
    parser.add_argument("--mode", default="triple",
                        choices=("dual", "triple", "quad"))
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--bge-m3", action="store_true",
                        help="启用 bge-m3 稠密/稀疏语义路（默认词面代理）")
    parser.add_argument("--reranker", action="store_true",
                        help="启用 bge-reranker-v2-m3 重排（需联网下载模型）")
    args = parser.parse_args()

    data = load_items()
    embedder = make_embedder() if args.bge_m3 else None
    reranker = None
    if args.reranker:
        from dnamemory.rerank import BGEReranker
        reranker = BGEReranker()
    mem = build_memory(data, db=args.db, embedder=embedder, reranker=reranker)
    rows = [score_item(mem, item, k=args.k, mode=args.mode)
            for item in data["items"]]
    by_ability = {}
    for row in rows:
        bl = by_ability.setdefault(row["ability"], {"ok": 0, "n": 0})
        bl["n"] += 1
        bl["ok"] += 1 if row["ok"] else 0
    report = {
        "dataset": data["name"],
        "db": args.db or "corpus:memory", "mode": args.mode, "k": args.k,
        "overall": sum(1 for r in rows if r["ok"]) / len(rows),
        "by_ability": {
            ab: {"recall": bl["ok"] / bl["n"], "n": bl["n"]}
            for ab, bl in by_ability.items()
        },
        "items": rows,
    }
    out_path = os.path.join(ROOT, "simulation", "longterm_eval_result.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"整体: {report['overall']:.3f} (mode={args.mode}, k={args.k})")
    for ab, bl in report["by_ability"].items():
        print(f"  {ab:<10} recall={bl['recall']:.3f} (n={bl['n']})")
    print(f"结果已保存: {out_path}")

    mem.close()


if __name__ == "__main__":
    main()
