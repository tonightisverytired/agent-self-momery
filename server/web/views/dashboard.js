/* 视图：仪表盘 · 记忆健康（0.8.2 前端改造 B）。
 * 数据源 /stats（原始行数）+ /graph counts（active 口径）+ /audit。
 * 布局：总量卡 → 记忆构成（事实/观点/意图等状态维度）→ 近期治理动作
 * → 分布横条 → 最近事件 → 原始 JSON 核对区。 */
"use strict";

(function () {
  const STAT_LABELS = [
    ["nodes", "节点"], ["edges", "边"], ["events", "事件"],
    ["entities", "实体"],
  ];
  const DIM_LABELS = [
    ["facts", "记住的事"], ["beliefs", "看法"], ["intents", "打算"],
    ["impacts", "受到的影响"], ["evidence", "依据"], ["patterns", "习惯规律"],
    ["triggers", "联想线索"], ["memory_links", "因果链"],
  ];
  const OP_LABEL = {
    write: "写入", supersede_fact: "更新记忆", fact_adjudicate: "裁决",
    conflict_resolve: "处理出入", tombstone: "标记删除", restore: "恢复",
    forget: "遗忘", resolve_entities: "合并重复的人", compress: "压缩",
    reflect: "月度总结", step_day: "过一天", derive_impact: "梳理影响",
  };
  const ACTIVE_OF = { nodes: "nodes_active", entities: "entities_active",
                      events: "events_active", edges: "edges_active" };
  const LIFE_ORDER = ["active", "archived", "tombstoned", "deleted"];
  const LIFE_COLOR = {
    active: "var(--ok)", archived: "var(--muted)",
    tombstoned: "var(--danger)", deleted: "var(--danger)",
  };
  const ACCESS_COLOR = {
    public: "var(--ok)", private: "#c98500", sensitive: "var(--danger)",
  };

  function barGroup(elId, data, colorOf, labelOf, order) {
    const el = document.getElementById(elId);
    const entries = Object.entries(data || {});
    entries.sort((a, b) => (order ? order.indexOf(a[0]) - order.indexOf(b[0])
                                    : (b[1] - a[1])));
    const max = Math.max(1, ...entries.map(e => e[1]));
    el.innerHTML = entries.map(([k, v]) =>
      bar(v, colorOf(k), `${labelOf(k)}`, max)
    ).join("") || '<span class="kpi-note">（无数据）</span>';
  }

  function auditRow(a) {
    const op = OP_LABEL[a.op] || a.op;
    const reason = (a.reason || "").slice(0, 40);
    return `<div class="ask-row">
      <span>${badge(op, "b-rel")} ${esc(a.target_type || "")}#${esc(a.target_id ?? "")}</span>
      <i title="${esc(a.reason || "")}">${esc(reason)} · ${fmt.ts(a.at)}</i>
    </div>`;
  }

  async function refresh() {
    let stats, graph, audit;
    try {
      [stats, graph, audit] = await Promise.all([
        fetchStats(), fetchGraph(null, true),
        apiFetch("/audit?limit=8").catch(() => ({ items: [] })),
      ]);
    } catch (e) { return; }
    const counts = graph.counts || {};

    // 总量卡：数值 + active 口径小字
    $("stat-cards").innerHTML = STAT_LABELS.map(([k, label]) => {
      const activeKey = ACTIVE_OF[k];
      const sub = activeKey && counts[activeKey] !== undefined
        ? `<i>active ${fmt.num(counts[activeKey])}</i>`
        : `<i>原始行数</i>`;
      return `<div class="stat-card"><b>${fmt.num(stats[k] ?? 0)}</b>
        <span>${label}</span>${sub}</div>`;
    }).join("");

    // 记忆构成（状态维度）
    $("dim-cards").innerHTML = DIM_LABELS.map(([k, label]) =>
      `<div class="dim-card"><b>${fmt.num(stats[k] ?? 0)}</b>
       <span>${label}</span></div>`).join("");

    // 近期治理动作
    const items = (audit && audit.items) || [];
    $("audit-list").innerHTML = items.length
      ? items.map(auditRow).join("")
      : '<span class="kpi-note">（暂无治理动作）</span>';

    $("kpi-note").textContent =
      "口径说明：大数字为 /stats 原始行数（含已归档）；active 小字来自 /graph " +
      "可见口径（排除 tombstoned/deleted，archived 按参数）。分布图按原始全表统计。";

    // 分布横条（纯 CSS，不依赖 echarts）
    barGroup("type-bars",
      { 实体: counts.entities, 事件: counts.events },
      k => k === "实体" ? VIZ.entity : VIZ.event, k => k);
    barGroup("rel-bars", counts.rel_counts || {},
      k => relHex(k) || "var(--accent)", k => k);
    barGroup("life-bars", counts.lifecycle || {},
      k => LIFE_COLOR[k] || "var(--accent)",
      k => `${LIFE_LABEL[k] || k}`);
    barGroup("access-bars", counts.access || {},
      k => ACCESS_COLOR[k] || "var(--accent)",
      k => `${ACCESS_LABEL[k] || k} · ${k}`);

    // 最近事件表
    const evs = graph.recent_events || [];
    $("recent-events").innerHTML = evs.length
      ? `<table class="table">
          <thead><tr><th>名称</th><th>类型</th><th>时间</th>
          <th>重要度</th><th>状态</th></tr></thead>
          <tbody>${evs.map(n => `<tr data-goto="#/graph?focus=${encodeURIComponent(n.name)}">
            <td class="clickable">${esc(n.name)}</td>
            <td>${badge(n.kind || "", "b-kind")}</td>
            <td>${fmt.ts(n.ts)}</td>
            <td>${bar(n.value_score ?? 0, "var(--accent)", "", 1)}</td>
            <td>${badge(LIFE_LABEL[n.lifecycle] || n.lifecycle, "b-life-" + n.lifecycle)}</td>
          </tr>`).join("")}</tbody></table>`
      : '<span class="kpi-note">（无事件）</span>';

    // 原始 JSON 核对区
    $("dashboard-json").textContent =
      JSON.stringify({ stats, graph, audit }, null, 2);

    // 行点击跳图谱聚焦
    document.querySelectorAll("#recent-events tr[data-goto]").forEach(tr => {
      tr.onclick = () => { location.hash = tr.dataset.goto; };
    });
  }

  window.VIEWS = window.VIEWS || {};
  window.VIEWS.dashboard = { refresh };
})();
