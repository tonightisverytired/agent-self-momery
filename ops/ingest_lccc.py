# -*- coding: utf-8 -*-
"""LCCC 中文对话数据集全流程注入脚本。

流程：采样 LCCC（≥4 轮日常对话）→ DeepSeek 抽取（批量）→
MemorySystem 结构化写入 → 治理链（冲突收敛 / 因果推导 / 月度反射 /
长期压缩）→ 注入报告。向量模型一律使用本地缓存（HF_HUB_OFFLINE=1）。
"""
from __future__ import annotations

import gzip
import json
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dnamemory import MemorySystem  # noqa: E402
from dnamemory.embeddings import BGEM3Embedder  # noqa: E402
from dnamemory.extract import DeepSeekExtractor, FallbackExtractor  # noqa: E402
from dnamemory.temporal import (derive_causes,  # noqa: E402
                                derive_impact_links)

DB_PATH = ROOT / "ops" / "data" / "lccc_memory.db"
REPORT_PATH = ROOT / "ops" / "data" / "ingest_report.json"
N_SAMPLE = 500
BATCH = 8          # 每批对话条数（合一 prompt 抽取）
MAX_TOKENS = 4000
START_TS = datetime(2026, 1, 5, 9, 0)
STEP = timedelta(days=4)   # 每批 +4 天 → 覆盖 2026-01 ~ 2026-09


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def load_dialogs():
    dialogs = []
    for name in ("lccc_base_valid.jsonl.gz", "lccc_base_test.jsonl.gz"):
        p = ROOT / "data" / name
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                d = json.loads(line)
                if isinstance(d, list) and len(d) >= 4:
                    dialogs.append(d)
    return dialogs


def sample_dialogs(dialogs, n=N_SAMPLE, seed=20260915):
    rng = random.Random(seed)
    seen, uniq = set(), []
    for d in dialogs:
        key = "".join(d)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(d)
    rng.shuffle(uniq)
    return uniq[:n]


def _count(mem):
    return {
        "nodes": len(mem.store.fetch_nodes()),
        "entities": sum(1 for n in mem.store.fetch_nodes()
                        if n.node_type == "entity"),
        "events": sum(1 for n in mem.store.fetch_nodes()
                      if n.node_type == "event"),
        "facts": len(mem.store.fetch_facts()),
        "beliefs": len(mem.store.fetch_beliefs()),
        "intents": len(mem.store.fetch_intents()),
        "evidence": len(mem.store.fetch_evidence()),
        "edges": len(mem.store.fetch_edges()),
        "memory_links": len(mem.store.fetch_memory_links()),
    }


def main():
    _force_utf8()
    if DB_PATH.exists():
        DB_PATH.unlink()

    dialogs = sample_dialogs(load_dialogs())
    texts = ["\n".join(d) for d in dialogs]
    print(f"[ingest] 采样 {len(texts)} 条对话（≥4 轮，去重后随机）")

    print("[ingest] 加载本地 bge-m3（离线）...")
    embedder = BGEM3Embedder()
    # 模型可用 DEEPSEEK_MODEL 覆盖；默认 deepseek-chat——实测同 prompt 下
    # 候选产出约为 v4-pro 的 2 倍且覆盖 fact/belief/intent（2026-09 对拍）
    model = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"
    extractor = DeepSeekExtractor(model=model, timeout=120, max_tokens=MAX_TOKENS)
    fallback = FallbackExtractor()
    mem = MemorySystem(str(DB_PATH), embedder=embedder, extractor=extractor)

    rejected, fallback_batches = [], 0
    n_batches = (len(texts) + BATCH - 1) // BATCH
    for i in range(0, len(texts), BATCH):
        bi = i // BATCH
        chunk = texts[i:i + BATCH]
        ts = START_TS + STEP * bi
        meta = {"recorded_at": ts.isoformat(),
                "today": ts.date().isoformat(),
                "conversation_id": f"lccc-batch-{bi:03d}"}
        try:
            res = mem.write_many(chunk, meta=meta, extractor=extractor,
                                 batch_size=BATCH)
        except Exception as e:  # noqa: BLE001 LLM 批次失败 → 确定性兜底
            fallback_batches += 1
            res = mem.write_many(chunk, meta=meta, extractor=fallback,
                                 batch_size=BATCH)
            rejected.append((f"batch-{bi}", f"LLM失败兜底: {e}"))
        rejected.extend((f"batch-{bi}-{j}", r[1])
                        for j, r in enumerate(res.rejected))
        if (bi + 1) % 10 == 0 or bi + 1 == n_batches:
            print(f"[ingest] 批 {bi + 1}/{n_batches} 完成"
                  f"（接受 {res.accepted}）")

    # 事件时间轴回填：模拟「事件发生时间=记录时间」，供 timeline/reflect/
    # derive_causes 治理链使用（LLM 抽取未提时间的事件 ts 为 None）。
    with mem.store.transaction() as conn:
        conn.execute("UPDATE nodes SET ts=created_at "
                     "WHERE node_type='event' AND ts IS NULL "
                     "AND lifecycle='active'")
    after_write = _count(mem)
    print(f"[ingest] 写入完成: {after_write}")

    print("[ingest] 治理链 1/4: resolve_conflicts（事实冲突收敛）")
    decisions = mem.resolve_conflicts()
    print(f"[ingest]   {len(decisions)} 个冲突决策")

    print("[ingest] 治理链 2/4: derive_causes + derive_impact_links")
    causes = derive_causes(mem.store)
    impact_links = derive_impact_links(mem.store)
    print(f"[ingest]   {causes} 条 fact 因果链 + "
          f"{impact_links} 条影响链")

    print("[ingest] 治理链 3/4: reflect_monthly（2026-01 ~ 09）")
    summaries = []
    for m in range(1, 10):
        s = mem.reflect_monthly(2026, m)
        if s is not None:
            summaries.append(s)
    print(f"[ingest]   {len(summaries)} 个月度摘要")

    print("[ingest] 治理链 4/4: compress_stable（长期稳定记忆）")
    stable = mem.compress_stable()
    print(f"[ingest]   {stable} 个 stable 节点")

    final = _count(mem)
    report = {
        "dataset": {
            "name": "LCCC-base (silver/lccc)",
            "source": "https://hf-mirror.com/datasets/silver/lccc",
            "splits": ["lccc_base_valid.jsonl.gz",
                       "lccc_base_test.jsonl.gz"],
            "sampled_dialogs": len(texts),
            "min_turns": 4,
            "time_range": [START_TS.isoformat(),
                           (START_TS + STEP * (n_batches - 1)).isoformat()],
            "time_synthesis": "每条对话按批间隔 4 天均匀分布在 8 个月（模拟连续聊天记录）",
        },
        "extraction": {
            "engine": "DeepSeekExtractor",
            "model": extractor.model,
            "batch_size": BATCH,
            "fallback_batches": fallback_batches,
            "rejected_candidates": len(rejected),
        },
        "after_write": after_write,
        "governance": {
            "conflict_decisions": len(decisions),
            "caused_by_links": causes,
            "monthly_summaries": len(summaries),
            "stable_memories": stable,
        },
        "final": final,
        "db": str(DB_PATH),
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"[ingest] 报告已写入 {REPORT_PATH}")
    mem.close()


if __name__ == "__main__":
    main()
