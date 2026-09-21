/* 视图：问答台（0.8.2 前端改造 A）。
 * 个人助手场景主界面：一句自然语言 → POST /context → 记忆回答视图。
 * 一屏回答「它记得什么、为什么给这些」：事实卡片带 memory_score 分项
 * 迷你条（分数即解释），notes/冲突显式警示，证据摘录可溯源。 */
"use strict";

(function () {
  const QT_LABEL = {
    current_state: "当前状态", history: "历史", timeline: "时间线",
    change: "变化", why_change: "为什么变", semantic_recall: "语义检索",
  };
  const SCORE_DIMS = [
    ["retrieval", "相关"], ["validity", "有效"],
    ["source", "来源"], ["evidence", "证据"],
  ];

  function scoreBars(ms) {
    if (!ms) return "";
    const bars = SCORE_DIMS.map(([k, label]) => {
      const v = ms[k];
      if (typeof v !== "number") return "";
      const pct = Math.round(Math.max(0, Math.min(1, v)) * 100);
      return `<span class="sb" title="${label} ${v}"><i>${label}</i>
        <b class="sb-track"><b class="sb-fill" style="width:${pct}%"></b></b>
      </span>`;
    }).join("");
    const fin = typeof ms.final === "number"
      ? `<span class="sb-final" title="综合分">${ms.final.toFixed(2)}</span>`
      : "";
    return `<span class="score-bars">${bars}${fin}</span>`;
  }

  function factCard(f) {
    return `<div class="ask-card">
      <div class="ask-card-head">
        <span class="ask-key">${esc(f.key)}</span>
        ${badge(f.source || "", "b-kind")}
        ${f.evidence_ids && f.evidence_ids.length
          ? badge(`证据×${f.evidence_ids.length}`, "b-ev") : ""}
      </div>
      <div class="ask-val">${esc(f.value)}</div>
      ${scoreBars(f.memory_score)}
    </div>`;
  }

  function render(data, elapsedMs) {
    const out = $("ask-result");
    const notes = (data.notes || []).map(n =>
      `<div class="ask-note">⚠ ${esc(n)}</div>`).join("");
    const meta = `<div class="ask-meta">
      ${badge(QT_LABEL[data.query_type] || data.query_type, "b-qt")}
      <span>耗时 ${Math.round(elapsedMs)} ms</span>
      ${data.score && typeof data.score.final === "number"
        ? `<span>上下文综合分 ${data.score.final.toFixed(2)}</span>` : ""}
      <span>记住的事 ${data.current_state.length} 条 · 相关事件
        ${data.recent_events.length} 件 · 依据 ${data.evidence.length} 条</span>
    </div>`;

    const facts = data.current_state.length
      ? `<h3 class="sub-h">我记得的</h3>
         <div class="ask-grid">${data.current_state.map(factCard).join("")}
         </div>`
      : "";
    const events = data.recent_events.length
      ? `<h3 class="sub-h">相关的事</h3><div class="ask-list">${data
          .recent_events.map(e => `<div class="ask-row">
            <span>${esc(e.name)}</span><i>${fmt.ts(e.ts)}</i>
          </div>`).join("")}</div>`
      : "";
    const beliefs = (data.beliefs || []).length + (data.intents || []).length
      ? `<h3 class="sub-h">看法与打算</h3><div class="ask-list">${(data.beliefs || [])
          .map(b => `<div class="ask-row"><span>${esc(b.proposition)}</span>
            ${badge(b.polarity || "", "b-kind")}</div>`).join("")}${(data.intents || [])
          .map(i => `<div class="ask-row"><span>${esc(i.proposition)}</span>
            ${badge(i.status || "", "b-life-active")}</div>`).join("")}</div>`
      : "";
    const conflicts = (data.conflicts || []).length
      ? `<h3 class="sub-h ask-warn-h">有出入的记忆（去「治理」页处理）</h3>
         <div class="ask-list">${data
          .conflicts.map(c => `<div class="ask-row ask-conflict">
            <span>${esc(c.key || "")}：${esc((c.values || []).join(" / "))}</span>
          </div>`).join("")}</div>`
      : "";
    const evid = (data.evidence || []).length
      ? `<h3 class="sub-h">依据</h3><div class="ask-list">${data.evidence
          .map(e => `<div class="ask-row">
            <span>${esc(e.source_ref || "")}</span>
            <i>${esc(e.source_type || "")} · ${fmt.ts(e.observed_at)}</i>
          </div>`).join("")}</div>`
      : "";
    const hist = (data.historical_changes || []).length
      ? `<h3 class="sub-h">以前的情况</h3><div class="ask-grid">${data
          .historical_changes.map(factCard).join("")}</div>`
      : "";
    const nothing = !facts && !events && !beliefs && !hist
      ? `<div class="ask-empty-hint">这些我都不记得。可以在下方「告诉它一件事」
         教我，或换个问法试试。</div>`
      : "";
    const raw = `<details class="panel sub ask-raw"><summary>trace / 原始 JSON</summary>
      <pre class="json-out">${esc(JSON.stringify(
        { trace: data.trace, blocks: data }, null, 2))}</pre></details>`;

    out.innerHTML = meta + notes + nothing + facts + events + beliefs
      + conflicts + evid + hist + raw;
    out.classList.remove("hidden");
  }

  async function teach() {
    const el = $("teach-input");
    const text = el.value.trim();
    if (!text) { toast("先写点什么", true); return; }
    try {
      const data = await apiFetch("/write", {
        method: "POST", body: JSON.stringify({ text }),
      });
      const n = data.accepted ?? 0;
      if (n > 0) {
        toast(`记住了 ${n} 条`);
        el.value = "";
      } else {
        toast("没听明白——换种说法试试（比如带上谁、什么时候、做了什么）",
              true);
      }
    } catch (e) { /* apiFetch 已 toast */ }
  }

  async function ask() {
    const q = $("ask-input").value.trim();
    if (!q) { toast("请输入问题", true); return; }
    const btn = $("ask-run");
    btn.disabled = true;
    btn.textContent = "回忆中…";
    $("ask-result").classList.add("hidden");
    $("ask-empty").classList.add("hidden");
    const t0 = performance.now();
    try {
      const data = await apiFetch("/context", {
        method: "POST", body: JSON.stringify({ text: q }),
      });
      render(data, performance.now() - t0);
    } catch (e) { /* apiFetch 已 toast */ } finally {
      btn.disabled = false;
      btn.textContent = "提问";
    }
  }

  window.VIEWS = window.VIEWS || {};
  window.VIEWS.ask = {
    refresh(params) {
      if (params && params.q) {
        $("ask-input").value = params.q;
        ask();
      }
    },
    bind() {
      $("ask-run").onclick = ask;
      $("ask-input").addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") ask();
      });
      $("teach-run").onclick = teach;
      $("teach-input").addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") teach();
      });
    },
  };
})();
