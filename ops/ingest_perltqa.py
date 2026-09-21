# -*- coding: utf-8 -*-
"""PerLTQA 中文个人长期记忆数据集全流程注入脚本（0.8.1 正式基准）。

数据集：PerLTQA zh（Elvin-Yiming-Du/PerLTQA，141 个虚拟人物：
结构化 profile / 社会关系 / 事件 / 时间戳对话）——正式发表的中文
个人长期记忆基准，QA 标注 1905 条（data/perltqa_zh.json）。

注入策略（确定性结构化映射，无 LLM 抽取方差）：
- profile 字段 → 主角实体 + fact（source="profile"）
- 社会关系 → 人物实体 + 主角关系 fact + occurs_with 边
- 事件 → event 节点（summary 为名、content 为描述、中文日期解析为 ts），
  主角与出场人物 participates 边
- 对话 → event 节点（每场一条，转写全文为描述，会话时间为 ts），
  主角 discusses 边、与关联事件 summarizes 边
- 全部对象绑定 evidence（source_type=external_data，指向数据集坐标）

向量：默认使用本地 bge-m3（F:\\wangan_agent\\models\\embedding\\bge-m3，
可用 DNAMEMORY_BGE_PATH 覆盖）；批量嵌入按 chunk 写入。
产出：ops/data/perltqa_memory.db + perltqa_mapping.json（评测用 id 映射）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dnamemory import MemorySystem  # noqa: E402
from dnamemory.embeddings import BGEM3Embedder  # noqa: E402

DB_PATH = ROOT / "ops" / "data" / "perltqa_memory.db"
MAP_PATH = ROOT / "ops" / "data" / "perltqa_mapping.json"
REPORT_PATH = ROOT / "ops" / "data" / "perltqa_ingest_report.json"
MEM_PATH = ROOT / "data" / "perltmem_zh.json"
MODEL_PATH = os.environ.get(
    "DNAMEMORY_BGE_PATH", r"F:\wangan_agent\models\embedding\bge-m3")
EMBED_CHUNK = 256
PROFILE_SKIP = {"Protagonist"}          # 主角名已是实体本身
REL = {"protagonist": "occurs_with", "event": "participates",
       "dialogue": "discusses", "dlg_event": "summarizes"}


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _cn_date(text):
    """「2022年5月12日」→ datetime；失败返回 None。"""
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _dlg_ts(text):
    try:
        return datetime.fromisoformat(text.strip())
    except (ValueError, AttributeError):
        return None


def _hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _evidence(mem, ref, text, observed_at, conn):
    return mem.store.insert_evidence(
        "external_data", ref, None, None, observed_at, _hash(text), 5,
        {"dataset": "PerLTQA-zh"}, observed_at,
        idempotency_key=f"perltqa:ev:{ref}", conn=conn)


def main():
    _force_utf8()
    if DB_PATH.exists():
        DB_PATH.unlink()
    personas = json.loads(MEM_PATH.read_text(encoding="utf-8"))
    # --limit N：只注入前 N 个有 QA 覆盖的人物（QA 文件顺序），
    # 小库冒烟（CPU 友好）；0=全量
    limit = int(os.environ.get("PERLTQA_LIMIT", "0") or 0)
    if limit > 0:
        qa_names = [next(iter(x)) for x in json.loads(
            (ROOT / "data" / "perltqa_zh.json").read_text(encoding="utf-8"))]
        by_name = {p["profile"]["Protagonist"]: p for p in personas}
        personas = [by_name[n] for n in qa_names[:limit] if n in by_name]
        print(f"[ingest] 小库模式：前 {len(personas)} 个 QA 人物")
    print(f"[ingest] {len(personas)} 个人物，模型 {MODEL_PATH}")
    embedder = BGEM3Embedder(model_name=MODEL_PATH)
    mem = MemorySystem(str(DB_PATH), embedder=embedder)
    now = datetime(2026, 9, 19, 9, 0)   # 统一 recorded_at（确定性）
    mapping, embed_queue, stats = {}, [], {}

    def bump(k, n=1):
        stats[k] = stats.get(k, 0) + n

    def flush_embed(conn):
        nonlocal embed_queue
        for i in range(0, len(embed_queue), EMBED_CHUNK):
            mem._embed_and_store(embed_queue[i:i + EMBED_CHUNK])
        embed_queue = []

    for pi, p in enumerate(personas):
        prof = p["profile"]
        who = prof["Protagonist"]
        pm = {"characters": {}, "profile_facts": {}, "rel_facts": {},
              "events": {}, "dialogues": {}}
        with mem.store.transaction() as conn:
            ev_prof = _evidence(mem, f"perltqa-zh:{who}/profile",
                                json.dumps(prof, ensure_ascii=False), now, conn)
            nid_p = mem.store.insert_node(
                "entity", "person", who, p["profile_description"], None, 0.9,
                True, "public", 0.0, 0.0, now, evidence_ids=[ev_prof],
                idempotency_key=f"perltqa:{who}:protagonist", conn=conn)
            pm["protagonist_nid"] = nid_p
            embed_queue.append((nid_p, f"{who} {p['profile_description'][:200]}"))
            bump("entities")
            for field, value in prof.items():
                if field in PROFILE_SKIP:
                    continue
                fid = mem.store.insert_fact(
                    nid_p, field, str(value), "profile", 0.9, now, now,
                    evidence_ids=[ev_prof],
                    idempotency_key=f"perltqa:{who}:profile:{field}", conn=conn)
                pm["profile_facts"][field] = fid
                bump("facts")
            # 社会关系
            for rid, rel in p.get("social_relationship", {}).items():
                cname = rel["Supporting Characters"]
                desc = rel["Description"]
                ev_rel = _evidence(mem, f"perltqa-zh:{who}/social/{rid}",
                                   desc, now, conn)
                nid_c = mem.store.insert_node(
                    "entity", "person", cname, desc, None, 0.7, False,
                    "public", 0.0, 0.0, now, evidence_ids=[ev_rel],
                    idempotency_key=f"perltqa:{who}:char:{rid}", conn=conn)
                fid = mem.store.insert_fact(
                    nid_p, f"关系_{cname}", rel["Relationship"], "profile",
                    0.9, now, now, evidence_ids=[ev_rel],
                    idempotency_key=f"perltqa:{who}:relfact:{rid}", conn=conn)
                mem.store.insert_edge(
                    nid_p, nid_c, REL["protagonist"], 0.8, 0.9, now, None,
                    now, idempotency_key=f"perltqa:{who}:edge:{rid}", conn=conn)
                pm["characters"][rid] = nid_c
                pm["rel_facts"][rid] = fid
                embed_queue.append((nid_c, f"{cname} {desc[:200]}"))
                bump("entities")
                bump("facts")
                bump("edges")
            # 事件
            for eid, ev in p.get("events", {}).items():
                ts = _cn_date(ev.get("Creation Time"))
                ev_ev = _evidence(mem, f"perltqa-zh:{who}/events/{eid}",
                                  ev["content"], ts or now, conn)
                nid_e = mem.store.insert_node(
                    "event", "life", ev["summary"], ev["content"], ts, 0.6,
                    False, "public", 50.0, 0.28, now, evidence_ids=[ev_ev],
                    idempotency_key=f"perltqa:{who}:event:{eid}", conn=conn)
                pm["events"][eid] = nid_e
                embed_queue.append((nid_e, f"{ev['summary']}\n{ev['content']}"))
                bump("events")
                mem.store.insert_edge(
                    nid_p, nid_e, REL["event"], 0.9, 0.9, ts or now, None,
                    now, idempotency_key=f"perltqa:{who}:pe:{eid}", conn=conn)
                bump("edges")
                chars = ev.get("Characters") or []
                if isinstance(chars, str):
                    chars = re.findall(r"'([^']+)'", chars)
                for cid in chars:
                    cnid = pm["characters"].get(cid)
                    if cnid:
                        mem.store.insert_edge(
                            cnid, nid_e, REL["event"], 0.8, 0.9, ts or now,
                            None, now,
                            idempotency_key=f"perltqa:{who}:ce:{eid}:{cid}",
                            conn=conn)
                        bump("edges")
            # 对话
            for did, dlg in p.get("dialogues", {}).items():
                linked = dlg.get("events")
                linked_ids = linked if isinstance(linked, list) \
                    else ([linked] if linked else [])
                ev_summary = next(
                    (p["events"][lid]["summary"] for lid in linked_ids
                     if lid in p.get("events", {})), "")
                for sess_ts, turns in dlg["contents"].items():
                    ts = _dlg_ts(sess_ts)
                    # 少量 turn 是嵌套 list（78/25246），拍平为字符串行
                    flat = []
                    for t in turns:
                        if isinstance(t, list):
                            flat.extend(str(x) for x in t)
                        else:
                            flat.append(str(t))
                    text = "\n".join(flat)
                    # 节点名带会话时间保证唯一（同名「对话：X」几十场是
                    # dialogues 类检索的主瓶颈）；描述保留完整转写
                    tag = ts.strftime("%m-%d %H:%M") if ts else sess_ts
                    name = (f"对话：{ev_summary}（{tag}）" if ev_summary
                            else f"对话记录（{tag}）")
                    ev_d = _evidence(
                        mem, f"perltqa-zh:{who}/dialogues/{did}/{sess_ts}",
                        text, ts or now, conn)
                    nid_d = mem.store.insert_node(
                        "event", "chat", name, text, ts, 0.5, False,
                        "public", 30.0, 0.32, now, evidence_ids=[ev_d],
                        idempotency_key=f"perltqa:{who}:dlg:{did}:{sess_ts}",
                        conn=conn)
                    # 多场会话：评测目标映射保留首场（其余场照常入库）
                    pm["dialogues"].setdefault(did, nid_d)
                    embed_queue.append((nid_d, f"{name}\n{text}"))
                    bump("events")
                    mem.store.insert_edge(
                        nid_p, nid_d, REL["dialogue"], 0.7, 0.9, ts or now,
                        None, now,
                        idempotency_key=f"perltqa:{who}:pd:{did}:{sess_ts}",
                        conn=conn)
                    bump("edges")
                    for lid in linked_ids:
                        if lid in pm["events"]:
                            mem.store.insert_edge(
                                nid_d, pm["events"][lid], REL["dlg_event"],
                                0.9, 0.9, ts or now, None, now,
                                idempotency_key=(
                                    f"perltqa:{who}:de:{did}:{sess_ts}:{lid}"),
                                conn=conn)
                            bump("edges")
        if len(embed_queue) >= 512:   # CPU 大批次嵌入更高效，攒够再刷
            flush_embed(conn)
        mapping[who] = pm
        if (pi + 1) % 20 == 0 or pi + 1 == len(personas):
            print(f"[ingest] {pi + 1}/{len(personas)} 人物完成")

    flush_embed(None)

    MAP_PATH.write_text(json.dumps(mapping, ensure_ascii=False),
                        encoding="utf-8")
    final = {
        "nodes": len(mem.store.fetch_nodes()),
        "entities": sum(1 for n in mem.store.fetch_nodes()
                        if n.node_type == "entity"),
        "events": sum(1 for n in mem.store.fetch_nodes()
                      if n.node_type == "event"),
        "facts": len(mem.store.fetch_facts()),
        "evidence": len(mem.store.fetch_evidence()),
        "edges": len(mem.store.fetch_edges()),
        "vectors": len(mem.store.fetch_node_vectors()),
    }
    REPORT_PATH.write_text(json.dumps(
        {"dataset": "PerLTQA-zh", "personas": len(personas),
         "model": MODEL_PATH, "writes": stats, "final": final,
         "db": str(DB_PATH)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ingest] 完成: {final}")
    mem.close()


if __name__ == "__main__":
    main()
