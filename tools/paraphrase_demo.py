# -*- coding: utf-8 -*-
"""同义改写验证：bge-m3 稠密路 vs 旧词面代理。

运行：py tools/paraphrase_demo.py（复用 data/mvp_memory.db）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from dnamemory import MemorySystem, RecallQuery  # noqa: E402
from dnamemory.embeddings import BGEM3Embedder  # noqa: E402


def show(mem, label, q, k=5):
    print(f"--- {label} ---")
    hits = mem.recall(q, k=k, mode="semantic", node_types=("event",))
    if not hits:
        print("  (无结果)")
    for h in hits:
        ts = h.ts.strftime("%Y-%m-%d") if h.ts else "no-ts"
        print(f"  {h.score:.4f}  {h.name}  [{ts}]")


def show_target_sims(mem, query_text, targets):
    qv = mem.embedder.embed(query_text).dense
    import math
    qn = math.sqrt(sum(x * x for x in qv))
    vecs = {nid: d for nid, d, *_ in mem.store.fetch_node_vectors()}
    nodes = {n.nid: n for n in mem.store.fetch_nodes()}
    print(f"目标相似度（查询: {query_text}）")
    for name in targets:
        node = next((n for n in nodes.values()
                     if n.node_type == "event" and name in n.name), None)
        if node is None:
            print(f"  {name}: 未找到")
            continue
        v = vecs[node.nid]
        vn = math.sqrt(sum(x * x for x in v))
        sim = sum(a * b for a, b in zip(qv, v)) / (qn * vn)
        print(f"  {name}: sim={sim:.4f}  (value={node.value_score})")


def main():
    mem = MemorySystem(path=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "mvp_memory.db"), embedder=BGEM3Embedder())
    show(mem, "稠密路: 通勤路上人很多", RecallQuery(text="通勤路上人很多"))
    show(mem, "稠密路: 晚饭给娃做了排骨", RecallQuery(text="晚饭给娃做了排骨"))
    show_target_sims(mem, "通勤路上人很多", ["地铁", "通勤"])
    show_target_sims(mem, "晚饭给娃做了排骨", ["排骨", "晚饭"])
    mem.close()

    mem2 = MemorySystem(path=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "mvp_memory.db"))
    show(mem2, "旧词面代理: 通勤路上人很多", RecallQuery(text="通勤路上人很多"))
    show(mem2, "旧词面代理: 晚饭给娃做了排骨", RecallQuery(text="晚饭给娃做了排骨"))
    mem2.close()


if __name__ == "__main__":
    main()
