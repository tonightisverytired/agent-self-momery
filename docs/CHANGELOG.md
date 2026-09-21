# Changelog

## 0.8.0（2026-09-16）— 目录大重构 + 读写全能力 API + 六页可视化后台

**Breaking changes**：
- `app/` 目录删除：`py -m app.dnamemory_server` → `dnamemory-server`（或 `py -m server.main`）；`py -m app.dnamemory_mcp` → `dnamemory-mcp`（或 `py -m server.mcp`）；`from app.dnamemory_server import create_app` → `from server.api.app import create_app`
- 错误体统一平铺 `{code, message}`（旧 `{"detail": {...}}` 移除）；401 增加 code E401
- 工具脚本路径迁移：`tools/` → `ops/`（门禁命令改为 `py ops/gate_check.py` 等）；`simulation/` 产物 → `ops/data/`

**新增**：
- `server/` 对外服务层（进 wheel，双包打包）：api 路由拆分（routes_query/write/govern + deps + schemas + serializers + static）、mcp.py、main.py（env DNAMEMORY_DB/TOKEN/HOST/PORT/CORS_ORIGINS）、web/ 静态后台
- HTTP 端点 8 → 30：stats/facts/neighbors/graph + 8 个结构化写入 + batch + 10 个治理端点；/context 16 区块全序列化（causes/impacts/impact_chains/patterns/score）
- `GET /graph` 图谱快照：节点/边/分布计数/最近事件，支持 `center` 子图 BFS 与 `limit` 边截断（counts 为截断前全量）
- 静态后台（零构建多视图 SPA，无 npm）：侧边栏六页——仪表盘（分布横条/最近事件/active 口径）、记忆图谱（echarts 力导向、节点详情抽屉、子图聚焦、CDN 四源降级为结构化表格）、分面浏览（事实/邻接/时间线/详情）、查询台（召回 + 上下文 16 区块）、写入表单（8 类结构化 + 抽取 + 批量）、治理操作台（10 操作 + 二次确认）；token 存 localStorage
- MCP 工具 7 → 10（stats/write_structured/govern；context 输出 16 区块）
- CORS（默认仅本机任意端口）+ lifespan 资源管理（仅工厂实例负责 close）
- `GET /health` 增加 `version` 字段（供后台顶栏版本徽标；`ok` 字段不变）

**修复（09-18，上下文膨胀与假冲突）**：

- **检索 0 命中时管线退化成「全库倾倒」**：`candidate_count=0` 却 `resolved_count=149`（fact/belief/intent/impact/pattern 五个维度本来就是全表裁决，候选只用于 event）；`causes` 取全库 `caused_by` **且无预算**（实测 503 条）；trigger 的裸子串匹配命中 291 行（`_rule_triggers_fact` 给每条 fact 写一行 entity trigger，文本都是「用户」）。结果任意查询都命中 14/16 个区块。修：新增 `_query_scope`「查询在场地」，**只收窄推理类区块**——`causes` 只解释上下文里展示的事实并受新预算 `causes=10` 约束、`impacts`/`patterns` 只保留主体在场地内的条目、`impact_chains` 的记忆集合改为「本次实际展示的内容」且丢弃单点链、无在场地对象时不做时间链推导；状态层仍全库确定性裁决（「相关性不等于状态」，§30）
- **无关查询的诚实化**：无在场地对象时不再假装有答案，`notes` 增加「未命中相关记忆：下面是库中当前状态，不是对本次问题的回答」；实测同一库「zzz 不存在的查询」从 14/16 降到 **10/16**（推理类 6 块全空），「用户住在哪里」causes 503→10、patterns 333→3
- **conflicts 出现「只有一个值」的假冲突**：`FactResolver` 按 canonical key 判冲突但**输出扁平 Fact 列表**（组结构丢失），`ContextBuilder` 又按 **fact 条数**截断（把并列组腰斩）、`serializers._conflict_dicts` 再按**原始 key** 重新分组且**缺 `len<2` 守卫**（把同义 key 劈开）。修：引入 `ConflictGroup` 并贯穿 resolve → context → serializer，截断语义改为**按组数**（`DEFAULT_BUDGETS["conflicts"]`），序列化只做格式转换 + 单值守卫。实测 `location` 由「6 值 + 独立 1 值 所在地」归并为 7 值组、`phone_brand` 由被腰斩的 1 值恢复为 2 值组
- **current_state 先截断后排序**：`build()` 先 `[:10]` 再按 MemoryScore 排序，等于「前 10 名内部重排」，更相关的事实永远捞不回来；改为**全量排序后取预算**
- **行为模式按主体生成**（`extract_patterns` 规则 4）：原实现对「每个邻接主体 × 每个 kind」都生成一行、`support` 写全库该 kind 事件数（实测 330 行只有 13 个命题、286 个主体）；改为只统计**主体自己的事件**且 ≥3 条才生成
- `trace.final_context_count` 补全（原实现漏掉 causes/impacts/impact_chains/patterns/temporal_chains，503 条的膨胀在 trace 里看不出来）；前台 trace 标签同步（影响/模式标注「在场地」）
- 查询台：`notes` 区块移入「直接回答」组，未命中提示直接出现在结果顶部
- **同值重复行不再判为冲突**：同源同置信但值完全相同的两条事实（重复抽取常见）原本也走冲突分支——两条都会从 `current_state` 消失，而冲突区块里又看不出谁赢；现在只在**存在 ≥2 个不同值**时才判冲突，否则正常裁决（一条 current、其余 history）
- 补 8 条回归：无关查询收口、causes 只解释展示事实 + 预算、conflicts 按组截断、单值组守卫、同义 key 不劈裂、同值不判冲突、行为模式按主体；并修 `test_fact_resolve_chain` 场景 2 的断言（冲突元素类型为契约变更）

**修复（09-17，六页改造回归）**：
- 视图路由错配：`route()` 原按视图名直取 DOM id，而 section id 为 `query-console`/`write-form`/`govern-console`，叠加 `.view{display:none}` 后**查询/写入/治理三页永久空白**；改为 `data-view` 属性定位
- 写入表单首次进入字段区为空（只挂 `onchange`，缺初始渲染）；改由视图 `refresh()` 保证
- 图谱首屏长时间空白：echarts 只在进入图谱页才开始加载（CDN 8s）+ 977 节点力导向，实测 20s 内画布全空且无提示；改为页面初始化即**预载** + CDN 按实测速度重排（npmmirror 1.5s 优先，bootcdn/staticfile 8s 后置）+ 加载占位，实测进入图谱页 **1~2s 出图**
- 图谱卡在「正在加载图表库…」不再返回：`loadEcharts()` 无超时，某个 CDN 请求挂起（代理半开/丢包，既不 `onload` 也不 `onerror`）时 Promise 永久 pending，占位文案永不清除。补**单源 5s 超时**+ 换源、`onload` 后校验 `window.echarts`（防 CDN 返回 HTML 错误页），并回显进度「正在加载图表库…（n/4 源）」；实测四源全挂起时 **20.4s 自动降级为表格视图**（不再无限等待）
- 全局 `.hidden` 工具类缺失：此前仅 `.toast.hidden`/`.drawer-mask.hidden`/`.degraded-banner.hidden` 三条组合规则，`#sub-banner`（空长条）/`#sub-exit`/`#graph-table`/`#browser-vis`/`#write-raw`/`#govern-raw-out` 的 hidden 全部失效、始终可见；补 `.hidden { display: none !important; }`
- 工具栏排版：`.toolbar input` 命中 checkbox，导致「隐藏孤立点」被 flex 拉伸成竖排；改为 `:not([type="checkbox"])` 并锁定 `.chk` 宽度
- 图谱「数据视图」与 CDN 降级未区分：图表可用时不再同时铺 2200+ 行节点/边表格（`tableRequested` 标记）
- `GET /facts` 不带 key 时列出该实体**全部当前事实**（原实现把空 key 交给 `fact_lookup`，canonical key 归一为空串后恒不匹配，实体有 139 条事实也返回空）；带 key 的精确查询语义不变
- 补 `test_view_route_contract`（nav ↔ VIEWS 键 ↔ section `data-view` 三方一致）、`test_health_version`、`test_facts_without_key_lists_all` 防复发
- 查询台上下文区块：**标题写 16 区块但只渲染 15 个卡片**（`trace` 被拆到下方折叠区，数量对不上）；改为把 `trace` 作为第 16 个区块一并渲染，名副其实
- 查询台首次进入（未查询时）两个结果区全空、无任何引导，看起来像「没展示」；`refresh()` 补引导文案（分别提示点「召回」/「上下文」），已查询过的结果在切换视图后仍保留
- **查询台上下文区重做（可读性）**：原先是 16 个等权灰卡片平铺、标题只有英文键名（`query_type`/`impact_chains`…），看不出每块是干什么的，且 503 条 `causes` 与空区块混在一起。改为——顶部摘要条（查询文本 / 路由类型 / 综合分 / 命中 N/16）+ 四组分栏（直接回答 / 观点与意图 / 推理链 / 证据与质量），每组带一行用途说明；每块给**中文名 + 英文键名 + 一行释义**；列表块默认只显示前若干条、其余收进「展开 N 条」（单次展开上限 50，超出提示看原始 JSON）；本问未命中的区块收成组内灰字一行（不再占卡片）；条数顶到 `ContextBuilder` 预算上限的块标「已截断(上限N)」——此前 `current_state` 只显示 10 条会被误读为「库里只有 10 条」；枚举值全部中文化（query_type / polarity / intent status / impact dimension·direction / pattern_type / 链关系 / trace 键名），事实 key 给常用中文释义、未收录的仍显示原键名；`trace` 改为 KV 表 + 原始 JSON 折叠，并注明「中间量，未按预算裁剪，条数不必等于区块条数」
- 补 `test_ctx_blocks_contract`：前端 16 区块清单 ↔ `/context` 实际响应键一一对应，且每块必须带中文名与释义（防再次脱节）
- **查询台上下文区图表化**：8 个数值/关系型区块在图内直接出图（echarts，复用既有 CDN 加载器）——`score` 六维雷达 +（左侧图、右侧明细分栏，综合分提到标题行）、`impacts` 维度×强度条形（按效价上色）、`causes` 按结果 key 聚合的因果条数条形（503 条一眼看出主要指向）、`temporal_chains` / `impact_chains` 关系图（箭头方向、可拖拽缩放、按容器宽度算间距）、`conflicts` 同 key 竞争值个数条形、`evidence` 来源分布条；事实卡把置信度从徽章换成数值条便于横向比较；链区块的明细改一行可读路径（图已画过，不再重复 SVG）；单类别的观点/意图/模式不画环形图（一片饼没信息量），仍走明细列表
- 图表全部带降级：echarts 不可用（CDN 挂）时链区块退回内联 SVG、其余退回「图表库不可用」提示 + 明细照常可看；重渲染先 `dispose` 旧实例（防内存泄漏），窗口尺寸变化按新宽度重算坐标
- 链图两处实测踩坑：**149 个节点的链把画布撑到 1.7 万像素，超过浏览器 canvas 宽度上限后整块画不出来**（白板）——改为单行最多画 40 个节点并在图下注明；节点多时按固定步长横向滚动，不再压缩成一团

**新增（09-17）：PostgreSQL 可选存储后端（配置开关式）**：
- `PGStore`（`src/dnamemory/pg_store.py`）：与 `SQLiteStore` **同签名的 PG 实现**，读写全路径；`store.py` 零改动，默认路径逐字节不变
- 配置开关：`DNAMEMORY_DSN` / `--dsn`（CLI、HTTP、MCP 三入口），解析收口在 `dnamemory.storage.resolve_dsn`
- `docker-compose.pg.yml`：pgvector/pg17 容器（端口 5433；5432 常被占用）
- `pg_schema.py`：DDL 单一真理源（16 张表 + HNSW 余弦索引）；`ops/pg_migrate.py` 改为薄壳（原内嵌 8 张表 DDL、缺 6 张表，是漂移源）
- `ops/migrate_sqlite_to_pg.py`：源库**只读**打开、id 保真 + `setval` 重置序列、向量双列投影、逐表行数/抽样/孤儿一致性报告
- pgvector：`node_vectors` 改 `dense vector(1024)` + `dense_json TEXT` 双列；仅 1024 维投影 ANN，其它维度沿用既有 numpy 降级契约
- 幂等改用 `ON CONFLICT(idempotency_key) DO NOTHING RETURNING id` + 同事务回查（消除 PG 并发下的唯一键冲突）
- 跨进程邻接缓存失效：`data_version` 表版本号 + 短 TTL 兜底；事务用 `pg_advisory_xact_lock` 对应 `BEGIN IMMEDIATE`
- 安全守卫（双向）：`sync_memory_to_postgres` 拒绝 `DROP` 主存储库；`PGStore` 连到精简库时在 `ensure_schema` 报 E009 说明原因
- `server/mcp.py` 补 `close()`（原实现从不释放存储，PG 下是连接泄漏）
- `ops/pg_smoke.py`：端到端冒烟（重建库→迁移→HTTP 读写治理→连接归零）
- **默认不变**：不设 DSN 时构造路径与既有行为逐字节相同，全量 282 用例全绿

**新增（09-17）：项目配置文件 + 对外 API 接入**
- `dnamemory.settings`：`.env` 风格配置文件（零依赖，自解析），查找顺序 `--config` > `DNAMEMORY_CONFIG` > `./dnamemory.env` > `~/.dnamemory.env`
- 优先级 **命令行 > 真实环境变量 > 配置文件 > 内置默认**；只注入 `DNAMEMORY_` / `DEEPSEEK_` 白名单前缀，已存在的环境变量不被覆盖
- 三入口（`dnamemory` / `dnamemory-server` / `dnamemory-mcp`）均支持 `--config`；CLI 的 `--db` 默认值改为走 `DNAMEMORY_DB`（原硬编码会遮蔽配置文件）
- `dnamemory.env.example` 配置模板（服务/存储/LLM 三组）；`.env.example` 并入
- `docs/API接入指南.md`：启动、鉴权、curl/Python/JS 示例、端点速查、**对外暴露安全清单**、OpenAPI、MCP 接入、常见问题

**不变**：核心库语义零改动；CLI 13 子命令；既有 8 端点路径/方法；MCP 既有 7 工具名与行为；三套门禁（eval_state 七类/gate_check/eval_longterm）指标不劣化。

## 0.7.0（2026-09-15）— Impact Memory + Trigger/Associative Recall + Personal Memory Model

- S0 短板修复：同义 key 事实归一化（静态族表）；current_state 查询相关性排序
- P1 Impact：impacts 表/模型/LLM 第 8 类候选/add_impact/ImpactResolver/context impacts 区块/评测第 6 类
- P2：derive_impact_links + ImpactChainBuilder（memory_links 加 source_dim 消歧列）；Trigger 四类 + 规则/LLM 生成 + recall_context 融合（Trigger 找到、State 判断）；评测第 7 类
- P3：Pattern 六类 + extract_patterns 规则引擎（挂 reflect 尾部）+ PatternResolver；source 强制 inferred
