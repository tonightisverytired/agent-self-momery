# dnamemory

长期个人智能体记忆 **Runtime**：以**带类型关联边的记忆图谱**为核心，提供多路召回、受控遗忘、冲突治理、访问控制、反射压缩，以及 0.5.0 新增的**记忆状态解析**（Fact/Belief/Intent/Evidence 分层）、**时间链**、**证据追溯**与**跨维度一致性**的 Python 记忆库。可嵌入智能体、进程内零配置使用，也可通过 FastAPI / MCP 服务化。

> 版本：0.5.0｜Python ≥ 3.10｜设计依据：[业务改造](docs/业务改造.md)、[改造系统设计与架构设计](docs/改造系统设计与架构设计.md)

## 核心能力

- **多路召回**：时间路 + 图谱路 + 稠密语义路（bge-m3）+ 稀疏路，支持 `dual / triple / quad` 模式与 RRF 融合，可选 reranker 重排；
- **记忆 Runtime（0.5.0）**：`recall_context()` 管线（候选扩展 → 状态解析 → 时间链 → 证据校验 → 一致性 → 结构化上下文）；确定性 QueryRouter（当前/历史/时间线/变化意图）；当前事实由规则裁决（`superseded_by`/来源可信级/置信度/新鲜度），不依赖 LLM 与向量相似度；
- **六类记忆对象**：Event / Entity / Fact / Belief / Intent / Evidence；Belief 观点演化链不覆盖历史；Intent 独立状态机（active/completed/cancelled/expired/superseded）；Evidence 可追溯（inferred 强制标注推断，不得伪装为事实）；
- **治理完备**：生命周期状态机（active → archived → tombstoned → deleted）、受控衰减、合规删除级联、冲突治理（Hard/Temporal/Source/Soft 四类）、三级访问控制（public/private/sensitive，全路径统一兜底）、月度反射压缩、实体消解；
- **工程严谨**：SQLite 事务/幂等/审计/墓碑、sqlite-vec 余弦向量索引与 numpy 降级链、统一错误码体系（E000-E017）、0.4.0 旧库自动迁移（幂等补列建表）；
- **多种形态**：Python API、CLI（新增 history/timeline/context/explain）、FastAPI 侧车（新增 /context、/timeline、/memory/{id}/history、/memory/{id}/explain）、FastMCP（新增 context/timeline/explain 工具）；
- **可验证**：全量 pytest、双门禁（快照召回 + 状态正确性）、中文五类能力评测集与状态正确性评测集。

## 安装

```powershell
pip install dnamemory                     # 基础（零可选依赖）
pip install dnamemory[embed]              # + bge-m3 语义路 / reranker
pip install dnamemory[vector]             # + sqlite-vec 向量索引
pip install dnamemory[server]             # + FastAPI 侧车
pip install dnamemory[mcp]                # + FastMCP 服务
pip install dnamemory[llm]                # + DeepSeek 抽取
pip install dnamemory[dev]                # + pytest 开发依赖
```

本仓库开发安装：

```powershell
pip install -e ".[dev,vector,server,mcp,llm]"
```

## 快速开始

### 1. 生成演示数据

```powershell
dnamemory demo --db user_memory.db
```

生成 40 条确定性事件（seed=42），可直接体验召回、遗忘与反射。

### 2. 命令行（CLI）

```powershell
dnamemory add-event --name "今天和张总讨论了项目A的预算调整" --db user_memory.db
dnamemory recall --text "预算" --k 5 --db user_memory.db
dnamemory recall --topic 项目A --k 5 --db user_memory.db
dnamemory reflect --year 2026 --month 8 --db user_memory.db
dnamemory forget --target 1 --reason retracted --db user_memory.db
dnamemory forget --target 1 --reason gdpr --force --db user_memory.db
```

子命令：`add-event / add-entity / add-edge / add-fact / recall / forget / reflect / demo / eval`；退出码 0=成功，1=业务/运行错误，2=参数错误。

### 3. Python API

```python
from dnamemory import MemorySystem, RecallQuery

mem = MemorySystem(path="user_memory.db")
mem.add_event("与张总讨论预算", None, kind="meeting", value_score=0.6)
mem.add_entity("张总", "person", "项目负责人")

hits = mem.recall(RecallQuery(text="预算"), k=5, mode="triple",
                  node_types=("event",))
for h in hits:
    print(h.name, h.score)
mem.close()
```

### 4. 启用 bge-m3 真实语义

```python
from dnamemory import MemorySystem
from dnamemory.embeddings import BGEM3Embedder
from dnamemory.rerank import BGEReranker

mem = MemorySystem(path="user_memory.db", embedder=BGEM3Embedder(),
                   reranker=BGEReranker())
hits = mem.recall(RecallQuery(text="经费开支"), k=5, mode="quad")
```

- 首次使用自动下载模型（约 2.3GB），之后走本地缓存；`HF_HUB_OFFLINE=1` 强制离线加载；
- 写入**新节点**时自动生成 1024 维稠密向量 + 词级稀疏权重（批量写入按批调用）；幂等重复写入不重复嵌入；查询时每次嵌入一次查询文本；
- **旧数据不自动回填**：在 embedder 注入前写入的节点没有向量，语义路会跳过它们，需要用带 embedder 的进程回填后再检索。

### 5. 记忆 Runtime（0.5.0）：状态解析与时间链

```python
# 事实版本 + 观点演化 + 意图
mem.add_fact("用户", "city", "南京", source="profile", confidence=0.9,
             valid_at=datetime(2025, 8, 1), invalid_at=datetime(2026, 7, 1))
mem.add_fact("用户", "city", "上海", source="profile", confidence=0.9,
             valid_at=datetime(2026, 7, 1))

ctx = mem.recall_context(RecallQuery(text="我现在住哪里"),
                         query_time=datetime(2026, 9, 1))
print(ctx.query_type)                       # current_state
print([f.value for f in ctx.current_state])  # ['上海']

# 时间线 / 历史 / 解释
mem.timeline(entity_id="用户", start=datetime(2025, 1, 1))
mem.memory_history("用户", dimension="fact")
mem.explain(2, kind="fact")  # 返回 Memory+Evidence+Version+Source+Related
```

- QueryRouter 纯规则路由（当前/以前/什么时候/为什么变…），无 LLM 可运行；
- 当前事实由确定性裁决（写入端固化的 `superseded_by` → 来源可信级 → 置信度 → 新鲜度），不使用向量相似度；
- 证据不足 → 自动 abstain 说明（不编造原因）；`inferred` 证据强制标注推断。

## 服务化

### FastAPI 侧车

```powershell
py -m app.dnamemory_server --path user_memory.db --token <token>
py -m app.dnamemory_server --path user_memory.db --token <token> --bge-m3 --fallback-extractor
```

- 端点：`GET /health`、`POST /recall`、`POST /write`、`POST /forget`；
- 鉴权：启动时必须提供 token（`--token` 或环境变量 `DNAMEMORY_TOKEN`），请求头 `Authorization: Bearer <token>`；
- Swagger：`http://127.0.0.1:8000/docs`，点击 **Authorize** 填入 token 后即可在页面内调试；
- `--bge-m3` 加载本地语义模型；`--fallback-extractor` 让 `/write` 无需 LLM 即可写入原始文本。

### FastMCP 服务

```powershell
py -m app.dnamemory_mcp --path user_memory.db                          # stdio
py -m app.dnamemory_mcp --transport streamable_http --token <token> --port 8765
```

工具：`dnamemory_recall` / `dnamemory_write_memory` / `dnamemory_forget` / `dnamemory_resolve_conflicts`。

## 评测与门禁

```powershell
py -m pytest                                # 59 例全量回归
py tools/gate_check.py                      # 固定快照门禁（缺失自动合成）
py tools/eval_longterm.py --bge-m3 --mode quad   # 中文五类能力评测
```

## 文档索引

| 文档 | 内容 |
|---|---|
| [需求设计](docs/需求设计.md) | 背景、功能需求 FR-01~15、非功能需求、边界、验收标准 |
| [系统设计与架构设计](docs/系统设计与架构设计.md) | 分层架构、数据模型与状态机、检索管线、治理、索引降级链、服务化、ADR |
| [功能清单](docs/功能清单.md) | 全部功能/入口/状态、验证结果、明确不包含项 |
| [开箱即用手册](docs/开箱即用手册.md) | 安装、CLI/API 快速开始、增强能力启用、评测、FAQ |
| [评估报告：Postgres 与 Neo4j 替换](docs/评估报告-Postgres与Neo4j替换.md) | 时间链/空间链换 Postgres/Neo4j 的对比验证与结论 |
| [开发计划：可插拔后端](docs/开发计划-可插拔后端.md) | TimeStore/GraphStore 抽象与默认路径零开销的实施计划 |
| [后端选型](docs/后端选型.md) | 规模/并发/深跳决策表、注入示例、性能数据 |
| [规模化检索方案](docs/规模化检索方案.md) | 暴力检索治理：ANN top-k、邻接缓存、时间 SQL 预筛与后续路线 |
| [业务改造](docs/业务改造.md) | 0.5.0 记忆 Runtime 业务设计稿（六类对象/状态解析/时间链/证据） |
| [改造系统设计与架构设计](docs/改造系统设计与架构设计.md) | 0.5.0 架构设计稿（模块/DDL/管线/API/评测，已按本稿实施） |

## 目录结构

```text
src/dnamemory/    核心库（models/store/retrieval/governance/memory/extract/embeddings/rerank/cli
                   + 0.5.0: resolve/temporal/coherence/context/backends）
app/              FastAPI 侧车 + FastMCP 服务
tools/            gate_check / eval_longterm / eval_state / pg_migrate
tests/            全量 pytest 用例
data/             中文能力评测集 + 状态正确性评测集
docs/             正式文档 + 两份 0.5.0 设计稿
.github/          CI workflow
```

## 验证状态（2026-09-15 基线）

| 项目 | 结果 |
|---|---|
| pytest 全量 | 全部通过（含 0.5.0 状态层/时间链/一致性/服务/CLI 回归） |
| 门禁（快照召回） | dual Recall 1.000 / MAP 1.000；triple 1.000 / 1.000 |
| 门禁（状态正确性） | §25 Test 01-05 全过（当前事实/时间链顺序/观点演化/多维共存/证据不足 abstain） |
| 状态正确性评测 | 五类能力（temporal_chain/belief_change/fact_conflict/evidence_grounding/cross_dimension）均 1.000 |
| 安装 | `dnamemory --version` = 0.5.0 |
| 向后兼容 | 0.4.0 旧库打开自动迁移（幂等补列建表），旧功能全绿 |
