# -*- coding: utf-8 -*-
"""对话摘要节点注入（0.8.2 四轮）：为长对话会话生成 LLM 摘要节点。

攻 dialogues 语义鸿沟（指代式提问：「那次指导对他有什么帮助」——问题
几乎不含可匹配词，答案埋在长转写里）。摘要节点是对会话的压缩重写，
关键词密集、长度短，词面/稠密两路都容易命中，并经边把查询引到原转写：

    摘要节点（kind=chat_summary, node_type=event, value_score=0.6）
    persona --discusses--> 摘要；摘要 --summarizes--> 会话原节点

幂等键 perltqa:sum:{session_nid}，重跑不重复（LLM 调用不省，断点续跑
靠 --skip-existing 先查库跳过已有摘要的会话）。

用法：
    py ops/ingest_perltqa_summaries.py                  # 全量（>200字会话）
    PERLTQA_SUM_LIMIT=20 py ops/ingest_perltqa_summaries.py   # 冒烟
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dnamemory import MemorySystem  # noqa: E402
from dnamemory.embeddings import BGEM3Embedder  # noqa: E402
from dnamemory.models import to_naive  # noqa: E402

DB_PATH = ROOT / "ops" / "data" / "perltqa_memory.db"
REPORT_PATH = ROOT / "ops" / "data" / "perltqa_summary_report.json"
MODEL_PATH = os.environ.get(
    "DNAMEMORY_BGE_PATH", r"F:\wangan_agent\models\embedding\bge-m3")
MIN_LEN = int(os.environ.get("PERLTQA_SUM_MIN_LEN", "200"))
LIMIT = int(os.environ.get("PERLTQA_SUM_LIMIT", "0") or 0)
EMBED_CHUNK = 32

SUM_PROMPT = (
    "请把下面这段对话压缩成 120 字以内的中文摘要。要求：保留关键事实、"
    "结论、计划，以及提及的人名、地名、事物名；用陈述句，直接写摘要，"
    "不要任何解释或前缀。\n\n对话：\n%s")

_now = None


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _summarize(transcript, model, key):
    import requests
    payload = {"model": model,
               "messages": [{"role": "user",
                             "content": SUM_PROMPT % transcript[:4000]}],
               "temperature": 0.2, "max_tokens": 220}
    last = None
    for _ in range(3):
        try:
            r = requests.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=60)
            r.raise_for_status()
            return (r.json()["choices"][0]["message"]["content"]
                    or "").strip()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise RuntimeError(f"摘要生成失败：{last}")


def main():
    _force_utf8()
    from dnamemory.extract import find_deepseek_key
    _, key = find_deepseek_key()
    if not key:
        raise SystemExit("未找到 DEEPSEEK_API_KEY")
    model = os.environ.get("DEEPSEEK_MODEL") or "deepseek-chat"

    mem = MemorySystem(str(DB_PATH),
                       embedder=BGEM3Embedder(model_name=MODEL_PATH))
    now = mem.clock()
    rows = mem.store.read(
        "SELECT id, name, description, ts FROM nodes"
        " WHERE kind='chat' AND LENGTH(description) > ?"
        " ORDER BY id", (MIN_LEN,))
    if LIMIT:
        rows = rows[:LIMIT]
    # 断点续跑：已有摘要节点的会话跳过
    done_keys = {r[0] for r in mem.store.read(
        "SELECT idempotency_key FROM nodes WHERE kind='chat_summary'")}
    todo = [r for r in rows
            if f"perltqa:sum:{r[0]}" not in done_keys]
    print(f"[sum] 长会话 {len(rows)}，待处理 {len(todo)}")

    stats = {"summaries": 0, "edges": 0, "errors": 0}
    embed_queue = []

    def flush_embed():
        nonlocal embed_queue
        for i in range(0, len(embed_queue), EMBED_CHUNK):
            mem._embed_and_store(embed_queue[i:i + EMBED_CHUNK])
        embed_queue = []

    for i, (sid, sname, sdesc, sts) in enumerate(todo):
        try:
            summary = _summarize(sdesc, model, key)
            sts_dt = to_naive(sts) if isinstance(sts, str) else sts
            with mem.store.transaction() as conn:
                nid = mem.store.insert_node(
                    "event", "chat_summary",
                    sname.replace("对话：", "摘要：", 1), summary, sts_dt, 0.6,
                    False, "public", 150.0, 0.02, now,
                    idempotency_key=f"perltqa:sum:{sid}", conn=conn)
                # persona --discusses--> 摘要；摘要 --summarizes--> 会话
                for (pid,) in mem.store.read(
                        "SELECT from_id FROM edges WHERE to_id=?"
                        " AND relation_type='discusses'", (sid,)):
                    mem.store.insert_edge(
                        pid, nid, "discusses", 0.8, 0.9, now, None, now,
                        idempotency_key=f"perltqa:sum:{sid}:pe:{pid}",
                        conn=conn)
                    stats["edges"] += 1
                mem.store.insert_edge(
                    nid, sid, "summarizes", 1.0, 0.95, now, None, now,
                    idempotency_key=f"perltqa:sum:{sid}:se", conn=conn)
                stats["edges"] += 1
            embed_queue.append((nid, f"{sname} {summary}"))
            stats["summaries"] += 1
            if len(embed_queue) >= EMBED_CHUNK:
                flush_embed()
        except Exception as e:  # noqa: BLE001 单条失败不中断
            stats["errors"] += 1
            print(f"[sum] 会话 {sid} 失败：{e}")
        if (i + 1) % 50 == 0:
            print(f"[sum] {i + 1}/{len(todo)} 摘要 {stats['summaries']}")
    flush_embed()
    mem.store.rebuild_vector_index(mem.embedder.model_name, 1024)
    REPORT_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"[sum] 完成：{stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
