# -*- coding: utf-8 -*-
"""MVP 阶段测试：200 条杂乱日常片段 → DeepSeek 拆解导入 → 记忆指标评测。
运行：py tools/mvp_llm_test.py
"""
from __future__ import annotations

import os
import random
import re
import sys
import time
from collections import defaultdict
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from dnamemory import MemorySystem, RecallQuery  # noqa: E402
from dnamemory.extract import DeepSeekExtractor, FallbackExtractor  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def load_fragments():
    with open(os.path.join(DATA, "memory_fragments_200.txt"),
              encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def materialize_relative_dates(text, as_of):
    """把语料中的相对时间换算成真实环境日期（采集当日 2026-08-03 前后）。

    这是"无时间信息被填当天时间戳"的数据侧修复：找数据/生成语料时必须
    携带真实日期上下文，而不是让抽取器事后编造。
    """
    weekday_map = {
        "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    }
    out = text
    # 下周一~下周日
    for cn, idx in weekday_map.items():
        d = as_of + timedelta(days=(7 - as_of.weekday()) + idx)
        out = out.replace(f"下周{cn}", d.isoformat())
    # 本周末/周末 → 最近一个周六
    sat = as_of + timedelta(days=(5 - as_of.weekday()) % 7)
    out = out.replace("本周末", sat.isoformat()).replace("周末", sat.isoformat())
    for token, delta in (("前天", -2), ("昨天", -1), ("今天", 0),
                         ("明天", 1), ("后天", 2)):
        d = (as_of + timedelta(days=delta)).isoformat()
        out = out.replace(token, d)
    # 一天中的时段（晚上/上午等）在"日常记录"语料中隐含当天：
    # 若文本尚未出现具体日期，则把该时段归到采集当日，体现真实环境。
    if not re.search(r"\d{4}-\d{2}-\d{2}", out):
        for slot in ("早上", "清晨", "上午", "中午", "下午", "傍晚",
                     "晚上", "深夜", "半夜", "凌晨"):
            if slot in out:
                out = out.replace(slot, f"{as_of.isoformat()} {slot}", 1)
                break
    # 下个月 → 下月 1 日（仅当明确提到“下个月”）
    if "下个月" in out:
        if as_of.month == 12:
            nxt = as_of.replace(year=as_of.year + 1, month=1, day=1)
        else:
            nxt = as_of.replace(month=as_of.month + 1, day=1)
        out = out.replace("下个月", nxt.isoformat())
    return out


def build_eval_dataset(mem):
    nodes = mem.store.fetch_nodes()
    by_id = {n.nid: n for n in nodes}
    edges = mem.store.fetch_edges()
    event_links = defaultdict(set)
    for e in edges:
        if e.lifecycle != "active":
            continue
        if by_id[e.from_id].node_type == "event":
            event_links[e.to_id].add(e.from_id)
        if by_id[e.to_id].node_type == "event":
            event_links[e.from_id].add(e.to_id)

    random.seed(42)
    entities = [n for n in nodes if n.node_type == "entity"
                and event_links.get(n.nid)]
    random.shuffle(entities)
    dataset = []
    for ent in entities[:80]:
        truth = event_links.get(ent.nid, set())
        if truth:
            dataset.append((RecallQuery(topic=[ent.name], text=ent.name),
                            truth, "topic"))

    events_ts = [n for n in nodes if n.node_type == "event" and n.ts]
    for n in random.sample(events_ts, min(60, len(events_ts))):
        truth = {x.nid for x in events_ts if abs((x.ts - n.ts).days) <= 1}
        dataset.append((RecallQuery(time=(n.ts, 2)), truth, "time"))
    return dataset


def link_mentions(mem):
    """确定性提及链接：事件摘要中出现实体名 → mentions 边（补齐图谱路）。"""
    nodes = mem.store.fetch_nodes()
    events = [n for n in nodes if n.node_type == "event"]
    entities = [n for n in nodes if n.node_type == "entity"]
    existing = set()
    for e in mem.store.fetch_edges():
        existing.add((e.from_id, e.to_id))
        existing.add((e.to_id, e.from_id))
    added = 0
    for ev in events:
        for ent in entities:
            if ent.name and len(ent.name) >= 2 and ent.name in ev.name:
                if (ev.nid, ent.nid) not in existing:
                    mem.add_edge(ev.nid, ent.nid, "mentions", 0.6, 0.7,
                                 valid_at=ev.ts or mem.clock())
                    existing.add((ev.nid, ent.nid))
                    added += 1
    return added


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-import", action="store_true",
                        help="复用 data/mvp_memory.db，跳过 DeepSeek 导入")
    parser.add_argument("--bge-m3", action="store_true",
                        help="启用 bge-m3：节点写入稠密+稀疏向量，三路走真实稠密路")
    args = parser.parse_args()

    fragments = load_fragments()
    print(f"片段总数: {len(fragments)}")
    as_of = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    print(f"语料相对时间基准（真实环境）: {as_of.isoformat()} "
          f"({ZoneInfo('Asia/Shanghai')})")
    fragments = [materialize_relative_dates(f, as_of) for f in fragments]
    print("时间换算示例：")
    for f in fragments[:3]:
        print(f"  -> {f[:70]}")
    meta = {
        "today": as_of.isoformat(),
        "timezone": "Asia/Shanghai",
        "recorded_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
    }
    db_path = os.path.join(DATA, "mvp_memory.db")
    if os.path.exists(db_path) and not args.no_import:
        os.remove(db_path)
    embedder = None
    if args.bge_m3:
        from dnamemory.embeddings import BGEM3Embedder  # noqa: E402
        print("加载 bge-m3（首次运行会下载模型，约 2.3GB）...")
        embedder = BGEM3Embedder()
        print("bge-m3 加载完成")
    mem = MemorySystem(path=db_path, embedder=embedder)

    if args.no_import:
        print("复用已导入数据库，跳过拆解导入")
        result = None
        elapsed = 0.0
    else:
        extractor = DeepSeekExtractor()
        t0 = time.time()
        print(f"开始 DeepSeek 批量拆解导入（batch=20, model="
              f"{extractor.model}）...")
        result = mem.write_many(fragments, meta=meta, extractor=extractor,
                                batch_size=20)
        elapsed = time.time() - t0

    nodes = mem.store.fetch_nodes()
    events = [n for n in nodes if n.node_type == "event"]
    entities = [n for n in nodes if n.node_type == "entity"]
    facts = mem.store.fetch_facts()
    edges = mem.store.fetch_edges()
    print(f"\n导入耗时: {elapsed:.1f}s")
    if result:
        print(f"写入结果: accepted={result.accepted}, "
              f"dropped={len(result.rejected)}")
        drop_stat = Counter((t, r.split(":")[0]) for t, r in result.rejected)
        if drop_stat:
            print("丢弃明细（类型, 原因 -> 数量）：")
            for (t, reason), n in drop_stat.most_common():
                print(f"  {t}: {reason} -> {n}")
        else:
            print("丢弃明细：无")
    print(f"节点: 事件={len(events)}, 实体={len(entities)}")
    print(f"事实: {len(facts)}, 关联边: {len(edges)}")
    kind_stat = defaultdict(int)
    for n in events:
        kind_stat[n.kind] += 1
    print(f"事件类型分布: {dict(kind_stat)}")
    print(f"事件带时间戳: {len([n for n in events if n.ts])}/{len(events)}")
    print(f"事件无时间戳: {len([n for n in events if not n.ts])}/{len(events)}")
    if embedder is not None:
        vecs = mem.store.fetch_node_vectors()
        print(f"节点向量: {len(vecs)} 条, "
              f"dim={vecs[0][4] if vecs else '-'}, "
              f"model={vecs[0][3] if vecs else '-'}")

    if not args.no_import:
        extractor = DeepSeekExtractor()
        print("\n--- 拆解样例（前 5 条片段对应的抽取结果）---")
        for frag in fragments[:5]:
            cands = extractor.extract(frag, meta)
            print(f"[片段] {frag[:40]}... "
                  f"(校验拒绝 {len(extractor.last_rejects)})")
            for c in cands:
                print(f"  -> {c.type}: {c.name} kind={c.kind} "
                      f"score={c.value_score}"
                      + (f" ts={c.ts.isoformat()}" if c.ts else ""))

    added = link_mentions(mem)
    print(f"\n提及链接补边: +{added} 条 mentions 边")
    edges = mem.store.fetch_edges()
    print(f"当前关联边总数: {len(edges)}")

    print("\n--- 记忆指标评测（基于导入数据自建真值）---")
    dataset = build_eval_dataset(mem)
    print(f"评测查询: topic={sum(1 for _, _, l in dataset if l == 'topic')}, "
          f"time={sum(1 for _, _, l in dataset if l == 'time')}")
    for mode in ("dual", "triple"):
        agg, by_label = mem.evaluate(dataset, k=5, mode=mode)
        print(f"\n{mode}: Recall@5={agg['recall']:.3f} "
              f"Precision@5={agg['precision']:.3f} MRR={agg['mrr']:.3f} "
              f"MAP@5={agg['map']:.3f} NDCG@5={agg['ndcg']:.3f}")
        for label in ("topic", "time"):
            bl = by_label.get(label)
            if bl:
                print(f"  {label}: Recall@5={bl['recall']:.3f} "
                      f"MAP@5={bl['map']:.3f} (n={bl['n']})")

    mem.close()
    print("\n完成。评测数据已入库: data/mvp_memory.db")


if __name__ == "__main__":
    main()
