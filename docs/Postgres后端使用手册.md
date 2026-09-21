# PostgreSQL 存储后端使用手册

> 适用版本：0.8.0+｜落地日期：2026-09-17
> 定位：**可选**存储后端。默认仍是 SQLite（零配置、嵌入即用）；只有需要多进程共享 /
> 服务化部署 / 运维能力时才切到 PG。

## 1. 什么时候该用（以及不该用）

| 场景 | 建议 |
|---|---|
| 单进程、边 < 5 万、嵌入 Agent 进程内使用 | **SQLite**（默认）。见 [评估报告](评估报告-Postgres与Neo4j替换.md)：时间链 0.29ms vs PG 4.26ms（含往返） |
| 多进程/多实例共享一个库（api + mcp + cli 同时读写） | **PG** |
| 需要在线备份、主从、连接池、监控等运维能力 | **PG** |
| 十万级以上时间链 | **PG** |
| 离线/内网单机、不想装服务 | **SQLite** |

一句话：**迁移到 PG 不会更快**（小库反而更慢），它换的是并发与运维能力。

架构上，「存储后端」（本手册）与「检索后端」（`TimeBackend`/`GraphBackend`，见
[后端选型](后端选型.md)）是**两条正交的可插拔轴**。

## 2. 快速开始

```powershell
# 1) 起库（pgvector 镜像；端口 5433，避开常被占用的 5432）
docker compose -f docker-compose.pg.yml up -d

# 2) 迁移现有 SQLite 数据（源库只读打开，可反复重跑）
py ops/migrate_sqlite_to_pg.py --src ops/data/lccc_memory.db `
   --dsn postgresql://dnatest:dnatest@127.0.0.1:5433/dnamemory --truncate

# 3) 切到 PG 跑服务
$env:DNAMEMORY_DSN="postgresql://dnatest:dnatest@127.0.0.1:5433/dnamemory"
dnamemory-server --token demo --port 8000

# 4) 回退（清掉变量重启即可，默认路径逐字节不变）
Remove-Item Env:\DNAMEMORY_DSN
```

CLI 同样可用：`dnamemory recall --text "预算" --dsn <DSN>`；不传 `--dsn` 且未设环境变量时用 `--db`。

## 3. 配置

| 入口 | 参数 | 说明 |
|---|---|---|
| 环境变量 | `DNAMEMORY_DSN` | 三个入口共用的默认值 |
| Python | `MemorySystem(dsn=...)` 或 `MemorySystem(store=PGStore(dsn))` | 二者与 `path=` 互斥时优先 `dsn`；`store=` 与 `dsn=` 不可同时给（报 E001） |
| CLI | `--dsn` | 命令行 > 环境变量 > `--db` 的 SQLite |
| HTTP 服务 | `--dsn` | `dnamemory-server --dsn <DSN>` |
| MCP | `--dsn` | `dnamemory-mcp --dsn <DSN>`（stdio / streamable_http 均可） |

解析逻辑只在 `dnamemory.storage.resolve_dsn` 一处。

## 4. 数据迁移

`ops/migrate_sqlite_to_pg.py`：

- **源库只读打开**（`mode=ro`）——迁移永不改动源库，**原 SQLite 库始终是权威副本**；
- PG 侧 `--truncate` 可反复重跑（`--append` 增量、`--dry-run` 只统计）；
- **id 保真**：显式插 id，搬完逐表 `setval` 重置序列（新写入不会撞既有 id）；
- 15 张表按父子序搬运，搬完做 FK 孤儿检查；
- 向量：源 `dense`（JSON 文本）原样进 `dense_json`，同时按 1024 维归一化投影到 pgvector 的 `dense` 列；
- 一致性报告写 `ops/data/migration_report.json`：逐表行数 / id 范围 / 序列值 /
  抽样逐列比对 / 孤儿检查；有任何不一致则退出码 1。

## 5. pgvector

- 镜像必须是 `pgvector/pgvector`（裸 `postgres` 镜像不含扩展，检索会静默降级）；
- `node_vectors` 是双列：`dense vector(1024)`（HNSW 余弦索引）+ `dense_json TEXT`
  （原始 JSON，供降级与逐字节对拍）；
- 仅 **1024 维**投影到 ANN 列；其它维度 `search_vectors` 返回 `None`，自动走
  numpy 全量余弦降级（既有契约，测试用的 dim=4 即走此路径）；
- 余弦距离语义 `<=>` = `1 - cos`，与 sqlite-vec 的 `distance_metric=Cosine` 一致；
- HNSW 是近似的：`ef_search` 默认 40 而调用方会传 `k=1000`，实现里按 k 动态
  `SET hnsw.ef_search`（否则严重欠召回）。

## 6. 已知限制

- **`_name2id` 跨进程陈旧**：`MemorySystem` 构造期预建名字→id 映射，其它进程新建的
  实体在本进程内需重建实例才可见；
- **单连接模型**：`PGStore` 用单连接 + 进程内锁 + `pg_advisory_xact_lock`，
  跨进程写是串行的；高并发写场景需连接池（当前未引入 `psycopg_pool`）；
- **`ops/pg_migrate.py` 已薄壳化**：DDL 单一来源改为 `dnamemory.pg_schema`，
  不再维护第二套（旧版只有 8 张表）；
- **`sync_memory_to_postgres` 与 PGStore 不共用库**：前者是只读时间链后端的精简
  schema（`nodes` 只有 5 列），且会 `DROP TABLE`。已加守卫：目标库若含
  `idempotency_key` 列则拒绝执行；`PGStore` 连到精简库时也会在 `ensure_schema`
  阶段报 E009 并说明原因。

## 7. 故障排查

| 现象 | 原因与处置 |
|---|---|
| 启动报 `E009 无法连接 PostgreSQL` | 容器没起或端口不对：`docker compose -f docker-compose.pg.yml ps` |
| `E009 目标库的 nodes 表缺少列 [...]` | 连到了时间链后端的精简库；换库或先跑迁移 |
| `E009 目标库是 dnamemory 主存储 schema，拒绝执行 DROP` | 反向守卫：`sync_memory_to_postgres` 被指向了主存储库 |
| 检索结果比 SQLite 少 | 向量未投影（`dim != 1024`）或忘 `CREATE EXTENSION vector`；查 `SELECT count(*) FROM node_vectors WHERE dense IS NOT NULL` |
| 迁移报行数不一致 | 看 `ops/data/migration_report.json` 的 `mismatches`；`--truncate` 重跑即可 |

## 8. 相关文件

| 文件 | 作用 |
|---|---|
| `docker-compose.pg.yml` | PG 容器定义（pgvector/pg17，5433） |
| `src/dnamemory/pg_schema.py` | DDL 单一真理源（16 张表 + HNSW 索引） |
| `src/dnamemory/pg_store.py` | `PGStore` 实现（与 `SQLiteStore` 同签名） |
| `src/dnamemory/storage.py` | DSN 解析与存储选择 |
| `ops/migrate_sqlite_to_pg.py` | 数据迁移 + 一致性报告 |
| `ops/pg_smoke.py` | 端到端冒烟（重建库→迁移→HTTP 读写→连接归零） |
| `ops/pg_migrate.py` | 建表薄壳（打印或执行 DDL） |
