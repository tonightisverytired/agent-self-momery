/* 视图：记忆图谱。echarts graph 力导向（CDN 失败降级为节点/边表格）。 */
"use strict";

(function () {
  let chart = null;
  let tableRequested = false;   // 用户手动切到「数据视图」（区别于 CDN 降级）
  let state = {
    data: null,          // /graph 响应
    subMode: false,
    hiddenRels: new Set(),
    hiddenTypes: new Set(),
    hideOrphans: false,
  };

  function showChartLoading(msg) {
    const el = $("graph-canvas");
    if (!el.querySelector("canvas")) {
      el.innerHTML = `<div class="chart-loading">${esc(msg)}</div>`;
    }
  }

  const NODE_CATS = [{ name: "entity" }, { name: "event" }];

  function visibleNodes() {
    let nodes = state.data.nodes || [];
    if (state.hiddenTypes.size) {
      nodes = nodes.filter(n => !state.hiddenTypes.has(n.node_type));
    }
    if (state.hideOrphans) nodes = nodes.filter(n => n.degree > 0);
    return nodes;
  }
  function visibleEdges() {
    const ids = new Set(visibleNodes().map(n => n.id));
    return (state.data.edges || []).filter(e =>
      !state.hiddenRels.has(e.rel) && ids.has(e.from) && ids.has(e.to));
  }
  function relChipsPresent() {
    const rels = [...new Set((state.data.edges || []).map(e => e.rel))];
    $("rel-chips").innerHTML = rels.map(rel => {
      const on = !state.hiddenRels.has(rel);
      return `<button class="chip ${on ? "on" : ""}" data-rel="${rel}"
        style="--chip:${relHex(rel)}"><i class="dot"
        style="background:${relHex(rel)}"></i>${esc(rel)}</button>`;
    }).join("") || '<span class="kpi-note">（无边）</span>';
    document.querySelectorAll("#rel-chips .chip").forEach(ch => {
      ch.onclick = () => {
        const rel = ch.dataset.rel;
        if (state.hiddenRels.has(rel)) state.hiddenRels.delete(rel);
        else state.hiddenRels.add(rel);
        ch.classList.toggle("on");
        renderChart();
      };
    });
  }

  function renderChart() {
    if (!chart) return;
    const nodes = visibleNodes();
    const edges = visibleEdges();
    const maxDeg = Math.max(1, ...nodes.map(n => n.degree));
    const showLabels = nodes.length < 40;
    chart.setOption({
      tooltip: {
        formatter: (p) => {
          if (p.dataType === "edge") {
            const e = p.data;
            const from = nodes.find(n => n.id === e.from);
            const to = nodes.find(n => n.id === e.to);
            return `${esc(from ? from.name : e.from)} → ${esc(to ? to.name : e.to)}<br>` +
              `rel: ${esc(e.rel)} · weight: ${e.weight}`;
          }
          const n = p.data;
          return `${esc(n.name)}<br>${n.node_type} · ${esc(n.kind || "")}` +
            `<br>degree: ${n.degree}`;
        },
      },
      series: [{
        type: "graph",
        layout: "force",
        roam: true,
        draggable: true,
        data: nodes.map(n => ({
          id: n.id, name: n.name,
          category: n.node_type === "entity" ? 0 : 1,
          symbolSize: 8 + 14 * Math.sqrt(n.degree / maxDeg),
          itemStyle: { color: n.node_type === "entity"
            ? "#3987e5" : "#d95926" },
          label: {
            show: showLabels,
            color: "#dbe4f5",
            fontSize: 11,
            position: "right",
            formatter: (p) => p.data.name.length > 12
              ? p.data.name.slice(0, 12) + "…" : p.data.name,
          },
          emphasis: { label: { show: true } },
          ...n,
        })),
        links: edges.map(e => ({
          source: e.from, target: e.to,
          lineStyle: {
            color: relHex(e.rel),
            width: 1 + 3 * e.weight,
            curveness: 0.12,
            opacity: 0.7,
          },
          from: e.from, to: e.to, rel: e.rel, weight: e.weight,
        })),
        categories: NODE_CATS,
        force: { repulsion: 260, edgeLength: [60, 140], gravity: 0.08 },
        emphasis: { focus: "adjacency", lineStyle: { width: 3 } },
      }],
    }, true);
  }

  function initChart() {
    $("graph-canvas").innerHTML = "";   // 清掉加载占位
    chart = echartsInit($("graph-canvas"));
    window.__charts.graph = chart;
    chart.on("click", (p) => {
      if (p.dataType === "node") openNodeDrawer(p.data);
    });
    if (!tableRequested) $("graph-table").classList.add("hidden");
    renderChart();
  }

  function nodeCard(n) {
    const edges = state.data.edges || [];
    const neigh = edges
      .filter(e => e.from === n.id || e.to === n.id)
      .map(e => {
        const otherId = e.from === n.id ? e.to : e.from;
        const other = (state.data.nodes || []).find(x => x.id === otherId);
        const dir = e.from === n.id ? "out" : "in";
        return { other, e, dir };
      });
    return `<div class="drawer-sec">
        <div class="badge-row">
          ${badge(TYPE_LABEL[n.node_type] || n.node_type, "b-" + n.node_type)}
          ${badge(n.kind || "", "b-kind")}
          ${badge(ACCESS_LABEL[n.access_label] || n.access_label, "b-access-" + n.access_label)}
          ${badge(LIFE_LABEL[n.lifecycle] || n.lifecycle, "b-life-" + n.lifecycle)}
          ${n.protected ? badge("protected", "b-protected") : ""}
        </div>
        ${n.description ? `<p class="muted">${esc(n.description)}</p>` : ""}
        <table class="table kv">
          <tr><th>id</th><td>${esc(n.id)}</td></tr>
          <tr><th>时间</th><td>${fmt.ts(n.ts)}</td></tr>
          <tr><th>重要度</th><td>${fmt.num(n.value_score)}</td></tr>
          <tr><th>度数</th><td>${fmt.num(n.degree)}</td></tr>
          <tr><th>life</th><td>${fmt.num(n.life)}</td></tr>
          <tr><th>decay</th><td>${fmt.num(n.decay_rate)}</td></tr>
          ${n.source ? `<tr><th>来源</th><td>${esc(n.source)}</td></tr>` : ""}
        </table>
      </div>
      <div class="drawer-sec">
        <b>相邻节点（${neigh.length}）</b>
        ${neigh.length ? neigh.map(({ other, e, dir }) => `
          <div class="neigh-row">
            <span class="clickable" data-goto="#/graph?focus=${encodeURIComponent(other.name)}">${esc(other.name)}</span>
            ${badge(e.rel, "b-rel")}
            ${badge(dir === "out" ? "出" : "入", "b-" + dir)}
            ${bar(e.weight, relHex(e.rel), "", 1)}
          </div>`).join("") : '<p class="muted">（孤立节点）</p>'}
      </div>
      <div class="drawer-sec drawer-actions">
        <button id="d-expl">解释</button>
        <button id="d-focus">聚焦二跳</button>
      </div>`;
  }

  function openNodeDrawer(n) {
    openDrawer(n.name, nodeCard(n));
    $("d-expl").onclick = () => {
      closeDrawer();
      location.hash = `#/browser?explain=${n.id}&kind=node`;
    };
    $("d-focus").onclick = () => {
      closeDrawer();
      $("sub-center").value = n.name;
      $("sub-hops").value = "2";
      enterSubgraph();
    };
    document.querySelectorAll("#drawer [data-goto]").forEach(el => {
      el.onclick = () => {
        closeDrawer();
        location.hash = el.dataset.goto;
      };
    });
  }

  function renderTableFallback() {
    const nodes = visibleNodes();
    const edges = visibleEdges();
    const nodeRows = nodes.map(n => `<tr>
      <td class="clickable" data-focus="${esc(n.name)}">${esc(n.name)}</td>
      <td>${badge(n.node_type, "b-" + n.node_type)}</td>
      <td>${esc(n.kind || "")}</td>
      <td>${fmt.num(n.degree)}</td>
      <td>${badge(LIFE_LABEL[n.lifecycle] || n.lifecycle, "b-life-" + n.lifecycle)}</td>
    </tr>`).join("");
    const edgeRows = edges.map(e => {
      const a = nodes.find(n => n.id === e.from);
      const b = nodes.find(n => n.id === e.to);
      return `<tr>
        <td>${esc(a ? a.name : e.from)}</td>
        <td>${esc(b ? b.name : e.to)}</td>
        <td>${badge(e.rel, "b-rel")}</td>
        <td>${fmt.num(e.weight)}</td>
      </tr>`;
    }).join("");
    return `<div class="panel sub"><h3>节点（${nodes.length}）</h3>
      <table class="table"><thead><tr><th>名称</th><th>类型</th><th>kind</th>
      <th>度数</th><th>状态</th></tr></thead><tbody>${nodeRows}</tbody></table></div>
      <div class="panel sub"><h3>边（${edges.length}）</h3>
      <table class="table"><thead><tr><th>起点</th><th>终点</th><th>关系</th>
      <th>权重</th></tr></thead><tbody>${edgeRows}</tbody></table></div>`;
  }

  async function enterSubgraph() {
    const center = $("sub-center").value.trim();
    if (!center) { toast("请输入中心节点名", true); return; }
    const hops = parseInt($("sub-hops").value || "2", 10);
    state.data = await fetchGraph({ center, hops, limit: 500 });
    if (!state.data.center.matched) {
      toast(`未找到节点「${center}」`, true);
      return;
    }
    state.subMode = true;
    $("sub-banner").textContent =
      `子图：以「${center}」为中心 ${hops} 跳 · ${state.data.nodes.length} 节点` +
      (state.data.truncated ? " · 已截断" : "");
    $("sub-banner").classList.remove("hidden");
    $("sub-exit").classList.remove("hidden");
    afterDataLoaded();
  }
  async function exitSubgraph() {
    state.subMode = false;
    $("sub-banner").classList.add("hidden");
    $("sub-exit").classList.add("hidden");
    await loadFull();
  }

  function afterDataLoaded() {
    relChipsPresent();
    if (chart) {
      if (!tableRequested) $("graph-table").classList.add("hidden");
      renderChart();
      return;
    }
    if (tableRequested) { renderGraphTables(); return; }
    // echarts 尚在加载：给占位提示（否则首屏是纯空白画布）
    showChartLoading("正在加载图表库…");
  }

  function renderGraphTables() {
    $("graph-table").innerHTML = renderTableFallback();
    $("graph-table").classList.remove("hidden");
    document.querySelectorAll("#graph-table [data-focus]").forEach(el => {
      el.onclick = () => { location.hash = "#/graph?focus=" + encodeURIComponent(el.dataset.focus); };
    });
  }

  async function loadFull() {
    state.data = await fetchGraph(null, true);
    $("sub-banner").classList.add("hidden");
    afterDataLoaded();
  }

  async function refresh(params) {
    try {
      if (!state.data) await loadFull();   // 已有数据则复用（重载用「重新加载」按钮）
    } catch (e) {
      // 网络类错误 apiFetch 已 toast；此处只暴露渲染期异常，不再静默吞掉
      if (e && e.message !== "401" && e.message !== "404") {
        toast(`图谱加载失败：${e.message}`, true);
      }
      return;
    }
    if (params && params.focus) {
      const n = (state.data.nodes || []).find(x => x.name === params.focus);
      if (n) {
        if (chart) {
          const idx = visibleNodes().indexOf(n);
          if (idx >= 0) {
            chart.dispatchAction({ type: "focusNodeAdjacency",
                                   dataIndex: idx });
          }
        }
        openNodeDrawer(n);
      } else {
        toast(`未找到节点「${params.focus}」`, true);
      }
      return;
    }
    if (!chart && !tableRequested) showChartLoading("正在加载图表库…");
    try {
      await loadEcharts((n, total) =>
        showChartLoading(`正在加载图表库…（${n}/${total} 源）`));
    } catch (e) {
      $("graph-canvas").innerHTML =
        '<div class="chart-loading">图表库加载失败（CDN 不可达），已降级为表格视图</div>';
      renderGraphTables();
      return;
    }
    if (!chart) initChart();
    else renderChart();
  }

  function bind() {
    $("graph-reload").onclick = () => {
      state.hiddenRels.clear(); state.hiddenTypes.clear();
      loadFull().catch(() => {});
    };
    $("graph-relayout").onclick = () => {
      if (!chart) return;
      renderChart();
    };
    document.querySelectorAll("#type-chips .chip").forEach(ch => {
      ch.onclick = () => {
        const t = ch.dataset.type;
        if (state.hiddenTypes.has(t)) state.hiddenTypes.delete(t);
        else state.hiddenTypes.add(t);
        ch.classList.toggle("on");
        renderChart();
      };
    });
    $("hide-orphans").onchange = (ev) => {
      state.hideOrphans = ev.target.checked;
      renderChart();
    };
    $("graph-search-btn").onclick = () => {
      const q = $("graph-search").value.trim();
      if (!q) { toast("请输入节点名", true); return; }
      const n = (state.data.nodes || []).find(x => x.name === q);
      if (!n) { toast(`未找到「${q}」`, true); return; }
      if (chart) {
        chart.dispatchAction({ type: "focusNodeAdjacency",
                               dataIndex: (state.data.nodes || []).indexOf(n) });
        openNodeDrawer(n);
      } else {
        toast("图表未加载（CDN 降级），请用表格视图", true);
      }
    };
    $("graph-data-view").onclick = () => {
      const el = $("graph-table");
      const show = el.classList.contains("hidden");
      if (show) {
        tableRequested = true;
        renderGraphTables();
        el.classList.remove("hidden");
      } else {
        tableRequested = false;
        el.classList.add("hidden");
      }
      $("graph-data-view").textContent = show ? "返回图谱" : "数据视图";
    };
    $("sub-go").onclick = () => enterSubgraph().catch(() => {});
    $("sub-exit").onclick = () => exitSubgraph().catch(() => {});
  }

  window.VIEWS = window.VIEWS || {};
  window.VIEWS.graph = { refresh, bind };
})();
