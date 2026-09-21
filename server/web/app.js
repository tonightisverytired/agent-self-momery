/* dnamemory 控制台（零构建原生 JS · 0.8.0 多视图改造）
 * app.js = 外壳：token / apiFetch / toast / hash 路由 / echarts CDN
 * 加载器 / 公共格式化与渲染 helpers。视图实现在 views/*.js。 */
"use strict";

window.VIEWS = window.VIEWS || {};

const $ = (id) => document.getElementById(id);

/* ---------------- 外壳：token / apiFetch / toast ---------------- */
function getToken() {
  return localStorage.getItem("dnamemory_token") || "";
}
function setToken(v) {
  localStorage.setItem("dnamemory_token", v);
}
function toast(msg, isError) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  setTimeout(() => el.classList.add("hidden"), 5000);
}
async function apiFetch(path, opts) {
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {});
  if (getToken()) headers["Authorization"] = "Bearer " + getToken();
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  if (resp.status === 401) {
    toast("401 未授权：请先配置正确的 token", true);
    setConn(false);
    throw new Error("401");
  }
  if (resp.status === 404) {
    toast("404 目标不存在", true);
    throw new Error("404");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    toast(`[${resp.status}] ${data.code || ""} ${data.message || resp.statusText}`,
          true);
    throw new Error(data.code || String(resp.status));
  }
  return data;
}
function setConn(ok) {
  const el = $("conn-status");
  el.className = "status-dot " + (ok ? "on" : "off");
  el.textContent = ok ? "已连接" : "未连接";
}

/* ---------------- 公共数据请求 ---------------- */
let _graphCache = null;
async function fetchStats() {
  return apiFetch("/stats");
}
async function fetchGraph(params, force) {
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  if (!force && !params && _graphCache) return _graphCache;
  const data = await apiFetch("/graph" + qs);
  if (!params) _graphCache = data;
  return data;
}

/* ---------------- 格式化 / 安全渲染 ---------------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = {
  ts: (v) => v ? String(v).replace("T", " ").slice(0, 16) : "—",
  num: (v) => (typeof v === "number" ? v.toLocaleString() : "—"),
  pct: (v) => (typeof v === "number" ? Math.round(v * 100) + "%" : "—"),
};
const badge = (text, cls) => `<span class="badge ${cls || ""}">${esc(text)}</span>`;
const bar = (frac, color, label, maxFrac) => {
  const pct = Math.max(0, Math.min(100,
    Math.round(frac / (maxFrac || 1) * 100)));
  return `<div class="bar-row">
    ${label ? `<span class="bar-label">${esc(label)}</span>` : ""}
    <div class="bar-track"><div class="bar-fill" style="width:${pct}%;
      background:${color}"></div></div>
    <span class="bar-val">${esc(fmt.num(frac))}</span>
  </div>`;
};
function renderJSON(elm, data) {
  $(elm).textContent = JSON.stringify(data, null, 2);
}
function rawToggle(preId, outId, data) {
  const pre = $(preId), out = $(outId);
  if (pre.classList.contains("hidden")) {
    pre.textContent = JSON.stringify(data, null, 2);
    pre.classList.remove("hidden");
  } else {
    pre.classList.add("hidden");
  }
  return out;
}

/* ---------------- 颜色系统（dark categorical · 与 CSS 变量同步） ---------------- */
const VIZ = {
  entity: "var(--viz1)", event: "var(--viz3)",
};
const REL_COLORS = {
  participates: "var(--viz1)", discusses: "var(--viz2)",
  mentions: "var(--viz3)", occurs_with: "var(--viz4)",
  precedes: "var(--viz5)", causes: "var(--viz6)",
  depends_on: "var(--viz7)", similar_to: "var(--viz8)",
  part_of: "var(--viz1)", prefers: "var(--viz2)",
  updates_to: "var(--viz3)", contradicts: "var(--viz4)",
  summarizes: "var(--viz5)", step_of: "var(--viz6)",
};
const VIZ_HEX = {
  viz1: "#3987e5", viz2: "#199e70", viz3: "#d95926", viz4: "#33a6b0",
  viz5: "#d55181", viz6: "#9085e9", viz7: "#008300", viz8: "#e66767",
};
const relHex = (rel) => VIZ_HEX[REL_COLORS[rel].replace("var(--", "").replace(")", "")];
const TYPE_LABEL = {
  entity: "实体", event: "事件", fact: "事实", belief: "观点",
  intent: "意图", impact: "影响", evidence: "证据",
};
const LIFE_LABEL = { active: "活跃", archived: "已归档", tombstoned: "已墓碑",
                     deleted: "已删除" };
const ACCESS_LABEL = { public: "公开", private: "私有", sensitive: "敏感" };

/* ---------------- echarts CDN 多源加载器 ---------------- */
/* 实测（2026-09-17）：npmmirror ~1.5s，jsdelivr ~5s，bootcdn/staticfile ~8s；
 * 按实测速度排序，慢源后置（仍保留四源降级）。 */
const ECHARTS_CDNS = [
  "https://registry.npmmirror.com/echarts/5.4.3/files/dist/echarts.min.js",
  "https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js",
  "https://cdn.bootcdn.net/ajax/libs/echarts/5.4.3/echarts.min.js",
  "https://cdn.staticfile.net/echarts/5.4.3/echarts.min.js",
];
/* 单源超时：正常 1.5s 内到货；挂起（代理半开/丢包，不触发 onload/onerror）
 * 时也必须换源，否则 Promise 永久 pending，图谱页会一直停在加载占位。 */
const CDN_TIMEOUT_MS = 5000;
let _echartsPromise = null;
let _echartsProgress = null;   // 最新进度回调（预加载已启动时也能回显）
let _echartsAttempt = 0;
function loadEcharts(onProgress) {
  if (onProgress) {
    _echartsProgress = onProgress;
    // 预加载已在跑：立即回显当前进度，不让回调石沉大海
    if (_echartsPromise && _echartsAttempt) {
      onProgress(_echartsAttempt, ECHARTS_CDNS.length);
    }
  }
  if (window.echarts) return Promise.resolve(window.echarts);
  if (_echartsPromise) return _echartsPromise;
  _echartsPromise = new Promise((resolve, reject) => {
    const tryNext = (i) => {
      _echartsAttempt = i + 1;
      if (_echartsProgress) _echartsProgress(i + 1, ECHARTS_CDNS.length);
      if (i >= ECHARTS_CDNS.length) {
        onChartsDegraded();
        reject(new Error("CDN 全部不可达"));
        return;
      }
      const s = document.createElement("script");
      let done = false;
      const finish = (ok) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (ok && window.echarts) { resolve(window.echarts); return; }
        s.onload = s.onerror = null;
        s.remove();
        tryNext(i + 1);
      };
      const timer = setTimeout(() => finish(false), CDN_TIMEOUT_MS);
      s.onload = () => finish(true);
      s.onerror = () => finish(false);
      s.src = ECHARTS_CDNS[i];
      document.head.appendChild(s);
    };
    tryNext(0);
  });
  return _echartsPromise;
}
function onChartsDegraded() {
  const el = $("charts-degraded");
  if (el) el.classList.remove("hidden");
}
function echartsInit(elm) {
  return window.echarts.init(elm, null, { renderer: "canvas" });
}
function onResizeCharts() {
  window.echarts && window.echarts.getInstanceByDom
    && Object.values(window.__charts || {}).forEach(c => c.resize());
}

/* ---------------- hash 路由 ---------------- */
function parseHash() {
  const h = location.hash || "#/dashboard";
  const [path, qs] = h.slice(1).split("?");
  const name = (path.replace(/^\/+/, "") || "dashboard");
  const params = {};
  if (qs) new URLSearchParams(qs).forEach((v, k) => { params[k] = v; });
  return { name, params };
}
async function route() {
  const { name, params } = parseHash();
  const viewName = window.VIEWS[name] ? name : "dashboard";
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(a =>
    a.classList.toggle("active", a.getAttribute("href") === "#/" + viewName));
  // 按 data-view 定位（section id 带 -console/-form 后缀，不可用视图名直取 id）
  const sec = document.querySelector('.view[data-view="' + viewName + '"]');
  if (sec) sec.classList.add("active");
  if (window.VIEWS[viewName] && window.VIEWS[viewName].refresh) {
    try {
      await window.VIEWS[viewName].refresh(params);
    } catch (e) {
      if (e.message !== "401" && e.message !== "404") {
        toast(`视图渲染失败：${e.message}`, true);
      }
    }
  }
}

/* ---------------- 装配 ---------------- */
async function probe() {
  try {
    const health = await apiFetch("/health");
    setConn(true);
    if (health && health.version) {
      $("version-badge").textContent = "v" + health.version;
    }
    const dash = window.VIEWS.dashboard;
    if (dash && dash.refresh) dash.refresh({}).catch(() => {});
  } catch (e) { /* 401 已提示 */ }
}
function init() {
  $("token-input").value = getToken();
  $("token-save").onclick = () => {
    setToken($("token-input").value.trim());
    probe();
  };
  $("stats-refresh").onclick = () => {
    const dash = window.VIEWS.dashboard;
    if (dash && dash.refresh) dash.refresh({}).catch(() => {});
  };
  $("drawer-close").onclick = closeDrawer;
  $("drawer-mask").onclick = closeDrawer;
  Object.values(window.VIEWS).forEach(v => { if (v.bind) v.bind(); });
  window.addEventListener("hashchange", route);
  window.addEventListener("resize", onResizeCharts);
  route();
  probe();
  // 后台预载 echarts：切到图谱页时通常已就绪，避免首屏长时间空白
  loadEcharts().catch(() => {});
}
document.addEventListener("DOMContentLoaded", init);

/* ---------------- 抽屉（节点详情侧滑） ---------------- */
function openDrawer(title, bodyHtml) {
  $("drawer-title").textContent = title;
  $("drawer-body").innerHTML = bodyHtml;
  $("drawer").classList.add("open");
  $("drawer-mask").classList.remove("hidden");
}
function closeDrawer() {
  $("drawer").classList.remove("open");
  $("drawer-mask").classList.add("hidden");
}
window.openDrawer = openDrawer;
window.closeDrawer = closeDrawer;

/* 公共调试出口（人工验收用） */
window.API = { apiFetch, fetchStats, fetchGraph, loadEcharts };
window.__charts = {};
