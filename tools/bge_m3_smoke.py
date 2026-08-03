# -*- coding: utf-8 -*-
"""bge-m3 冒烟验证：加载模型、输出维度/稀疏键、中文相似度对照。

运行：py tools/bge_m3_smoke.py
首次运行会自动下载 BAAI/bge-m3（约 2.3GB，缓存到 HuggingFace 目录）。
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from dnamemory.embeddings import BGEM3Embedder  # noqa: E402


def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb)


def main():
    t0 = time.time()
    emb = BGEM3Embedder()
    print(f"模型加载完成：{time.time() - t0:.1f}s")
    pairs = [
        ("与张总讨论项目A预算", "项目预算会议", True),
        ("与张总讨论项目A预算", "周末去爬山", False),
        ("晚上做了糖醋排骨，娃说好吃", "晚饭给娃做了排骨", True),
        ("地铁人挤人忘带耳机", "通勤路上很挤", True),
        ("老板说下周一交方案", "周末去爬山", False),
    ]
    t1 = time.time()
    results = emb.embed_batch([p[0] for p in pairs] + [p[1] for p in pairs])
    print(f"批量嵌入：{time.time() - t1:.2f}s，维度={len(results[0].dense)}")
    for i, (a, b, expect) in enumerate(pairs):
        ra, rb = results[i], results[len(pairs) + i]
        print(f"sim({a[:12]}…, {b[:12]}…) = {cos(ra.dense, rb.dense):.4f}"
              f"  [语义相关: {expect}]"
              f"  稀疏键 {len(ra.sparse or {})}/{len(rb.sparse or {})}")


if __name__ == "__main__":
    main()
