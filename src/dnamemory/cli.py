# -*- coding: utf-8 -*-
"""dnamemory 命令行：零配置，参数显式传入。

退出码：0 成功；1 业务/运行错误；2 参数用法错误。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta

from . import MemorySystem, RecallQuery, __version__
from .errors import MemoryError


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _handle(e):
    code = getattr(e, "code", "E000")
    print(f"Error {code}: {e}", file=sys.stderr)
    return 1


def _hit(h):
    return {"node_id": h.node_id, "node_type": h.node_type,
            "name": h.name, "ts": h.ts.isoformat() if h.ts else None,
            "score": h.score, "sources": list(h.sources)}


def cmd_add_event(args):
    mem = MemorySystem(path=args.db)
    try:
        ts = datetime.fromisoformat(args.ts) if args.ts else None
        nid = mem.add_event(args.name, ts, kind=args.kind,
                            value_score=args.value_score,
                            protected=args.protected,
                            access_label=args.access_label)
        _emit({"id": nid})
        return 0
    except (MemoryError, ValueError) as e:
        return _handle(e)
    finally:
        mem.close()


def cmd_add_entity(args):
    mem = MemorySystem(path=args.db)
    try:
        nid = mem.add_entity(args.name, kind=args.kind,
                             description=args.description,
                             protected=args.protected,
                             access_label=args.access_label)
        _emit({"id": nid})
        return 0
    except MemoryError as e:
        return _handle(e)
    finally:
        mem.close()


def cmd_add_edge(args):
    mem = MemorySystem(path=args.db)
    try:
        va = datetime.fromisoformat(args.valid_at) if args.valid_at else None
        eid = mem.add_edge(args.from_, args.to, args.rel,
                           weight=args.weight, confidence=args.confidence,
                           valid_at=va)
        _emit({"id": eid})
        return 0
    except (MemoryError, ValueError) as e:
        return _handle(e)
    finally:
        mem.close()


def cmd_add_fact(args):
    mem = MemorySystem(path=args.db)
    try:
        va = datetime.fromisoformat(args.valid_at) if args.valid_at else None
        fid = mem.add_fact(args.entity, args.key, args.value,
                           source=args.source, confidence=args.confidence,
                           valid_at=va)
        _emit({"id": fid})
        return 0
    except (MemoryError, ValueError) as e:
        return _handle(e)
    finally:
        mem.close()


def cmd_recall(args):
    mem = MemorySystem(path=args.db)
    try:
        t0 = datetime.fromisoformat(args.time) if args.time else None
        query = RecallQuery(
            text=args.text,
            time=(t0, args.tol_days) if t0 is not None else None,
            topic=args.topic or None)
        hits = mem.recall(query, k=args.k, mode=args.mode,
                          node_types=args.node_types or None,
                          rerank_top_n=args.rerank_top_n)
        _emit({"items": [_hit(h) for h in hits]})
        return 0
    except (MemoryError, ValueError) as e:
        return _handle(e)
    finally:
        mem.close()


def cmd_forget(args):
    mem = MemorySystem(path=args.db)
    try:
        target = (int(args.target) if str(args.target).strip().isdigit()
                  else args.target)
        mem.forget(target, reason=args.reason, force=args.force)
        _emit({"ok": True})
        return 0
    except MemoryError as e:
        return _handle(e)
    finally:
        mem.close()


def cmd_reflect(args):
    mem = MemorySystem(path=args.db)
    try:
        summary_id = mem.reflect_monthly(args.year, args.month)
        _emit({"summary_id": summary_id})
        return 0
    except MemoryError as e:
        return _handle(e)
    finally:
        mem.close()


def cmd_demo(args):
    """生成确定性演示库：实体+事件+关联边（seed=42），用于开箱即用体验。"""
    random.seed(42)
    mem = MemorySystem(path=args.db)
    entities = [
        ("张总", "person", "项目负责人"), ("李工", "person", "后端工程师"),
        ("项目A", "project", "核心交付项目"), ("项目B", "project", "创新项目"),
        ("微服务", "concept", "分布式架构"), ("Kubernetes", "skill", "容器编排"),
        ("预算", "concept", "资金计划"), ("供应商B", "vendor", "云服务商"),
        ("技术选型", "concept", "方案对比"), ("部署流程", "skill", "上线步骤"),
    ]
    for name, kind, desc in entities:
        mem.add_entity(name, kind, desc)
    for a, b, rel in [("张总", "项目A", "participates"),
                      ("李工", "项目A", "participates"),
                      ("项目A", "微服务", "depends_on")]:
        mem.add_edge(a, b, rel, weight=0.8, confidence=0.9)
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) \
        - timedelta(days=30)
    templates = [
        ("与张总讨论{proj}预算问题", ["张总", "预算", "项目A"], "meeting"),
        ("与李工进行技术选型", ["李工", "技术选型", "微服务"], "decision"),
        ("{proj}例会：同步供应商进展", ["供应商B", "项目A"], "meeting"),
        ("部署Kubernetes集群到生产环境", ["Kubernetes", "部署流程"], "deploy"),
    ]
    for i in range(40):
        tmpl, atoms, kind = random.choice(templates)
        name = tmpl.format(proj=random.choice(["项目A", "项目B"]))
        eid = mem.add_event(name, start + timedelta(days=i % 20), kind=kind,
                            value_score=0.5 + 0.1 * (i % 5))
        for atom in atoms:
            mem.add_edge(eid, atom, "discusses", 0.7, 0.8,
                         valid_at=start + timedelta(days=i % 20))
    mem.close()
    _emit({"ok": True, "events": 40, "db": args.db})
    return 0


def _iso(v):
    return v.isoformat() if v else None


def cmd_history(args):
    mem = MemorySystem(path=args.db)
    try:
        items = mem.memory_history(args.entity, dimension=args.dimension)
        if args.dimension == "fact":
            rows = [{"id": f.fid, "key": f.key, "value": f.value,
                     "source": f.source, "confidence": f.confidence,
                     "valid_at": _iso(f.valid_at), "invalid_at": _iso(f.invalid_at)}
                    for f in items]
        else:
            rows = [{"id": getattr(m, "id", getattr(m, "nid", None)),
                     "proposition": getattr(m, "proposition",
                                            getattr(m, "name", "")),
                     "polarity": getattr(m, "polarity", None),
                     "status": getattr(m, "status", None),
                     "ts": _iso(getattr(m, "ts", None))} for m in items]
        _emit({"entity": args.entity, "dimension": args.dimension,
               "items": rows})
        return 0
    except (MemoryError, ValueError) as e:
        return _handle(e)
    finally:
        mem.close()


def cmd_timeline(args):
    mem = MemorySystem(path=args.db)
    try:
        start = datetime.fromisoformat(args.start) if args.start else None
        end = datetime.fromisoformat(args.end) if args.end else None
        nodes = mem.timeline(entity_id=args.entity, start=start, end=end)
        _emit({"nodes": [
            {"dimension": n.dimension, "relation": n.relation,
             "at": _iso(n.at),
             "id": getattr(n.memory, "nid",
                           getattr(n.memory, "fid",
                                   getattr(n.memory, "id", None))),
             "name": getattr(n.memory, "name",
                             getattr(n.memory, "value", ""))}
            for n in nodes]})
        return 0
    except (MemoryError, ValueError) as e:
        return _handle(e)
    finally:
        mem.close()


def cmd_context(args):
    mem = MemorySystem(path=args.db)
    try:
        t0 = datetime.fromisoformat(args.time) if args.time else None
        ctx = mem.recall_context(
            RecallQuery(text=args.text), query_type=args.query_type,
            query_time=t0)
        _emit({
            "query_type": ctx.query_type,
            "current_state": [
                {"key": f.key, "value": f.value, "source": f.source,
                 "valid_at": _iso(f.valid_at)} for f in ctx.current_state],
            "historical_changes": [
                {"key": f.key, "value": f.value, "valid_at": _iso(f.valid_at)}
                for f in ctx.historical_changes],
            "beliefs": [{"proposition": b.proposition,
                         "polarity": b.polarity} for b in ctx.beliefs],
            "intents": [{"proposition": i.proposition, "status": i.status}
                        for i in ctx.intents],
            "notes": ctx.notes,
        })
        return 0
    except (MemoryError, ValueError) as e:
        return _handle(e)
    finally:
        mem.close()


def cmd_explain(args):
    mem = MemorySystem(path=args.db)
    try:
        target = (int(args.target) if str(args.target).strip().isdigit()
                  else args.target)
        ex = mem.explain(target, kind=args.kind)
        m = ex["memory"]
        _emit({
            "memory_id": target,
            "name": getattr(m, "name", getattr(m, "value", "")),
            "source": ex["source"],
            "evidence": [{"id": e.id, "source_type": e.source_type,
                          "source_ref": e.source_ref} for e in ex["evidence"]],
            "versions": [{"version": v[0], "content": v[1]}
                         for v in ex["versions"]],
            "related": ex["related"],
        })
        return 0
    except (MemoryError, ValueError) as e:
        return _handle(e)
    finally:
        mem.close()


def cmd_eval(args):
    queries = args.queries
    if not os.path.exists(queries):
        alt = os.path.join(os.getcwd(), "data", "eval_longterm.json")
        if os.path.exists(alt):
            queries = alt
    if not os.path.exists(queries):
        print(f"Error: 评测集不存在: {queries}", file=sys.stderr)
        return 1
    with open(queries, encoding="utf-8") as f:
        data = json.load(f)
    if os.path.exists(args.db):
        mem = MemorySystem(path=args.db)
    else:
        mem = MemorySystem(path=":memory:")
        corpus = data.get("corpus") or {}
        for ent in corpus.get("entities", []):
            mem.add_entity(ent["name"], ent.get("kind", "concept"),
                           ent.get("description", ""))
        for ev in corpus.get("events", []):
            ts = datetime.fromisoformat(ev["ts"]) if ev.get("ts") else None
            mem.add_event(ev["name"], ts, kind=ev.get("kind", "life"),
                          value_score=ev.get("value_score", 0.5))
        for edge in corpus.get("edges", []):
            try:
                mem.add_edge(edge["from"], edge["to"],
                             edge.get("rel", "mentions"), 0.6, 0.8)
            except Exception:  # noqa: BLE001
                continue
    rows = []
    for item in data["items"]:
        if item.get("time"):
            t0 = datetime.fromisoformat(item["time"])
            query = RecallQuery(time=(t0, int(item.get("tol_days", 2))),
                                text=item.get("query"),
                                topic=item.get("topic"))
        elif item.get("topic"):
            query = RecallQuery(topic=item["topic"],
                                text=item.get("query"))
        else:
            query = RecallQuery(text=item.get("query"))
        hits = mem.recall(query, k=args.k, mode=args.mode,
                          node_types=("event",))
        names = {h.name for h in hits}
        expected = set(item.get("expected", []))
        ok = (len(hits) == 0) if not expected else bool(expected & names)
        rows.append({"id": item["id"], "ability": item["ability"],
                     "ok": ok,
                     "expected": sorted(expected),
                     "hits": [h.name for h in hits[:5]]})
    by_ability = {}
    for r in rows:
        bl = by_ability.setdefault(r["ability"], {"ok": 0, "n": 0})
        bl["n"] += 1
        bl["ok"] += 1 if r["ok"] else 0
    report = {
        "dataset": data.get("name", queries),
        "overall": sum(1 for r in rows if r["ok"]) / max(len(rows), 1),
        "by_ability": {ab: {"recall": bl["ok"] / bl["n"], "n": bl["n"]}
                       for ab, bl in by_ability.items()},
        "items": rows,
    }
    _emit(report)
    mem.close()
    return 0


def build_parser():
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--db", default="user_memory.db",
                        help="SQLite 库路径（默认 user_memory.db）")
    parser = argparse.ArgumentParser(prog="dnamemory",
                                     description="dnamemory 命令行")
    parser.add_argument("--version", action="version",
                        version=f"dnamemory {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-event", parents=[parent], help="写入事件")
    p.add_argument("--name", required=True)
    p.add_argument("--ts", default=None, help="ISO 时间，可省略")
    p.add_argument("--kind", default="meeting")
    p.add_argument("--value-score", type=float, default=0.5)
    p.add_argument("--protected", action="store_true")
    p.add_argument("--access-label", default="public")
    p.set_defaults(func=cmd_add_event)

    p = sub.add_parser("add-entity", parents=[parent], help="写入实体")
    p.add_argument("--name", required=True)
    p.add_argument("--kind", default="concept")
    p.add_argument("--description", default="")
    p.add_argument("--protected", action="store_true")
    p.add_argument("--access-label", default="public")
    p.set_defaults(func=cmd_add_entity)

    p = sub.add_parser("add-edge", parents=[parent], help="写入关联边")
    p.add_argument("--from", dest="from_", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--rel", required=True)
    p.add_argument("--weight", type=float, default=0.5)
    p.add_argument("--confidence", type=float, default=0.8)
    p.add_argument("--valid-at", default=None)
    p.set_defaults(func=cmd_add_edge)

    p = sub.add_parser("add-fact", parents=[parent], help="写入事实")
    p.add_argument("--entity", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--value", required=True)
    p.add_argument("--source", default="chat")
    p.add_argument("--confidence", type=float, default=0.7)
    p.add_argument("--valid-at", default=None)
    p.set_defaults(func=cmd_add_fact)

    p = sub.add_parser("recall", parents=[parent], help="召回记忆")
    p.add_argument("--text", default=None)
    p.add_argument("--topic", nargs="*", default=None)
    p.add_argument("--time", default=None)
    p.add_argument("--tol-days", type=int, default=2)
    p.add_argument("--k", type=int, default=8)
    p.add_argument("--mode", default="triple",
                   choices=("time", "graph", "semantic", "dual", "triple",
                            "quad"))
    p.add_argument("--node-types", nargs="*", default=None)
    p.add_argument("--rerank-top-n", type=int, default=20)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("forget", parents=[parent], help="遗忘节点")
    p.add_argument("--target", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("reflect", parents=[parent], help="月度反射压缩")
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--month", type=int, required=True)
    p.set_defaults(func=cmd_reflect)

    p = sub.add_parser("history", parents=[parent], help="记忆历史（按维度）")
    p.add_argument("--entity", required=True)
    p.add_argument("--dimension", default="fact",
                   choices=("fact", "belief", "intent", "event"))
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("timeline", parents=[parent], help="实体记忆时间线")
    p.add_argument("--entity", default=None)
    p.add_argument("--start", default=None, help="ISO 起始时间")
    p.add_argument("--end", default=None, help="ISO 结束时间")
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("context", parents=[parent],
                       help="结构化记忆上下文（状态解析）")
    p.add_argument("text", help="自然语言查询")
    p.add_argument("--query-type", default=None,
                   choices=("current_state", "history", "timeline", "change",
                            "why_change", "semantic_recall"))
    p.add_argument("--time", default=None, help="查询时间 ISO")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("explain", parents=[parent], help="解释记忆来源")
    p.add_argument("target", help="记忆 id（node/fact/belief/intent）")
    p.add_argument("--kind", default=None,
                   choices=("node", "fact", "belief", "intent"),
                   help="维度消歧（跨表 id 空间重叠时使用）")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("demo", parents=[parent],
                       help="生成确定性演示库（40 事件）")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("eval", parents=[parent], help="运行中文能力评测")
    p.add_argument("--queries",
                   default=os.path.join(os.path.dirname(
                       os.path.dirname(os.path.dirname(
                           os.path.abspath(__file__)))),
                       "data", "eval_longterm.json"))
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--mode", default="triple",
                   choices=("dual", "triple", "quad"))
    p.set_defaults(func=cmd_eval)
    return parser


def main(argv=None):
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MemoryError as e:
        return _handle(e)


if __name__ == "__main__":
    sys.exit(main())
