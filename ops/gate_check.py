# -*- coding: utf-8 -*-
"""正式项目 CI 门禁：固定快照 + 固定 seed 的指标检查。

阶段 D 起新增「干扰集 + 自然问句」探针（paraphrase_probe）：独立确定性
合成库，查询与事件名仅有部分 bigram 重叠，锁住词面路的真实相关性下限；
不触碰既有快照与 dual/triple/state 门禁。

运行（验收方/CI 执行）：
  py tools/gate_check.py
  py tools/gate_check.py --bge-m3 --mode quad
输出：simulation/gate_result.json；达标退出码 0，否则 1。
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from dnamemory import MemorySystem, RecallQuery  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "data", "mvp_memory.db")
STATE_QUERY_TIME = datetime(2026, 9, 1)


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


GATES = {
    "dual_recall": 0.95,
    "dual_map": 0.90,
    "triple_ge_dual_recall": True,
    "triple_ge_dual_map": True,
    # 阶段 D 探针门禁（校准只改这里）：自然问句 hit@5 下限、域外零命中率
    "paraphrase_hit_at_5": 0.8,
    "paraphrase_abstain": 1.0,
}

PARAPHRASE_SEED = 20260804

# 目标事件 + 自然问句：问句与事件名仅有部分 bigram 重叠（非词面同构），
# 每条问句标注唯一期望事件。事件 value_score=0.6，高于干扰事件（0.5），
# 模拟显著个人事件；干扰集只稀释候选池。
PARAPHRASE_ITEMS = [
    ("计划2026-08-04看牙", "看牙的计划安排在什么时候"),
    ("被猫抓伤后打疫苗", "被猫抓了要去打疫苗吗"),
    ("体检报告胆固醇偏高", "体检胆固醇查出来高吗"),
    ("健身房办了张年卡", "健身年卡花了多少钱"),
    ("晚上做糖醋排骨", "糖醋排骨打算什么时候做"),
    ("收到妈妈寄的腊肠并煮煲仔饭", "妈妈寄的腊肠怎么吃"),
    ("点奶茶少冰半糖却收到全糖", "奶茶点半糖结果给了全糖"),
    ("和同事吃烤肉", "中午和同事吃了烤肉"),
    ("娃的数学考试成绩进步奖励冰激凌", "娃数学考试成绩出来了吗"),
    ("与妈妈视频通话四十分钟", "跟妈妈视频聊了多久"),
    ("给花浇水发现多肉烂根", "多肉是不是烂根了"),
    ("老板要求2026-08-10交方案", "老板要的方案什么时候交"),
    ("周报未写2026-08-04上午截止", "周报什么时候截止"),
    ("项目上线出bug紧急修复", "项目上线出了什么bug"),
    ("同事离职吃散伙饭", "同事离职散伙饭定在哪"),
    ("地铁二号线晚点导致迟到", "地铁二号线今天晚点了吗"),
    ("地铁通勤忘带耳机", "早上挤地铁耳机忘带了"),
    ("爬山看云海", "周末爬山看到云海了吗"),
    ("快递显示签收但没收到", "快递显示签收了东西呢"),
    ("洗衣机漏水报修", "洗衣机漏水找谁修"),
    ("物业费涨价通知贴电梯", "物业费是不是涨价了"),
    ("给猫换猫粮它不吃", "新买的猫粮猫不吃怎么办"),
    ("信用卡账单分期还清", "信用卡账单还清了吗"),
    ("公积金提取到账", "公积金提取的钱到账了吗"),
    ("报名了周末羽毛球局", "周末羽毛球局还缺人吗"),
    ("图书馆借的小说到期", "图书馆借的小说哪天到期"),
    ("手机贴膜摔裂了", "手机膜摔裂了去哪贴"),
    ("抢到了演唱会门票", "演唱会门票抢到了吗"),
    ("阳台种的番茄结果了", "阳台番茄结果没有"),
    ("理发店换了个新发型", "新发型是在哪家理发店做的"),
]

# 域外问句：与库内主题无关，期望零命中（abstain）
OUT_OF_DOMAIN_QUERIES = [
    "火星探测器什么时候返回地球",
    "欧冠决赛哪天开球",
    "欧元兑美元汇率走势",
    "秦始皇陵考古进展",
    "量子计算机量产时间表",
]

# 干扰事件模板：与目标主题无关，笛卡尔积展开 ~228 条
DISTRACTOR_SPECS = [
    ("优化{mod}模块的{asp}性能",
     {"mod": ["订单", "支付", "库存", "搜索", "消息", "风控", "结算", "推荐"],
      "asp": ["查询", "写入", "缓存", "并发", "启动"]}),
    ("{lang}脚本处理{file}文件",
     {"lang": ["Python", "Shell", "SQL", "Go"],
      "file": ["日志", "账单", "影像", "问卷", "字典", "清单"]}),
    ("训练{part}肌群{sets}组",
     {"part": ["胸部", "背部", "腿部", "肩部", "手臂", "核心"],
      "sets": ["三", "四", "五", "六"]}),
    ("通关{game}第{ch}章节",
     {"game": ["星穹铁道", "原神", "黑神话", "塞尔达", "艾尔登", "幻兽帕鲁"],
      "ch": ["一", "二", "三", "四", "五", "六"]}),
    ("撰写{topic}方向的综述初稿",
     {"topic": ["图神经网络", "强化学习", "知识图谱", "蛋白质预测",
                "量子纠错", "钙钛矿", "扩散模型", "大模型微调"]}),
    ("复盘{idx}指数本周{dir}原因",
     {"idx": ["沪深", "恒生", "纳斯达克", "标普"],
      "dir": ["震荡", "反弹", "回调", "缩量"]}),
    ("保养{part}更换{oil}",
     {"part": ["发动机", "变速箱", "刹车系统", "轮胎"],
      "oil": ["机油", "滤芯", "刹车片", "火花塞"]}),
    ("整理{room}的{stuff}收纳",
     {"room": ["书房", "储物间", "衣帽间", "储藏间"],
      "stuff": ["旧杂志", "数据线", "工具", "药箱"]}),
    ("申购{fund}基金{amt}元",
     {"fund": ["沪深300", "中证500", "红利低波", "国债指数"],
      "amt": ["500", "1000", "1500", "2000"]}),
    ("学习{skill}第{wk}周课程",
     {"skill": ["法语", "钢琴", "烘焙", "水彩"],
      "wk": ["一", "二", "三", "四", "五", "六", "七", "八"]}),
]

PARAPHRASE_ENTITIES = [
    ("妈妈", "person", "用户母亲"), ("老板", "person", "直属上级"),
    ("同事", "person", "同组同事"), ("娃", "person", "用户孩子"),
    ("猫", "pet", "家养橘猫"),
]

PARAPHRASE_EDGES = [
    ("收到妈妈寄的腊肠并煮煲仔饭", "妈妈", "mentions"),
    ("与妈妈视频通话四十分钟", "妈妈", "mentions"),
    ("老板要求2026-08-10交方案", "老板", "discusses"),
    ("和同事吃烤肉", "同事", "mentions"),
    ("同事离职吃散伙饭", "同事", "mentions"),
    ("娃的数学考试成绩进步奖励冰激凌", "娃", "mentions"),
    ("被猫抓伤后打疫苗", "猫", "mentions"),
    ("给猫换猫粮它不吃", "猫", "mentions"),
]


def build_synthetic_snapshot(path):
    """快照缺失时生成确定性合成库（与回归语料同构，seed=42）。"""
    random.seed(42)
    mem = MemorySystem(path=path)
    entities = [
        ("张总", "person", "项目负责人"), ("李工", "person", "后端工程师"),
        ("项目A", "project", "核心交付项目"), ("项目B", "project", "创新项目"),
        ("微服务", "concept", "分布式架构"), ("单体架构", "concept", "一体化架构"),
        ("Kubernetes", "skill", "容器编排"), ("预算", "concept", "资金计划"),
        ("供应商B", "vendor", "云服务商"), ("技术选型", "concept", "方案对比"),
        ("部署流程", "skill", "上线步骤"), ("供应商C", "vendor", "监控工具商"),
    ]
    for name, kind, desc in entities:
        mem.add_entity(name, kind, desc)
    for a, b, rel in [("张总", "项目A", "participates"),
                      ("李工", "项目A", "participates"),
                      ("项目A", "微服务", "depends_on"),
                      ("项目A", "Kubernetes", "depends_on"),
                      ("微服务", "单体架构", "similar_to")]:
        mem.add_edge(a, b, rel, weight=0.8, confidence=0.9)
    from datetime import datetime, timedelta
    start = datetime(2026, 3, 1)
    templates = [
        ("与张总讨论{proj}预算问题", ["张总", "预算", "项目A"], "meeting",
         "transient", 0.45),
        ("与李工进行{tech}技术选型", ["李工", "技术选型", "微服务"], "decision",
         "core", 0.85),
        ("{proj}例会：同步{sup}进展", ["供应商B", "项目A"], "meeting",
         "transient", 0.5),
        ("部署{stack}集群到生产环境", ["Kubernetes", "部署流程"], "deploy",
         "core", 0.9),
        ("评估微服务与单体架构架构差异", ["微服务", "单体架构"], "decision",
         "core", 0.8),
        ("与张总复盘{proj}延期原因", ["项目A", "张总"], "meeting",
         "transient", 0.55),
    ]
    day = 0
    for i in range(120):
        day += 1 if i % 3 == 0 else 2
        tmpl, atoms, kind, etype, imp = random.choice(templates)
        proj = random.choice(["项目A", "项目B"])
        name = tmpl.format(proj=proj,
                           tech=random.choice(["微服务", "Kubernetes"]),
                           sup=random.choice(["供应商B", "供应商C"]),
                           stack="Kubernetes")
        core = etype == "core"
        eid = mem.add_event(name, start + timedelta(days=day), kind=kind,
                            value_score=imp,
                            life=150.0 if core else 50.0,
                            decay_rate=0.02 if core else 0.28)
        for atom in atoms:
            rel = "participates" if atom in {"张总", "李工"} else "discusses"
            mem.add_edge(eid, atom, rel, weight=0.9 if core else 0.5,
                         confidence=0.8,
                         valid_at=start + timedelta(days=day))
    mem.close()
    return path


def build_dataset(mem):
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

    by_name = defaultdict(set)
    for n in nodes:
        if n.node_type == "entity" and event_links.get(n.nid):
            by_name[n.name].add(n.nid)

    random.seed(42)
    dataset = []
    for name in sorted(by_name):
        truth = set()
        for nid in by_name[name]:
            truth |= event_links[nid]
        dataset.append((RecallQuery(topic=[name], text=name),
                        truth, "topic"))
    events_ts = [n for n in nodes if n.node_type == "event" and n.ts]
    for n in random.sample(events_ts, min(60, len(events_ts))):
        truth = {x.nid for x in events_ts if abs((x.ts - n.ts).days) <= 1}
        dataset.append((RecallQuery(time=(n.ts, 2)), truth, "time"))
    return dataset


def build_state_memory():
    """0.5.0 状态正确性合成库（设计稿 §25 Test 01-05，确定性）。"""
    mem = MemorySystem(path=":memory:")
    u = mem.add_entity("用户", "person")
    # Test 01 事实版本：南京 → 上海
    mem.add_fact(u, "city", "南京", source="profile", confidence=0.9,
                 valid_at=datetime(2025, 8, 1),
                 invalid_at=datetime(2026, 7, 1))
    mem.add_fact(u, "city", "上海", source="profile", confidence=0.9,
                 valid_at=datetime(2026, 7, 1))
    # Test 03 观点变化：B1 → B2
    t1, t2 = datetime(2025, 1, 1), datetime(2026, 1, 1)
    mem.store.insert_belief(u, "上海工作机会多", "positive", 0.8, "chat",
                            t1, None, None, "active", "public", [], t1)
    mem.store.insert_belief(u, "上海生活成本高", "negative", 0.8, "chat",
                            t2, None, None, "active", "public", [], t2)
    # Test 04 意图（多维共存）
    mem.store.insert_intent(u, "考虑离开上海", "active", 0.7, "chat",
                            t2, None, "active", "public", [], t2)
    # Test 05 证据不足：搬迁事件无证据
    ev = mem.add_event("搬到上海", datetime(2026, 7, 1), kind="life")
    mem.add_edge(ev, u, "discusses", 0.7, 0.8,
                 valid_at=datetime(2026, 7, 1))
    return mem


def check_state_gates(mem):
    """§25 Test 01-05 确定性断言：全部通过才算达标。"""
    checks = {}
    # Test 01：当前事实 = 上海
    ctx = mem.recall_context(RecallQuery(text="我现在住哪里"),
                             query_time=STATE_QUERY_TIME)
    checks["test01_current_fact"] = \
        [f.value for f in ctx.current_state] == ["上海"]
    # Test 02：时间链顺序 南京 → 上海
    tl = mem.timeline(entity_id=1, start=datetime(2025, 1, 1),
                      end=STATE_QUERY_TIME)
    chain_vals = [getattr(n.memory, "value", "") for n in tl]
    checks["test02_timeline_order"] = \
        "南京" in chain_vals and "上海" in chain_vals \
        and chain_vals.index("南京") < chain_vals.index("上海")
    # Test 03：观点变化链：当前 = 最新观点
    ctx = mem.recall_context(RecallQuery(text="我对上海的看法发生过什么变化"),
                             query_time=STATE_QUERY_TIME)
    checks["test03_belief_current"] = bool(ctx.beliefs) \
        and ctx.beliefs[0].proposition == "上海生活成本高"
    # Test 04：Fact + Belief + Intent 多维共存，无冲突
    ctx = mem.recall_context(RecallQuery(text="我现在的情况"),
                             query_time=STATE_QUERY_TIME)
    checks["test04_cross_dimension"] = (
        [f.value for f in ctx.current_state] == ["上海"]
        and bool(ctx.beliefs) and bool(ctx.intents)
        and ctx.conflicts == [])
    # Test 05：证据不足 → abstain（不编造原因）
    ctx = mem.recall_context(RecallQuery(text="我为什么离开南京"),
                             query_time=STATE_QUERY_TIME)
    checks["test05_abstain"] = any("证据" in n for n in ctx.notes)
    return checks


def build_paraphrase_memory():
    """探针合成库（seed 固定）：30 目标事件 + ~228 模板干扰事件 + 少量实体/边。

    返回 (mem, {目标事件名: nid}, 干扰事件数)。目标事件先入库（nid 小、
    平分决胜稳定），干扰事件按 seed 洗牌后入库。
    """
    rng = random.Random(PARAPHRASE_SEED)
    mem = MemorySystem(path=":memory:")
    for name, kind, desc in PARAPHRASE_ENTITIES:
        mem.add_entity(name, kind, desc)
    expected = {}
    base = datetime(2026, 7, 20)
    for i, (name, _q) in enumerate(PARAPHRASE_ITEMS):
        expected[name] = mem.add_event(name, base + timedelta(days=i % 30),
                                       kind="life", value_score=0.6)
    for a, b, rel in PARAPHRASE_EDGES:
        mem.add_edge(a, b, rel, weight=0.9, confidence=0.8, valid_at=base)
    distractors = []
    for tpl, slots in DISTRACTOR_SPECS:
        keys = sorted(slots)
        for combo in itertools.product(*(slots[k] for k in keys)):
            distractors.append(tpl.format(**dict(zip(keys, combo))))
    rng.shuffle(distractors)
    for i, name in enumerate(distractors):
        mem.add_event(name, base + timedelta(days=i % 60), kind="life",
                      value_score=0.5)
    return mem, expected, len(distractors)


def paraphrase_probe(k=5):
    """阶段 D 探针：干扰集 + 自然问句，无 embedder triple 模式。

    指标：hit@k（30 条标注问句的唯一期望事件是否进 top-k）与
    域外零命中率（5 条域外问句是否全部零命中）。
    """
    mem, expected, n_distractors = build_paraphrase_memory()
    try:
        miss = []
        for name, q in PARAPHRASE_ITEMS:
            hits = mem.recall(RecallQuery(text=q), k=k, mode="triple",
                              node_types=("event",))
            if expected[name] not in {h.node_id for h in hits}:
                miss.append({"query": q, "expected": name,
                             "hits": [h.name for h in hits]})
        false_hits = []
        for q in OUT_OF_DOMAIN_QUERIES:
            hits = mem.recall(RecallQuery(text=q), k=k, mode="triple",
                              node_types=("event",))
            if hits:
                false_hits.append({"query": q,
                                   "hits": [h.name for h in hits]})
    finally:
        mem.close()
    n_q, n_out = len(PARAPHRASE_ITEMS), len(OUT_OF_DOMAIN_QUERIES)
    return {
        "hit_at_5": round((n_q - len(miss)) / n_q, 4),
        "abstain": round((n_out - len(false_hits)) / n_out, 4),
        "n_queries": n_q, "n_out": n_out, "k": k,
        "n_distractors": n_distractors,
        "miss": miss, "false_hits": false_hits,
    }


def main():
    _force_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=SNAPSHOT)
    parser.add_argument("--bge-m3", action="store_true",
                        help="启用真实稠密/稀疏语义路（需本地模型缓存）")
    parser.add_argument("--mode", default="triple",
                        choices=("dual", "triple", "quad"))
    args = parser.parse_args()
    snapshot_source = args.db
    if not os.path.exists(args.db):
        os.makedirs(os.path.join(ROOT, ".pytest_tmp"), exist_ok=True)
        args.db = os.path.join(ROOT, ".pytest_tmp", "gate_snapshot.db")
        build_synthetic_snapshot(args.db)
        print(f"快照不存在，已生成确定性合成库: {args.db}")

    embedder = None
    if args.bge_m3:
        from dnamemory.embeddings import BGEM3Embedder

        class Cache:
            def __init__(self, inner):
                self._inner = inner
                self.model_name = inner.model_name
                self._c = {}

            def embed(self, text):
                if text not in self._c:
                    self._c[text] = self._inner.embed(text)
                return self._c[text]

            def embed_batch(self, texts):
                return [self.embed(t) for t in texts]

        embedder = Cache(BGEM3Embedder())

    mem = MemorySystem(path=args.db, embedder=embedder)
    dataset = build_dataset(mem)
    dual, _ = mem.evaluate(dataset, k=5, mode="dual")
    triple, _ = mem.evaluate(dataset, k=5, mode=args.mode)

    checks = {
        "dual_recall": dual["recall"],
        "dual_map": dual["map"],
        "triple_recall": triple["recall"],
        "triple_map": triple["map"],
    }
    ok = True
    reasons = []
    if checks["dual_recall"] < GATES["dual_recall"]:
        ok = False
        reasons.append("dual recall 低于门禁")
    if checks["dual_map"] < GATES["dual_map"]:
        ok = False
        reasons.append("dual map 低于门禁")
    if checks["triple_recall"] + 1e-9 < checks["dual_recall"]:
        ok = False
        reasons.append("triple recall 低于 dual")
    if checks["triple_map"] + 1e-9 < checks["dual_map"]:
        ok = False
        reasons.append("triple map 低于 dual")

    # 0.5.0 状态正确性门禁（§25 Test 01-05 确定性断言）
    smem = build_state_memory()
    state_checks = check_state_gates(smem)
    smem.close()
    state_ok = all(state_checks.values())
    if not state_ok:
        ok = False
        failed = [k for k, v in state_checks.items() if not v]
        reasons.append(f"状态正确性门禁未通过: {failed}")

    # 阶段 D 探针：干扰集 + 自然问句（独立合成库，不触碰快照）
    probe = paraphrase_probe()
    if probe["hit_at_5"] < GATES["paraphrase_hit_at_5"]:
        ok = False
        reasons.append(
            f"自然问句 hit@{probe['k']} 低于门禁: "
            f"{probe['hit_at_5']} < {GATES['paraphrase_hit_at_5']}")
    if probe["abstain"] < GATES["paraphrase_abstain"]:
        ok = False
        reasons.append(
            f"域外零命中率低于门禁: {probe['abstain']} < "
            f"{GATES['paraphrase_abstain']}")

    report = {
        "db": args.db, "snapshot_source": snapshot_source,
        "mode": args.mode, "bge_m3": args.bge_m3,
        "gates": GATES, "metrics": checks, "state": state_checks,
        "paraphrase": probe,
        "ok": ok,
        "reasons": reasons,
    }
    out = os.path.join(ROOT, "ops", "data", "gate_result.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    mem.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
