# sb_coding
The combination of demand analysis, automated process development, and code implementation skills essentially aims to raise the minimum threshold of coding intelligence. As long as the requirements are clear, one can directly use vibe coding and sb_coding to get started

---

# DNA-MEMORY 项目

长期个人智能体记忆系统：从"DNA 双螺旋"概念出发，经评估与重构，落地为**带类型关联边的记忆库**，目标是成为可被其他系统调用、可嵌入其他系统的 Python 依赖库。

## 状态

| 里程碑 | 状态 |
|-------|-----|
| 概念与模型评估 | ✅ 完成（docs/01-03） |
| 权威方案（v2 关联边模型） | ✅ 完成（docs/00） |
| 模拟验证（实验 1-8，指标全达标） | ✅ 完成（simulation/） |
| MAP 可行性探究 | ✅ 完成（docs/05） |
| 依赖库化可行性评估 | ✅ 完成（docs/06） |
| 系统架构设计 | ✅ 完成（docs/07） |
| 实施计划 v2（M0-M6） | ✅ 完成（docs/08） |
| 能力点详细设计 + 沙箱验证 | ✅ 完成（docs/09/10，S1-S10 全通过；沙箱代码已归档） |
| M1 核心库实现 | ✅ 完成（src/dnamemory，pytest 25/25 通过） |
| M2 LLM 拆解初版 + MVP 测试 | ✅ 完成（DeepSeek 200 条导入 + 指标评测，docs/12） |
| M2.1 抽取质量改进（Pydantic 校验/时间映射/提示词） | ✅ 完成（docs/12 v1.1） |
| M3 语义路（bge-m3 稠密检索 + 双向量存储） | ✅ 完成（docs/13；三路 1.000 ≥ 双路 0.986） |
| M4 访问控制与反射压缩 | ⏳ 下一轮 |

## 核心结论

- **废弃"碱基对"**，改用带类型关联边（participates / discusses / prefers / precedes 等 14 类），事件与实体统一为异构节点；
- **三路召回**（时间 / 图谱 / 语义）+ 分数加权 RRF，双路 Recall@5=1.000、MAP@5=0.928；
- **四维治理**：新鲜度（双时态+可信源）、矛盾处理、受控遗忘（价值优先+保护区）、审计（版本+墓碑）；
- **嵌入形态**：进程内库（默认）→ FastAPI 侧车 → MCP 服务 → Postgres 共享后端，共用同一内核。

## 文档索引

| 文档 | 内容 |
|-----|-----|
| [docs/00-长期个人智能体记忆系统方案.md](docs/00-长期个人智能体记忆系统方案.md) | 唯一执行依据：数据模型、检索 API、治理管线、指标体系 |
| [docs/05-MAP可行性探究.md](docs/05-MAP可行性探究.md) | MAP 指标可行性实证 |
| [docs/06-依赖库化与嵌入可行性评估.md](docs/06-依赖库化与嵌入可行性评估.md) | 依赖库化可行性评估 |
| [docs/07-系统架构设计.md](docs/07-系统架构设计.md) | dnamemory 库架构设计 |
| [docs/08-实施计划.md](docs/08-实施计划.md) | 实施计划 v2：M0-M6 里程碑、能力验收矩阵、回归门禁 |
| [docs/09-能力点详细设计.md](docs/09-能力点详细设计.md) | C1-C11 能力点详细设计 |
| [docs/10-沙箱模拟报告.md](docs/10-沙箱模拟报告.md) | 沙箱验证结果与设计修正（S1-S10） |
| [docs/11-详细开发计划.md](docs/11-详细开发计划.md) | M1 开工版详细开发计划（含 03 §六 / 05 §4.5 需求落点） |
| [docs/12-MVP测试报告.md](docs/12-MVP测试报告.md) | DeepSeek 200 条导入 + 记忆指标评测报告 |
| [docs/13-M3语义路验证报告.md](docs/13-M3语义路验证报告.md) | bge-m3 稠密检索 + 稠密/稀疏双向量存储验证报告 |
| [data/memory_fragments_200.txt](data/memory_fragments_200.txt) | MVP 测试语料（200 条杂乱日常片段） |
| [tools/mvp_llm_test.py](tools/mvp_llm_test.py) | MVP 测试工具（导入+补边+指标评测，--bge-m3 开启语义路） |
| [tools/bge_m3_smoke.py](tools/bge_m3_smoke.py) / [tools/paraphrase_demo.py](tools/paraphrase_demo.py) / [tools/dense_tuning.py](tools/dense_tuning.py) | bge-m3 冒烟 / 同义改写演示 / 稠密阈值调优 |
| [src/dnamemory/](src/dnamemory/) | 正式包（M1 核心库） |
| [tests/](tests/) | pytest 全量回归（25 用例） |
| [simulation/simulation_report.md](simulation/simulation_report.md) | 模拟实验报告（实验 1-8） |
| [simulation/dna_helix_memory_sim.py](simulation/dna_helix_memory_sim.py) | 可运行模拟（v2 关联边模型） |

## 运行模拟

```powershell
py simulation/dna_helix_memory_sim.py
```

## 运行测试

```powershell
py -m pytest
```

## 快速开始（M1 已可用）

```python
from dnamemory import MemorySystem, RecallQuery
from datetime import datetime

mem = MemorySystem(path="user_memory.db")
mem.add_event("与张总讨论预算", datetime(2026, 3, 15), kind="meeting", value_score=0.6)
hits = mem.recall(RecallQuery(topic=["预算"]), k=5, node_types=("event",))
```

> 配置全部走代码调用层（`MemoryConfig`/方法参数/回调注入），零配置文件。
