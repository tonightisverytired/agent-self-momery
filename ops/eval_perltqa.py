# -*- coding: utf-8 -*-
"""PerLTQA 中文正式基准评测（0.8.1）。

对 32 个 QA 人物的 1905 条人工标注问答，测两类指标：

- retrieval_hit@k：recall(triple, k=10) 的 top-10 是否命中 Reference Memory
  对应的节点（事件/对话/人物/主角）——测召回；
- answer_in_context：recall_context 全管线上文（current_state 事实值、
  近期事件名与描述、历史、冲突、证据等区块文本）是否包含任一答案锚点
  词（Memory Anchors）——测相关度/最后一公里。

上下文管线较贵，默认对每层抽样（--ctx-per-cat，每人物每类上限）；
retrieval 评测默认全量。产出 ops/data/perltqa_eval_result.json。

用法：
    py ops/eval_perltqa.py                     # 全量 retrieval + 抽样 context
    py ops/eval_perltqa.py --retrieval-n 200   # retrieval 也抽样（快速）
    py ops/eval_perltqa.py --gate              # 门禁模式（阈值退出码）
"""
from __future__ import annotations

import argparse
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
from dnamemory.models import MemoryConfig  # noqa: E402

DB_PATH = ROOT / "ops" / "data" / "perltqa_memory.db"
MAP_PATH = ROOT / "ops" / "data" / "perltqa_mapping.json"
QA_PATH = ROOT / "data" / "perltqa_zh.json"
OUT_PATH = ROOT / "ops" / "data" / "perltqa_eval_result.json"
MODEL_PATH = os.environ.get(
    "DNAMEMORY_BGE_PATH", r"F:\wangan_agent\models\embedding\bge-m3")
CATS = ("profile", "social_relationship", "events", "dialogues")
GATES = {"retrieval_hit": 0.7, "answer_in_context": 0.6}


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _as_qas(obj):
    """统一为 QA dict 列表：兼容 list[QA]、单个 QA dict、嵌套 dict。"""
    if isinstance(obj, list):
        return [q for q in obj if isinstance(q, dict)]
    if isinstance(obj, dict):
        if "Question" in obj:
            return [obj]
        out = []
        for v in obj.values():
            out.extend(_as_qas(v))
        return out
    return []


def iter_qa(qa_data, mapping):
    """展开为统一条目：{persona, cat, ref_id, q, answer, anchors, nid}。"""
    for item in qa_data:
        persona = next(iter(item))
        p = item[persona]
        pm = mapping.get(persona)
        if pm is None:
            continue
        for cat in CATS:
            block = p.get(cat)
            if not block:
                continue
            if isinstance(block, list):           # profile：list[QA]
                entries = [(None, block)]
            else:                                  # 其它：{ref_id: ...}
                entries = list(block.items())
            for ref_id, qas_obj in entries:
                for qa in _as_qas(qas_obj):
                    anchors = [t for a in (qa.get("Memory Anchors") or [])
                               for t in a.keys()]
                    yield {
                        "persona": persona, "cat": cat, "ref_id": ref_id,
                        "q": qa["Question"], "answer": qa["Answer"],
                        "anchors": anchors,
                        "nid": _target_nid(pm, cat, ref_id),
                    }


def _target_nid(pm, cat, ref_id):
    if cat == "profile":
        return pm["protagonist_nid"]
    if cat == "social_relationship":
        return pm["characters"].get(ref_id)
    if cat == "events":
        return pm["events"].get(ref_id)
    if cat == "dialogues":
        return pm["dialogues"].get(ref_id)
    return None


def _usable_anchors(row):
    """过滤主角名/过短锚点（出现即命中，没有区分度）。"""
    return [a for a in row["anchors"]
            if a and a != row["persona"] and len(a.strip()) >= 2]


def _ctx_text(ctx):
    parts = []
    for f in list(ctx.current_state) + list(ctx.historical_changes):
        parts.append(f"{getattr(f, 'key', '')}={getattr(f, 'value', '')}")
    for e in list(ctx.recent_events):
        parts.append(f"{getattr(e, 'name', '')} {getattr(e, 'description', '')}")
    for b in list(ctx.beliefs) + list(ctx.intents):
        parts.append(getattr(b, "proposition", "") or "")
    for c in list(ctx.conflicts):
        for f in getattr(c, "facts", []) or []:
            parts.append(str(getattr(f, "value", "")))
    for ch in list(ctx.temporal_chains):
        for nd in getattr(ch, "nodes", []) or []:
            parts.append(str(getattr(nd, "name", "")))
    return "\n".join(parts)


def main():
    _force_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--retrieval-n", type=int, default=0,
                    help="retrieval 评测抽样条数（0=全量）")
    ap.add_argument("--ctx-per-cat", type=int, default=5,
                    help="context 评测每人物每类抽样上限（0=全量）")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--reranker", action="store_true",
                    help="接入本地 bge-reranker-v2-m3 交叉编码器重排")
    ap.add_argument("--reranker-path",
                    default=os.environ.get(
                        "DNAMEMORY_RERANKER_PATH",
                        r"F:\wangan_agent\models\reranker\bge-reranker-v2-m3"))
    ap.add_argument("--chat-min-sim", type=float, default=None,
                    help="覆盖 chat 类节点稠密阈值（A/B 用；缺省用库配置）")
    ap.add_argument("--hyde", action="store_true",
                    help="HyDE 查询改写（DeepSeek，攻语义鸿沟）")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    qa_data = json.loads(QA_PATH.read_text(encoding="utf-8"))
    mapping = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    rows = list(iter_qa(qa_data, mapping))
    rng = random.Random(args.seed)

    print(f"[eval] QA 总数 {len(rows)}，加载库与模型…")
    reranker = None
    if args.reranker:
        from dnamemory.rerank import BGEReranker
        reranker = BGEReranker(model_name=args.reranker_path)
    config = None
    if args.chat_min_sim is not None:
        config = MemoryConfig(dense_min_sim_by_kind={"chat": args.chat_min_sim})
    rewriter = None
    if args.hyde:
        from dnamemory.hyde import DeepSeekHyDERewriter
        rewriter = DeepSeekHyDERewriter()
    mem = MemorySystem(args.db, embedder=BGEM3Embedder(model_name=MODEL_PATH),
                       reranker=reranker, config=config,
                       query_rewriter=rewriter)

    # ---- retrieval hit@k（全量或抽样） ----
    r_rows = rows if args.retrieval_n <= 0 else rng.sample(
        rows, min(args.retrieval_n, len(rows)))
    r_stat = {c: [0, 0] for c in CATS}   # cat -> [hit, n]
    r_fail = []
    for i, row in enumerate(r_rows):
        if row["nid"] is None:
            continue
        hits = mem.recall(RecallQuery(text=row["q"]), k=args.k, mode="triple")
        ok = any(h.node_id == row["nid"] for h in hits)
        r_stat[row["cat"]][1] += 1
        r_stat[row["cat"]][0] += int(ok)
        if not ok and len(r_fail) < 10:
            r_fail.append({"cat": row["cat"], "q": row["q"],
                           "expect": row["answer"][:40]})
        if (i + 1) % 300 == 0:
            print(f"[eval] retrieval {i + 1}/{len(r_rows)}")

    # ---- answer_in_context（按层抽样） ----
    if args.ctx_per_cat > 0:
        buckets = {}
        for row in rows:
            buckets.setdefault((row["persona"], row["cat"]), []).append(row)
        c_rows = []
        for key, bucket in buckets.items():
            c_rows.extend(rng.sample(bucket,
                                     min(args.ctx_per_cat, len(bucket))))
    else:
        c_rows = rows
    c_stat = {c: [0, 0] for c in CATS}
    c_fail = []
    for i, row in enumerate(c_rows):
        anchors = _usable_anchors(row)
        if not anchors:
            continue
        ctx = mem.recall_context(RecallQuery(text=row["q"]))
        text = _ctx_text(ctx)
        ok = any(a in text for a in anchors)
        c_stat[row["cat"]][1] += 1
        c_stat[row["cat"]][0] += int(ok)
        if not ok and len(c_fail) < 10:
            c_fail.append({"cat": row["cat"], "q": row["q"],
                           "anchors": anchors[:3]})
        if (i + 1) % 100 == 0:
            print(f"[eval] context {i + 1}/{len(c_rows)}")

    def _rate(stat):
        per = {c: {"rate": round(h / n, 4) if n else None, "n": n}
               for c, (h, n) in stat.items()}
        th = sum(h for h, _ in stat.values())
        tn = sum(n for _, n in stat.values())
        return per, round(th / tn, 4) if tn else 0.0

    r_per, r_all = _rate(r_stat)
    c_per, c_all = _rate(c_stat)
    result = {"dataset": "PerLTQA-zh", "k": args.k, "mode": "triple",
              "reranker": bool(args.reranker),
              "chat_min_sim": args.chat_min_sim,
              "hyde": bool(args.hyde),
              "retrieval_hit": {"overall": r_all, "by_cat": r_per,
                                "n": len(r_rows), "fail_samples": r_fail},
              "answer_in_context": {"overall": c_all, "by_cat": c_per,
                                    "n": len(c_rows), "fail_samples": c_fail},
              "db": args.db}
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"[eval] retrieval_hit@{args.k}: {r_all}  {r_per}")
    print(f"[eval] answer_in_context: {c_all}  {c_per}")
    print(f"[eval] 已保存 {args.out}")

    if args.gate:
        bad = []
        if r_all < GATES["retrieval_hit"]:
            bad.append(f"retrieval_hit {r_all} < {GATES['retrieval_hit']}")
        if c_all < GATES["answer_in_context"]:
            bad.append(
                f"answer_in_context {c_all} < {GATES['answer_in_context']}")
        if bad:
            print("门禁未通过: " + "; ".join(bad))
            return 1
        print("门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
