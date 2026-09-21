/* 视图：人物（0.8.2 前端改造——按人聚合，解决"数据零散"观感）。
 * 左栏人物列表（/entities?kind=person），右栏聚合：画像（/facts）、
 * 关系（/neighbors）、经历过的事（/graph?center&hops=1 的相邻事件）。 */
"use strict";

(function () {
  let allPeople = null;    // [{id, name, kind, fact_count}]
  let selected = null;

  function renderList(filter) {
    const q = (filter || "").trim().toLowerCase();
    const items = (allPeople || []).filter(p =>
      !q || p.name.toLowerCase().includes(q));
    $("people-items").innerHTML = items.length
      ? items.map(p => `<div class="people-item${
          selected === p.name ? " on" : ""}" data-name="${esc(p.name)}">
          <span>${esc(p.name)}</span><i>${fmt.num(p.fact_count)} 条</i>
        </div>`).join("")
      : '<div class="ask-empty-hint">没有匹配的人</div>';
    document.querySelectorAll(".people-item").forEach(el => {
      el.onclick = () => selectPerson(el.dataset.name);
    });
  }

  function factRow(f) {
    return `<tr><td class="muted">${esc(f.key)}</td>
      <td>${esc(f.value)}</td>
      <td>${badge(fmt.pct(f.confidence), "b-conf")}</td></tr>`;
  }

  async function selectPerson(name) {
    selected = name;
    renderList($("people-filter").value);
    const box = $("people-detail");
    box.innerHTML = '<div class="ask-empty-hint">加载中…</div>';
    try {
      const [facts, neigh, sub] = await Promise.all([
        apiFetch("/facts?entity=" + encodeURIComponent(name)),
        apiFetch("/neighbors?node=" + encodeURIComponent(name)),
        apiFetch("/graph?center=" + encodeURIComponent(name) + "&hops=1"),
      ]);

      const head = `<div class="people-head">
          <b>${esc(name)}</b>
          <button id="people-ask" class="ask-btn">问 TA</button>
        </div>`;

      const factItems = facts.items || [];
      const factsHtml = factItems.length
        ? `<h3 class="sub-h">画像（${factItems.length}）</h3>
           <table class="table"><tbody>${factItems.map(factRow).join("")}
           </tbody></table>`
        : '<h3 class="sub-h">画像</h3><div class="ask-empty-hint">没有画像事实</div>';

      const relItems = (neigh.items || []).filter(it =>
        it.node && it.node.kind === "person");
      const relHtml = relItems.length
        ? `<h3 class="sub-h">关系（${relItems.length}）</h3>
           <div class="ask-list">${relItems.map(it => `<div class="ask-row">
             <span class="clickable people-jump" data-name="${esc(it.node.name)}">
               ${esc(it.node.name)}</span>
             ${badge(it.rel, "b-rel")}</div>`).join("")}</div>`
        : "";

      const evs = (sub.nodes || [])
        .filter(n => n.node_type === "event")
        .sort((a, b) => String(b.ts || "").localeCompare(String(a.ts || "")));
      const evHtml = evs.length
        ? `<h3 class="sub-h">经历过的事（${evs.length}）</h3>
           <div class="ask-list">${evs.map(n => `<div class="ask-row">
             <span>${esc(n.name)}</span><i>${fmt.ts(n.ts)}</i>
           </div>`).join("")}</div>`
        : "";

      box.innerHTML = head + factsHtml + relHtml + evHtml;
      $("people-ask").onclick = () => {
        location.hash = "#/ask?q=" + encodeURIComponent(name + "的情况");
      };
      box.querySelectorAll(".people-jump").forEach(el => {
        el.onclick = () => selectPerson(el.dataset.name);
      });
    } catch (e) {
      box.innerHTML = '<div class="ask-empty-hint">加载失败</div>';
    }
  }

  window.VIEWS = window.VIEWS || {};
  window.VIEWS.people = {
    async refresh() {
      if (allPeople === null) {
        try {
          const d = await apiFetch("/entities?kind=person");
          allPeople = d.items || [];
        } catch (e) { return; }
      }
      renderList($("people-filter") && $("people-filter").value);
    },
    bind() {
      $("people-filter").addEventListener("input",
        (ev) => renderList(ev.target.value));
    },
  };
})();
