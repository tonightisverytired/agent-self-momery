# dnamemory

长期个人智能体记忆 **Runtime**：以**带类型关联边的记忆图谱**为核心，提供多路召回、受控遗忘、冲突治理、访问控制、反射压缩，以及 0.5.0 新增的**记忆状态解析**（Fact/Belief/Intent/Evidence 分层）、**时间链**、**证据追溯**与**跨维度一致性**的 Python 记忆库。可嵌入智能体、进程内零配置使用，也可通过 FastAPI / MCP 服务化。

> 版本：0.8.2｜Python ≥ 3.10｜设计依据：[业务改造](docs/业务改造.md)、[改造系统设计与架构设计](docs/改造系统设计与架构设计.md)；召回/相关度/解释度改进见 [改进方案](docs/改进方案-召回相关度解释度.md)

## 核心能力

- **多路召回**：时间路 + 图谱路 + 词面路（中文 bigram 包含度，0.8.1 重写）+ 稠密语义路（bge-m3）+ 稀疏路，词面/稠密**双路并行**互不稀释，支持 `dual / triple / quad` 模式与 RRF 融合，可选 reranker 重排；`MemoryHit.path_scores` 透出各路原始分与名次（0.8.1）；
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

### FastAPI 服务（0.8.0 起：读写全能力 + 静态后台）

```powershell
# 方式一：配置文件（推荐；CLI / HTTP / MCP 三个入口共用同一份）
cp dnamemory.env.example dnamemory.env    # 改 host / token / 库路径即可
dnamemory-server

# 方式二：命令行参数 / 环境变量
dnamemory-server --path user_memory.db --token <token>
dnamemory-server --path user_memory.db --token <token> --bge-m3 --fallback-extractor
# 环境变量：DNAMEMORY_DB / DNAMEMORY_TOKEN / DNAMEMORY_HOST / DNAMEMORY_PORT / DNAMEMORY_DSN
```

- **配置文件**（`.env` 风格，零依赖）：查找顺序 `--config` > `DNAMEMORY_CONFIG` > `./dnamemory.env` > `~/.dnamemory.env`；优先级 **命令行 > 真实环境变量 > 配置文件 > 内置默认**（容器/systemd 注入的值不会被文件覆盖）；只识别 `DNAMEMORY_` / `DEEPSEEK_` 前缀的键。模板见 [dnamemory.env.example](dnamemory.env.example)；
- **对外提供 API**：改配置文件里的 `DNAMEMORY_HOST=0.0.0.0` 与 `DNAMEMORY_CORS_ORIGINS` 即可，安全清单与调用示例见 [API 接入指南](docs/API接入指南.md)。

- **后台页面**：启动后打开 `http://127.0.0.1:8000/`（零构建静态控制台：侧边栏导航分主功能与高级两组——**问答台**（首页：提问 + 「告诉它一件事」文本记忆 + 事实卡片带 memory_score 分项迷你条）、**人物**（0.8.2：按人聚合——左栏人物列表，右栏画像/关系/经历过的事，解决原子化存储的零散观感）、总览·记忆健康（记忆构成卡/近期治理动作/分布横条/最近事件）、**记忆图谱**（echarts 力导向，大库 >800 节点默认子图引导态，CDN 失败降级表格）、治理（需要确认/日常整理/危险操作三组）；高级：分面浏览、查询台（16 区块）、写入表单；token 在页面顶栏配置）；
- 端点 32 个（读写全能力）：元数据 `GET /health`、`GET /stats`；查询 `POST /recall`、`POST /context`（16 区块）、`POST /timeline`、`GET /memory/{id}/history`、`GET /memory/{id}/explain`、`GET /facts`、`GET /neighbors`、`GET /graph`（图谱快照：节点/边/分布计数/最近事件，支持 center 子图与 limit 截断）、`GET /audit`（裁决/治理审计查询，0.8.1）、`GET /entities`（按 kind 列实体带事实数，0.8.2）；写入 `POST /entities /events /facts /beliefs /intents /impacts /evidence /edges /write /batch`；治理 `POST /forget /resolve-conflicts /confirm /resolve-entities /reflect /compress-stable /extract-patterns /derive-impact-links /step-day /restore`。全表见 [API 文档](docs/api.md)；
- 鉴权：启动时必须提供 token（`--token` 或 `DNAMEMORY_TOKEN`），请求头 `Authorization: Bearer <token>`；错误体统一 `{code, message}`；
- Swagger：`http://127.0.0.1:8000/docs`；
- `--bge-m3` 加载本地语义模型；`--fallback-extractor` 让 `/write` 无需 LLM 即可写入原始文本。

### PostgreSQL 存储后端（可选，0.8.0 起）

默认仍是 SQLite（零配置、进程内嵌入）；需要**多进程共享 / 服务化运维**时用 DSN 切到 PostgreSQL：

```powershell
docker compose -f docker-compose.pg.yml up -d                     # pgvector/pg17，端口 5433
py ops/migrate_sqlite_to_pg.py --src user_memory.db --dsn <DSN>   # 迁移（源库只读，可重跑）
$env:DNAMEMORY_DSN="postgresql://dnatest:dnatest@127.0.0.1:5433/dnamemory"
dnamemory-server --token <token>                                  # CLI / HTTP / MCP 三入口都认 DSN
```

- **注意**：小库迁到 PG **不会更快**（边 < 5 万时 SQLite 更优，见 [评估报告](docs/评估报告-Postgres与Neo4j替换.md)），换的是并发与运维能力；
- 向量用 pgvector（`vector(1024)` + HNSW 余弦索引）；非 1024 维自动回退 numpy 全量余弦；
- 回退：清掉 `DNAMEMORY_DSN` 重启即可，默认路径与既有行为逐字节相同；
- 完整说明见 [Postgres 后端使用手册](docs/Postgres后端使用手册.md)。

### FastMCP 服务

```powershell
dnamemory-mcp --path user_memory.db                                   # stdio
dnamemory-mcp --transport streamable_http --token <token> --port 8765
```

工具 10 个：`dnamemory_recall` / `dnamemory_write_memory` / `dnamemory_forget` / `dnamemory_resolve_conflicts` / `dnamemory_context`（16 区块）/ `dnamemory_timeline` / `dnamemory_explain` / `dnamemory_stats` / `dnamemory_write_structured` / `dnamemory_govern`。

## 评测与门禁

```powershell
py -m pytest                                # 全量回归
py ops/gate_check.py                        # 固定快照门禁（缺失自动合成）
py ops/eval_longterm.py --bge-m3            # 中文五类能力评测
py ops/eval_state.py                        # 七类状态正确性评测
```

## 文档索引

| 文档 | 内容 |
|---|---|
| [需求设计](docs/需求设计.md) | 背景、功能需求 FR-01~19（含 0.5.0 记忆 Runtime 与 0.8.0 服务化/后台）、非功能需求、边界、验收标准 |
| [系统设计与架构设计](docs/系统设计与架构设计.md) | 分层架构、数据模型与状态机、六类记忆对象、检索管线与 Runtime 管线、治理、索引降级链、服务化（30 端点/GET /graph/静态后台架构）、安全与错误码、ADR |
| [功能清单](docs/功能清单.md) | 全部功能/入口/状态、验证结果、明确不包含项 |
| [API 文档](docs/api.md) | 0.8.0 HTTP 29 端点全表（路径/请求/响应/错误码） |
| [后台验收清单](docs/admin_checklist.md) | 静态后台人工验收步骤 |
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
                   + resolve/temporal/coherence/context/impact_chain/backends）
server/           对外服务层（api/ FastAPI 路由 + mcp.py + main.py + web/ 静态后台）
ops/              工具与评测（gate_check / eval_longterm / eval_state / pg_migrate / LCCC 注入与检索）
tests/            全量 pytest 用例
data/             中文能力评测集 + 状态正确性评测集
docs/             正式文档 + 设计稿
.github/          CI workflow
```

## 验证状态（2026-09-19 基线，0.8.1 改进迭代后实测）

| 项目 | 结果 |
|---|---|
| pytest 全量 | 347 通过（含召回/证据/解释性新增用例；PG/Neo4j 2 项无服务跳过） |
| 门禁（快照召回） | dual Recall 1.000 / MAP 1.000；triple 1.000 / 1.000 |
| 门禁（paraphrase 探针，0.8.1 新增） | 228 条干扰事件下自然问句 hit@5 = 1.000，域外 abstain = 1.000 |
| 门禁（状态正确性） | §25 Test 01-05 全过（当前事实/时间链顺序/观点演化/多维共存/证据不足 abstain） |
| 状态正确性评测 | 七类（temporal_chain/belief_change/fact_conflict/evidence_grounding/cross_dimension/impact_state/associative_recall）均 1.000 |
| 长期能力评测（无向量 triple） | overall 0.800（0.8.0 为 0.450）：temporal 0.75 / multi_hop 1.0 / update 0.75 / abstain 1.0 / paraphrase 0.5 |
| 真实语料探针（LCCC 500 对话库，n=30） | 整名召回 1.000（无向量与 bge-m3 一致；0.8.0 bge-m3 仅 0.5）；**答案可得率 0.833**（0.8.0 为 0.233）；事实源事件召回率 1.000 |
| 事实检索（LCCC，bge-m3 triple） | 严格命中率 0.900（27/30；0.8.0 为 0.167），词面相关率 1.000 |
| **PerLTQA-zh 正式基准**（2026-09-19/20，官方数据集，人工标注 QA） | 小库（2 人物/129 节点，444 QA 全量）：retrieval_hit@10 = **0.964**、answer_in_context = **0.954**；规模库（105 人物/7228 节点/13240 边，400+128 抽样）：retrieval = **0.878**、answer = **0.873**；**规模库+reranker（最优配置）：retrieval = 0.9025**（events 0.94 / dialogues 0.83 / social 0.97 / profile 1.00） |
| explain 证据追溯（LCCC） | 有效追溯率 1.000（7/7；0.8.0 为 0/7 全部 E013 弃权）——写入端批级证据自动闭环生效 |
| 安装 | `dnamemory --version` = 0.8.2 |
| 0.8.2 个人助手场景优化（2026-09-20 实测，规模库） | recall_context 延迟 p50 **4.3s → 1.45s**（读快照缓存/解析缓存/打分索引化）；profile/social 类原失败样本点验 5/6 修复（查询侧 key 族 + 分层相关性 + 实体锚定进候选）；pytest 367 全绿 |
| 向后兼容 | 0.4.0 旧库打开自动迁移（幂等补列建表），旧功能全绿 |
