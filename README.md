# dnamemory

长期个人智能体记忆系统：以**带类型关联边的记忆图谱**为核心，提供多路召回、受控遗忘、冲突治理、访问控制与反射压缩的 Python 记忆库。可嵌入智能体、进程内零配置使用，也可通过 FastAPI / MCP 服务化。

> 版本：0.3.0｜Python ≥ 3.10｜正式版（MVP 验证阶段已归档清理）

## 核心能力

- **多路召回**：时间路 + 图谱路 + 稠密语义路（bge-m3）+ 稀疏路，支持 `dual / triple / quad` 模式与 RRF 融合，可选 reranker 重排；
- **治理完备**：生命周期状态机（active → archived → tombstoned → deleted）、受控衰减、合规删除级联、冲突治理、三级访问控制（public/private/sensitive）、月度反射压缩、实体消解；
- **工程严谨**：SQLite 事务/幂等/审计/墓碑、sqlite-vec 余弦向量索引与 numpy 降级链、统一错误码体系（E000-E011）；
- **多种形态**：Python API、CLI、FastAPI 侧车（Bearer 鉴权 + Swagger Authorize）、FastMCP（stdio / streamable_http）；
- **可验证**：50 个 pytest 用例、固定快照门禁、自包含中文五类能力评测集。

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
py -m pytest                                # 50 例全量回归
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

## 目录结构

```text
src/dnamemory/    核心库（models/store/retrieval/governance/memory/extract/embeddings/rerank/cli）
app/              FastAPI 侧车 + FastMCP 服务
tools/            gate_check / eval_longterm / pg_migrate
tests/            50 个 pytest 用例
data/             自包含中文能力评测集
docs/             四份正式文档
.github/          CI workflow
```

## 验证状态（2026-08-03 基线）

| 项目 | 结果 |
|---|---|
| pytest 全量 | 50/50 通过 |
| 门禁（真实快照） | dual Recall 0.993 / MAP 0.978；triple 1.000 / 0.996 |
| 中文能力评测（bge-m3，triple/quad） | 五类能力整体 1.000 |
| 安装 | `pip install -e .` 成功，`dnamemory --version` = 0.3.0 |
| 待外部执行 | reranker 同义改写对比（需下载约 2GB 模型） |
