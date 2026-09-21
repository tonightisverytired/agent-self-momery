# -*- coding: utf-8 -*-
"""丰富演示数据注入（0.8.0 静态后台展示用）。

在 `dnamemory demo --db <db>` 生成的 40 事件 + 10 实体之上，补全
fact / belief / intent / impact / evidence / edge 六类对象，并跑一次
治理链（冲突收敛 → 影响链推导 → 模式提取），让仪表盘分布图、
上下文 16 区块与图谱页都有真实数据可看。幂等：重复跑会写重复行，
重新导入请先删库再 demo。

用法：
    dnamemory demo --db user_memory.db
    python -X utf8 ops/seed_rich_demo.py --db user_memory.db
"""
from __future__ import annotations

import argparse
from datetime import datetime

from dnamemory import MemorySystem
from dnamemory.temporal import derive_impact_links


def seed(db: str) -> None:
    mem = MemorySystem(path=db)
    try:
        # 主角实体（demo 库里没有）
        mem.add_entity("用户", "person", "记忆系统使用者的个人画像")
        # ---- facts：预算演化链（当前事实裁决 + 时间链） ----
        mem.add_fact("项目A", "budget", "800万", source="meeting",
                     confidence=0.9, valid_at=datetime(2026, 1, 10),
                     invalid_at=datetime(2026, 6, 30))
        mem.add_fact("项目A", "budget", "640万", source="meeting",
                     confidence=0.95, valid_at=datetime(2026, 7, 1))
        mem.add_fact("项目A", "phase", "开发中", source="weekly_report",
                     confidence=0.85)
        mem.add_fact("项目B", "phase", "待立项", source="weekly_report",
                     confidence=0.7)
        mem.add_fact("用户", "city", "上海", source="profile",
                     confidence=0.9, valid_at=datetime(2025, 8, 1),
                     invalid_at=datetime(2026, 7, 1))
        mem.add_fact("用户", "city", "南京", source="profile",
                     confidence=0.9, valid_at=datetime(2026, 7, 1))
        mem.add_fact("李工", "role", "后端负责人", source="org",
                     confidence=0.95)
        mem.add_fact("供应商B", "contract", "已签署", source="procurement",
                     confidence=0.8, valid_at=datetime(2026, 3, 15))
        # 故意构造的同源并列事实：查询端冲突态（治理台「解决冲突」可演示）
        mem.add_fact("项目B", "phase", "规划中", source="weekly_report",
                     confidence=0.8, valid_at=datetime(2026, 6, 1))
        mem.add_fact("项目B", "phase", "已立项", source="weekly_report",
                     confidence=0.8, valid_at=datetime(2026, 6, 1))

        # ---- beliefs：观点演化（正负混合，不覆盖历史） ----
        mem.add_belief("用户", "微服务拆分能提升交付速度", polarity="positive",
                       confidence=0.8, source="chat",
                       valid_at=datetime(2026, 2, 1),
                       invalid_at=datetime(2026, 5, 1))
        mem.add_belief("用户", "微服务拆分提升运维成本", polarity="negative",
                       confidence=0.7, source="chat",
                       valid_at=datetime(2026, 5, 2))
        mem.add_belief("李工", "Kubernetes 适合当前规模", polarity="positive",
                       confidence=0.85, source="meeting")
        mem.add_belief("张总", "供应商B 报价偏高", polarity="negative",
                       confidence=0.6, source="meeting",
                       valid_at=datetime(2026, 2, 10),
                       invalid_at=datetime(2026, 4, 1))

        # ---- intents：独立状态机（各状态一条） ----
        mem.add_intent("用户", "9 月底完成项目A 上线", status="active",
                       confidence=0.9, source="plan",
                       valid_at=datetime(2026, 8, 1))
        mem.add_intent("用户", "6 月完成供应商选型", status="completed",
                       confidence=0.9, source="plan",
                       valid_at=datetime(2026, 3, 1),
                       invalid_at=datetime(2026, 6, 30))
        mem.add_intent("用户", "引入消息队列中间件", status="cancelled",
                       confidence=0.5, source="chat",
                       valid_at=datetime(2026, 2, 1),
                       invalid_at=datetime(2026, 3, 15))
        mem.add_intent("李工", "升级 CI 流水线", status="active",
                       confidence=0.8, source="meeting")

        # ---- impacts：多维度影响（含 cause_event 链） ----
        ev_budget = mem.store.fetch_nodes()
        ev_name = next((n for n in ev_budget
                        if n.name == "与张总讨论项目A预算问题"), None)
        ev_id = ev_name.nid if ev_name else None
        mem.add_impact("项目A", "cost", "decrease", "positive", magnitude=0.8,
                       kind="objective", evaluator="agent",
                       description="预算缩减 20% 后成本压力下降",
                       cause_event=ev_id, source="inferred", confidence=0.75,
                       valid_at=datetime(2026, 7, 2))
        mem.add_impact("项目A", "risk", "increase", "negative", magnitude=0.6,
                       kind="objective", evaluator="agent",
                       description="预算缩减带来交付风险",
                       cause_event=ev_id, source="inferred", confidence=0.7,
                       valid_at=datetime(2026, 7, 2))
        mem.add_impact("用户", "stress", "increase", "negative", magnitude=0.5,
                       kind="subjective", evaluator="user",
                       description="赶进度压力上升", source="chat",
                       confidence=0.65, valid_at=datetime(2026, 8, 15))
        mem.add_impact("李工", "work", "increase", "neutral", magnitude=0.4,
                       kind="objective", evaluator="agent",
                       description="技术选型工作量增加", source="inferred",
                       confidence=0.6, valid_at=datetime(2026, 3, 1))

        # ---- evidence：多来源证据 ----
        mem.add_evidence("user_statement", source_ref="我说过预算要砍",
                         observed_at=datetime(2026, 7, 1),
                         trust_level=0.9, access_label="public",
                         metadata={"session": "s-001"})
        mem.add_evidence("conversation", source_ref="2026-07 周会记录",
                         observed_at=datetime(2026, 7, 8),
                         trust_level=0.8, access_label="public")
        mem.add_evidence("system_record", source_ref="git commit 项目A 预算表",
                         observed_at=datetime(2026, 7, 3),
                         trust_level=0.9, access_label="private")
        mem.add_evidence("inferred", source_ref="由预算事实推导的影响",
                         observed_at=datetime(2026, 7, 2),
                         trust_level=0.7, access_label="public")

        # ---- edges：补关系类型 ----
        mem.add_edge("张总", "项目A", "participates", weight=0.9)
        mem.add_edge("李工", "技术选型", "discusses", weight=0.8)
        mem.add_edge("技术选型", "微服务", "precedes", weight=0.6)
        mem.add_edge("微服务", "Kubernetes", "depends_on", weight=0.7)
        mem.add_edge("供应商B", "项目A", "part_of", weight=0.5)
        mem.add_edge("预算", "项目A", "part_of", weight=0.6)
        mem.add_edge("部署流程", "Kubernetes", "summarizes", weight=0.4)
        mem.add_edge("项目A", "项目B", "similar_to", weight=0.3)

        # ---- 治理链（冲突收敛 → 影响链 → 模式提取） ----
        mem.resolve_conflicts()
        n_links = derive_impact_links(mem.store)
        n_patterns = mem.extract_patterns()
        print(f"seed ok: impact_links={n_links} patterns={n_patterns}")
    finally:
        mem.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="丰富演示数据注入")
    parser.add_argument("--db", default="user_memory.db")
    args = parser.parse_args()
    seed(args.db)


if __name__ == "__main__":
    main()
