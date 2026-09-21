/* 视图：治理操作台。按钮触发 + 结果结构化渲染。 */
"use strict";

(function () {
  let lastData = null;

  const DESTRUCTIVE = new Set(["forget", "compress-stable", "reflect",
                               "restore", "step-day"]);
  const CONFIRM_TEXT = {
    "forget": "忘掉后仍可在「治理」页按编号恢复。确认忘掉？",
    "compress-stable": "压缩会把长期稳定的信息合并，确认执行？",
    "reflect": "将为指定年月生成一份总结，确认执行？",
    "restore": "将恢复该编号的记忆，确认执行？",
    "step-day": "将让所有记忆「过一天」：不重要的会淡忘。确认执行？",
  };

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
        return `<table class="table"><thead><tr><th>类别</th><th>编号</th>
          <th>事项</th><th>保留</th><th>舍弃</th></tr></thead>
          <tbody>${decisionRows(ds)}</tbody></table>`;
      }
      case "resolve-entities": {
        const n = data.merged || 0;
        return `<div class="fact-card">合并了
          <span class="hero-num">${fmt.num(n)}</span> 组重复的人</div>`;
      }
      case "derive-impact-links": {
        const ls = data.links || [];
        return ls.length
          ? `<div class="fact-card">梳理出
             <span class="hero-num">${ls.length}</span> 条影响</div>`
          : '<div class="fact-card">没有新影响可梳理（幂等）</div>';
      }
      case "extract-patterns": {
        const n = data.patterns || 0;
        return `<div class="fact-card">发现了
          <span class="hero-num">${fmt.num(n)}</span> 条习惯规律</div>`;
      }
      case "step-day": {
        const as = data.archived || [];
        return `<div class="fact-card">淡忘了
          <span class="hero-num">${as.length}</span> 条不重要的记忆</div>`;
      }
      case "reflect": {
        return `<div class="fact-card">月度总结完成
          <span class="hero-num">编号 ${fmt.num(data.summary_id)}</span></div>`;
      }
      case "compress-stable": {
        const cs = data.created || [];
        return `<div class="fact-card">压缩生成
          <span class="hero-num">${cs.length}</span> 条</div>`;
      }
      case "restore": {
        return `<div class="fact-card">${badge("ok", "b-ok")}
          已恢复</div>`;
      }
      case "forget": {
        return `<div class="fact-card">${badge("ok", "b-ok")}
          已忘掉（原因已记录）</div>`;
      }
      default:
        return "";
    }
  }

  async function runGovern(action) {
    if (DESTRUCTIVE.has(action)
        && !confirm(CONFIRM_TEXT[action] || `确认执行 ${action} ？`)) return;
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
