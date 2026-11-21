"use strict";
/* Invoice desk front end. No framework: one file, plain DOM. */

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (n) => n == null ? "–" : new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 }).format(n);
const inr0 = (n) => n == null ? "–" : new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(n);
const dur = (ms) => ms == null ? "–" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
const pct = (x) => x == null ? "–" : `${Math.round(x * 100)}%`;

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch { /* not json */ }
  if (!r.ok) throw new Error((body && body.error) || `Request failed (${r.status})`);
  return body;
}

const STAGES = [
  ["intake", "Receive the file"], ["read", "Read the document"], ["classify", "Check it is an invoice"],
  ["validate", "Check the document itself"], ["vendor", "Match the vendor"], ["po", "Match the purchase order"],
  ["risk", "Look for duplicates and date problems"], ["decide", "Decide"],
];
const STATUS_WORD = { approved: "Approved", part_approved: "Part approved", on_hold: "On hold", rejected: "Rejected" };
const STAMP_WORD = { approved: "APPROVED", part_approved: "PART APPROVED", on_hold: "ON HOLD", rejected: "REJECTED" };
const ICON = {
  ok: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3 3 7-7.5" fill="none" stroke="currentColor" stroke-width="2"/></svg>',
  hold: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="3" width="2.6" height="10" fill="currentColor"/><rect x="9.4" y="3" width="2.6" height="10" fill="currentColor"/></svg>',
  no: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="2"/></svg>',
};
const iconFor = (s) => ({ approved: ICON.ok, part_approved: ICON.ok, on_hold: ICON.hold, rejected: ICON.no })[s];
const chip = (status) => `<span class="outcome ${status}">${iconFor(status)}${STATUS_WORD[status]}</span>`;

/* ---------------- routing ---------------- */
const state = { runId: null, source: null, samples: [], runFilter: { status: null, owner: null } };

function route() {
  const [, view = "process", id] = location.hash.split("/");
  const v = ["dashboard", "how"].includes(view) ? view : "process";
  $("#view-process").hidden = v !== "process";
  $("#view-dashboard").hidden = v !== "dashboard";
  $("#view-how").hidden = v !== "how";
  if (v === "how") loadHow();
  document.querySelectorAll("[data-nav]").forEach((a) => { if (a.dataset.nav !== v) a.removeAttribute("aria-current"); else a.setAttribute("aria-current", "page"); });
  if (v === "dashboard") { loadDashboard(); loadReaders(); }
  if (v === "process" && id && id !== state.runId) openRun(id, false);
}
window.addEventListener("hashchange", route);

/* ---------------- samples and upload ---------------- */
async function loadSamples() {
  const { samples } = await api("/api/samples");
  state.samples = samples;
  const groups = [...new Set(samples.map((s) => s.group))];
  $("#samples").innerHTML = groups.map((g) => `
    <div class="group"><h3>${esc(g)}</h3>
      ${samples.filter((s) => s.group === g).map((s) => `
        <button class="sample" data-sample="${s.id}">
          <span class="t">${esc(s.title)}${s.scanned ? '<span class="tag">scan</span>' : ""}</span>
          <span class="v">${esc(s.vendor)}</span>
        </button>`).join("")}
    </div>`).join("");
  $("#samples").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-sample]");
    if (!b) return;
    document.querySelectorAll(".sample").forEach((x) => x.removeAttribute("aria-current"));
    b.setAttribute("aria-current", "true");
    try {
      const { run_id } = await api("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sample_id: b.dataset.sample }) });
      openRun(run_id, true);
    } catch (err) { showUploadError(err.message); }
  });
}

function showUploadError(msg) { const p = $("#upload-error"); p.textContent = msg; p.hidden = !msg; }

async function upload(file) {
  showUploadError("");
  if (!file) return;
  const fd = new FormData(); fd.append("file", file);
  try {
    const { run_id } = await api("/api/runs", { method: "POST", body: fd });
    document.querySelectorAll(".sample").forEach((x) => x.removeAttribute("aria-current"));
    openRun(run_id, true);
  } catch (err) { showUploadError(err.message); }
}
function wireUpload() {
  const drop = $("#drop"), input = $("#file");
  input.addEventListener("change", () => { upload(input.files[0]); input.value = ""; });
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));
}

/* ---------------- a run ---------------- */
function resetRunPanel(runId) {
  if (state.source) { state.source.close(); state.source = null; }
  state.runId = runId;
  history.replaceState(null, "", `#/process/${runId}`);
  $("#sheet-empty").hidden = true;
  $("#sheet").hidden = false;
  $("#stamp").hidden = true; $("#stamp").classList.remove("land");
  $("#sheet-img").src = `/api/runs/${runId}/page.png`;
  $("#decision").hidden = true; $("#details").hidden = true;
  $("#run-title").textContent = "Run";
  $("#run-file").textContent = "";
  $("#stages").innerHTML = STAGES.map(([k, label], i) =>
    `<li class="stage" data-stage="${k}" data-n="${i + 1}" data-status="pending"><div class="stage-row"><span class="stage-label">${label}</span><span class="stage-ms"></span></div><div class="stage-detail"></div></li>`).join("");
}

function stageNode(ev, evaluation) {
  if (ev.stage === "human" || ev.stage === "error" || evaluation > 1) {
    const li = document.createElement("li");
    li.className = "stage" + (ev.stage === "human" ? " human" : "");
    li.dataset.stage = ev.stage;
    li.dataset.n = "";
    $("#stages").appendChild(li);
    return li;
  }
  return $(`#stages [data-stage="${ev.stage}"]`);
}

function paintStage(ev) {
  const li = stageNode(ev, ev.evaluation || 1);
  if (!li) return;
  li.dataset.status = ev.stage === "human" ? "ok" : ev.status;
  const checks = (ev.checks || []).filter((c) => c.status === "fail");
  li.innerHTML = `<div class="stage-row"><span class="stage-label">${esc(ev.label)}</span><span class="stage-ms">${ev.stage === "human" ? "" : dur(ev.ms)}</span></div>
    <div class="stage-detail">${esc(ev.detail)}</div>
    ${checks.length ? `<ul class="stage-checks">${checks.map((c) => `<li class="fail-${c.severity}">${esc(c.id)} ${esc(c.message)}</li>`).join("")}</ul>` : ""}`;
}

function markWorking(stage) {
  const li = $(`#stages [data-stage="${stage}"]`);
  if (li && li.dataset.status === "pending") li.dataset.status = "working";
}

async function openRun(runId, live) {
  resetRunPanel(runId);
  if (!live) {
    const run = await api(`/api/runs/${runId}`).catch(() => null);
    if (!run) { $("#run-title").textContent = "Run not found"; return; }
    run.events.forEach(paintStage);
    renderRun(run, false);
    return;
  }
  // Live: events arrive from the server as stages finish. Reveal them at a readable
  // pace (the times shown are the real ones); the stamp lands when the decision does.
  markWorking("intake");
  const queue = []; let done = false, timer = null, last = 0;
  const pump = () => {
    if (!queue.length) {
      timer = null;
      if (done) finish();
      return;
    }
    const wait = Math.max(0, 320 - (performance.now() - last));
    timer = setTimeout(() => {
      const ev = queue.shift(); last = performance.now();
      paintStage(ev);
      const i = STAGES.findIndex(([k]) => k === ev.stage);
      if (i >= 0 && i < STAGES.length - 1) markWorking(STAGES[i + 1][0]);
      pump();
    }, wait);
  };
  const finish = async () => {
    const run = await api(`/api/runs/${runId}`);
    if (state.runId === runId) renderRun(run, true);
  };
  const es = new EventSource(`/api/runs/${runId}/events`);
  state.source = es;
  es.addEventListener("stage", (m) => { queue.push(JSON.parse(m.data)); if (!timer) pump(); });
  es.addEventListener("done", () => { es.close(); done = true; if (!timer) pump(); });
  es.onerror = () => { es.close(); done = true; if (!timer) pump(); };
}

function renderRun(run, animate) {
  $("#run-title").textContent = run.invoice_number ? `Invoice ${run.invoice_number}` : "Run";
  $("#run-file").textContent = `${run.vendor_name || ""}${run.vendor_name ? ", " : ""}${run.file_name}`;
  if (run.state === "error") {
    $("#decision").hidden = false;
    $("#decision").innerHTML = `<p class="error">This run failed: ${esc(run.error)}. The file was kept; try uploading it again or check that it is a readable PDF.</p>`;
    return;
  }
  renderDecision(run);
  stamp(run.final_status, animate);
  renderDetails(run);
}

function stamp(status, animate) {
  const s = $("#stamp");
  $("#stamp-word").textContent = STAMP_WORD[status];
  const d = new Date();
  $("#stamp-meta").textContent = `${d.getDate()} ${d.toLocaleString("en-IN", { month: "short" }).toUpperCase()} ${d.getFullYear()}  KAVERI AP`;
  s.hidden = false;
  s.classList.remove("land");
  if (animate) { void s.offsetWidth; s.classList.add("land"); }
}

/* ---------------- decision card and actions ---------------- */
function renderDecision(run) {
  const d = run.decision || {};
  const fs = run.final_status;
  const lastAction = run.actions.at(-1);
  const c09 = run.checks.find((c) => c.id === "C09" && c.status === "fail");
  const candidate = c09 && c09.evidence.candidates && c09.evidence.candidates[0] && run.po_how === "inferred" ? c09.evidence.candidates[0][1] : null;
  let html = `${chip(fs)}<p class="decision-summary">${esc(d.summary)}</p>`;
  if (lastAction) {
    const verb = { approve: "Approved", reject: "Rejected", approve_part: "Part approved", confirm_po: "PO confirmed" }[lastAction.action];
    html += `<p class="note">${verb} by ${esc(lastAction.actor)}: ${esc(lastAction.reason)}</p>`;
  }
  if (fs === "on_hold") {
    html += `<div class="ask"><div class="who">Needs ${esc(d.owner)}</div><div class="q">${esc(d.question)}</div></div>`;
    html += `<dl class="kv"><dt>Invoice total</dt><dd>${inr(run.total)}</dd>`;
    if (d.approvable_amount != null) html += `<dt>Can be approved now</dt><dd>${inr(d.approvable_amount)} pre-tax</dd><dt>Held</dt><dd>${inr(d.held_amount)} pre-tax</dd>`;
    if (d.reasons && d.reasons.length > 1) html += `<dt>Other reasons</dt><dd>${d.reasons.length - 1}</dd>`;
    html += `</dl><div class="actions">
      ${candidate ? `<button class="btn" data-act="confirm_po" data-po="${esc(candidate)}">Confirm ${esc(candidate)}</button>` : ""}
      ${d.approvable_amount != null ? `<button class="btn" data-act="approve_part">Approve ${inr0(d.approvable_amount)} now</button>` : ""}
      <button class="btn quiet" data-act="approve">Approve anyway</button>
      <button class="btn danger" data-act="reject">Reject</button>
    </div><div id="reason-box"></div>`;
  } else if (fs === "rejected" && d.vendor_message && run.outcome === "REJECT") {
    html += `<div class="vendor-msg">${esc(d.vendor_message)}</div>
      <div class="actions"><button class="btn quiet" id="copy-msg">Copy message for the vendor</button></div>`;
  } else if (fs === "approved" && d.approval_level) {
    html += `<dl class="kv"><dt>Invoice total</dt><dd>${inr(run.total)}</dd><dt>Approval</dt><dd>${esc(d.approval_level)}</dd></dl>`;
  }
  const box = $("#decision");
  box.innerHTML = html; box.hidden = false;
  box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => onAction(run, b.dataset.act, b.dataset.po)));
  const copy = $("#copy-msg");
  if (copy) copy.addEventListener("click", async () => { await navigator.clipboard.writeText(d.vendor_message); copy.textContent = "Message copied"; });
}

function onAction(run, act, po) {
  if (act === "approve" || act === "reject") {
    const label = act === "approve" ? "Why approve an invoice the checks held? This is kept in the run history." : "Reason the vendor will see";
    const verb = act === "approve" ? "Approve invoice" : "Reject invoice";
    $("#reason-box").innerHTML = `<div class="reason"><label for="reason">${label}</label><textarea id="reason"></textarea>
      <div class="actions"><button class="btn" id="do-act">${verb}</button><button class="btn quiet" id="cancel-act">Cancel</button></div><p class="error" id="act-error" hidden></p></div>`;
    $("#reason").focus();
    $("#cancel-act").addEventListener("click", () => { $("#reason-box").innerHTML = ""; });
    $("#do-act").addEventListener("click", () => sendAction(run.run_id, act, $("#reason").value));
  } else {
    sendAction(run.run_id, act, "", po);
  }
}

async function sendAction(runId, action, reason, po) {
  try {
    const run = await api(`/api/runs/${runId}/actions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, reason, po }) });
    $("#stages").innerHTML = "";
    STAGES.forEach(([k, label], i) => $("#stages").insertAdjacentHTML("beforeend", `<li class="stage" data-stage="${k}" data-n="${i + 1}" data-status="pending"><div class="stage-row"><span class="stage-label">${label}</span></div></li>`));
    run.events.forEach(paintStage);
    renderRun(run, true);
  } catch (err) {
    const p = $("#act-error");
    if (p) { p.textContent = err.message; p.hidden = false; } else alert(err.message);
  }
}

/* ---------------- details tabs ---------------- */
const FIELD_LABEL = {
  doc_type: "Document type", vendor_name: "Vendor name", vendor_gstin: "Vendor GSTIN", buyer_gstin: "Billed to GSTIN",
  invoice_number: "Invoice number", invoice_date: "Invoice date", po_reference: "PO number", subtotal: "Subtotal (pre-tax)",
  cgst: "CGST", sgst: "SGST", igst: "IGST", total: "Total", bank_account: "Bank account", ifsc: "IFSC",
};
let currentRun = null, currentTab = "checks";

function renderDetails(run) {
  currentRun = run;
  $("#details").hidden = false;
  document.querySelectorAll(".tabs [data-tab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === currentTab));
  const body = $("#tab-body");
  if (currentTab === "checks") body.innerHTML = checksTable(run);
  if (currentTab === "fields") body.innerHTML = fieldsTable(run);
  if (currentTab === "lines") body.innerHTML = linesTable(run);
  if (currentTab === "history") body.innerHTML = historyTable(run);
}
document.addEventListener("click", (e) => {
  const t = e.target.closest(".tabs [data-tab]");
  if (t && currentRun) { currentTab = t.dataset.tab; renderDetails(currentRun); }
});

function evidenceText(ev) {
  const lines = [];
  const walk = (v, pre) => {
    if (v && typeof v === "object" && !Array.isArray(v)) Object.entries(v).forEach(([k, x]) => walk(x, pre ? `${pre}.${k}` : k));
    else if (Array.isArray(v) && v.length && typeof v[0] === "object" && !Array.isArray(v[0])) v.forEach((x, i) => walk(x, `${pre}[${i + 1}]`));
    else lines.push(`${pre}: ${Array.isArray(v) ? JSON.stringify(v) : v}`);
  };
  walk(ev, "");
  return lines.join("\n");
}

function checksTable(run) {
  const rows = run.checks.map((c) => {
    const cls = c.status === "fail" ? `fail-${c.severity}` : c.status;
    const word = c.status === "pass" ? "Passed" : c.status === "skip" ? "Not run" : c.severity === "reject" ? "Reject" : "Hold";
    const ev = c.evidence && Object.keys(c.evidence).length ? `<details class="ev"><summary>Evidence</summary><pre>${esc(evidenceText(c.evidence))}</pre></details>` : "";
    return `<tr><td>${esc(c.id)}</td><td>${esc(c.name)}</td><td><span class="res ${cls}">${word}</span></td><td>${esc(c.message)}${c.status === "fail" && c.owner ? `<div class="muted">Owner: ${esc(c.owner)}</div>` : ""}${ev}</td></tr>`;
  }).join("");
  return `<table><thead><tr><th>ID</th><th>Check</th><th>Result</th><th>What was found</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function fieldsTable(run) {
  const ex = run.extraction;
  const rows = Object.entries(FIELD_LABEL).map(([k, label]) => {
    const f = ex.fields[k] || {};
    const v = f.value == null || f.value === "" ? '<span class="muted">not found</span>' : typeof f.value === "number" ? inr(f.value) : esc(f.value);
    const c = f.value == null || f.value === "" ? "" : `<span class="conf"><i style="width:${Math.round((f.confidence || 0) * 100)}%"></i></span>${pct(f.confidence)}`;
    return `<tr><td>${label}</td><td>${v}</td><td>${c}</td><td class="muted">${esc(f.evidence || "")}</td></tr>`;
  }).join("");
  const routes = (run.route_log || []).map((l) => `${l.route}${l.skipped ? " (skipped: no model)" : ""}${l.error ? " (failed)" : ""}`).join(", then ");
  return `<p class="note" style="margin:0 0 12px">Read by <b>${esc(ex.route)}</b>. Routes tried: ${esc(routes)}.${ex.notes.length ? " " + esc(ex.notes.join("; ")) : ""}</p>
    <table><thead><tr><th>Field</th><th>Value</th><th>Confidence</th><th>Read from</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function linesTable(run) {
  const ex = run.extraction;
  const c11 = run.checks.find((c) => c.id === "C11") || { evidence: {} };
  const c12 = run.checks.find((c) => c.id === "C12") || { evidence: {} };
  const price = Object.fromEntries((c11.evidence.lines || []).map((r) => [r.line, r]));
  const qty = Object.fromEntries((c12.evidence.lines || []).map((r) => [r.line, r]));
  const rows = ex.lines.map((l) => {
    const p = price[l.description], q = qty[l.description];
    const diff = p ? `<span class="${p.diff_pct > 0.5 ? "over" : ""}">${p.diff_pct > 0 ? "+" : ""}${p.diff_pct}%</span>` : "–";
    const avail = q ? `<span class="${l.qty > q.available ? "over" : ""}">${q.available}</span> <span class="muted">of ${q.basis_qty}</span>` : "–";
    return `<tr><td>${esc(l.description)}<div class="muted">HSN ${esc(l.hsn || "–")}</div></td><td class="num">${l.qty}</td><td class="num">${inr(l.unit_price)}</td>
      <td class="num">${p ? inr(p.po_price_pre_tax) : "–"}${p && p.basis === "incl_tax" ? '<div class="muted">from tax-inclusive PO</div>' : ""}</td>
      <td class="num">${diff}</td><td class="num">${avail}</td><td class="num">${inr(l.amount)}</td></tr>`;
  }).join("");
  const how = { inferred: "inferred, not printed", confirmed: "confirmed by a person", printed: "printed on the invoice" }[run.po_how] || "";
  const po = run.po_number ? `Matched to ${esc(run.po_number)} (${how}).` : "No PO matched.";
  return `<p class="note" style="margin:0 0 12px">${po} Available to bill = accepted at goods receipt (or ordered, for services) minus already billed.</p>
    <table><thead><tr><th>Invoice line</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">PO price, pre-tax</th><th class="num">Difference</th><th class="num">Available to bill</th><th class="num">Amount</th></tr></thead><tbody>${rows || '<tr><td colspan="7" class="muted">No line items were read.</td></tr>'}</tbody></table>`;
}

function historyTable(run) {
  const t0 = run.events.length ? run.events[0].ts : 0;
  const rows = run.events.map((e) => `<tr><td class="num">${e.stage === "human" ? "" : "+" + Math.round((e.ts - t0) * 1000) + " ms"}</td><td>${esc(e.label)}</td><td>${esc(e.status)}</td><td>${esc(e.detail)}</td></tr>`).join("");
  const acts = run.actions.map((a) => `<tr><td>${new Date(a.ts * 1000).toLocaleString("en-IN")}</td><td>${esc(a.actor)}</td><td>${esc(a.action.replace("_", " "))}</td><td>${esc(a.reason)}</td></tr>`).join("");
  return `<table><thead><tr><th class="num">When</th><th>Step</th><th>Result</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table>
    ${acts ? `<h3 style="margin:20px 0 8px">People</h3><table><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Reason</th></tr></thead><tbody>${acts}</tbody></table>` : ""}`;
}

/* ---------------- dashboard ---------------- */
async function loadDashboard() {
  const [m, { runs }] = await Promise.all([api("/api/metrics"), api("/api/runs")]);
  const el = $("#dash");
  if (!m.processed) {
    el.innerHTML = `<div class="dash-empty"><p>No invoices processed yet. Process one on the desk, or run all 20 samples to fill this page.</p>
      <button class="btn" id="run-all-empty">Run all 20 samples</button></div>`;
    $("#run-all-empty").addEventListener("click", runAll);
    return;
  }
  const waitingN = m.open_holds;
  const owners = Object.entries(m.queue_by_owner).sort((a, b) => b[1].amount - a[1].amount);
  const maxOwner = Math.max(1, ...owners.map(([, o]) => o.amount));
  const maxReason = Math.max(1, ...m.hold_reasons.map((r) => r.count));
  const holdsTotal = m.hold_reasons.reduce((s, r) => s + r.count, 0);
  const oc = m.outcomes, ocTotal = oc.APPROVE + oc.HOLD + oc.REJECT;
  const routes = Object.entries(m.routes).map(([k, v]) => `${{ text: "PDF text", ocr: "OCR", llm: "model" }[k] || k} ${v}`).join(", ");
  el.innerHTML = `
    <section class="kpis" aria-label="Headline figures">
      <div class="kpi"><div class="k">Invoices processed</div><div class="v">${m.processed}</div><div class="s">read by ${esc(routes)}</div></div>
      <div class="kpi"><div class="k">Approved with no person involved</div><div class="v">${pct(m.no_touch_rate)}</div><div class="s">${oc.APPROVE} approved by the checks</div></div>
      <div class="kpi"><div class="k">Waiting on a person</div><div class="v">${inr0(m.amount_waiting)}</div><div class="s">${waitingN} invoice${waitingN === 1 ? "" : "s"} on hold</div></div>
      <div class="kpi"><div class="k">Typical time to a decision</div><div class="v">${dur(m.median_ms)}</div><div class="s">median, from file to decision</div></div>
    </section>
    <div class="panels">
      <section class="panel"><h2>Why invoices were held</h2><p class="sub">Failed checks on held invoices. One invoice can fail more than one.</p>
        ${m.hold_reasons.length ? m.hold_reasons.map((r) => `<div class="hbar" data-tip="${esc(r.name)}: ${r.count} of ${holdsTotal} failed checks (${Math.round(100 * r.count / holdsTotal)}%)">
          <span class="name">${esc(r.name)} <small>${esc(r.check_id)}</small></span>
          <span class="track"><span class="fill" style="width:${(100 * r.count / maxReason).toFixed(1)}%;display:block"></span></span>
          <span class="val">${r.count}</span></div>`).join("") : '<p class="muted">Nothing held yet.</p>'}
      </section>
      <section class="panel"><h2>Who needs to act</h2><p class="sub">Open holds by owner, by invoice value. Select one to filter the list.</p>
        ${owners.length ? owners.map(([o, v]) => `<div class="meter" data-owner="${esc(o)}" data-tip="${esc(o)}: ${v.count} invoice${v.count === 1 ? "" : "s"}, ${inr(v.amount)}" tabindex="0">
          <span>${esc(o)}</span><span class="track"><span class="fill" style="display:block;width:${(100 * v.amount / maxOwner).toFixed(1)}%"></span></span>
          <span class="fig">${inr0(v.amount)} <span class="muted">${v.count}</span></span></div>`).join("") : '<p class="muted">Nobody: the queue is empty.</p>'}
        <h2 style="margin-top:24px">Decisions</h2>
        <div class="split" role="img" aria-label="Approved ${oc.APPROVE}, on hold ${oc.HOLD}, rejected ${oc.REJECT}">
          ${[["APPROVE", "var(--good)", "Approved"], ["HOLD", "var(--warn)", "On hold"], ["REJECT", "var(--bad)", "Rejected"]].filter(([k]) => oc[k]).map(([k, c, w]) => `<span style="flex:${oc[k]};background:${c}" data-tip="${w}: ${oc[k]} of ${ocTotal}"></span>`).join("")}
        </div>
        <div class="legend"><span><i style="background:var(--good)"></i>Approved ${oc.APPROVE}</span><span><i style="background:var(--warn)"></i>On hold ${oc.HOLD}</span><span><i style="background:var(--bad)"></i>Rejected ${oc.REJECT}</span></div>
      </section>
    </div>
    <section class="panel"><h2>All runs</h2><p class="sub">Select a run to open it on the desk.</p>
      <div class="filters" id="filters"></div><div id="runs-table"></div></section>`;
  el.querySelectorAll(".meter").forEach((mtr) => {
    const go = () => { state.runFilter.owner = mtr.dataset.owner; state.runFilter.status = "on_hold"; renderRuns(runs); };
    mtr.addEventListener("click", go); mtr.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  });
  renderRuns(runs);
}

function renderRuns(runs) {
  const f = state.runFilter;
  const opts = [[null, "All"], ["approved", "Approved"], ["part_approved", "Part approved"], ["on_hold", "On hold"], ["rejected", "Rejected"]];
  $("#filters").innerHTML = opts.map(([v, w]) => `<button class="chip" data-f="${v ?? ""}" aria-pressed="${f.status === v}">${w}</button>`).join("")
    + (f.owner ? `<button class="chip" data-clear-owner aria-pressed="true">Owner: ${esc(f.owner)}, clear</button>` : "");
  $("#filters").querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => { f.status = b.dataset.f || null; renderRuns(runs); }));
  const co = $("#filters [data-clear-owner]");
  if (co) co.addEventListener("click", () => { f.owner = null; renderRuns(runs); });
  const list = runs.filter((r) => r.state === "done" && (!f.status || r.final_status === f.status) && (!f.owner || r.owner === f.owner));
  $("#runs-table").innerHTML = `<table><thead><tr><th>When</th><th>Vendor</th><th>Invoice</th><th class="num">Total</th><th>Read by</th><th>Decision</th><th>Waiting on</th></tr></thead><tbody>
    ${list.map((r) => `<tr class="clickable" data-run="${r.run_id}" tabindex="0"><td>${new Date(r.created_at * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}</td>
      <td>${esc(r.vendor_name || "unknown")}</td><td>${esc(r.invoice_number || r.file_name)}</td><td class="num">${inr(r.total)}</td>
      <td>${esc({ text: "PDF text", ocr: "OCR", llm: "model" }[r.route] || r.route || "")}</td><td>${chip(r.final_status)}</td>
      <td>${r.final_status === "on_hold" ? esc(r.owner) : ""}</td></tr>`).join("") || '<tr><td colspan="7" class="muted">No runs match this filter.</td></tr>'}
  </tbody></table>`;
  $("#runs-table").querySelectorAll("[data-run]").forEach((tr) => {
    const go = () => { state.runId = null; location.hash = `#/process/${tr.dataset.run}`; };
    tr.addEventListener("click", go); tr.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  });
}

async function runAll() {
  const before = (await api("/api/metrics")).processed;
  const total = state.samples.length || 20;
  await api("/api/demo/run-all", { method: "POST" });
  const p = $("#run-all-progress"); p.hidden = false;
  document.querySelectorAll("#run-all, #run-all-empty").forEach((b) => b.disabled = true);
  const tick = async () => {
    const m = await api("/api/metrics");
    const done = m.processed - before;
    p.textContent = `Processed ${done} of ${total} samples`;
    if (done >= total) { p.hidden = true; document.querySelectorAll("#run-all, #run-all-empty").forEach((b) => b.disabled = false); loadDashboard(); return; }
    setTimeout(tick, 1200);
  };
  tick();
}

async function modelCheck() {
  const box = $("#model-check-result"), btn = $("#model-check");
  box.hidden = false; btn.disabled = true;
  box.innerHTML = "<p>Sending two sample scans to the reader and comparing its reading with their answer keys. A cloud model takes under a minute; a private model without a GPU can take a few minutes.</p>";
  try {
    const r = await api("/api/model-check", { method: "POST" });
    if (!r.ok) {
      box.innerHTML = `<h3>The reader check failed</h3><p class="error">${esc(r.error)}</p>`;
    } else {
      box.innerHTML = `<h3>${esc(r.model)} is working</h3><ul>${r.docs.map((d) => `<li>${esc(d.sample)}: ${d.fields_right} of ${d.fields} fields and ${d.lines_right} of ${d.lines} lines exactly right, ${d.seconds} s${d.cached ? " (saved answer, no new call)" : ""}${
        Object.keys(d.wrong).length ? ". Misread: " + Object.entries(d.wrong).map(([f, v]) => `${esc(f)} (expected ${esc(v.expected)}, got ${esc(v.got)})`).join("; ") : ""}</li>`).join("")}</ul>`;
    }
  } catch (err) {
    box.innerHTML = `<p class="error">${esc(err.message)}</p>`;
  } finally { btn.disabled = false; }
}

/* ---------------- reader ---------------- */
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

function showReader(r) {
  const cur = r.options.find((o) => o.id === r.current);
  $("#model-note").textContent = `Scans are read by: ${r.label}`;
  $("#upload-privacy").hidden = !(cur && cur.kind === "cloud");
  const box = $("#reader-options");
  box.innerHTML = r.options.map((o) => `<label class="reader-opt" data-kind="${esc(o.kind)}">
    <input type="radio" name="reader" value="${esc(o.id)}"${o.id === r.current ? " checked" : ""}>
    <span class="reader-name">${esc(o.label)}${o.kind === "private" ? ' <span class="tag">private</span>' : ""}</span>
    <span class="reader-note">${esc(o.note)}</span></label>`).join("");
  box.querySelectorAll("input").forEach((i) => i.addEventListener("change", async () => {
    try {
      const d = await post("/api/readers/select", { id: i.value });
      showReader(d);
      $("#reader-status").textContent = `New runs will be read by: ${d.label}`;
    } catch (err) { $("#reader-status").textContent = err.message; loadReaders(); }
  }));
}
async function loadReaders() { showReader(await api("/api/readers")); }

async function findReaders(url) {
  const st = $("#reader-status"), btns = [$("#reader-scan"), $("#reader-connect")];
  btns.forEach((b) => b.disabled = true);
  st.textContent = url ? `Connecting to ${url}...` : "Looking for model servers on this machine...";
  try {
    const d = await post("/api/readers/find", url ? { url } : {});
    showReader(d);
    const n = d.found.length;
    st.textContent = n
      ? `Found ${n} vision model${n > 1 ? "s" : ""}${d.added.length ? `, ${d.added.length} new` : ""}. Choose one above to use it.`
      : "No vision model servers found on this machine. Start one with scripts/serve_reader.py, or connect to its address.";
  } catch (err) { st.textContent = err.message; }
  finally { btns.forEach((b) => b.disabled = false); }
}

/* ---------------- how it works ---------------- */
const EDGE_CASES = [
  ["E1", "A contract billed in parts goes over its value", "Housekeeping is billed monthly against a 12-month PO. The third bill takes it past the PO value. The desk approves only what is left on the PO and holds the excess for Procurement."],
  ["E2", "The same bill sent again with a new number", "A paid invoice is reissued 23 days later under a new number. A plain invoice-number check passes it; the desk compares amount, lines, dates and number and asks AP whether it is the same bill."],
  ["E3", "A scan with no PO number on it", "Most real invoices do not print the PO. The desk ranks the vendor's open POs by how well their lines fit and asks AP to confirm the best match with one click."],
  ["E4", "A price rise hidden inside a normal-looking total", "One unit price is 12% above the PO while the total stays below the value of goods received. Line-by-line matching catches what a total-only check would pass."],
];

function stepBox(x, y, n, title, sub, accent) {
  return `<g><rect x="${x}" y="${y}" width="200" height="68" rx="8" fill="${accent ? "var(--stamp-soft)" : "none"}" stroke="${accent ? "var(--stamp)" : "var(--rule)"}" stroke-width="${accent ? 2 : 1.25}"/>
    <text x="${x + 14}" y="${y + 26}" font-size="14" font-weight="700" fill="var(--ink)">${n}  ${title}</text>
    <text x="${x + 14}" y="${y + 48}" font-size="12" fill="var(--ink-2)">${sub}</text></g>`;
}
function outcomeBox(x, y, color, title, l1, l2) {
  return `<g><rect x="${x}" y="${y}" width="290" height="78" rx="8" fill="none" stroke="${color}" stroke-width="2"/>
    <rect x="${x}" y="${y}" width="6" height="78" rx="3" fill="${color}"/>
    <text x="${x + 20}" y="${y + 26}" font-size="15" font-weight="700" fill="var(--ink)">${title}</text>
    <text x="${x + 20}" y="${y + 48}" font-size="12" fill="var(--ink-2)">${l1}</text>
    <text x="${x + 20}" y="${y + 65}" font-size="12" fill="var(--ink-2)">${l2}</text></g>`;
}
function processMap() {
  const X = [20, 260, 500, 740], r1 = 30, r2 = 160, ro = 300, w = 200;
  const arrow = (d) => `<path d="${d}" fill="none" stroke="var(--ink-3)" stroke-width="1.25" marker-end="url(#pm-arrow)"/>`;
  return `<svg viewBox="0 0 960 400" role="img" aria-label="Eight steps from file to decision, ending in approve, hold or reject" font-family="Work Sans, system-ui, sans-serif">
    <defs><marker id="pm-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="var(--ink-3)"/></marker></defs>
    ${stepBox(X[0], r1, 1, "Receive the file", "fingerprint, open, count pages")}
    ${stepBox(X[1], r1, 2, "Read the document", "PDF text; model for scans", true)}
    ${stepBox(X[2], r1, 3, "Is it an invoice?", "quotation, credit note, unclear")}
    ${stepBox(X[3], r1, 4, "Check the document", "fields, GSTIN, totals, tax type")}
    ${stepBox(X[0], r2, 5, "Match the vendor", "approved? bank details unchanged?")}
    ${stepBox(X[1], r2, 6, "Match the PO", "printed or inferred; price, qty")}
    ${stepBox(X[2], r2, 7, "Duplicates and dates", "same bill twice? plausible dates?")}
    ${stepBox(X[3], r2, 8, "Decide", "fixed rules, no AI")}
    ${arrow(`M${X[0] + w} ${r1 + 34}H${X[1] - 4}`)}${arrow(`M${X[1] + w} ${r1 + 34}H${X[2] - 4}`)}${arrow(`M${X[2] + w} ${r1 + 34}H${X[3] - 4}`)}
    ${arrow(`M${X[3] + w / 2} ${r1 + 68}V${r1 + 98}H${X[0] + w / 2}V${r2 - 4}`)}
    ${arrow(`M${X[0] + w} ${r2 + 34}H${X[1] - 4}`)}${arrow(`M${X[1] + w} ${r2 + 34}H${X[2] - 4}`)}${arrow(`M${X[2] + w} ${r2 + 34}H${X[3] - 4}`)}
    ${arrow(`M${X[3] + w / 2} ${r2 + 68}V${ro - 22}H${165}V${ro - 4}`)}
    ${arrow(`M${X[3] + w / 2} ${ro - 22}H${480}V${ro - 4}`)}
    ${arrow(`M${X[3] + w / 2} ${ro - 22}H${795}V${ro - 4}`)}
    ${outcomeBox(20, ro, "var(--good)", "Approve", "every check passed", "manager sign-off above \u20b95 lakh")}
    ${outcomeBox(335, ro, "var(--warn)", "Hold for a person", "names the owner and one question", "approve, part-approve, confirm PO or reject")}
    ${outcomeBox(650, ro, "var(--bad)", "Reject", "a message the vendor can act on", "unreadable, duplicate, wrong buyer")}
  </svg>`;
}

let howLoaded = false;
async function loadHow() {
  if (howLoaded) return;
  $("#process-map").innerHTML = processMap() + `<figcaption class="sub" style="margin-top:12px">Steps 3 to 7 are 16 named checks. A failed check never decides on its own: step 8 turns the results into one decision, and a person can see every check and its evidence.</figcaption>`;
  $("#edges").innerHTML = EDGE_CASES.map(([id, t, d]) => `<div class="edge"><h3>${esc(t)}</h3><p>${esc(d)}</p><button class="btn quiet" data-run-edge="${id}">Run this case</button></div>`).join("");
  $("#edges").querySelectorAll("[data-run-edge]").forEach((b) => b.addEventListener("click", async () => {
    const { run_id } = await api("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sample_id: b.dataset.runEdge }) });
    state.runId = run_id;
    location.hash = `#/process/${run_id}`;
    openRun(run_id, true);
  }));
  const { rules } = await api("/api/rules");
  $("#rules").innerHTML = rules.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  howLoaded = true;
}

/* ---------------- tooltip ---------------- */
function wireTooltip() {
  const tip = $("#tooltip");
  document.addEventListener("mousemove", (e) => {
    const t = e.target.closest("[data-tip]");
    if (!t) { tip.hidden = true; return; }
    tip.textContent = t.dataset.tip; tip.hidden = false;
    tip.style.left = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = (e.clientY + 16) + "px";
  });
}

/* ---------------- start ---------------- */
(async function init() {
  wireUpload(); wireTooltip();
  $("#run-all").addEventListener("click", runAll);
  $("#model-check").addEventListener("click", modelCheck);
  $("#reset").addEventListener("click", async () => {
    if (!confirm("Clear every run from this desk? Samples stay; history and dashboard figures are removed.")) return;
    await api("/api/demo/reset", { method: "POST" }); loadDashboard();
  });
  $("#reader-scan").addEventListener("click", () => findReaders(""));
  $("#reader-find").addEventListener("submit", (e) => { e.preventDefault(); const u = $("#reader-url").value.trim(); if (u) findReaders(u); });
  await loadReaders();
  await loadSamples();
  if (!location.hash) location.hash = "#/process";
  route();
})();
