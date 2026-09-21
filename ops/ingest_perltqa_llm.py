# -*- coding: utf-8 -*-
"""PerLTQA 写入侧细粒度增强层（0.8.1 二轮 P2，治本）。

在确定性结构化注入（ops/ingest_perltqa.py）之上，对有 QA 覆盖的 32 个
人物的对话转写叠加 LLM 抽取层：DeepSeek 把每场对话拆成细粒度
事件/事实/观点/意图候选（写入走 `write_many` → 批级证据自动闭环绑定原文
转写），随后跑治理链（冲突收敛 + 因果推导 + 影响链）。

原理：语义鸿沟类问题的病根是「900 字跨话题转写 = 一个记忆节点」；
细粒度抽取让问题命中命题级记忆，而非整段转写。

幂等：LLM 候选带 llm:hash 幂等键，重跑不产生重复行。
环境：DEEPSEEK_API_KEY 必填；DEEPSEEK_MODEL 默认 deepseek-chat。
用法：py ops/ingest_perltqa_llm.py [--personas N]（默认全部 32 个 QA 人物）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dnamemory import MemorySystem  # noqa: E402
from dnamemory.embeddings import BGEM3Embedder  # noqa: E402
from dnamemory.extract import DeepSeekExtractor, FallbackExtractor  # noqa: E402
from dnamemory.temporal import derive_causes, derive_impact_links  # noqa: E402

DB_PATH = ROOT / "ops" / "data" / "perltqa_memory.db"
MEM_PATH = ROOT / "data" / "perltmem_zh.json"
QA_PATH = ROOT / "data" / "perltqa_zh.json"
REPORT_PATH = ROOT / "ops" / "data" / "perltqa_llm_report.json"
MODEL_PATH = os.environ.get(
    "DNAMEMORY_BGE_PATH", r"F:\wangan_agent\models\embedding\bge-m3")
BATCH = 8
MAX_TOKENS = 4000


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _sess_dt(text):
    """会话时间容忍非补零格式（'2022-5-13 9:00'）；解析失败返回 None。"""
    import re
    m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?",
                 text or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4) or 0), int(m.group(5) or 0))
    except ValueError:
        return None


def main():
    _force_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--personas", type=int, default=0,
                    help="只处理前 N 个 QA 人物（0=全部 32 个）")
    args = ap.parse_args()

    personas = {p["profile"]["Protagonist"]: p
                for p in json.loads(MEM_PATH.read_text(encoding="utf-8"))}
    qa_names = [next(iter(x))
                for x in json.loads(QA_PATH.read_text(encoding="utf-8"))]
    names = [n for n in qa_names if n in personas]
    if args.personas > 0:
        names = names[:args.personas]
    print(f"[llm-layer] {len(names)} 个人物的对话细粒度抽取")

    embedder = BGEM3Embedder(model_name=MODEL_PATH)
    model = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"
    extractor = DeepSeekExtractor(model=model, timeout=120,
                                  max_tokens=MAX_TOKENS)
    fallback = FallbackExtractor()
    mem = MemorySystem(str(DB_PATH), embedder=embedder, extractor=extractor)

    stats = {"sessions": 0, "accepted": 0, "rejected": 0,
             "fallback_batches": 0}
    for pi, who in enumerate(names):
        texts, metas = [], []
        for did, dlg in personas[who].get("dialogues", {}).items():
            for sess_ts, turns in dlg["contents"].items():
                flat = []
                for t in turns:
                    if isinstance(t, list):
                        flat.extend(str(x) for x in t)
                    else:
                        flat.append(str(t))
                texts.append("\n".join(flat))
                dt = _sess_dt(sess_ts)
                metas.append({
                    "recorded_at": dt.isoformat() if dt else None,
                    "today": dt.date().isoformat() if dt else None,
                    "conversation_id": f"perltqa:{who}:{did}:{sess_ts}",
                })
        for i in range(0, len(texts), BATCH):
            chunk = texts[i:i + BATCH]
            meta = metas[i]  # 批级 meta 取首场（recorded_at 逐批对齐）
            try:
                res = mem.write_many(chunk, meta=meta, extractor=extractor,
                                     batch_size=BATCH)
            except Exception as e:  # noqa: BLE001 LLM 批次失败 → 确定性兜底
                stats["fallback_batches"] += 1
                res = mem.write_many(chunk, meta=meta, extractor=fallback,
                                     batch_size=BATCH)
            stats["sessions"] += len(chunk)
            stats["accepted"] += res.accepted
            stats["rejected"] += len(res.rejected)
        print(f"[llm-layer] {pi + 1}/{len(names)} {who} 完成"
              f"（累计接受 {stats['accepted']}）")

    print("[llm-layer] 治理链：resolve_conflicts")
    decisions = mem.resolve_conflicts()
    print(f"[llm-layer]   {len(decisions)} 个冲突决策")
    print("[llm-layer] 治理链：derive_causes + derive_impact_links")
    causes = derive_causes(mem.store)
    impact_links = derive_impact_links(mem.store)
    n_causes = causes if isinstance(causes, int) else len(causes)
    n_impacts = impact_links if isinstance(impact_links, int) else len(impact_links)
    print(f"[llm-layer]   {n_causes} 因果 + {n_impacts} 影响链")

    REPORT_PATH.write_text(json.dumps({
        "personas": names, "model": model, "stats": stats,
        "conflict_decisions": len(decisions), "causes": n_causes,
        "impact_links": n_impacts,
        "db": str(DB_PATH)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[llm-layer] 完成: {stats}")
    mem.close()


if __name__ == "__main__":
    main()
