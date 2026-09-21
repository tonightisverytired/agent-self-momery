/* 视图：人物（重设计版——像"联系人卡片"，不像数据库表）。
 * 左栏：带头像的人物列表；右栏：名片头（年龄/性别/职业一眼看到）、
 * 中文分组画像、关系、按年分组的经历时间线。 */
"use strict";

(function () {
  let allPeople = null;    // 按名字合并后：[{name, fact_count, nodes}]
  let selected = null;
  let showEmpty = false;   // 是否显示"只被提过名字"的人

  /* ---------- 事实 key → 中文标签 & 分组 ---------- */
  const KEY_LABEL = {
    "Age": "年龄", "Gender": "性别", "Nationality": "国籍",
    "Ethnic Background": "族裔", "Nickname": "昵称", "Title": "头衔",
    "Physical Characteristics": "外貌特征", "Birthplace": "出生地",
    "Location": "所在地",
    "Occupation": "职业", "occupation": "职业", "profession": "职业",
    "role": "角色", "Employer": "工作单位", "School": "学校",
    "Education Background": "教育背景", "research_field": "研究领域",
    "research_topic": "研究课题", "research_interest": "研究兴趣",
    "expertise": "专长", "skill": "技能", "mentor": "导师",
    "Achievements": "成就", "Awards": "获奖", "award": "获奖",
    "Awards and Role Models": "获奖与榜样", "Role Models": "榜样",
    "Hobbies": "爱好", "hobby": "爱好", "interest": "兴趣",
    "favorite_food": "喜欢的食物", "favorite_activity": "喜欢的活动",
    "preference": "偏好", "Personality": "性格", "Values": "价值观",
    "Goals": "目标", "Health": "健康",
  };
  const GROUP_BASIC = new Set(["Age", "Gender", "Nationality",
    "Ethnic Background", "Nickname", "Title", "Physical Characteristics",
    "Birthplace", "Location", "Personality", "Health"]);
  const GROUP_WORK = new Set(["Occupation", "occupation", "profession",
    "role", "Employer", "School", "Education Background",
    "research_field", "research_topic", "research_interest", "expertise",
    "skill", "mentor", "Achievements", "Awards", "award",
    "Awards and Role Models", "Role Models"]);
  const GROUP_LIFE = new Set(["Hobbies", "hobby", "interest",
    "favorite_food", "favorite_activity", "preference", "Values",
    "Goals"]);
  const REL_KEY_RE = /^关系_(.+)$/;
  const REL_KEYS = new Set(["relationship"]);

  function label(key) { return KEY_LABEL[key] || key; }
  function groupOf(key) {
    if (GROUP_BASIC.has(key)) return "basic";
    if (GROUP_WORK.has(key)) return "work";
    if (GROUP_LIFE.has(key)) return "life";
    return "other";
  }

  /* ---------- 值规整："['阅读', '健身']" → ["阅读","健身"] ---------- */
  function parseVal(v) {
    const s = String(v == null ? "" : v).trim();
    if (s.length > 1 && s[0] === "[" && s[s.length - 1] === "]") {
      const inner = s.slice(1, -1);
      const re = /'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"/g;
      const out = []; let m;
      while ((m = re.exec(inner))) out.push(m[1] !== undefined ? m[1] : m[2]);
      if (out.length) return out;
    }
    return [s];
  }

  /* ---------- 头像：姓氏首字 + 按名字定色 ---------- */
  function hueOf(name) {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
    return h;
  }
  function avatar(name, cls) {
    const h = hueOf(name);
    return `<span class="p-avatar ${cls || ""}" style="background:hsla(${h},55%,50%,.28);
      color:hsl(${h},70%,74%);border-color:hsla(${h},55%,55%,.5)">${esc(name[0] || "?")}</span>`;
  }

  /* ---------- 左栏列表（同名合并；默认只列"有画像"的人） ---------- */
  function groupByName(items) {
    const m = new Map();
    items.forEach(p => {
      const g = m.get(p.name) ||
        { name: p.name, fact_count: 0, nodes: 0 };
      g.fact_count += p.fact_count;
      g.nodes += 1;
      m.set(p.name, g);
    });
    return [...m.values()].sort((a, b) => b.fact_count - a.fact_count ||
      a.name.localeCompare(b.name, "zh"));
  }

  function renderList(filter) {
    const q = (filter || "").trim().toLowerCase();
    let items = (allPeople || []).filter(p =>
      !q || p.name.toLowerCase().includes(q));
    const emptyCount = items.filter(p => p.fact_count === 0).length;
    if (!showEmpty) items = items.filter(p => p.fact_count > 0);
    const rows = items.map(p => `<div class="p-item${selected === p.name ? " on" : ""}"
        data-name="${esc(p.name)}">${avatar(p.name, "sm")}
        <span class="p-item-name">${esc(p.name)}</span>
        ${p.nodes > 1 ? `<span class="p-dup" title="记忆里有 ${p.nodes} 个叫这个名字的节点（不同场合提到的同名的人），画像已合并展示">×${p.nodes}</span>` : ""}
        <i>${fmt.num(p.fact_count)} 条</i></div>`);
    const toggle = emptyCount
      ? `<label class="p-toggle"><input type="checkbox" id="people-show-empty"
          ${showEmpty ? "checked" : ""}> 显示只被提过名字的人（${fmt.num(emptyCount)}）</label>`
      : "";
    $("people-items").innerHTML =
      (rows.length ? rows.join("")
        : '<div class="ask-empty-hint">没有匹配的人</div>') + toggle;
    document.querySelectorAll(".p-item").forEach(el => {
      el.onclick = () => selectPerson(el.dataset.name);
    });
    const cb = $("people-show-empty");
    if (cb) cb.onchange = () => { showEmpty = cb.checked; renderList(filter); };
  }

  /* ---------- 右栏：名片头 ---------- */
  function heroCard(name, facts, relCount, evCount) {
    const pick = {};
    facts.forEach(f => { if (!(f.key in pick)) pick[f.key] = f.value; });
    const chips = [];
    [["Age", "岁"], ["Gender", ""], ["Occupation", ""], ["occupation", ""],
     ["Employer", ""], ["Education Background", ""]].forEach(([k, suf]) => {
      if (pick[k] == null) return;
      const v = parseVal(pick[k]).join("、");
      if (v) chips.push(`<span class="p-chip">${esc(label(k))} · ${esc(v)}${suf && /^\d+$/.test(v) ? suf : ""}</span>`);
    });
    return `<div class="p-hero">
      ${avatar(name, "lg")}
      <div class="p-hero-main">
        <div class="p-hero-name">${esc(name)}
          <button id="people-ask" class="ask-btn">问 TA</button></div>
        <div class="p-hero-chips">${chips.join("")}</div>
        <div class="p-hero-stats">${fmt.num(facts.length)} 条画像 ·
          ${fmt.num(relCount)} 个相关的人 · ${fmt.num(evCount)} 件事</div>
      </div>
    </div>`;
  }

  /* ---------- 右栏：分组画像 ---------- */
  function factsSection(facts) {
    const groups = { basic: [], work: [], life: [], other: [] };
    facts.filter(f => !REL_KEY_RE.test(f.key) && !REL_KEYS.has(f.key))
      .forEach(f => groups[groupOf(f.key)].push(f));
    const names = { basic: "基本情况", work: "工作与学业",
      life: "生活与兴趣", other: "其他" };
    return ["basic", "work", "life", "other"].map(g => {
      const items = groups[g];
      if (!items.length) return "";
      return `<h3 class="sub-h">${names[g]}（${items.length}）</h3>
        <div class="p-facts">${items.map(f => {
          const vals = parseVal(f.value);
          const body = vals.length > 1
            ? vals.map(v => `<span class="p-chip">${esc(v)}</span>`).join("")
            : esc(vals[0]);
          return `<div class="p-fact"><span class="p-fact-k">${esc(label(f.key))}</span>
            <span class="p-fact-v">${body}</span></div>`;
        }).join("")}</div>`;
    }).join("");
  }

  /* ---------- 右栏：关系（关系_* 事实 + 相邻人物） ---------- */
  function relSection(facts, relPeople) {
    const relFacts = [];
    facts.forEach(f => {
      const m = REL_KEY_RE.exec(f.key);
      if (m) relFacts.push({ who: m[1], what: parseVal(f.value).join("、") });
      else if (REL_KEYS.has(f.key))
        relFacts.push({ who: parseVal(f.value).join("、"), what: "" });
    });
    const known = new Set((allPeople || []).map(p => p.name));
    const rows = relFacts.map(r => {
      const nm = known.has(r.who)
        ? `<span class="clickable people-jump" data-name="${esc(r.who)}">${esc(r.who)}</span>`
        : `<b>${esc(r.who)}</b>`;
      return `<div class="p-rel-row">${avatar(r.who, "sm")}${nm}
        <span class="p-rel-what">${esc(r.what)}</span></div>`;
    });
    const chips = relPeople.map(it => {
      const n = it.node.name;
      return `<span class="p-person-chip clickable people-jump"
        data-name="${esc(n)}">${avatar(n, "sm")}${esc(n)}</span>`;
    });
    if (!rows.length && !chips.length) return "";
    return `<h3 class="sub-h">关系（${rows.length + chips.length}）</h3>
      ${rows.length ? `<div class="p-rel">${rows.join("")}</div>` : ""}
      ${chips.length ? `<div class="p-people-chips">${chips.join("")}</div>` : ""}`;
  }

  /* ---------- 右栏：经历时间线（按年分组） ---------- */
  function eventsSection(evs) {
    if (!evs.length) return "";
    const byYear = {};
    evs.forEach(n => {
      const y = String(n.ts || "").slice(0, 4) || "未知年份";
      (byYear[y] = byYear[y] || []).push(n);
    });
    const years = Object.keys(byYear).sort().reverse();
    return `<h3 class="sub-h">经历过的事（${evs.length}）</h3>
      <div class="p-tl">${years.map(y => `
        <div class="p-tl-year">${esc(y)}</div>
        ${byYear[y].map(n => `<details class="p-tl-ev">
          <summary><span class="p-tl-date">${esc(String(n.ts || "").slice(5, 10))}</span>
            ${esc(n.name)}</summary>
          <div class="p-tl-desc">${esc(n.description || "（没有详细描述）")}</div>
        </details>`).join("")}`).join("")}</div>`;
  }

  /* ---------- 选中一个人 ---------- */
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
      const factItems = facts.items || [];
      const relPeople = (neigh.items || []).filter(it =>
        it.node && it.node.kind === "person" && it.node.name !== name);
      const evs = (sub.nodes || [])
        .filter(n => n.node_type === "event")
        .sort((a, b) => String(b.ts || "").localeCompare(String(a.ts || "")));

      const grp = (allPeople || []).find(p => p.name === name);
      const dupNote = grp && grp.nodes > 1
        ? `<div class="ask-note">记忆里有 ${grp.nodes} 个叫「${esc(name)}」的节点
           （不同场合提到的同名的人），上面的画像已合并展示。</div>`
        : "";

      box.innerHTML =
        heroCard(name, factItems, relPeople.length, evs.length) +
        dupNote +
        factsSection(factItems) +
        relSection(factItems, relPeople) +
        eventsSection(evs);

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
          const d = await apiFetch("/entities?kind=person&limit=2000");
          allPeople = groupByName(d.items || []);
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
