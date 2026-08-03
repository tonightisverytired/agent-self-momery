# MVP 测试报告 v1.1：DeepSeek 拆解导入 200 条杂乱记忆 + 指标评测（二轮修复）

> 日期：2026-08-03｜阶段：M2.1 抽取质量与调用稳定性修复
> 语料：[data/memory_fragments_200.txt](../data/memory_fragments_200.txt)（网络公开日常文本风格取材改写，非真实个人信息）
> 工具：[tools/mvp_llm_test.py](../tools/mvp_llm_test.py)
> 模型：`deepseek-v4-pro`（官方当前模型名；`deepseek-chat` 仍兼容），凭据 `DEEPSEEK_API_KEY`，可用 `DEEPSEEK_MODEL` 覆盖

---

## 1. 四个遗留问题的结论

| # | 用户报告的问题 | 根因（对照 DeepSeek 官方文档） | 修复与实测 |
|---|--------------|------------------------------|-----------|
| 1 | DeepSeek 调用挂起（>180s） | v4 模型 **thinking 默认开启**，`max_tokens` 被推理全部消耗（实测 `reasoning_tokens=3000`、`finish_reason=length`、`content` 为空，单次 30s+ 后返回空），并非参数缺失 | 按官方文档补齐 `response_format={"type":"json_object"}`、prompt 含 "json"+示例、显式 `max_tokens`，并新增 `thinking={"type":"disabled"}`；实测单批 10s 内 `finish_reason=stop`，10 批共 170.8s |
| 2 | LLM 输出残缺候选导致整批失败 | 旧 `_parse` 整批手工解析，缺字段静默丢弃 | 新增 `MemoryCandidate`（Pydantic v2，PydanticAI 同源 schema）+ `TypeAdapter`/`model_validate` **逐条校验**：只丢弃坏候选并记录原因，好候选照常写入；样例批校验拒绝 0 条 |
| 3 | 无时间信息也被填当天时间戳 | 写入层 `c.ts or now` 把无时间事件回填为导入时刻 | 数据侧修复：语料加载时按真实环境（2026-08-03，Asia/Shanghai）换算"今天/明天/周末/晚上"等相对时间；写入层 `ts` 缺省保持 `NULL`，真实写入时刻只进 `created_at/recorded_at` |
| 4 | fact/edge 抽取质量低（79 条 dropped） | 提示词无完整示例，fact/edge 缺 key/value/端点 | 提示词加入四种类型完整 JSON 示例与硬性约束（fact 必须 key+value、edge 必须 from_+to+合法 rel）；二轮 dropped 从 79 降至 39，fact 0→17、edge 0→15 |

官方文档依据：
- DeepSeek JSON Output：https://api-docs.deepseek.com/guides/json_mode/
- Chat Completions API：https://api-docs.deepseek.com/api/create-chat-completion/
- Thinking Mode（默认开启与关闭方式）：https://api-docs.deepseek.com/guides/thinking_mode/

## 2. 二轮执行结果

### 2.1 导入

| 项 | 一轮（M2 初版） | 二轮（M2.1） |
|---|---------------|-------------|
| 片段数 | 200 | 200 |
| DeepSeek 调用 | 10 批，73.4s | 10 批，170.8s（v4-pro，无挂起） |
| 写入候选 | accepted=297，dropped=79 | accepted=328，dropped=39 |
| 节点 | 事件 199、实体 98 | 事件 197、实体 99 |
| 事实 / 关联边 | 0 / 0 | 17 / 15（另补 mentions 281 条，总边 296） |
| 事件带时间戳 | 全部（无时间也被回填） | 137/197 带时间戳，60 条无时间戳保持 `NULL` |

二轮丢弃明细：`edge 端点不存在 18`、`fact 实体不存在 21`（实体名与事实端点命名不完全一致，属抽取对齐问题，见 §4）。

### 2.2 记忆指标（140 查询：80 主题 + 60 时间，k=5）

| 路径 | Recall@5 | Precision@5 | MRR | MAP@5 | NDCG@5 |
|-----|---------|------------|-----|-------|--------|
| 双路 | 1.000 | 0.660 | 0.986 | 0.985 | 0.988 |
| 三路 | 1.000 | 0.663 | 1.000 | 1.000 | 1.000 |

主题与时间两类查询均 ≥0.974（MAP）。真值仍由导入数据自建（闭环评测），分数接近上限属预期；检索管线端到端可用性再次验证，区分度需人工标注/对抗性查询补强。

## 3. 回归确认

- `pytest` 全量：**20/20 通过**（新增 3 条 M2.1 校验回归：Pydantic 逐条校验、混合批次只丢坏候选、无时间事件不回填当天时间戳）。
- M1 门禁（双路 Recall≥0.95/MAP≥0.90、带噪 Recall≥0.65、生命周期/并发/迁移）不受影响。

## 4. 结论与下一轮改进项

**结论：M2.1 四项问题全部闭环。** DeepSeek 调用稳定（显式超时 + 重试 + 关闭 thinking）；LLM 输出经 Pydantic 逐条强校验；时间信息由数据侧按真实环境换算、无时间不再伪造；fact/edge 抽取质量显著提升（dropped 79→39，fact/edge 从 0 起步）。

下一轮改进项（M2.2/M3）：
1. 实体名归一化与端点消解：fact/edge 端点不存在（39 条中的主体）由 LLM 命名不一致导致，增加"实体模糊匹配 + 同义词归一"或让模型复用既有实体名；
2. 抽取评测集（20 条片段：JSON 合法率/字段完整率/时间准确率）纳入 CI 门禁；
3. 指标评测补充人工标注真值与对抗性查询，避免闭环分数虚高；
4. M3 语义向量路（sqlite-vec/pgvector）接入真实 embedding。

---

> 版本: v1.1 | 日期: 2026-08-03
