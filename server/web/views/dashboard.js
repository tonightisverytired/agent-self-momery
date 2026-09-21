/* 视图：仪表盘。数据源 /stats（原始行数）+ /graph counts（active 口径）。 */
"use strict";

(function () {
  const STAT_LABELS = [
    ["nodes", "节点"], ["entities", "实体"], ["events", "事件"],
    ["facts", "事实"], ["beliefs", "观点"], ["intents", "意图"],
    ["impacts", "影响"], ["evidence", "证据"], ["edges", "边"],
    ["triggers", "触发器"], ["patterns", "模式"],
    ["memory_links", "记忆链接"],
  ];
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

  async function refresh() {
    let stats, graph;
    try {
      [stats, graph] = await Promise.all([fetchStats(), fetchGraph(null, true)]);
    } catch (e) { return; }
    const counts = graph.counts || {};

    // 统计卡：数值 + active 口径小字
    $("stat-cards").innerHTML = STAT_LABELS.map(([k, label]) => {
      const activeKey = ACTIVE_OF[k];
      const sub = activeKey && counts[activeKey] !== undefined
        ? `<i>active ${fmt.num(counts[activeKey])}</i>`
        : `<i>原始行数</i>`;
      return `<div class="stat-card"><b>${fmt.num(stats[k] ?? 0)}</b>
        <span>${label}</span>${sub}</div>`;
    }).join("");

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
      JSON.stringify({ stats, graph }, null, 2);

    // 行点击跳图谱聚焦
    document.querySelectorAll("#recent-events tr[data-goto]").forEach(tr => {
      tr.onclick = () => { location.hash = tr.dataset.goto; };
    });
  }

  window.VIEWS = window.VIEWS || {};
  window.VIEWS.dashboard = { refresh };
})();
