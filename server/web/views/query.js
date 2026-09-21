/* 视图：查询台。召回结果表 / 上下文 16 区块（分组卡片，每块带中文名+释义） / 原始 JSON。
 *
 * 16 区块的展示契约（与 /context 响应的 16 键一一对应，见 server/api/serializers.py
 * 的 _ctx_dict）：按「直接回答 / 观点与意图 / 推理链 / 证据与质量」四组呈现，
 * 每块给出中文名 + 英文键名 + 一行人话释义；本问未命中的区块收成组内灰字一行。
 * 改区块清单时，同步改 CTX_BLOCKS 与 tests/test_web_static.py 的契约断言。
 */
"use strict";

(function () {
  let lastData = null;
  let recallRendered = false;
  let ctxRendered = false;

  /* ---------- 词表：英文枚举 → 中文（认不出就原样显示，不猜） ---------- */
  const QUERY_TYPE_CN = {
    current_state: "当前状态问答", history: "历史回顾", timeline: "时间线",
    change: "变化", why_change: "变化原因", semantic_recall: "语义召回",
  };
  const POL_CN = { positive: "正面", negative: "负面", neutral: "中性",
                   mixed: "混合", unknown: "未知" };
  const INTENT_CN = { active: "进行中", completed: "已完成", cancelled: "已取消",
                      expired: "已过期", superseded: "被取代" };
  const DIM_CN = { stress: "压力", satisfaction: "满意度", health: "健康",
                   cost: "成本", income: "收入", mood: "情绪", finance: "财务",
                   relationship: "关系", study: "学业", work: "工作", sleep: "睡眠",
                   social: "社交", safety: "安全", time: "时间" };
  const DIR_CN = { increase: "上升 ↑", decrease: "下降 ↓", stable: "持平 →",
                   appear: "出现 +", disappear: "消失 −" };
  const PAT_CN = { behavior: "行为", impact: "影响", preference: "偏好",
                   habit: "习惯", mood: "情绪", social: "社交" };
  const SRC_CN = { conversation: "对话", chat: "聊天", profile: "资料",
                   document: "文档", inferred: "推断", system: "系统",
                   import: "导入", manual: "手工" };
  const REL_CN = { before: "先于", after: "后于", same_time: "同时",
                   causes: "导致", caused_by: "由…导致", summarizes: "汇总",
                   occurs_with: "同时发生", mentions: "提及" };
  /* 链节点的维度（fact/belief/intent…） */
  const KIND_CN = { fact: "事实", belief: "观点", intent: "意图",
                    impact: "影响", event: "事件", evidence: "证据",
                    entity: "实体", pattern: "模式", trigger: "触发" };
  /* 常用事实 key 的中文释义（用户自定义 key，认不出只显示原键名） */
  const KEY_CN = {
    location: "所在地", city: "城市", hometown: "家乡", address: "住址",
    role: "角色", mood_activity: "心情相关活动",
    status: "状态", age: "年龄", name: "姓名", gender: "性别",
    has_child: "有孩子", has_baby: "有宝宝", marital_status: "婚姻状况",
    relationship_status: "感情状态", occupation: "职业", job: "工作",
    employment_status: "就业状态", company: "公司", salary: "薪资",
    income: "收入", financial_status: "财务状况", education: "学历",
    education_level: "学历", education_path: "求学路径", education_stage: "就读阶段",
    major: "专业", school: "学校", cohort: "届别", exam_date: "考试日期",
    exam_status: "考试状态", health_status: "健康状况", mood: "心情",
    habit: "习惯", exercise_habit: "运动习惯", sleep_schedule: "作息",
    bedtime_snack_habit: "睡前零食习惯", food_preference: "饮食偏好",
    favorite_food: "爱吃", food_like: "爱吃", food_dislike: "不吃",
    favorite_tea: "爱喝的茶", clothing_preference: "穿衣偏好",
    appearance: "外貌", avatar_style: "头像风格", eye_size: "眼睛大小",
    phone_brand: "手机品牌", driving_license: "驾照", drinking_ability: "酒量",
    attendance: "出勤", work_schedule: "工作作息", budget: "预算",
    interest: "兴趣", hobby: "爱好", preference: "偏好", goal: "目标",
  };
  const SCORE_CN = [
    ["retrieval", "检索命中"], ["temporal", "时间相关"], ["validity", "当前有效"],
    ["source", "来源可信"], ["evidence", "证据支撑"], ["coherence", "跨维一致"],
    ["conflict_penalty", "冲突罚分"],
  ];
  const TRACE_CN = {
    query: "查询文本", query_type: "路由类型", retrieval_mode: "检索模式",
    candidate_count: "初筛候选", rrf_candidates: "融合候选",
    resolved_count: "状态解析命中", current_state_count: "当前事实(候选)",
    history_count: "历史版本", belief_count: "观点", chain_count: "时间链",
    conflict_count: "冲突(交叉一致性)", evidence_count: "证据",
    impact_count: "影响(在场地)", impact_chain_count: "影响链",
    trigger_count: "Trigger 命中行数", pattern_count: "模式(在场地)",
    final_context_count: "最终上下文条数", llm_used: "用到 LLM",
    fallback_used: "降级路径",
  };

  /* ---------- 区块清单：分组 + 中文名 + 释义 + 列表预览条数 ---------- */
  /* budget 抄自 ContextBuilder.DEFAULT_BUDGETS：条数顶到上限时提示「已截断」，
   * 否则用户会以为「当前状态只有 10 条」（实际候选可能上百条）。 */
  const CTX_GROUPS = [
    { id: "answer", cn: "直接回答",
      desc: "回答问题本身：现在是什么、以前是什么、发生了什么" },
    { id: "stance", cn: "观点与意图",
      desc: "态度与目标：怎么想、打算做什么" },
    { id: "reason", cn: "推理链",
      desc: "为什么会这样：因果、时间与影响如何传导" },
    { id: "quality", cn: "证据与质量",
      desc: "凭什么信：证据、冲突、管线提示与评分" },
  ];
  const CTX_BLOCKS = [
    { key: "query_type", group: "answer", cn: "问题类型", noCount: true,
      hint: "管线把这个问题判定成哪一类，决定下面取哪些区块" },
    { key: "historical_changes", group: "answer", cn: "历史变化",
      hint: "已失效或被新版本取代的旧值，回答「以前是什么」",
      budget: 20, preview: 4 },
    { key: "current_state", group: "answer", cn: "当前状态", wide: true,
      hint: "此刻成立的事实（按与问题的相关性排序），回答「现在是什么」",
      budget: 10, preview: 5 },
    { key: "recent_events", group: "answer", cn: "相关事件",
      hint: "与问题相关的记忆事件", budget: 10, preview: 5 },
    { key: "notes", group: "answer", cn: "管线提示", alert: true,
      hint: "推断记忆被移出、证据不足、未命中相关记忆等提示（有内容才亮）" },
    { key: "beliefs", group: "stance", cn: "观点",
      hint: "对人或事的看法与极性（观点演化不覆盖历史）",
      budget: 10, preview: 5 },
    { key: "intents", group: "stance", cn: "意图",
      hint: "目标与待办的状态机（进行中／已完成／取消）",
      budget: 10, preview: 5 },
    { key: "causes", group: "reason", cn: "因果关系", wide: true,
      hint: "什么导致了这条事实变化（回答「为什么变了」）", preview: 5 },
    { key: "temporal_chains", group: "reason", cn: "时间链", wide: true,
      hint: "同一维度上的先后顺序（谁在谁之前）", budget: 5, preview: 3 },
    { key: "impacts", group: "reason", cn: "影响", wide: true,
      hint: "主体在各维度上受影响的方向、强度与好坏",
      budget: 8, preview: 4 },
    { key: "impact_chains", group: "reason", cn: "影响链", wide: true,
      hint: "事件 → 影响 → 观点 → 意图 的传导链", budget: 5, preview: 3 },
    { key: "patterns", group: "reason", cn: "个人模式",
      hint: "从历史里推断出的重复规律（一律标注 inferred）",
      budget: 5, preview: 5 },
    { key: "evidence", group: "quality", cn: "证据来源",
      hint: "支撑上面结论的原始出处", budget: 20, preview: 6 },
    { key: "conflicts", group: "quality", cn: "冲突",
      hint: "同一个 key 出现多个值、尚未裁决（按组计，单值不构成冲突）",
      budget: 10, preview: 5 },
    { key: "score", group: "quality", cn: "记忆评分", wide: true,
      hint: "当前状态各维度的打分与加权综合分，用来判断该不该信" },
    { key: "trace", group: "quality", cn: "管线统计", wide: true,
      hint: "各阶段计数，排查用（中间量，未按区块预算裁剪）" },
  ];

  /* ---------- 小工具 ---------- */
  const cn = (map, v) => map[v] || v || "—";
  function blockCount(v) {
    if (Array.isArray(v)) return v.length;
    if (v == null) return 0;
    return typeof v === "object" ? Object.keys(v).length : 1;
  }
  /* 事实 key：有中文释义就「中文 key」，没有就只显示原键名 */
  function keyHtml(k) {
    const gloss = KEY_CN[k];
    return `<span class="fact-key">${gloss ? `<b>${esc(gloss)}</b>` : ""}`
      + `<code>${esc(k)}</code></span>`;
  }
  function tsBadge(f) {
    if (f.invalid_at) return badge(`至 ${fmt.ts(f.invalid_at)} 失效`, "b-time");
    if (f.valid_at) return badge(`自 ${fmt.ts(f.valid_at)}`, "b-time");
    return "";
  }
  function factLine(f) {
    // 0.8.1 IA-4：单记忆分项分（memory_score）透出时附 final 分徽标，
    // 悬停可见六分项明细
    const ms = f.memory_score;
    const msBadge = ms ? `<span class="badge b-conf" title="召回 ${esc(fmt.num(ms.retrieval))} / 时效 ${esc(fmt.num(ms.temporal))} / 有效 ${esc(fmt.num(ms.validity))} / 来源 ${esc(fmt.num(ms.source))} / 证据 ${esc(fmt.num(ms.evidence))} / 一致 ${esc(fmt.num(ms.coherence))} / 冲突罚 ${esc(fmt.num(ms.conflict_penalty))}">分 ${esc(fmt.num(ms.final))}</span>` : "";
    return `<div class="fact-card">${keyHtml(f.key)}
      <span class="fact-eq">=</span>
      <span class="fact-val">${esc(f.value)}</span>
      <span class="conf-mini" title="置信度 ${esc(f.confidence)}">
        <i style="width:${Math.round((f.confidence || 0) * 100)}%"></i></span>
      <span class="conf-num">${esc(fmt.num(f.confidence))}</span>
      ${f.source ? badge(cn(SRC_CN, f.source), "b-src") : ""}
      ${tsBadge(f)}${msBadge}</div>`;
  }

  /* ---------- 内联 SVG 链（时间链 / 影响链共用） ---------- */
  function renderChainSVG(nodes) {
    if (!nodes || nodes.length < 2) return "";
    const w = Math.max(320, nodes.length * 170);
    const cx = (i) => 60 + i * 150;
    // 节点名缺失（影响链只有维度没有名字）时退回维度中文，不留空点；
    // 维度小字只在维度发生变化时标注，避免整条链重复同一个词
    const dots = nodes.map((n, i) => {
      const kind = cn(KIND_CN, n.dimension);
      const main = String(n.name || kind).slice(0, 10);
      const sub = (i > 0 && n.dimension === nodes[i - 1].dimension)
        ? "" : kind;
      return `<circle cx="${cx(i)}" cy="45" r="9" fill="${i % 2 ? "#d95926" : "#3987e5"}"/>` +
        `<text x="${cx(i)}" y="78" text-anchor="middle" fill="#dbe4f5"
          font-size="10">${esc(main)}</text>` +
        `<text x="${cx(i)}" y="92" text-anchor="middle" fill="#8b96ad"
          font-size="9">${esc(main === sub ? "" : sub)}</text>`;
    }).join("");
    // 关系标签只在发生变化时标注，避免一长串「先于」刷屏
    const links = nodes.slice(0, -1).map((n, i) => {
      const rel = nodes[i + 1].relation;
      const same = i > 0 && rel === nodes[i].relation;
      return `<line x1="${cx(i) + 9}" y1="45" x2="${cx(i + 1) - 9}" y2="45"
        stroke="#4f8cff" stroke-width="1.5"/>` + (same ? "" :
        `<text x="${(cx(i) + cx(i + 1)) / 2}" y="32" text-anchor="middle"
        fill="#8b96ad" font-size="9">${esc(cn(REL_CN, rel))}</text>`);
    }).join("");
    return `<svg viewBox="0 0 ${w} 100" width="${w}" height="100">${links}${dots}</svg>`;
  }

  /* ---------- 图表（echarts，区块内可视化） ----------
   * 每个可图表化的区块 = 图（本段）+ 明细（下面 blockBody/itemHtml 那套，保持可展开）。
   * echarts 不可用（CDN 挂了）时退回文字/SVG 老形态，不白屏。 */
  const AXIS_COLOR = "#2a3348";
  const LABEL_C = "#8b96ad";
  const TEXT_C = "#dbe4f5";
  const ACCENT_C = "#4f8cff";
  const VALENCE_HEX = { positive: "#30a46c", negative: "#e5484d", neutral: "#8b96ad",
                        mixed: "#33a6b0", unknown: "#8b96ad" };
  let chartSpecs = [];      // 本次渲染的图表清单（build 延迟到拿到容器宽度后执行）
  let mounted = [];         // 已挂载的 echarts 实例 [{el, inst, spec}]

  /* 图表容器占位：mountCharts 在 innerHTML 落地后按 data-chart-idx 初始化。
   * minWidth：图需要的宽度超过容器时，给内层定宽 + 外层横向滚动 */
  function chartBox(h, spec) {
    const idx = chartSpecs.push(Object.assign({ h }, spec)) - 1;
    return `<div class="ctx-chart-scroll">
      <div class="ctx-chart" data-chart-idx="${idx}" style="height:${h}px"></div>
    </div>${spec.note ? `<div class="ctx-note">${esc(spec.note)}</div>` : ""}`;
  }
  function disposeCharts() {
    mounted.forEach(({ inst }) => { try { inst.dispose(); } catch (e) { /* 已销毁 */ } });
    mounted = [];
    Object.keys(window.__charts || {}).forEach(k => {
      if (k.indexOf("ctx") === 0) delete window.__charts[k];
    });
  }
  function drawChart(entry, spec) {
    const wrap = entry.el.parentElement;
    const avail = (wrap && wrap.clientWidth) || entry.el.clientWidth || 600;
    entry.el.style.width = (spec.minWidth && spec.minWidth > avail)
      ? spec.minWidth + "px" : "100%";
    const w = entry.el.clientWidth || avail;
    if (!entry.inst) entry.inst = echartsInit(entry.el);
    entry.inst.setOption(spec.build(w), true);
  }
  function mountCharts() {
    disposeCharts();
    document.querySelectorAll("#ctx-blocks [data-chart-idx]").forEach(el => {
      const spec = chartSpecs[el.dataset.chartIdx];
      if (!spec) return;
      const entry = { el, inst: null, spec };
      mounted.push(entry);
      if (window.echarts) { drawChart(entry, spec); return; }
      el.innerHTML = '<div class="chart-loading">图表加载中…</div>';
      loadEcharts().then(() => {
        el.innerHTML = "";
        drawChart(entry, spec);
        window.__charts["ctx" + el.dataset.chartIdx] = entry.inst;
      }).catch(() => {
        el.innerHTML = spec.fallback
          || '<div class="kpi-note">图表库不可用（CDN 未加载）；下方明细照常可看。</div>';
        el.style.height = "auto";
      });
    });
  }
  /* 窗口尺寸变化：图内坐标按宽度现算，需要重画而不只是 resize */
  let _ctxResizeBound = false;
  function bindCtxResize() {
    if (_ctxResizeBound) return;
    _ctxResizeBound = true;
    window.addEventListener("resize", () => {
      mounted.forEach(entry => {
        if (entry.inst) drawChart(entry, entry.spec);
      });
    });
  }

  /* 评分雷达：6 个「越高越好」的维度画图，冲突罚分单独提示 */
  function radarOption(s) {
    const dims = SCORE_CN.filter(([k]) => k !== "conflict_penalty");
    return {
      tooltip: {},
      radar: {
        indicator: dims.map(([k, label]) => ({ name: label, max: 1 })),
        radius: "68%", center: ["50%", "52%"],
        axisName: { color: LABEL_C, fontSize: 10 },
        splitLine: { lineStyle: { color: AXIS_COLOR } },
        axisLine: { lineStyle: { color: AXIS_COLOR } },
        splitArea: { show: false },
      },
      series: [{
        type: "radar", symbolSize: 4,
        data: [{ value: dims.map(([k]) => s[k] ?? 0), name: "评分",
                 lineStyle: { color: ACCENT_C, width: 2 },
                 itemStyle: { color: ACCENT_C },
                 areaStyle: { color: "rgba(79,140,255,.25)" } }],
      }],
    };
  }
  /* 影响条形：维度 × 强度，按效价上色 */
  function impactsOption(items) {
    return {
      grid: { left: 4, right: 44, top: 6, bottom: 2, containLabel: true },
      tooltip: {
        trigger: "axis", axisPointer: { type: "shadow" },
        formatter: (ps) => {
          const it = items[ps[0].dataIndex];
          return `${esc(it.description || "")}<br>${cn(DIM_CN, it.dimension)} `
            + `${cn(DIR_CN, it.direction)} · ${cn(POL_CN, it.valence)} · `
            + `强度 ${it.magnitude}`;
        },
      },
      xAxis: { type: "value", max: 1, axisLabel: { color: LABEL_C, fontSize: 10 },
               splitLine: { lineStyle: { color: "#232c40" } } },
      yAxis: {
        type: "category", inverse: true,
        data: uniqueLabels(items.map(im => cn(DIM_CN, im.dimension))),
        axisLabel: { color: TEXT_C, fontSize: 11 },
        axisLine: { lineStyle: { color: AXIS_COLOR } }, axisTick: { show: false },
      },
      series: [{
        type: "bar", barWidth: 12,
        data: items.map(im => ({ value: im.magnitude,
          itemStyle: { color: VALENCE_HEX[im.valence] || ACCENT_C,
                       borderRadius: 3 } })),
        label: { show: true, position: "right", color: LABEL_C, fontSize: 10 },
      }],
    };
  }
  /* 因果条：503 条按「被导致的事实 key」聚合取前 8，一眼看出主要因果指向 */
  function causesOption(items) {
    const counts = new Map();
    items.forEach(c => {
      const k = c.caused?.key || "?";
      counts.set(k, (counts.get(k) || 0) + 1);
    });
    const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
    return {
      grid: { left: 4, right: 40, top: 6, bottom: 2, containLabel: true },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" },
                 formatter: (ps) => `${ps[0].name}：${ps[0].value} 条因果`
                   + `<br>（本卡片共 ${items.length} 条，此处按结果 key 聚合）` },
      xAxis: { type: "value", axisLabel: { color: LABEL_C, fontSize: 10 },
               splitLine: { lineStyle: { color: "#232c40" } } },
      yAxis: { type: "category", inverse: true,
               data: top.map(([k]) => KEY_CN[k] || k),
               axisLabel: { color: TEXT_C, fontSize: 11 },
               axisLine: { lineStyle: { color: AXIS_COLOR } }, axisTick: { show: false } },
      series: [{ type: "bar", barWidth: 12,
                 data: top.map(([, n]) => ({ value: n,
                   itemStyle: { color: "#9085e9", borderRadius: 3 } })),
                 label: { show: true, position: "right", color: LABEL_C,
                          fontSize: 10 } }],
    };
  }
  /* 环形：用于观点极性 / 意图状态 / 模式类型 */
  function donutOption(pairs) {
    return {
      tooltip: { trigger: "item", formatter: "{b}：{c} 条（{d}%）" },
      series: [{
        type: "pie", radius: ["46%", "72%"], center: ["50%", "52%"],
        label: { color: TEXT_C, fontSize: 10, formatter: "{b} {c}" },
        labelLine: { lineStyle: { color: AXIS_COLOR } },
        data: pairs.map(([name, value, color]) => ({
          name, value, itemStyle: { color: color || ACCENT_C } })),
      }],
    };
  }
  /* 同一 key 的竞争值个数（冲突分布条） */
  function conflictsOption(items) {
    return {
      grid: { left: 4, right: 40, top: 6, bottom: 2, containLabel: true },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" },
                 formatter: (ps) => {
                   const c = items[ps[0].dataIndex];
                   return `${cn(KEY_CN, c.key)} ${c.key}<br>`
                     + `竞争值 ${(c.values || []).length} 个：${(c.values || []).join(" / ")}`;
                 } },
      xAxis: { type: "value", minInterval: 1,
               axisLabel: { color: LABEL_C, fontSize: 10 },
               splitLine: { lineStyle: { color: "#232c40" } } },
      yAxis: { type: "category", inverse: true,
               data: uniqueLabels(items.map(c => KEY_CN[c.key] || c.key)),
               axisLabel: { color: TEXT_C, fontSize: 11 },
               axisLine: { lineStyle: { color: AXIS_COLOR } }, axisTick: { show: false } },
      series: [{ type: "bar", barWidth: 12,
                 data: items.map(c => ({ value: (c.values || []).length,
                   itemStyle: { color: "#e5484d", borderRadius: 3 } })),
                 label: { show: true, position: "right", color: LABEL_C,
                          fontSize: 10 } }],
    };
  }
  /* 来源分布条：证据按 source_type 计数 */
  function evidenceOption(items) {
    const counts = new Map();
    items.forEach(e => {
      const t = cn(SRC_CN, e.source_type);
      counts.set(t, (counts.get(t) || 0) + 1);
    });
    const pairs = [...counts.entries()][0] ? [...counts.entries()] : [];
    return {
      grid: { left: 4, right: 40, top: 6, bottom: 2, containLabel: true },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      xAxis: { type: "value", minInterval: 1,
               axisLabel: { color: LABEL_C, fontSize: 10 },
               splitLine: { lineStyle: { color: "#232c40" } } },
      yAxis: { type: "category", inverse: true, data: pairs.map(p => p[0]),
               axisLabel: { color: TEXT_C, fontSize: 11 },
               axisLine: { lineStyle: { color: AXIS_COLOR } }, axisTick: { show: false } },
      series: [{ type: "bar", barWidth: 12,
                 data: pairs.map(p => ({ value: p[1],
                   itemStyle: { color: "#9085e9", borderRadius: 3 } })),
                 label: { show: true, position: "right", color: LABEL_C,
                          fontSize: 10 } }],
    };
  }
  /* 链关系图：每行一条链，固定步长（节点多时横向滚动，不压缩成糊）。
   * CHAIN_MAX_NODES 是画布保护：149 节点 × 112px ≈ 1.7 万像素，超过浏览器
   * canvas 宽度上限会导致整块画不出来，所以单行只画前 40 个节点。 */
  const CHAIN_STEP = 112;
  const CHAIN_MAX_NODES = 40;
  function chainRows(chains) {
    return Math.min(chains.length, 5);
  }
  function chainsOption(chains, w) {
    const rows = chainRows(chains);
    const shown = chains.slice(0, rows)
      .map(c => c.slice(0, CHAIN_MAX_NODES));
    const maxN = Math.max(...shown.map(c => c.length), 2);
    const step = Math.max(70, Math.min(CHAIN_STEP, (w - 80) / (maxN - 1)));
    const data = [], links = [];
    shown.forEach((nodes, r) => {
      const y = 34 + r * 78;
      nodes.forEach((n, i) => {
        const name = `${r}-${i}`;
        data.push({
          name, x: 40 + i * step, y,
          symbolSize: 15,
          itemStyle: { color: i % 2 ? "#d95926" : "#3987e5" },
          label: { show: true, position: "bottom", color: TEXT_C, fontSize: 10,
                   formatter: String(n.name || cn(KIND_CN, n.dimension)).slice(0, 8) },
        });
        if (i) {
          const rel = n.relation;
          const same = i > 1 && rel === nodes[i - 1].relation;
          links.push({
            source: `${r}-${i - 1}`, target: name,
            label: same ? { show: false }
              : { show: true, formatter: cn(REL_CN, rel), color: LABEL_C, fontSize: 9 },
          });
        }
      });
    });
    return {
      tooltip: { formatter: (p) => p.dataType === "edge" ? "" : p.name },
      series: [{
        type: "graph", layout: "none", roam: false, draggable: false,
        edgeSymbol: ["none", "arrow"], edgeSymbolSize: 7,
        lineStyle: { color: ACCENT_C, width: 1.5 },
        data, links,
      }],
    };
  }
  /* 链的图需要多宽：够放下最长那条链（不够就横向滚动） */
  function chainsWidth(chains) {
    const rows = chainRows(chains);
    const maxN = Math.max(...chains.slice(0, rows)
      .map(c => Math.min(c.length, CHAIN_MAX_NODES)), 2);
    return 80 + (maxN - 1) * CHAIN_STEP;
  }

  /* 各区块 → 图表（不图表化的区块返回空串，保持原样） */
  function chartOf(b, v) {
    switch (b.key) {
      case "score":
        if (!v) return "";
        return chartBox(250, { build: () => radarOption(v) });
      case "impacts":
        if (!v.length) return "";
        return chartBox(Math.max(130, v.length * 30 + 46),
                        { build: () => impactsOption(v) });
      case "causes": {
        if (!v.length) return "";
        const kinds = new Set(v.map(c => c.caused?.key)).size;
        if (kinds < 2) return "";   // 只有一种因果指向时条形图没信息量
        return chartBox(Math.max(130, Math.min(kinds, 8) * 26 + 46),
                        { build: () => causesOption(v) });
      }
      case "beliefs": {
        const counts = tally(v, x => cn(POL_CN, x.polarity));
        if (counts.length < 2) return "";
        return chartBox(170, { build: () => donutOption(
          counts.map(([n, c]) => [n, c, VALENCE_HEX[
            Object.keys(POL_CN).find(k => POL_CN[k] === n)] || ACCENT_C])) });
      }
      case "intents": {
        const counts = tally(v, x => cn(INTENT_CN, x.status));
        if (counts.length < 2) return "";
        return chartBox(170, { build: () => donutOption(
          counts.map(([n, c]) => [n, c, n === "进行中" ? "#30a46c" : "#8b96ad"])) });
      }
      case "patterns": {
        const counts = tally(v, x => cn(PAT_CN, x.pattern_type));
        if (counts.length < 2) return "";
        return chartBox(170, { build: () => donutOption(
          counts.map(([n, c], i) => [n, c,
            ["#199e70", "#3987e5", "#d95926", "#33a6b0", "#d55181", "#9085e9"][i % 6]])) });
      }
      case "conflicts":
        if (!v.length) return "";
        return chartBox(Math.max(130, v.length * 30 + 46),
                        { build: () => conflictsOption(v) });
      case "evidence":
        if (!v.length) return "";
        return chartBox(Math.max(90, new Set(v.map(e => e.source_type)).size * 30 + 46),
                        { build: () => evidenceOption(v) });
      case "temporal_chains":
      case "impact_chains": {
        if (!v.length) return "";
        const rows = Math.min(v.length, 5);
        const longest = Math.max(...v.slice(0, rows).map(c => c.length));
        return chartBox(rows * 78 + 30, {
          build: (w) => chainsOption(v, w),
          minWidth: chainsWidth(v),
          fallback: v.map(ch =>
            `<div class="chain-wrap">${renderChainSVG(ch.slice(0, CHAIN_MAX_NODES))}
             </div>`).join(""),
          note: `共 ${rows} 条链，最长 ${longest} 个节点`
            + (longest > CHAIN_MAX_NODES
              ? `；图中每行最多画前 ${CHAIN_MAX_NODES} 个，完整见「原始 JSON」` : "")
            + (longest > 9 ? "，可横向滚动查看" : ""),
        });
      }
      default:
        return "";
    }
  }
  /* 同一图上重名的类目标签加序号：健康 / 健康 (2) */
  function uniqueLabels(names) {
    const seen = new Map();
    return names.map(n => {
      const c = (seen.get(n) || 0) + 1;
      seen.set(n, c);
      return c === 1 ? n : `${n} (${c})`;
    });
  }
  function tally(items, keyOf) {
    const m = new Map();
    items.forEach(it => { const k = keyOf(it); m.set(k, (m.get(k) || 0) + 1); });
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }

  /* ---------- 各区块的体渲染 ----------
   * 列表类区块（LIST_KEYS）统一走 preview + itemHtml，此处只处理整体型区块。 */
  const LIST_KEYS = ["current_state", "historical_changes", "recent_events",
    "beliefs", "intents", "temporal_chains", "evidence", "conflicts",
    "causes", "impacts", "impact_chains", "patterns"];
  const isList = (key) => LIST_KEYS.indexOf(key) >= 0;
  /* 内联排版的列表（chip 流式排列）：展开后靠元素自身 display 恢复 */
  const INLINE_KEYS = { evidence: 1 };

  function blockBody(b, v) {
    switch (b.key) {
      case "query_type":
        return `<div class="qt-badge">${badge(cn(QUERY_TYPE_CN, v), "b-qt")}
          <code class="qt-raw">${esc(v)}</code></div>`;
      case "notes":
        return v.map(n => `<span class="ctx-alert-line">${esc(n)}</span>`).join("");
      case "score":
        return scoreHtml(v);
      case "trace":
        return traceHtml(v);
      default:
        return "";
    }
  }
  function scoreHtml(s) {
    if (!s) return "";
    return SCORE_CN.map(([k, label]) =>
      bar(s[k] ?? 0, "var(--viz1)", `${label} ${k}`, 1)).join("");
  }
  function traceHtml(t) {
    const rows = Object.keys(t).map(k => `<div>
      <span>${esc(TRACE_CN[k] || k)}</span>
      <span><code>${esc(k)}</code> <b>${esc(
        typeof t[k] === "object" ? JSON.stringify(t[k]) : String(t[k]))}</b></span>
    </div>`).join("");
    return `<div class="ctx-kv">${rows}</div>
      <p class="ctx-note">这些是管线中间量，未按区块预算裁剪，
      所以条数不必等于上方区块里的条数。</p>
      <details><summary>管线原始 JSON</summary>
      <pre>${esc(JSON.stringify(t, null, 2))}</pre></details>`;
  }

  /* ---------- 区块卡片（含「展开全部」） ---------- */
  function cardHtml(b, v) {
    const n = blockCount(v);
    const preview = b.preview || 5;
    const cap = b.maxExpand || 50;   // 单次展开上限：503 条一口气铺开没法读
    let body, moreBtn = "";
    if (isList(b.key)) {
      const items = v || [];
      const tag = INLINE_KEYS[b.key] ? "span" : "div";
      const rest = items.length - preview;
      const shown = Math.max(0, Math.min(rest, cap));
      body = items.slice(0, preview).map(it => itemHtml(b, it)).join("")
        + items.slice(preview, preview + shown).map(it =>
          `<${tag} class="ctx-more">${itemHtml(b, it)}</${tag}>`).join("");
      if (rest > 0) {
        moreBtn = `<button class="ctx-more-btn">展开 ${shown} 条 ▾</button>`;
        if (rest > shown) {
          body += `<div class="${tag} ctx-more ctx-more-end">
            本卡片共 ${items.length} 条，此处只展开前 ${preview + shown} 条；
            其余 ${rest - shown} 条见顶部「原始 JSON」。</div>`;
        }
      }
    } else {
      body = blockBody(b, v);
    }
    const unit = isList(b.key) ? "条" : "项";
    const trim = (b.budget && n >= b.budget)
      ? `<span class="ctx-trim" title="区块预算上限 ${b.budget} 条，候选更多">
         已截断(上限${b.budget})</span>`
      : "";
    return `<div class="ctx-card${b.wide ? " wide" : ""}${b.alert && n ? " alert" : ""}"
        data-block="${b.key}">
      <div class="ctx-card-head">
        <b>${esc(b.cn)}</b>
        <code>${esc(b.key)}</code>
        ${b.noCount ? "" : `<span class="ctx-n">${n} ${unit}</span>`}${trim}
        ${b.key === "score" && v
          ? `<span class="score-head">综合分 <b>${fmt.num(v.final)}</b>
             <span class="muted">（各维度加权和，越高越可信）</span></span>` : ""}
      </div>
      <div class="ctx-hint">${esc(b.hint)}</div>
      ${chartHtml(b, v, body, moreBtn)}
    </div>`;
  }
  /* 图与明细的排布：评分用左右分栏（雷达窄、明细宽），其余图上明细下 */
  function chartHtml(b, v, body, moreBtn) {
    const chart = isList(b.key) || b.key === "score" ? chartOf(b, v) : "";
    if (!chart) return `<div class="ctx-body">${body}</div>${moreBtn}`;
    if (b.key === "score") {
      return `<div class="ctx-split">${chart}
        <div class="ctx-body">${body}</div></div>`;
    }
    return `${chart}<div class="ctx-body">${body}</div>${moreBtn}`;
  }
  function itemHtml(b, it) {
    switch (b.key) {
      case "current_state":
      case "historical_changes":
        return factLine(it);
      case "recent_events":
        return `<div class="fact-card"><span class="strong">${esc(it.name)}</span>
          ${badge(fmt.ts(it.ts), "b-time")}</div>`;
      case "beliefs":
        return `<div class="fact-card"><span>${esc(it.proposition)}</span>
          ${badge(cn(POL_CN, it.polarity), "b-pol-" + it.polarity)}</div>`;
      case "intents":
        return `<div class="fact-card"><span>${esc(it.proposition)}</span>
          ${badge(cn(INTENT_CN, it.status), "b-status-" + it.status)}</div>`;
      case "evidence":
        return `<span class="chip-line">${badge(cn(SRC_CN, it.source_type), "b-evid")}
          <span>${esc(it.source_ref || "—")}</span>
          ${it.trust_level != null ? badge("信任 " + fmt.num(it.trust_level), "b-conf") : ""}</span>`;
      case "conflicts":
        return `<div class="fact-card">${badge("未裁决", "b-rel")} ${keyHtml(it.key)}
          <span class="muted">出现 ${(it.values || []).length} 个值：</span>
          ${(it.values || []).map(x => `<span class="fact-val">${esc(x)}</span>`)
            .join('<span class="muted"> / </span>')}</div>`;
      case "causes":
        return `<div class="fact-card">
          <span class="strong">${esc(it.cause?.name || it.cause?.id || "—")}</span>
          <span class="muted">导致</span> ${keyHtml(it.caused?.key)}
          <span class="fact-eq">=</span>
          <span class="fact-val">${esc(it.caused?.value)}</span>
          ${badge("置信 " + fmt.num(it.confidence), "b-conf")}</div>`;
      case "impacts":
        return `<div class="fact-card">${badge(cn(DIM_CN, it.dimension), "b-dim")}
          ${badge(cn(DIR_CN, it.direction), "b-dir-" + it.direction)}
          ${badge(cn(POL_CN, it.valence), "b-pol-" + it.valence)}
          <span>${esc(it.description || "—")}</span>
          ${badge("强度 " + fmt.num(it.magnitude), "b-conf")}</div>`;
      case "patterns":
        return `<div class="fact-card">${badge(cn(PAT_CN, it.pattern_type), "b-dim")}
          <span class="strong">${esc(it.proposition)}</span>
          ${badge("支撑 " + fmt.num(it.support), "b-conf")}
          ${it.source === "inferred" ? badge("推断", "b-evid") : ""}</div>`;
      case "temporal_chains":
      case "impact_chains": {
        // 图已在上方画过，明细只留一行可读的路径（超长链截断，完整见原始 JSON）
        const cap = 12;
        const shown = it.slice(0, cap).map((n, i) =>
          (i ? '<span class="muted"> → </span>' : "")
          + esc(String(n.name || cn(KIND_CN, n.dimension)).slice(0, 14))).join("");
        return `<div class="chain-line">${shown}` + (it.length > cap
          ? `<span class="muted"> … 等 ${it.length} 个节点</span>` : "") + "</div>";
      }
      default:
        return "";
    }
  }

  /* ---------- 16 区块总渲染 ---------- */
  function renderCtx(data) {
    lastData = data;
    ctxRendered = true;
    chartSpecs = [];          // 每次重渲染重建图表清单（旧实例在 mountCharts 里销毁）
    const hits = CTX_BLOCKS.filter(b => blockCount(data[b.key]) > 0).length;
    const q = (data.trace && data.trace.query) || $("ctx-text").value || "—";
    const summary = `<div class="ctx-summary">
      <span class="ctx-q">${esc(q)}</span>
      <span class="ctx-stat">路由 <b>${esc(cn(QUERY_TYPE_CN, data.query_type))}</b>
        <code>${esc(data.query_type || "")}</code></span>
      ${data.score ? `<span class="ctx-stat">综合分 <b>${fmt.num(data.score.final)}</b></span>` : ""}
      <span class="ctx-stat">命中 <b>${hits} / ${CTX_BLOCKS.length}</b> 个区块</span>
    </div>`;

    const groups = CTX_GROUPS.map(g => {
      const blocks = CTX_BLOCKS.filter(b => b.group === g.id);
      const filled = blocks.filter(b => blockCount(data[b.key]) > 0);
      const empty = blocks.filter(b => !blockCount(data[b.key]));
      if (!filled.length && !empty.length) return "";
      const cards = filled.map(b => cardHtml(b, data[b.key])).join("");
      const emptyLine = empty.length
        ? `<div class="ctx-empty">本组未命中：${empty.map(b =>
            `${esc(b.cn)} <code>${esc(b.key)}</code>`).join("、")}</div>`
        : "";
      return `<div class="ctx-group">
        <div class="ctx-group-head"><b>${esc(g.cn)}</b>
          <span>${esc(g.desc)}</span>
          <span class="ctx-n">${filled.length}/${blocks.length} 命中</span></div>
        <div class="ctx-cards">${cards}</div>${emptyLine}
      </div>`;
    }).join("");

    $("ctx-blocks").innerHTML = summary + (hits ? groups
      : '<p class="kpi-note">这次查询没有任何区块命中：库里没有与问题相关的记忆，'
        + '或问题太泛。换个说法或先写入记忆再试。</p>');
    // 「展开全部」切换（同一卡片内展开/收起，不重渲染）
    $("ctx-blocks").querySelectorAll(".ctx-more-btn").forEach(btn => {
      const label = btn.textContent.trim();
      btn.onclick = () => {
        const card = btn.closest(".ctx-card");
        btn.textContent = card.classList.toggle("open") ? "收起 ▴" : label;
      };
    });
    // trace 已作为第 16 个区块渲染（见 CTX_BLOCKS），此处只留「原始 JSON」按钮的出口
    $("query-result").innerHTML = "";
    mountCharts();
    bindCtxResize();
  }

  /* ---------- 召回结果表 ---------- */
  function recallRows(items) {
    const maxScore = Math.max(...items.map(h => h.score), 0.0001);
    return items.map((h, i) => `<tr data-explain="${h.node_id}">
      <td>${i + 1}</td>
      <td>${badge(TYPE_LABEL[h.node_type] || h.node_type, "b-" + h.node_type)}</td>
      <td class="strong clickable">${esc(h.name)}</td>
      <td class="score-cell">${bar(h.score, "var(--accent)", "", maxScore)}</td>
      <td>${(h.sources || []).map(s => badge(s, "b-src")).join(" ")}</td>
      <td>${fmt.ts(h.ts)}</td>
    </tr>`).join("");
  }
  function renderRecall(data) {
    const items = data.items || [];
    lastData = data;
    recallRendered = true;
    $("recall-result").innerHTML = items.length
      ? `<table class="table"><thead><tr><th>#</th><th>类型</th><th>名称</th>
         <th>得分</th><th>来源</th><th>时间</th></tr></thead>
         <tbody>${recallRows(items)}</tbody></table>`
      : '<span class="kpi-note">（无结果）</span>';
    document.querySelectorAll("#recall-result tr[data-explain]").forEach(tr => {
      tr.onclick = () => {
        location.hash = `#/browser?explain=${tr.dataset.explain}&kind=node`;
      };
    });
  }

  /* ---------- 动作 ---------- */
  async function runRecall() {
    const body = {
      text: $("recall-text").value || null,
      k: parseInt($("recall-k").value || "8", 10),
      mode: $("recall-mode").value,
    };
    renderRecall(await apiFetch("/recall", {
      method: "POST", body: JSON.stringify(body),
    }));
  }
  async function runContext() {
    const text = $("ctx-text").value.trim();
    if (!text) { toast("请输入查询文本", true); return; }
    const body = { text, include_history: true };
    const qt = $("ctx-type").value;
    if (qt) body.query_type = qt;
    renderCtx(await apiFetch("/context", {
      method: "POST", body: JSON.stringify(body),
    }));
  }

  function refresh() {
    // 首次进入（或还没跑过对应查询）时给出引导，否则页面看起来是空的
    if (!recallRendered) {
      $("recall-result").innerHTML =
        '<span class="kpi-note">输入查询文本后点「召回」，查看多路检索结果</span>';
    }
    if (!ctxRendered) {
      $("ctx-blocks").innerHTML =
        '<span class="kpi-note">输入查询文本后点「上下文」，这里会按'
        + '「直接回答 / 观点与意图 / 推理链 / 证据与质量」四组，'
        + '列出 16 个区块（每块带中文名、英文键名与释义）。</span>';
    }
  }

  function bind() {
    $("recall-run").onclick = () => runRecall().catch(() => {});
    $("ctx-run").onclick = () => runContext().catch(() => {});
    $("query-raw").onclick = () => {
      const pre = $("query-result");
      if (lastData && pre.classList.contains("raw-on")) {
        pre.classList.remove("raw-on");
        const trace = lastData.trace || {};
        pre.innerHTML = `<details><summary>trace（管线统计）</summary>
          <pre>${esc(JSON.stringify(trace, null, 2))}</pre></details>`;
      } else if (lastData) {
        pre.classList.add("raw-on");
        pre.innerHTML = `<details open><summary>原始 JSON（完整响应）</summary>
          <pre>${esc(JSON.stringify(lastData, null, 2))}</pre></details>`;
      }
    };
  }

  window.VIEWS = window.VIEWS || {};
  window.VIEWS.query = { refresh, bind };
})();
