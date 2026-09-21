# dnamemory HTTP API 文档（0.8.3）

> 鉴权：所有端点要求 `Authorization: Bearer <token>`（启动参数 `--token` 或 `DNAMEMORY_TOKEN`）。
> 错误体统一 `{code, message}`；状态码映射见文末。
> 时间字段一律 ISO 格式（`2026-07-01T09:00:00` 或 `2026-07-01`）。
> 在线文档：服务启动后访问 `/docs`（Swagger UI，右上角 Authorize 填 token 可在线试调）或 `/redoc`；端点按 写入/查询/治理/系统 分组（0.8.3）。

## 元数据

| 路径/方法 | 说明 | 响应 |
|---|---|---|
| GET /health | 存活检查 | `{ok: true}` |
| GET /stats | 库统计 | `{nodes, entities, events, facts, beliefs, intents, impacts, evidence, edges, triggers, patterns, memory_links}` 计数 |

## 查询

| 路径/方法 | 请求 | 响应要点 |
|---|---|---|
| POST /recall | `{text?, time?, tol_days?, topic?, relation_node?, relation_type?, k=8, mode=triple, node_types?, kinds?, include_archived=false, access_labels?, rerank_top_n=20}` | `{items: [{node_id, node_type, name, ts, score, sources, path_scores}]}`（path_scores：各路原始分与名次，0.8.1 新增） |
| POST /context | `{text, query_type?, time?, include_history=false, include_beliefs=true, include_evidence=true}` | 16 区块：query_type / current_state / historical_changes / recent_events / beliefs / intents / temporal_chains / evidence / conflicts / notes / **causes / impacts / impact_chains / patterns / score** / trace；current_state/historical_changes 各项附 `memory_score` 六分项（0.8.1 新增） |
| POST /timeline | `{entity?, start?, end?}` | `{nodes: [{dimension, relation, at, id, name}]}` |
| GET /memory/{id}/history?dimension=fact\|belief\|intent\|event | — | `{items: [...]}`（按维度序列化） |
| GET /memory/{id}/explain?kind=node\|fact\|belief\|intent | — | `{memory, evidence(全字段), versions(含 fact/belief/intent 历史链), related(带 confidence), audits}`（0.8.1 起 evidence 全字段 + audits） |
| GET /audit?target_type=&target_id=&op=&limit=50 | — | `{items: [{id, op, target_type, target_id, reason, meta, at}]}`（id 倒序，limit 上限 200；0.8.1 新增） |
| GET /entities?kind=person&limit=500 | — | `{total, items: [{id, name, kind, fact_count}]}`（按事实数降序，仅 active；0.8.2 新增，人物聚合视图数据源） |
| GET /facts?entity=&key= | — | `{items: [fact_dict]}`；实体缺失返回空 items（不 404）；0.8.3 起浏览口径合并同名 active 人物节点的事实（批量注入的同名节点画像聚合展示；召回侧 fact_lookup 不受影响） |
| GET /neighbors?node=&rel= | — | `{items: [{node: {node_id, node_type, kind, name, ts, value_score, access_label, lifecycle}, rel, direction: "in"\|"out", weight, confidence, valid_at, invalid_at}]}`（按节点名精确匹配） |
| GET /graph?limit=500&node_types=entity,event&include_archived=false&access_labels=public,private&center=节点名&hops=2 | — | 图谱快照：`{nodes: [{id, node_type, kind, name, description, ts, value_score, protected, access_label, lifecycle, life, decay_rate, source, degree}], edges: [{id, from, to, rel, weight, confidence, valid_at, invalid_at, lifecycle}], counts: {nodes, nodes_active, entities, entities_active, events, events_active, edges, edges_active, lifecycle, access, rel_counts}, recent_events, center: {matched, node, hops}, truncated}`。limit 为边数截断目标（1-5000）；center 按名精确匹配 BFS（出+入）hops 层，未命中返回空图不 404；counts 统计截断前全量 |

## 写入（结构化，全部返回 `{id}`）

| 路径/方法 | 请求体要点 |
|---|---|
| POST /entities | `{name, kind=concept, description="", protected=false, access_label="public", idempotency_key?}` |
| POST /events | `{name, ts?, kind=meeting, value_score?, protected=false, access_label="public", life?, decay_rate?, idempotency_key?}` |
| POST /facts | `{entity, key, value, source=chat, confidence=0.7, valid_at?, invalid_at?, explicit_confirmation=false, idempotency_key?}` |
| POST /beliefs | `{subject, proposition, polarity=neutral, confidence=0.7, source=chat, valid_at?, invalid_at?, access_label, evidence_ids?, idempotency_key?}` |
| POST /intents | `{subject, proposition, status=active, confidence=0.7, source=chat, valid_at?, invalid_at?, access_label, evidence_ids?, idempotency_key?}` |
| POST /impacts | `{subject, dimension, direction, valence, magnitude=0.5, kind=objective, evaluator=agent, description="", cause_event?, source=chat, confidence=0.7, valid_at?, invalid_at?, access_label, evidence_ids?, idempotency_key?}` |
| POST /evidence | `{source_type, source_ref="", conversation_id?, message_id?, observed_at?, content_hash?, trust_level?, access_label, metadata?, idempotency_key?}` |
| POST /edges | `{a, b, rel, weight=0.5, confidence=0.8, valid_at?, invalid_at?, idempotency_key?}` |
| POST /write | `{text, meta?}` → `{accepted, rejected, ids}`（需 extractor，否则 400 E010 提示） |
| POST /batch | `{texts[], meta?, batch_size=20}` → `{accepted, rejected, ids}` |

枚举校验（非法值 422 E422）：polarity ∈ positive/negative/neutral；status ∈ active/completed/cancelled/expired/superseded；direction ∈ increase/decrease/stable/appear/disappear；valence ∈ positive/negative/neutral/mixed/unknown；kind ∈ objective/subjective；source_type ∈ user_statement/conversation/system_record/external_data/imported_memory/inferred；rel ∈ 内置关系集（participates/discusses/mentions/occurs_with/precedes/causes/depends_on/similar_to/part_of/prefers/updates_to/contradicts/summarizes/step_of）。

## 治理

| 路径/方法 | 请求 | 响应 |
|---|---|---|
| POST /forget | `{target, reason, force=false}` | `{ok: true}` |
| POST /resolve-conflicts | `{}` | `{decisions: [{kind, node_id, fact_key, winner, loser}]}` |
| POST /confirm | `{decision, choice_value}` | `{ok: true}` |
| POST /resolve-entities | `{merge_similar=false, min_similarity=0.85}` | `{merged: n}` |
| POST /reflect | `{year, month}` | `{summary_id}` |
| POST /compress-stable | `{}` | `{created: n}` |
| POST /extract-patterns | `{}` | `{patterns: n}`（幂等） |
| POST /derive-impact-links | `{}` | `{links: n}`（幂等） |
| POST /step-day | `{}` | `{archived: [node_id...]}` |
| POST /restore | `{node_id}` | `{ok: true}` |

## 错误码映射

| HTTP | code | 场景 |
|---|---|---|
| 401 | E401 | 缺失/错误 token |
| 400 | E001/E002/E003/E005 | 参数非法/数值域/双时态/protected 未 force（消息文本含 E010=无 extractor 提示） |
| 404 | E004/E006/E013 | deleted 不可恢复/目标不存在/无证据 |
| 409 | — | 冲突类 |
| 422 | E422 | 请求体校验失败；语义冲突（时间链/上下文构建/一致性） |
| 502 | E011 | 嵌入失败 |
| 503 | E009 | 存储繁忙 |
| 500 | — | 未预期错误 |

## 版本迁移（0.7.0 → 0.8.0 breaking）

| 旧 | 新 |
|---|---|
| `py -m app.dnamemory_server ...` | `dnamemory-server ...`（或 `py -m server.main ...`） |
| `py -m app.dnamemory_mcp ...` | `dnamemory-mcp ...`（或 `py -m server.mcp ...`） |
| `from app.dnamemory_server import create_app` | `from server.api.app import create_app` |
| 错误体 `{"detail": {"code": ...}}` | `{"code": ..., "message": ...}`（顶层平铺） |
| /context 11 键 | 16 键（只增不减，旧键形状不变） |
