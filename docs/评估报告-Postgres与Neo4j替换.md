# 评估报告：时间链换 PostgresStore、空间链换 Neo4j

> 日期：2026-08-05｜版本：基于 dnamemory 0.3.0
> 验证工具：[tools/backend_compare.py](../tools/backend_compare.py)
> 结果数据：[simulation/backend_compare_result.json](../simulation/backend_compare_result.json)（本地，gitignore）

## 1. 背景与目标

评估两个架构替换方向：

1. **时间链 → PostgresStore**：时间窗召回的数据层从 SQLite 换到 PostgreSQL；
2. **空间链 → Neo4j**：实体-事件关联图的多跳遍历从"SQLite 边表 + Python BFS"换到 Neo4j 图数据库。

方法：同一份确定性合成数据分别加载到 SQLite（当前实现）、Postgres（docker，端口 5433）、Neo4j（docker，端口 7687），对比时间链与空间链的**召回正确性**（结果集一致性）与**查询延迟**，共三档规模。

## 2. 验证环境

- SQLite：进程内，`nodes.ts` 索引 + `edges(from_id/to_id)` 索引；
- Postgres：`postgres:15-alpine`，`nodes.ts` B-tree 索引 + `edges` 双向索引，时间链用索引范围扫描，空间链用递归 CTE；
- Neo4j：`neo4j:2025.03.0-community`，`Event(ts)` 属性索引 + `Entity/Event(id)` 唯一约束，空间链用 Cypher 变长路径 `-[*1..2]-`；
- 参数：max_hops=2，时间容差 ±2 天，每档 20 个查询，各后端先 warmup 再计时。

## 3. 结果

### 3.1 正确性

三档规模 × 时间链/空间链 × 20 查询：**Postgres、Neo4j 与 SQLite 当前实现的结果集完全一致**（`correctness = true`）。

### 3.2 时间链（时间窗召回，ms，p50）

| 规模（事件/边） | SQLite（当前） | Postgres | Neo4j |
|---|---:|---:|---:|
| 1,000 / 4,496 | 0.19 | 2.13 | 11.09 |
| 5,000 / 14,543 | 0.16 | 1.27 | 18.86 |
| 20,000 / 53,973 | 0.41 | 1.98 | 10.74 |

### 3.3 空间链（实体种子 2 跳遍历，ms，p50）

| 规模（事件/边） | SQLite + Python BFS（当前） | Postgres 递归 CTE | Neo4j Cypher |
|---|---:|---:|---:|
| 1,000 / 4,496 | 4.22 | 4.74 | 11.31 |
| 5,000 / 14,543 | 13.19 | 2.57 | 16.93 |
| 20,000 / 53,973 | 74.55 | 6.63 | 6.59 |

## 4. 分析

### 4.1 时间链

- SQLite 在 2 万事件以内始终最快（0.2-0.4ms）：进程内零网络往返，索引查询直接命中；
- Postgres 稳定在 1.3-2.1ms：主要开销是每次查询的网络往返与解析，但**并发多进程、时间分区、更大数据集（十万级以上）与在线备份**是 SQLite 不具备的；
- Neo4j 时间属性查询 11-42ms，明显不适合承担时间链。

### 4.2 空间链

- 小规模（万边内）当前 Python BFS 足够快（4-13ms），零依赖优势明显；
- 中规模（约 1.5 万边）Postgres 递归 CTE 已反超 Python BFS（2.6ms vs 13.2ms），原因是 BFS 每查询要把全部边读进 Python；
- 大规模（约 5.4 万边）Python BFS 退化到 74.5ms，Postgres（6.6ms）与 Neo4j（6.6ms）持平；
- Neo4j 的真正优势（深多跳、路径/社区分析、大规模图）在 hop=2 的基准里没有体现；其代价是常驻服务、驱动往返、许可证与运维成本。

## 5. 结论与建议

1. **不替换默认实现**：当前 SQLite 在 5 万边以下综合最优（速度 + 零配置 + 单文件），满足 NFR-01/02；
2. **PostgresStore 值得作为"可选后端"实现**：方向正确，收益在**多进程共享、十万级以上时间链、运维能力**；建议按 `StoreBackend` 协议实现并保持默认 SQLite 不变；
3. **Neo4j 作为空间链替换，当前不建议**：收益不足（hop=2 场景与 Postgres/SQLite 拉不开）、成本高（破坏零配置、新增服务依赖）；仅当图谱达**十万边以上且查询以深多跳/路径分析为主**时再评估；
4. **落地顺序建议**：若推进，先把"时间链/空间链"解耦为可插拔后端接口（TimeStore / GraphStore），再分别实现 Postgres 与 Neo4j 适配，避免整体替换带来的回归风险；
5. 后续补测项：并发吞吐、写入延迟、10 万+边规模、hop≥3、故障恢复与多进程场景。

## 5.1 关键发现与设计原则（必须遵守）

**验证结论（关键发现）**：

- SQLite 进程内路径在 5 万边以内全面占优（时间链 0.2-0.4ms、空间链 4-13ms），Postgres/Neo4j 的收益只在更大规模或特定场景（多进程、深多跳）才出现；
- 因此，**可插拔后端抽象层只服务"显式启用后端"的场景；默认小批量路径不得经过任何抽象分发**，否则每一次 `recall` 都会付出协议派发/接口调用/连接往返的额外开销，反而拖慢默认场景。

**设计原则（写入开发计划与系统设计）**：

1. 默认路径零开销：`MemorySystem(time_backend=None, graph_backend=None)` 时，检索继续走现有 SQLite 直连实现（`governance.recall` 原路径，零改动、零派发）；
2. 抽象层按需启用：仅在调用方显式注入 `time_backend` / `graph_backend` 时才做协议派发；
3. 后端语义对齐：任何后端必须返回与现有实现一致的打分语义（score dict），RRF 融合与过滤链逻辑保持唯一；
4. 小批量自动决策：默认文档化阈值（如边数 < 5 万且单进程）建议直接使用内置 SQLite，不引入后端。

## 6. 局限

- 合成数据、单机本地、hop=2；未覆盖真实语料分布与写入并发；
- Postgres/Neo4j 延迟包含每次查询的连接/往返开销（本地回环）；
- 未评估许可证（Neo4j Community GPLv3）与部署/备份运维成本。
