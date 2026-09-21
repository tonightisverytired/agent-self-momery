/* 视图：记忆分面浏览。事实表格 / 邻接星图+列表 / 时间线 / 历史 / 解释。 */
"use strict";

(function () {
  let lastMode = null;
  let lastData = null;

  function setResult(mode, html, data, rawId) {
    lastMode = mode;
    lastData = data;
    $("browser-result").innerHTML = html;
    const pre = $("browser-raw-out");
    pre.classList.add("hidden");
  }
  function toggleRaw() {
    const pre = $("browser-raw-out");
    if (pre.classList.contains("hidden")) {
      pre.textContent = JSON.stringify(lastData, null, 2);
      pre.classList.remove("hidden");
    } else {
      pre.classList.add("hidden");
    }
  }

  /* ---------- 事实表格 ---------- */
  function factRows(items) {
    return items.map(f => `<tr>
      <td>${esc(f.id)}</td><td>${esc(f.key)}</td>
      <td class="strong">${esc(f.value)}</td>
      <td>${badge(f.source, "b-src")}</td>
      <td>${bar(f.confidence ?? 0, "var(--ok)", "", 1)}</td>
      <td>${fmt.ts(f.valid_at)}</td>
      <td>${fmt.ts(f.invalid_at)}</td>
      <td>${f.evidence_ids?.length ? f.evidence_ids.length + " 条" : "—"}</td>
    </tr>`).join("");
  }

  /* ---------- 邻接：星形子图 + 列表 ---------- */
  let neighChart = null;
  function renderNeighChart(items, centerName) {
    loadEcharts().then(() => {
      if (!neighChart) {
        neighChart = echartsInit($("browser-vis"));
        window.__charts.neigh = neighChart;
      }
      $("browser-vis").classList.remove("hidden");
      const nodeIds = new Set();
      items.forEach(it => nodeIds.add(it.node.node_id));
      const data = [
        { id: "c", name: centerName, symbolSize: 26,
          itemStyle: { color: "#3987e5" } },
        ...[...nodeIds].map((nid, i) => {
          const it = items.find(x => x.node.node_id === nid);
          return {
            id: nid, name: it.node.name,
            symbolSize: 12 + 8 * it.weight,
            itemStyle: { color: it.node.node_type === "entity"
              ? "#3987e5" : "#d95926" },
          };
        }),
      ];
      const links = items.map(it => ({
        source: "c", target: it.node.node_id,
        lineStyle: { color: relHex(it.rel), width: 1 + 3 * it.weight,
                     curveness: 0.1 },
        label: { show: true, formatter: it.rel, color: "#8b96ad",
                 fontSize: 9 },
      }));
      neighChart.setOption({
        tooltip: {},
        series: [{
          type: "graph", layout: "force", roam: true,
          force: { repulsion: 300, edgeLength: 90, gravity: 0.05 },
          label: { show: true, color: "#dbe4f5", fontSize: 11 },
          data, links,
        }],
      }, true);
    }).catch(() => {});
  }
  function neighRows(items) {
    return items.map(it => `<div class="neigh-row">
      <span class="clickable" data-goto="#/graph?focus=${encodeURIComponent(it.node.name)}">${esc(it.node.name)}</span>
      ${badge(it.node.node_type, "b-" + it.node.node_type)}
      ${badge(it.rel, "b-rel")}
      ${badge(it.direction === "out" ? "出" : "入", "b-" + it.direction)}
      ${bar(it.weight, relHex(it.rel), "", 1)}
      <span class="muted">conf ${fmt.num(it.confidence)}</span>
    </div>`).join("");
  }

  /* ---------- 时间线 ---------- */
  function timelineRows(nodes) {
    const sorted = [...nodes].sort((a, b) =>
      (b.at || "").localeCompare(a.at || ""));
    return sorted.map(n => `<div class="tl-item">
      <div class="tl-dot"></div>
      <div class="tl-card">
        ${badge(n.dimension, "b-dim")}
        ${badge(n.relation || "—", "b-rel")}
        ${badge(fmt.ts(n.at), "b-time")}
        <span class="strong">${esc(n.name)}</span>
        <span class="muted">id ${esc(n.id)}</span>
      </div>
    </div>`).join("") || '<span class="kpi-note">（无节点）</span>';
  }

  /* ---------- 历史 ---------- */
  function historyRows(items) {
    if (!items || !items.length) return '<span class="kpi-note">（无历史）</span>';
    return items.map(v => `<div class="ver-card">
      ${badge("v" + v.version, "b-ver")}
      <span>${esc(v.content)}</span>
      <span class="muted">${fmt.ts(v.created_at)}</span>
    </div>`).join("");
  }

  /* ---------- 解释 ---------- */
  function explainHtml(data) {
    const m = data.memory || {};
    const ev = data.evidence || [];
    const vs = data.versions || [];
    const rel = data.related || [];
    const audits = data.audits || [];
    // 版本链条目两种形态（0.8.1 IA-4）：node 为 {version, content,
    // created_at}；fact 为 {key, value, valid_at}；belief/intent 为
    // {proposition, valid_at}
    const verRows = vs.map(v => {
      if (v.version !== undefined) {
        return `<tr><td>v${esc(v.version)}</td><td>${esc(v.content)}</td>
          <td>${fmt.ts(v.created_at)}</td></tr>`;
      }
      const content = v.value !== undefined
        ? `${esc(v.key)} = <span class="strong">${esc(v.value)}</span>`
        : esc(v.proposition || "—");
      return `<tr><td>#${esc(v.id)}</td><td>${content}</td>
        <td>${fmt.ts(v.valid_at)}</td></tr>`;
    }).join("");
    return `<div class="viz-grid">
      <div class="panel sub">
        <h3>记忆</h3>
        <table class="table kv">
          <tr><th>id</th><td>${esc(m.id)}</td></tr>
          <tr><th>名称</th><td class="strong">${esc(m.name)}</td></tr>
          <tr><th>来源</th><td>${esc(m.source || "—")}</td></tr>
          <tr><th>证据</th><td>${(m.evidence_ids || []).map(i =>
            badge("#" + i, "b-evid")).join(" ") || "—"}</td></tr>
        </table>
      </div>
      <div class="panel sub">
        <h3>证据（${ev.length}）</h3>
        ${ev.map(e => `<div class="chip-line">
          ${badge(e.source_type, "b-evid")}
          <span>${esc(e.source_ref || "—")}</span>
          ${e.trust_level != null ? badge("信任 " + fmt.num(e.trust_level), "b-conf") : ""}
          ${e.observed_at ? badge(fmt.ts(e.observed_at), "b-time") : ""}
        </div>`).join("") || '<span class="kpi-note">（无）</span>'}
      </div>
      <div class="panel sub">
        <h3>版本（${vs.length}）</h3>
        ${vs.length ? `<table class="table"><thead><tr><th>版本</th>
          <th>内容</th><th>时间</th></tr></thead><tbody>
          ${verRows}</tbody></table>` : '<span class="kpi-note">（无）</span>'}
      </div>
      <div class="panel sub">
        <h3>关联（${rel.length}）</h3>
        ${rel.length ? `<table class="table"><thead><tr><th>关系</th>
          <th>对象</th><th>类型</th><th>置信</th></tr></thead><tbody>
          ${rel.map(r => `<tr><td>${badge(r.relation, "b-rel")}</td>
          <td>${esc(r.other_id)}</td><td>${esc(r.source_type)}</td>
          <td>${r.confidence != null ? fmt.num(r.confidence) : "—"}</td></tr>`).join("")}
          </tbody></table>` : '<span class="kpi-note">（无）</span>'}
      </div>
      <div class="panel sub">
        <h3>审计（${audits.length}）</h3>
        ${audits.length ? `<table class="table"><thead><tr><th>操作</th>
          <th>原因</th><th>时间</th></tr></thead><tbody>
          ${audits.map(a => `<tr><td>${badge(a.op, "b-dim")}</td>
          <td>${esc(a.reason || "—")}</td><td>${fmt.ts(a.at)}</td></tr>`).join("")}
          </tbody></table>` : '<span class="kpi-note">（无）</span>'}
      </div>
    </div>`;
  }

  /* ---------- 查询动作 ---------- */
  async function browseFacts() {
    const entity = $("facts-entity").value.trim();
    const key = $("facts-key").value.trim();
    if (!entity) { toast("请输入实体名", true); return; }
    const params = new URLSearchParams({ entity });
    if (key) params.set("key", key);
    const data = await apiFetch("/facts?" + params.toString());
    const items = data.items || [];
    setResult("facts", items.length
      ? `<table class="table"><thead><tr><th>id</th><th>key</th><th>值</th>
         <th>来源</th><th>置信度</th><th>生效</th><th>失效</th><th>证据</th>
         </tr></thead><tbody>${factRows(items)}</tbody></table>`
      : '<span class="kpi-note">（无事实）</span>', data);
  }
  async function browseNeighbors() {
    const node = $("neighbors-node").value.trim();
    if (!node) { toast("请输入节点名", true); return; }
    const data = await apiFetch("/neighbors?node=" + encodeURIComponent(node));
    const items = data.items || [];
    setResult("neighbors",
      `<div class="split">
        <div class="neigh-list">${items.length
          ? neighRows(items)
          : '<span class="kpi-note">（无邻接边）</span>'}</div>
      </div>`, data);
    if (items.length) renderNeighChart(items, node);
    bindGoto();
  }
  async function browseTimeline() {
    const entity = $("timeline-entity").value.trim();
    if (!entity) { toast("请输入实体名", true); return; }
    const data = await apiFetch("/timeline", {
      method: "POST", body: JSON.stringify({ entity }),
    });
    setResult("timeline",
      `<div class="timeline">${timelineRows(data.nodes || [])}</div>`, data);
  }
  async function browseHistory() {
    const id = $("detail-id").value.trim();
    const dim = $("detail-dim").value;
    if (!id) { toast("请输入记忆 id", true); return; }
    const data = await apiFetch(`/memory/${id}/history?dimension=${dim}`);
    const items = data.items || [];
    setResult("history", items.length
      ? `<div class="ver-list">${historyRows(items)}</div>`
      : '<span class="kpi-note">（无历史）</span>', data);
  }
  async function browseExplain() {
    const id = $("detail-id").value.trim();
    const kind = $("detail-kind").value;
    if (!id) { toast("请输入记忆 id", true); return; }
    const data = await apiFetch(`/memory/${id}/explain?kind=${kind}`);
    setResult("explain", explainHtml(data), data);
  }

  function bindGoto() {
    document.querySelectorAll("#browser-result [data-goto]").forEach(el => {
      el.onclick = () => { location.hash = el.dataset.goto; };
    });
  }

  async function refresh(params) {
    if (params && params.explain) {
      $("detail-id").value = params.explain;
      if (params.kind) $("detail-kind").value = params.kind;
      return browseExplain();
    }
  }

  function bind() {
    $("facts-query").onclick = () => browseFacts().catch(() => {});
    $("neighbors-query").onclick = () => browseNeighbors().catch(() => {});
    $("timeline-query").onclick = () => browseTimeline().catch(() => {});
    $("detail-history").onclick = () => browseHistory().catch(() => {});
    $("detail-explain").onclick = () => browseExplain().catch(() => {});
    $("browser-raw").onclick = toggleRaw;
  }

  window.VIEWS = window.VIEWS || {};
  window.VIEWS.browser = { refresh, bind };
})();
