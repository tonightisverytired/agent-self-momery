# -*- coding: utf-8 -*-
"""稠密路打分规则调优：sim×value vs sim×(0.5+0.5×value) × 阈值扫描。

运行：py tools/dense_tuning.py（复用 data/mvp_memory.db）
"""
import importlib.util
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import dnamemory.retrieval as R  # noqa: E402
from dnamemory import MemorySystem, RecallQuery  # noqa: E402
from dnamemory.embeddings import BGEM3Embedder  # noqa: E402


def make_dense_path(threshold, blend):
    def wrapped(store, embedder, text, filters, min_sim=0.55):
        import numpy
        qres = embedder.embed(text)
        qv = numpy.asarray(qres.dense, dtype=float)
        qn = numpy.linalg.norm(qv)
        if qn == 0:
            return {}
        qv = qv / qn
        nodes = {n.nid: n for n in store.fetch_nodes()}
        out = {}
        for nid, dense, _s, _m, _d, _a in store.fetch_node_vectors():
            node = nodes.get(nid)
            if node is None:
                continue
            v = numpy.asarray(dense, dtype=float)
            norm = numpy.linalg.norm(v)
            if norm == 0:
                continue
            sim = float(qv @ (v / norm))
            if sim < threshold:
                continue
            if blend:
                out[nid] = sim * (0.5 + 0.5 * node.value_score)
            else:
                out[nid] = sim * node.value_score
        return out
    return wrapped


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "mvt", os.path.join(root, "tools", "mvp_llm_test.py"))
    mvt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mvt)

    mem = MemorySystem(path=os.path.join(root, "data", "mvp_memory.db"),
                       embedder=BGEM3Embedder())
    dataset = mvt.build_eval_dataset(mem)
    orig = R._dense_path
    print("threshold | scoring             | recall | map")
    best = None
    for blend in (False, True):
        for th in (0.50, 0.55, 0.60):
            R._dense_path = make_dense_path(th, blend)
            agg, _ = mem.evaluate(dataset, k=5, mode="triple")
            tag = "sim*value" if not blend else "sim*blend"
            print(f"{th:.2f}    | {tag:18} | {agg['recall']:.4f} | {agg['map']:.4f}")
            if best is None or agg["map"] > best[2]:
                best = (th, blend, agg["map"], agg["recall"])
    print(f"最优: threshold={best[0]} blend={best[1]} "
          f"MAP={best[2]:.4f} Recall={best[3]:.4f}")
    R._dense_path = orig

    th, blend, *_ = best
    R._dense_path = make_dense_path(th, blend)
    for text in ("通勤路上人很多", "晚饭给娃做了排骨"):
        print(f"\n同义改写 top5（{text}）:")
        for h in mem.recall(RecallQuery(text=text), k=5, mode="semantic",
                            node_types=("event",)):
            print(f"  {h.score:.4f}  {h.name}")
    R._dense_path = orig
    mem.close()


if __name__ == "__main__":
    main()
