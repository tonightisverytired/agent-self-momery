/* 视图：写入表单。动态字段 + 文本抽取 + batch 批量写入。 */
"use strict";

(function () {
  let lastData = null;

  const WRITE_FIELDS = {
    entities: [["name", "名称"], ["kind", "类型"],
               ["description", "描述"], ["protected", "protected"]],
    events: [["name", "名称"], ["ts", "时间(ISO)"], ["kind", "类型"],
             ["value_score", "重要度 0-1"]],
    facts: [["entity", "实体"], ["key", "key"], ["value", "值"],
            ["source", "来源"], ["confidence", "置信 0-1"],
            ["valid_at", "valid_at(ISO)"]],
    beliefs: [["subject", "主体"], ["proposition", "命题"],
              ["polarity", "polarity"], ["confidence", "置信 0-1"]],
    intents: [["subject", "主体"], ["proposition", "意图"],
              ["status", "status"]],
    impacts: [["subject", "主体"], ["dimension", "维度"],
              ["direction", "direction"], ["valence", "valence"],
              ["magnitude", "程度 0-1"], ["kind", "kind"],
              ["evaluator", "评价者"], ["cause_event", "关联事件名"]],
    evidence: [["source_type", "source_type"], ["source_ref", "引用"],
               ["conversation_id", "conversation_id"],
               ["message_id", "message_id"]],
    edges: [["a", "起点"], ["b", "终点"], ["rel", "关系"],
            ["weight", "权重 0-1"], ["confidence", "置信 0-1"]],
  };
  const ENUM_OPTIONS = {
    polarity: ["positive", "negative", "neutral"],
    status: ["active", "completed", "cancelled", "expired", "superseded"],
    direction: ["increase", "decrease", "stable", "appear", "disappear"],
    valence: ["positive", "negative", "neutral", "mixed", "unknown"],
    kind: ["objective", "subjective"],
    source_type: ["user_statement", "conversation", "system_record",
                  "external_data", "imported_memory", "inferred"],
    rel: ["participates", "discusses", "mentions", "occurs_with",
          "precedes", "causes", "depends_on", "similar_to", "part_of",
          "prefers", "updates_to", "contradicts", "summarizes", "step_of"],
  };

  function renderWriteFields() {
    const kind = $("write-kind").value;
    const fields = WRITE_FIELDS[kind] || [];
    $("write-fields").innerHTML = fields.map(([key, label]) => {
      if (ENUM_OPTIONS[key]) {
        const opts = ENUM_OPTIONS[key].map(v =>
          `<option value="${v}">${v}</option>`).join("");
        return `<label>${label}<select data-field="${key}">${opts}</select></label>`;
      }
      return `<label>${label}<input data-field="${key}"></label>`;
    }).join("");
  }

  function showResult(html, data) {
    lastData = data;
    $("write-result").innerHTML = html;
    $("write-raw").textContent = JSON.stringify(data, null, 2);
  }

  async function submitWrite() {
    const kind = $("write-kind").value;
    const body = {};
    document.querySelectorAll("#write-fields [data-field]").forEach(el => {
      const v = el.value.trim();
      if (v) body[el.dataset.field] = v;
    });
    const data = await apiFetch("/" + kind, {
      method: "POST", body: JSON.stringify(body),
    });
    showResult(
      `<div class="fact-card">${badge(kind, "b-kind")} 写入成功
       <span class="hero-num">id = ${fmt.num(data.id)}</span></div>`, data);
    refreshDash();
  }
  async function submitWriteText() {
    const text = $("write-text").value.trim();
    if (!text) { toast("请输入文本", true); return; }
    const data = await apiFetch("/write", {
      method: "POST", body: JSON.stringify({ text }),
    });
    showResult(
      `<div class="fact-card">抽取写入：<span class="hero-num">
       ${fmt.num(data.accepted)}</span> 条接受
       ${badge("rejected " + (data.rejected || []).length, "b-rel")}
       ids: ${(data.ids || []).map(i => badge("#" + i, "b-evid")).join(" ")}
       </div>`, data);
    refreshDash();
  }
  async function submitBatch() {
    const texts = $("write-batch").value.split("\n")
      .map(s => s.trim()).filter(Boolean);
    if (!texts.length) { toast("请至少输入一行文本", true); return; }
    if (texts.length > 100) { toast("最多 100 条", true); return; }
    const data = await apiFetch("/batch", {
      method: "POST", body: JSON.stringify({ texts, batch_size: 20 }),
    });
    showResult(
      `<table class="table"><thead><tr><th>#</th><th>文本</th><th>结果</th>
       </tr></thead><tbody>${texts.map((t, i) => `<tr>
       <td>${i + 1}</td><td>${esc(t)}</td>
       <td>${(data.ids || [])[i] !== undefined
         ? badge("✓ #" + data.ids[i], "b-ok")
         : badge("rejected", "b-rel")}</td></tr>`).join("")}
       </tbody></table>`, data);
    refreshDash();
  }
  function refreshDash() {
    const dash = window.VIEWS.dashboard;
    if (dash && dash.refresh) dash.refresh({}).catch(() => {});
  }

  function refresh() {
    // 首次进入即渲染字段（bind 只挂 onchange；不切下拉时表单会是空的）
    if (!$("write-fields").children.length) renderWriteFields();
  }

  function bind() {
    $("write-kind").onchange = renderWriteFields;
    $("write-submit").onclick = () => submitWrite().catch(() => {});
    $("write-clear").onclick = () => {
      $("write-result").innerHTML = "";
      $("write-raw").textContent = "";
      lastData = null;
    };
    $("write-text-submit").onclick = () => submitWriteText().catch(() => {});
    $("batch-submit").onclick = () => submitBatch().catch(() => {});
  }

  window.VIEWS = window.VIEWS || {};
  window.VIEWS.write = { refresh, bind };
})();
