/* 视图：治理操作台。按钮触发 + 结果结构化渲染。 */
"use strict";

(function () {
  let lastData = null;

  const DESTRUCTIVE = new Set(["forget", "compress-stable", "reflect",
                               "restore", "step-day"]);

  function showResult(html, data) {
    lastData = data;
    $("govern-result").innerHTML = html;
  }
  function rawGovernToggle() {
    const pre = $("govern-raw-out");
    if (pre.classList.contains("hidden")) {
      pre.textContent = JSON.stringify(lastData, null, 2);
      pre.classList.remove("hidden");
    } else {
      pre.classList.add("hidden");
    }
  }

  function decisionRows(decisions) {
    return decisions.map(d => `<tr>
      <td>${badge(d.kind, "b-rel")}</td>
      <td>${esc(d.node_id ?? "—")}</td>
      <td>${esc(d.fact_key ?? "—")}</td>
      <td class="strong">${esc(d.winner ?? "—")}</td>
      <td class="muted">${esc(d.loser ?? "—")}</td>
    </tr>`).join("");
  }
  function renderByAction(action, data) {
    switch (action) {
      case "resolve-conflicts": {
        const ds = data.decisions || [];
        return `<table class="table"><thead><tr><th>类型</th><th>节点</th>
          <th>key</th><th>胜者</th><th>败者</th></tr></thead>
          <tbody>${decisionRows(ds)}</tbody></table>`;
      }
      case "resolve-entities": {
        const n = data.merged || 0;
        return `<div class="fact-card">实体消解：合并
          <span class="hero-num">${fmt.num(n)}</span> 组</div>`;
      }
      case "derive-impact-links": {
        const ls = data.links || [];
        return ls.length
          ? `<div class="fact-card">新增影响链
             <span class="hero-num">${ls.length}</span> 条</div>`
          : '<div class="fact-card">幂等：<span class="hero-num">0</span> 条新增</div>';
      }
      case "extract-patterns": {
        const n = data.patterns || 0;
        return `<div class="fact-card">模式提取：本次生成
          <span class="hero-num">${fmt.num(n)}</span> 条新模式</div>`;
      }
      case "step-day": {
        const as = data.archived || [];
        return `<div class="fact-card">归档
          <span class="hero-num">${as.length}</span> 条记忆</div>`;
      }
      case "reflect": {
        return `<div class="fact-card">月度反射完成
          <span class="hero-num">summary_id = ${fmt.num(data.summary_id)}</span></div>`;
      }
      case "compress-stable": {
        const cs = data.created || [];
        return `<div class="fact-card">压缩生成
          <span class="hero-num">${cs.length}</span> 条</div>`;
      }
      case "restore": {
        return `<div class="fact-card">${badge("ok", "b-ok")}
          节点已恢复</div>`;
      }
      case "forget": {
        return `<div class="fact-card">${badge("ok", "b-ok")}
          遗忘完成（reason 记录在库）</div>`;
      }
      default:
        return "";
    }
  }

  async function runGovern(action) {
    if (DESTRUCTIVE.has(action)
        && !confirm(`确认执行治理操作 ${action} ？`)) return;
    try {
      let body = {};
      let url = "/" + action;
      if (action === "reflect") {
        url = "/reflect";
        body = { year: parseInt($("reflect-year").value || "2026", 10),
                 month: parseInt($("reflect-month").value || "1", 10) };
      } else if (action === "restore") {
        url = "/restore";
        body = { node_id: parseInt($("restore-id").value || "0", 10) };
      } else if (action === "forget") {
        url = "/forget";
        body = { target: $("forget-target").value.trim(),
                 reason: $("forget-reason").value.trim() || "manual",
                 force: false };
        if (!body.target) { toast("请输入 forget 目标", true); return; }
      }
      const data = await apiFetch(url, {
        method: "POST", body: JSON.stringify(body),
      });
      showResult(renderByAction(action, data), data);
      const dash = window.VIEWS.dashboard;
      if (dash && dash.refresh) dash.refresh({}).catch(() => {});
    } catch (e) { /* toast 已提示 */ }
  }

  function refresh() {}

  function bind() {
    document.querySelectorAll("[data-gov]").forEach(btn => {
      btn.onclick = () => runGovern(btn.dataset.gov).catch(() => {});
    });
    $("govern-raw").onclick = rawGovernToggle;
  }

  window.VIEWS = window.VIEWS || {};
  window.VIEWS.govern = { refresh, bind };
})();
