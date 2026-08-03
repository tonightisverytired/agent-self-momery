# dnamemory

长期个人智能体记忆系统：以**带类型关联边的记忆图谱**为核心，提供多路召回、受控遗忘、冲突治理、访问控制与反射压缩的 Python 记忆库。可嵌入智能体、进程内零配置使用，也可通过 FastAPI / MCP 服务化。

> 版本：0.3.0｜正式版（demo 与 MVP 验证阶段已归档清理）

## 核心能力

- **多路召回**：时间路 + 图谱路 + 稠密语义路（bge-m3）+ 稀疏路（quad），RRF 融合，可选重排；
- **治理完备**：生命周期状态机、受控遗忘、合规删除级联、冲突治理、三级访问控制、月度反射压缩、实体消解；
- **工程严谨**：SQLite 事务/幂等/审计/墓碑、sqlite-vec 向量索引与 numpy 降级链、错误码体系；
- **多种形态**：Python API、CLI、FastAPI 侧车、FastMCP（stdio/streamable_http）；
- **可验证**：50 个 pytest 用例、门禁脚本、中文五类能力评测集。

## 快速开始

```powershell
pip install dnamemory

# 生成演示库并体验
dnamemory demo --db user_memory.db
dnamemory recall --text "预算" --k 5 --db user_memory.db
```

Python：

```python
from dnamemory import MemorySystem, RecallQuery

mem = MemorySystem(path="user_memory.db")
mem.add_event("与张总讨论预算", None, kind="meeting", value_score=0.6)
hits = mem.recall(RecallQuery(text="预算"), k=5, mode="triple")
```

增强能力（真实语义/重排/索引/服务化）见[开箱即用手册](docs/开箱即用手册.md)。

## 文档索引

| 文档 | 内容 |
|---|---|
| [需求设计](docs/需求设计.md) | 背景、功能/非功能需求、边界、验收标准 |
| [系统设计与架构设计](docs/系统设计与架构设计.md) | 分层架构、数据模型、检索管线、治理、索引、服务化 |
| [功能清单](docs/功能清单.md) | 全部功能、入口、状态与验证结果 |
| [开箱即用手册](docs/开箱即用手册.md) | 安装、快速开始、增强能力启用、评测、FAQ |

## 目录结构

```text
src/dnamemory/    核心库（models/store/retrieval/governance/memory/...）
app/              FastAPI 侧车 + FastMCP 服务
tools/            评测、门禁、Postgres DDL
tests/            50 个 pytest 用例
data/             中文能力评测集
docs/             四份正式文档
.github/          CI workflow
```

## 验证状态（2026-08-03）

- pytest：50/50 通过；
- 门禁：dual Recall 0.993 / MAP 0.978，triple 1.000 / 0.996；
- 中文能力评测（自包含语料，bge-m3 triple/quad）：五类能力整体 1.000；
- 待外部执行：reranker 同义改写对比（需下载约 2GB 模型）。
