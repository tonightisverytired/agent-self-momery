# -*- coding: utf-8 -*-
"""状态正确性评测（0.5.0）：temporal_chain / belief_change / fact_conflict /
evidence_grounding / cross_dimension 五类，自包含语料。

运行：
  py tools/eval_state.py
输出：simulation/state_eval_result.json + 控制台汇总。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from dnamemory import MemorySystem, RecallQuery  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUERY_TIME = datetime(2026, 9, 1)


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _parse_ts(text):
    if not text:
        return None
    return datetime.fromisoformat(text)


def build_memory(data):
    """自包含语料构建内存库（含 0.5.0 全维度写入）。"""
    mem = MemorySystem(path=":memory:", clock=lambda: QUERY_TIME)
    corpus = data.get("corpus") or {}
    for ent in corpus.get("entities", []):
        mem.add_entity(ent["name"], ent.get("kind", "concept"),
                       ent.get("description", ""))
    for f in corpus.get("facts", []):
        nid = mem._name2id.get(f["entity"])
        if nid is None:
            continue
        mem.add_fact(nid, f["key"], f["value"],
                     source=f.get("source", "chat"),
                     confidence=f.get("confidence", 0.7),
                     valid_at=_parse_ts(f.get("valid_at")),
                     invalid_at=_parse_ts(f.get("invalid_at")))
    for ev in corpus.get("events", []):
        eid = mem.add_event(ev["name"], _parse_ts(ev.get("ts")),
                            kind=ev.get("kind", "life"))
        for ent in corpus.get("entities", []):
            nid = mem._name2id.get(ent["name"])
            if nid is not None:
                mem.add_edge(eid, nid, "discusses", 0.7, 0.8,
                             valid_at=_parse_ts(ev.get("ts")))
    for b in corpus.get("beliefs", []):
        nid = mem._name2id.get(b["subject"])
        if nid is None:
            continue
        t = _parse_ts(b.get("valid_at"))
        mem.store.insert_belief(nid, b["proposition"],
                                b.get("polarity", "neutral"),
                                b.get("confidence", 0.8),
                                b.get("source", "chat"), t, None, None,
                                "active", "public", [], t)
    for i in corpus.get("intents", []):
        nid = mem._name2id.get(i["subject"])
        if nid is None:
            continue
        t = _parse_ts(i.get("valid_at"))
        mem.store.insert_intent(nid, i["proposition"],
                                i.get("status", "active"),
                                i.get("confidence", 0.7),
                                i.get("source", "chat"), t, None,
                                "active", "public", [], t)
    for e in corpus.get("evidence", []):
        mem.store.insert_evidence(
            e["source_type"], e.get("source_ref", ""), None, None,
            _parse_ts(e.get("observed_at")), e.get("content_hash"),
            e.get("trust_level"), {}, _parse_ts(e.get("observed_at"))
            or QUERY_TIME)
    for im in corpus.get("impacts", []):
        nid = mem._name2id.get(im["subject"])
        if nid is None:
            continue
        cause = im.get("cause")
        cause_id = None
        if cause:
            for n in mem.store.fetch_nodes():
                if n.node_type == "event" and n.name == cause:
                    cause_id = n.nid
                    break
        mem.store.insert_impact(
            nid, im["dimension"], im.get("direction", "increase"),
            im.get("valence", "neutral"), im.get("magnitude", 0.5),
            kind=im.get("kind", "objective"),
            evaluator=im.get("evaluator", "agent"),
            description=im.get("description", ""),
            cause_event_id=cause_id,
            source=im.get("source", "chat"),
            confidence=im.get("confidence", 0.7),
            valid_at=_parse_ts(im.get("valid_at")),
            invalid_at=_parse_ts(im.get("invalid_at")),
            created_at=_parse_ts(im.get("valid_at")) or QUERY_TIME)
    for tg in corpus.get("triggers", []):
        md = tg["memory"]
        mtype = md["type"]
        mid = None
        if mtype == "event":
            mid = mem._name2id.get(md["name"])
        elif mtype == "intent":
            for i in mem.store.fetch_intents():
                if i.proposition == md["proposition"]:
                    mid = i.id
                    break
        if mid is None:
            continue
        mem.store.insert_trigger(
            mid, mtype, tg["trigger_type"], tg["text"], source="llm",
            confidence=0.8, created_at=QUERY_TIME)
    return mem


def score_item(mem, item):
    ctx = mem.recall_context(RecallQuery(text=item["query"]),
                             query_time=QUERY_TIME)
    exp = item.get("expected") or {}
    detail, ok = {}, True

    def got(key, cond):
        nonlocal ok
        ok = ok and cond
        detail[key] = bool(cond)

    if "current_fact" in exp:
        got("current_fact",
            exp["current_fact"] in {f.value for f in ctx.current_state})
    if "history" in exp:
        got("history",
            [f.value for f in ctx.historical_changes] == exp["history"])
    if "current_belief" in exp:
        got("current_belief", bool(ctx.beliefs) and
            ctx.beliefs[0].proposition == exp["current_belief"])
    if "has_belief" in exp:
        got("has_belief", bool(ctx.beliefs))
    if "has_intent" in exp:
        got("has_intent", bool(ctx.intents))
    if "has_conflict" in exp:
        got("has_conflict", bool(ctx.conflicts))
    if "abstain" in exp:
        got("abstain", any("证据" in n for n in ctx.notes))
    if "event" in exp:
        got("event", any(exp["event"] in n.name
                         for n in ctx.recent_events))
    if "impact" in exp:
        target = exp["impact"]
        got("impact", any(
            (i.dimension, i.direction, i.valence)
            == (target["dimension"], target["direction"],
                target["valence"])
            for i in ctx.impacts))
    if "impacts_include" in exp:
        triples = {(i.dimension, i.valence) for i in ctx.impacts}
        got("impacts_include", all(
            (t["dimension"], t["valence"]) in triples
            for t in exp["impacts_include"]))
    trig_used = ctx.trace is not None and ctx.trace.trigger_count > 0
    if "trigger_event" in exp:
        got("trigger_event", trig_used and any(
            exp["trigger_event"] in n.name for n in ctx.recent_events))
    if "trigger_has_intent" in exp:
        got("trigger_has_intent", trig_used and bool(ctx.intents))
    return {"id": item["id"], "ability": item["ability"],
            "query": item["query"], "ok": ok, "detail": detail,
            "query_type": ctx.query_type}


def run(data_path, out_path):
    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)
    mem = build_memory(data)
    rows = [score_item(mem, it) for it in data["items"]]
    mem.close()
    by_ability = {}
    for row in rows:
        bl = by_ability.setdefault(row["ability"], {"ok": 0, "n": 0})
        bl["n"] += 1
        bl["ok"] += 1 if row["ok"] else 0
    report = {
        "dataset": data["name"],
        "overall": sum(1 for r in rows if r["ok"]) / max(len(rows), 1),
        "by_ability": {
            ab: {"accuracy": bl["ok"] / bl["n"], "n": bl["n"]}
            for ab, bl in by_ability.items()
        },
        "items": rows,
    }
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def main():
    _force_utf8()
    parser = argparse.ArgumentParser(description="dnamemory 状态正确性评测")
    parser.add_argument("--data",
                        default=os.path.join(ROOT, "data", "eval_state.json"))
    parser.add_argument("--out",
                        default=os.path.join(ROOT, "ops", "data",
                                             "state_eval_result.json"))
    args = parser.parse_args()
    report = run(args.data, args.out)
    print(f"整体: {report['overall']:.3f}")
    for ab, bl in report["by_ability"].items():
        print(f"  {ab:<18} accuracy={bl['accuracy']:.3f} (n={bl['n']})")
    for r in report["items"]:
        if not r["ok"]:
            print(f"  FAIL {r['id']} {r['query']}: {r['detail']}")
    print(f"结果已保存: {args.out}")
    return 0 if report["overall"] >= 0.95 else 1


if __name__ == "__main__":
    sys.exit(main())
