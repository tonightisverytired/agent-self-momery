# -*- coding: utf-8 -*-
"""一次性迁移：按 ops/ingest_perltqa.py 的对话命名新规修复既有库。

对话节点：name 补会话时间（去同质化）、description 存完整转写、
value_score 0.4→0.5，并重嵌入。按 idempotency_key 定位，幂等可重跑。
"""
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

DB = ROOT / "ops" / "data" / "perltqa_memory.db"
MODEL = os.environ.get("DNAMEMORY_BGE_PATH",
                       r"F:\wangan_agent\models\embedding\bge-m3")


def _dlg_ts(text):
    try:
        return datetime.fromisoformat(text.strip())
    except (ValueError, AttributeError):
        return None


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    personas = json.loads(
        (ROOT / "data" / "perltmem_zh.json").read_text(encoding="utf-8"))
    embedder = BGEM3Embedder(model_name=MODEL)
    mem = MemorySystem(str(DB), embedder=embedder)
    updated = 0
    queue = []          # 待重嵌入 (nid, 文本)
    with mem.store.transaction() as conn:
        for p in personas:
            who = p["profile"]["Protagonist"]
            for did, dlg in p.get("dialogues", {}).items():
                linked = dlg.get("events")
                lids = linked if isinstance(linked, list) \
                    else ([linked] if linked else [])
                summary = next((p["events"][l]["summary"] for l in lids
                                if l in p.get("events", {})), "")
                for sess_ts, turns in dlg["contents"].items():
                    rows = conn.execute(
                        "SELECT id, name FROM nodes WHERE idempotency_key=?",
                        (f"perltqa:{who}:dlg:{did}:{sess_ts}",)).fetchall()
                    if not rows:
                        continue
                    ts = _dlg_ts(sess_ts)
                    flat = []
                    for t in turns:
                        if isinstance(t, list):
                            flat.extend(str(x) for x in t)
                        else:
                            flat.append(str(t))
                    text = "\n".join(flat)
                    tag = ts.strftime("%m-%d %H:%M") if ts else sess_ts
                    name = (f"对话：{summary}（{tag}）" if summary
                            else f"对话记录（{tag}）")
                    nid = rows[0][0]
                    if rows[0][1] != name:
                        conn.execute(
                            "UPDATE nodes SET name=?, description=?, "
                            "value_score=0.5 WHERE id=?",
                            (name, text, nid))
                        updated += 1
                    # 向量恢复策略（0.8.1 分块嵌入后）：
                    # - 短转写（≤600 字）：已有向量则跳过；缺向量则嵌
                    #   300 字头部恢复（与基线一致，快）
                    # - 长转写（>600 字）：删旧向量后嵌全文（分块生效处）
                    has_vec = conn.execute(
                        "SELECT 1 FROM node_vectors WHERE node_id=?",
                        (nid,)).fetchone()
                    if len(text) <= 600:
                        if has_vec:
                            continue
                        queue.append((nid, f"{name} {text[:300]}"))
                        continue
                    # 重嵌入前必须先删旧向量：_embed_and_store 对已有向量
                    # 的节点幂等跳过，不删则静默无效
                    conn.execute(
                        "DELETE FROM node_vectors WHERE node_id=?", (nid,))
                    queue.append((nid, f"{name}\n{text}"))
    # 重嵌入在事务外进行（嵌入写库走独立连接，避免写锁冲突）
    for i in range(0, len(queue), 256):
        mem._embed_and_store(queue[i:i + 256])
        print(f"[patch] 嵌入 {min(i + 256, len(queue))}/{len(queue)}")
    print(f"[patch] 更新 {updated} 个对话节点")
    # node_vectors 变了，sqlite-vec ANN 索引必须重建（否则检索仍走向量旧值）
    mem.store.rebuild_vector_index(embedder.model_name, 1024)
    print("[patch] ANN 索引已重建")
    mem.close()


if __name__ == "__main__":
    main()
