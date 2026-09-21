# -*- coding: utf-8 -*-
"""PerLTQA 生成式评测（0.8.2 四轮）：端到端「答对率」口径。

与 eval_perltqa.py 的锚点包含口径（answer_in_context，「答案文本在
上下文里」的代理指标）不同，本脚本走真实生成链路：

  recall_context → 上下文区块 → DeepSeek 生成回答 → DeepSeek 裁判
  对照标准答案判语义对错（同义算对，如「朋友」≈「熟人」——锚点口径
  结构性不可赢的样本在此口径下可赢）。

抽样逻辑与 eval_perltqa.py 完全一致（同 seed 同 ctx-per-cat），产出可
与 answer_in_context 严格对拍；同时记录每条样本的锚点命中以便逐条
对比两种口径。输出 ops/data/perltqa_eval_gen.json。

用法：
    py ops/eval_perltqa_gen.py                    # 同 128 题抽样，reranker 开
    py ops/eval_perltqa_gen.py --limit 20         # 快速冒烟
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
sys.path.insert(0, str(ROOT / "ops"))

from dnamemory import MemorySystem, RecallQuery  # noqa: E402
from dnamemory.embeddings import BGEM3Embedder  # noqa: E402
from eval_perltqa import (CATS, MAP_PATH, MODEL_PATH, QA_PATH,  # noqa: E402
                          _ctx_text, _force_utf8, _usable_anchors, iter_qa)

GEN_PROMPT = (
    "你是个人记忆问答助手。下面是从用户记忆中召回的相关内容，"
    "请只依据这些内容回答问题；内容不足以回答时就回答「不知道」。\n"
    "注意：问题指向唯一答案时只回答最确定的那一个，不要罗列多个候选；"
    "问什么答什么，不要扩展。\n\n"
    "【记忆】\n{ctx}\n\n【问题】{q}\n\n请简洁回答：")
JUDGE_PROMPT = (
    "请判断「回答」相对「标准答案」是否正确：语义一致即算对"
    "（同义表述、更详细的正确表述都算对，如「朋友」≈「熟人」）；"
    "信息缺失、答非所问、与标准答案矛盾都算错；回答「不知道」算错。"
    "只输出一个字：对 或 错。\n\n"
    "问题：{q}\n标准答案：{gold}\n回答：{gen}")


class _DeepSeek:
    def __init__(self, model):
        from dnamemory.extract import find_deepseek_key
        self.model = model
        self._key = find_deepseek_key()[1]
        if not self._key:
            raise ValueError("未找到 DEEPSEEK_API_KEY")

    def chat(self, prompt, max_tokens=300):
        import requests
        payload = {"model": self.model,
                   "messages": [{"role": "user", "content": prompt}],
                   "temperature": 0.0, "max_tokens": max_tokens}
        last = None
        for _ in range(2):
            try:
                r = requests.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": f"Bearer {self._key}",
                             "Content-Type": "application/json"},
                    json=payload, timeout=60)
                r.raise_for_status()
                return (r.json()["choices"][0]["message"]["content"]
                        or "").strip()
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"DeepSeek 调用失败：{last}")


def main():
    _force_utf8()
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "ops" / "data"
                                        / "perltqa_memory.db"))
    ap.add_argument("--ctx-per-cat", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--limit", type=int, default=0, help="冒烟用：只评前 N 条")
    ap.add_argument("--no-reranker", action="store_true")
    ap.add_argument("--gen-model", default=os.environ.get(
        "DEEPSEEK_MODEL") or "deepseek-chat")
    ap.add_argument("--judge-model", default=None)
    ap.add_argument("--out", default=str(ROOT / "ops" / "data"
                                         / "perltqa_eval_gen.json"))
    args = ap.parse_args()

    qa_data = json.loads(QA_PATH.read_text(encoding="utf-8"))
    mapping = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    rows = list(iter_qa(qa_data, mapping))
    rng = random.Random(args.seed)
    buckets = {}
    for row in rows:
        buckets.setdefault((row["persona"], row["cat"]), []).append(row)
    c_rows = []
    for key, bucket in buckets.items():
        c_rows.extend(rng.sample(bucket,
                                 min(args.ctx_per_cat, len(bucket))))
    if args.limit:
        c_rows = c_rows[:args.limit]
    print(f"[gen-eval] 样本 {len(c_rows)} 条，加载库与模型…")

    reranker = None
    if not args.no_reranker:
        from dnamemory.rerank import BGEReranker
        reranker = BGEReranker(model_name=os.environ.get(
            "DNAMEMORY_RERANKER_PATH",
            r"F:\wangan_agent\models\reranker\bge-reranker-v2-m3"))
    mem = MemorySystem(args.db, embedder=BGEM3Embedder(model_name=MODEL_PATH),
                       reranker=reranker)
    gen = _DeepSeek(args.gen_model)
    judge = _DeepSeek(args.judge_model or args.gen_model)

    stat = {c: [0, 0] for c in CATS}       # gen 正确
    a_stat = {c: [0, 0] for c in CATS}     # 同样本锚点命中（口径对拍）
    fails, errors = [], []
    for i, row in enumerate(c_rows):
        try:
            ctx = mem.recall_context(RecallQuery(text=row["q"]))
            text = _ctx_text(ctx) + "\n" + "\n".join(ctx.notes or [])
            anchors = _usable_anchors(row)
            a_hit = bool(anchors) and any(a in text for a in anchors)
            answer = gen.chat(GEN_PROMPT.format(ctx=text[:6000],
                                                q=row["q"]))
            verdict = judge.chat(JUDGE_PROMPT.format(
                q=row["q"], gold=row["answer"], gen=answer), max_tokens=8)
            ok = verdict.startswith("对")
        except Exception as e:  # noqa: BLE001 单条失败不中断，单列错误桶
            errors.append({"cat": row["cat"], "q": row["q"], "err": str(e)[:80]})
            continue
        stat[row["cat"]][1] += 1
        stat[row["cat"]][0] += int(ok)
        if anchors:
            a_stat[row["cat"]][1] += 1
            a_stat[row["cat"]][0] += int(a_hit)
        if not ok and len(fails) < 15:
            fails.append({"cat": row["cat"], "q": row["q"],
                          "gold": row["answer"][:60], "gen": answer[:60],
                          "anchor_hit": a_hit})
        if (i + 1) % 20 == 0:
            print(f"[gen-eval] {i + 1}/{len(c_rows)}")

    def _rate(stat_):
        per = {c: {"rate": round(h / n, 4) if n else None, "n": n}
               for c, (h, n) in stat_.items()}
        th = sum(h for h, _ in stat_.values())
        tn = sum(n for _, n in stat_.values())
        return per, round(th / tn, 4) if tn else 0.0

    g_per, g_all = _rate(stat)
    a_per, a_all = _rate(a_stat)
    result = {"dataset": "PerLTQA-zh", "metric": "generative_qa",
              "gen_model": args.gen_model,
              "judge_model": args.judge_model or args.gen_model,
              "reranker": not args.no_reranker, "seed": args.seed,
              "ctx_per_cat": args.ctx_per_cat,
              "gen_correct": {"overall": g_all, "by_cat": g_per,
                              "n": len(c_rows), "errors": len(errors)},
              "anchor_hit_same_sample": {"overall": a_all, "by_cat": a_per},
              "fail_samples": fails, "error_samples": errors[:10],
              "db": args.db}
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"[gen-eval] 生成式答对率: {g_all}  {g_per}")
    print(f"[gen-eval] 同样本锚点口径: {a_all}（对拍用）  错误 {len(errors)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
