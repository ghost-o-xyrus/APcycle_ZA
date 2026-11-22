"use strict";
/* Invoice desk front end. No framework: one file, plain DOM. */

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (n) => n == null ? "–" : new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 }).format(n);
const money = (n, cur) => n == null ? "–" : new Intl.NumberFormat(!cur || cur === "INR" ? "en-IN" : "en-US", { style: "currency", currency: cur || "INR", maximumFractionDigits: cur === "JPY" ? 0 : 2 }).format(n);
const money0 = (n, cur) => n == null ? "–" : new Intl.NumberFormat(!cur || cur === "INR" ? "en-IN" : "en-US", { style: "currency", currency: cur || "INR", maximumFractionDigits: 0 }).format(n);
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
  ["validate", "Check the document itself"], ["vendor", "Match the vendor"], ["po", "Three-way match: PO and goods receipt"],
  ["risk", "Look for duplicates, date and currency problems"], ["decide", "Decide"],
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
  if (v === "how") loadHow().then(() => {
    // #/how/<part>: scroll to that part of the instructions (#/how alone: the top)
    const el = id && document.getElementById(`how-${id}`);
    if (el) { el.scrollIntoView({ block: "start" }); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1600); }
    else window.scrollTo(0, 0);
  });
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
    const verb = { approve: "Approved", reject: "Rejected", approve_part: "Part approved", confirm_po: "PO confirmed", recheck: "Checked again" }[lastAction.action];
    html += `<p class="note">${verb} by ${esc(lastAction.actor)}: ${esc(lastAction.reason)}</p>`;
  }
  if (fs === "on_hold") {
    html += `<div class="ask"><div class="who">Needs ${esc(d.owner)}</div><div class="q">${esc(d.question)}</div></div>`;
    html += `<dl class="kv"><dt>Invoice total</dt><dd>${money(run.total, run.currency)}</dd>`;
    if (d.approvable_amount != null) html += `<dt>Can be approved now</dt><dd>${money(d.approvable_amount, run.currency)} pre-tax</dd><dt>Held</dt><dd>${money(d.held_amount, run.currency)} pre-tax</dd>`;
    if (d.reasons && d.reasons.length > 1) html += `<dt>Other reasons</dt><dd>${d.reasons.length - 1}</dd>`;
    const failed = new Set(run.checks.filter((c) => c.status === "fail").map((c) => c.id));
    const erpFix = failed.has("C07") ? ["/erp/#/vendors/new", "Add the vendor in the ERP"]
      : failed.has("C09") && !candidate ? ["/erp/#/pos/new", "Add the PO in the ERP"]
      : failed.has("C12") && run.po_number ? [`/erp/#/receipts/new?po=${encodeURIComponent(run.po_number)}`, "Record the goods receipt in the ERP"] : null;
    if (erpFix) html += `</dl><p class="note erp-fix">If the record exists but is missing from the ERP, <a href="${erpFix[0]}" target="_blank" rel="noopener">${erpFix[1]} ↗</a>, then check again.</p><dl class="kv">`;
    html += `</dl><div class="actions">
      ${candidate ? `<button class="btn" data-act="confirm_po" data-po="${esc(candidate)}">Confirm ${esc(candidate)}</button>` : ""}
      <button class="btn" data-act="recheck">Check again</button>
      ${d.approvable_amount != null ? `<button class="btn" data-act="approve_part">Approve ${money0(d.approvable_amount, run.currency)} now</button>` : ""}
      <button class="btn quiet" data-act="approve">Approve anyway</button>
      <button class="btn danger" data-act="reject">Reject</button>
    </div><div id="reason-box"></div>`;
  } else if (fs === "rejected" && d.vendor_message && run.outcome === "REJECT") {
    html += `<div class="vendor-msg">${esc(d.vendor_message)}</div>
      <div class="actions"><button class="btn quiet" id="copy-msg">Copy message for the vendor</button></div>`;
  } else if (fs === "approved" && d.approval_level) {
    html += `<dl class="kv"><dt>Invoice total</dt><dd>${money(run.total, run.currency)}</dd><dt>Approval</dt><dd>${esc(d.approval_level)}</dd></dl>`;
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
  cgst: "CGST", sgst: "SGST", igst: "IGST", tax: "VAT or sales tax", total: "Total", currency: "Currency",
  vendor_tax_id: "Vendor tax number", buyer_name: "Billed to", buyer_tax_id: "Billed to tax number",
  bank_account: "Bank account or IBAN", ifsc: "Bank code (IFSC, BIC or routing)",
};
const GST_ONLY = new Set(["vendor_gstin", "buyer_gstin", "cgst", "sgst", "igst"]);
const ABROAD_ONLY = new Set(["tax", "vendor_tax_id", "buyer_tax_id"]);
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
  const abroad = run.currency && run.currency !== "INR";
  const rows = Object.entries(FIELD_LABEL).filter(([k]) => {
    const f = ex.fields[k] || {};
    const empty = f.value == null || f.value === "";
    return !(empty && (abroad ? GST_ONLY.has(k) : ABROAD_ONLY.has(k)));  // hide the other system's empty fields
  }).map(([k, label]) => {
    const f = ex.fields[k] || {};
    const v = f.value == null || f.value === "" ? '<span class="muted">not found</span>' : typeof f.value === "number" ? money(f.value, run.currency) : esc(f.value);
    const c = f.value == null || f.value === "" ? "" : `<span class="conf"><i style="width:${Math.round((f.confidence || 0) * 100)}%"></i></span>${pct(f.confidence)}`;
    return `<tr><td>${label}</td><td>${v}</td><td>${c}</td><td class="muted">${esc(f.evidence || "")}</td></tr>`;
  }).join("");
  const routes = (run.route_log || []).map((l) => `${l.route}${l.skipped ? " (skipped: no model)" : ""}${l.error ? " (failed)" : ""}`).join(", then ");
  return `<p class="note" style="margin:0 0 12px">Read by <b>${esc(ex.route)}</b>. Routes tried: ${esc(routes)}.${ex.notes.length ? " " + esc(ex.notes.join("; ")) : ""}</p>
    <table><thead><tr><th>Field</th><th>Value</th><th>Confidence</th><th>Read from</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function linesTable(run) {
  const ex = run.extraction, cur = run.currency;
  const matches = Object.fromEntries((run.matches || []).map((m) => [m.line, m]));
  const tick = (ok, text) => `<span class="${ok ? "ok-mark" : "over"}">${ok ? "✓" : "✗"} ${text}</span>`;
  const rows = ex.lines.map((l, i) => {
    const m = matches[i] || {};
    const inv = `<b>${esc(l.description)}</b>${l.hsn ? `<div class="muted">HSN ${esc(l.hsn)}</div>` : ""}<div>${l.qty} × ${money(l.unit_price, cur)} = ${money(l.amount, cur)}</div>`;
    if (!m.po) return `<tr><td>${inv}</td><td colspan="3" class="muted">No PO line matches this invoice line.</td></tr>`;
    const p = m.po;
    const po = `<b>${esc(p.po_number)} line ${p.line_no}</b> ${p.doc ? `<a href="${esc(p.doc)}" target="_blank" rel="noopener">PO document</a>` : ""}
      <div>${p.ordered} ${esc(p.uom)} ordered at ${money(p.unit_price, cur)}${p.basis === "incl_tax" ? " incl. tax" : ""}</div>
      ${p.basis === "incl_tax" ? `<div class="muted">${money(p.price_pre_tax, cur)} before ${p.tax_rate}% tax</div>` : `<div class="muted">${p.tax_rate}% tax</div>`}`;
    const grn = !m.needs_receipt ? '<span class="muted">Services: nothing to receive, so this line is matched two ways (invoice and PO).</span>'
      : m.receipts.length ? m.receipts.map((r) => `<div><b>${esc(r.grn)}</b> ${esc(r.date)} ${r.doc ? `<a href="${esc(r.doc)}" target="_blank" rel="noopener">receipt</a>` : ""}<br>${r.received} received, ${r.accepted} accepted <span class="muted">(${esc(r.by)})</span></div>`).join("")
      : '<span class="over">No goods receipt yet: nothing has been accepted against this line.</span>';
    const verdict = `<div>${tick(m.price_ok, `price within ${money(m.tolerance, cur)} of the PO`)}</div>
      <div>${tick(m.qty_ok, `${l.qty} billed, ${m.available} available`)}</div>
      <div class="muted">${m.needs_receipt ? "accepted" : "ordered"} minus ${m.billed_before} already billed</div>`;
    return `<tr><td>${inv}</td><td>${po}</td><td>${grn}</td><td>${verdict}</td></tr>`;
  }).join("");
  const how = { inferred: "inferred, not printed", confirmed: "confirmed by a person", printed: "printed on the invoice" }[run.po_how] || "";
  const head = run.po_number ? `Invoice against <b>${esc(run.po_number)}</b> (${how}) and its goods receipts, all read from the ERP. Each line must agree on all three before it is paid.`
    : "No PO matched, so there is nothing to match against.";
  return `<p class="note" style="margin:0 0 12px">${head} <a href="/erp/${run.po_number ? "#/pos/" + encodeURIComponent(run.po_number) : ""}" target="_blank" rel="noopener">Open in the ERP ↗</a></p>
    <div class="wide"><table class="three-way"><thead><tr><th>Invoice</th><th>Purchase order</th><th>Goods receipt</th><th>Match</th></tr></thead><tbody>${rows || '<tr><td colspan="4" class="muted">No line items were read.</td></tr>'}</tbody></table></div>`;
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
    el.innerHTML = `<div class="dash-empty"><p>No invoices processed yet. Process one on the desk, or run all 25 samples to fill this page.</p>
      <button class="btn" id="run-all-empty">Run all 25 samples</button></div>`;
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
      <div class="kpi"><div class="k">Waiting on a person</div><div class="v">${inr0(m.amount_waiting)}</div><div class="s">${waitingN} invoice${waitingN === 1 ? "" : "s"} on hold${(m.currencies || []).length > 1 ? "; all currencies in rupees at demo rates" : ""}</div></div>
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

const OUTCOME_WORD = { APPROVE: "Approve", HOLD: "Hold", REJECT: "Reject" };

// What each sample run should have decided. Samples are designed with one outcome, with two
// honest exceptions: a sample approved earlier is a duplicate when run again (reject), and a
// scan read by OCR alone may be held for a person instead of approved.
function expectations(runs) {
  const bySample = Object.fromEntries(state.samples.map((x) => [x.id, x]));
  const out = {}, approvedBefore = new Set();
  [...runs].filter((r) => r.state === "done").sort((a, b) => a.created_at - b.created_at).forEach((r) => {
    const sm = bySample[r.sample_id];
    if (!sm || !sm.expect) return;
    let expect = sm.expect, why = "", ok;
    if (approvedBefore.has(sm.id)) { expect = "REJECT"; why = "run before and approved: a duplicate now"; }
    const got = r.first_outcome || r.outcome;  // the machine's decision, before any person acted
    ok = got === expect;
    if (!ok && expect === "APPROVE" && sm.scanned && r.route === "ocr" && got === "HOLD") {
      ok = true; why = "a scan read by OCR alone goes to a person";
    }
    if (r.outcome === "APPROVE" || ["approved", "part_approved"].includes(r.final_status)) approvedBefore.add(sm.id);
    out[r.run_id] = { expect, why, ok, got, title: sm.title };
  });
  return out;
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
  const exp = expectations(runs);
  const show = !!state.showExpected;
  const graded = list.filter((r) => exp[r.run_id]);
  const right = graded.filter((r) => exp[r.run_id].ok).length;
  $("#filters").insertAdjacentHTML("beforeend", `<button class="chip expect-toggle" id="expect-toggle" aria-pressed="${show}">${show ? "Hide" : "Check against"} expected outcomes</button>`);
  $("#expect-toggle").addEventListener("click", () => { state.showExpected = !show; renderRuns(runs); });
  const cols = show ? 10 : 7;
  $("#runs-table").innerHTML = (show ? `<p class="expect-sum">${graded.length ? `<b>${right} of ${graded.length}</b> sample runs ended as designed${right < graded.length ? `; <b class="bad-ink">${graded.length - right} did not</b> (marked below)` : ""}.` : "No sample runs in this view."} The machine's decision is compared, before any person acted on it. Uploaded files have no expected outcome.</p>` : "")
    + `<table><thead><tr><th>When</th><th>Vendor</th><th>Invoice</th><th class="num">Total</th><th>Read by</th><th>Decision</th><th>Waiting on</th>${show ? "<th>Sample</th><th>Expected</th><th>As designed?</th>" : ""}</tr></thead><tbody>
    ${list.map((r) => { const e = exp[r.run_id]; return `<tr class="clickable${show && e && !e.ok ? " miss" : ""}" data-run="${r.run_id}" tabindex="0"><td>${new Date(r.created_at * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}</td>
      <td>${esc(r.vendor_name || "unknown")}</td><td>${esc(r.invoice_number || r.file_name)}</td><td class="num">${money(r.total, r.currency)}</td>
      <td>${esc({ text: "PDF text", ocr: "OCR", llm: "model" }[r.route] || r.route || "")}</td><td>${chip(r.final_status)}</td>
      <td>${r.final_status === "on_hold" ? esc(r.owner) : ""}</td>
      ${show ? (e ? `<td title="${esc(e.title)}">${esc(r.sample_id)}</td><td>${esc(OUTCOME_WORD[e.expect])}${e.why ? `<div class="muted">${esc(e.why)}</div>` : ""}</td>
        <td>${e.ok ? '<span class="ok-mark">✓ Yes</span>' : `<span class="bad-ink">✗ No: ${esc(OUTCOME_WORD[e.got] || e.got)}</span>`}</td>` : '<td class="muted" colspan="3">uploaded file</td>') : ""}</tr>`; }).join("") || `<tr><td colspan="${cols}" class="muted">No runs match this filter.</td></tr>`}
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
  // the header stays one line: "NuExtract3 on your computer", the full name on hover
  const short = cur && cur.kind === "browser" ? cur.label.replace(/ \(.*$/, "") + (cur.connected ? "" : " (not connected)") : r.label;
  $("#model-note").textContent = `Scans are read by: ${short}`;
  $("#model-note").title = r.label;
  $("#upload-privacy").hidden = !(cur && cur.kind === "cloud");
  $("#reader-find").hidden = !r.server_scan;  // searching the server's own machine: self-hosted desks only
  const box = $("#reader-options");
  box.innerHTML = r.options.map((o) => `<label class="reader-opt" data-kind="${esc(o.kind)}">
    <input type="radio" name="reader" value="${esc(o.id)}"${o.id === r.current ? " checked" : ""}>
    <span class="reader-name">${esc(o.label)}${o.kind === "private" ? ' <span class="tag">private</span>' : ""}${o.kind === "browser" ? ` <span class="tag${o.connected ? "" : " off"}">${o.connected ? "your computer" : "not connected"}</span>` : ""}</span>
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

async function loadErpStatus() {
  const pill = $("#erp-pill");
  try {
    const st = await api("/api/erp/status");
    pill.textContent = st.error ? "ERP not reachable" : `ERP connected${st.mode === "api" ? " by API" : ""}`;
    pill.dataset.state = st.error ? "down" : "up";
    pill.title = st.error || (st.records ? `${st.records.purchaseOrders} POs, ${st.records.itemReceipts} goods receipts, ${st.records.vendors} vendors` : "");
  } catch (e) { pill.textContent = "ERP status unknown"; pill.dataset.state = "down"; }
}

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
      : "No vision model servers found on the desk's server. Start one there with serve_reader.py, or connect to its address.";
  } catch (err) { st.textContent = err.message; }
  finally { btns.forEach((b) => b.disabled = false); }
}

/* ---------------- a model on this computer (see local-model.js) ---------------- */
function renderLocal(st) {
  const on = !!(st.saved && st.connected);
  $("#local-actions").hidden = on;
  $("#local-on").hidden = !on;
  if (on) {
    const where = st.saved.url.replace(/^https?:\/\//, "");
    $("#local-on-text").innerHTML = `<b>Connected:</b> ${esc(st.saved.model)} at ${esc(where)}. ${st.busy ? `Reading ${st.busy} scan${st.busy > 1 ? "s" : ""} now. ` : ""}${st.handled ? `${st.handled} answered so far. ` : ""}Keep a desk tab open while invoices run.`;
  }
  if (st.error) $("#local-status").textContent = st.error;
}

function foundList(models) {
  const box = $("#local-found");
  box.innerHTML = models.length > 1 ? `<p class="sub">Several models answered. Choose one:</p>` + models.map((m, i) => `<button class="btn quiet" data-use="${i}" ${m.vision ? "" : "disabled"}>
      ${esc(m.model)} <span class="muted">at ${esc(m.url.replace(/^https?:\/\//, ""))}${m.vision ? "" : ", cannot read images"}</span></button>`).join(" ") : "";
  box.querySelectorAll("[data-use]").forEach((b) => b.addEventListener("click", () => useLocal(models[+b.dataset.use])));
}

async function useLocal(m) {
  const msg = $("#local-status");
  try {
    const d = await LocalModel.connect(m);
    $("#local-found").innerHTML = "";
    showReader(d);
    msg.innerHTML = `New scans will be read by ${esc(m.model)} on your computer. <button class="linklike" id="local-test">Test it now</button>`;
    $("#local-test").addEventListener("click", () => { modelCheck(); $("#model-check-result").scrollIntoView({ block: "nearest" }); });
  } catch (e) { msg.textContent = e.message; }
}

async function findLocal(address) {
  const msg = $("#local-status"), btns = [$("#local-scan"), $("#local-connect")];
  btns.forEach((b) => b.disabled = true);
  $("#local-found").innerHTML = "";
  msg.textContent = address ? `Asking ${address}...` : "Looking on this computer (your browser may ask for permission first)...";
  try {
    const r = await LocalModel.scan(address || null);
    const vision = r.models.filter((m) => m.vision);
    if (address) {
      const base = LocalModel.normalise(address);
      const here = r.models.filter((m) => m.url === base);
      if (!here.length) throw new Error(`Nothing answered at ${base}. Is the model server running there, and does it allow this site? See "If it does not work" in the instructions.`);
      const v = here.filter((m) => m.vision);
      if (!v.length) throw new Error(`${base} serves ${here.map((m) => m.model).join(", ")}, which cannot read images. Load NuExtract3, Qwen3-VL or another vision model.`);
      return v.length === 1 ? useLocal(v[0]) : foundList(here);
    }
    if (vision.length === 1) return useLocal(vision[0]);
    if (vision.length > 1) { msg.textContent = ""; return foundList(r.models); }
    if (r.permission === "denied") {
      msg.innerHTML = `Your browser has blocked this site from reaching your computer. Click the icon at the left of the address bar, open the site's settings, allow the local network permission, reload, and try again. <a href="#/how/models-trouble">Help</a>`;
    } else if (r.models.length) {
      msg.innerHTML = `Found ${esc(r.models.map((m) => `${m.model} (${m.url.replace(/^https?:\/\//, "")})`).join(", "))}, but none can read images. Load NuExtract3 or Qwen3-VL. <a href="#/how/models">How</a>`;
    } else {
      msg.innerHTML = `No model answered on this computer. Start one with the launcher and wait until it says <i>ready</i>, then try again. An Ollama started without the launcher must allow this site (OLLAMA_ORIGINS). <a href="#/how/models">Step-by-step instructions</a>`;
    }
  } catch (e) { msg.innerHTML = esc(e.message) + ` <a href="#/how/models-trouble">Help</a>`; }
  finally { btns.forEach((b) => b.disabled = false); }
}

/* ---------------- how it works ---------------- */
const EDGE_CASES = [
  ["I2", "An import into India, in euros", "A German supplier bills our Indian company in euros with no GST, which is right for an import: IGST is paid at customs. The desk checks the German VAT ID's check digit, matches the euro PO, and converts at the demo rate to apply the rupee approval limit, so 7,800 euros needs a manager."],
  ["I3", "VAT charged where reverse charge applies", "A French supplier bills our German company for a service and adds 20% French VAT. Between EU businesses this should be reverse-charged, and VAT charged this way cannot be reclaimed. The desk holds it and asks for a reissue."],
  ["I4", "Billed in the wrong currency", "The PO is in euros; the invoice has the same figures in dollars. A check that compares only numbers would pay it. The desk holds it for the currency."],
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
  const X = [20, 260, 500, 740], r1 = 30, r2 = 160, re = 262, ro = 370, w = 200;
  const arrow = (d) => `<path d="${d}" fill="none" stroke="var(--ink-3)" stroke-width="1.25" marker-end="url(#pm-arrow)"/>`;
  return `<svg viewBox="0 0 960 470" role="img" aria-label="Eight steps from file to decision, with the vendor master, purchase orders and goods receipts read from the ERP, ending in approve, hold or reject" font-family="Work Sans, system-ui, sans-serif">
    <defs><marker id="pm-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="var(--ink-3)"/></marker></defs>
    ${stepBox(X[0], r1, 1, "Receive the file", "fingerprint, open, count pages")}
    ${stepBox(X[1], r1, 2, "Read the document", "PDF text; model for scans", true)}
    ${stepBox(X[2], r1, 3, "Is it an invoice?", "quotation, credit note, unclear")}
    ${stepBox(X[3], r1, 4, "Check the document", "fields, tax numbers, totals")}
    ${stepBox(X[0], r2, 5, "Match the vendor", "approved? bank details unchanged?")}
    ${stepBox(X[1], r2, 6, "Three-way match", "PO price, goods received, qty")}
    <g><rect x="${X[0]}" y="${re}" width="${X[1] + w - X[0]}" height="56" rx="8" fill="none" stroke="var(--ink-3)" stroke-dasharray="5 4"/>
      <text x="${X[0] + 14}" y="${re + 23}" font-size="14" font-weight="700" fill="var(--ink)">Kaveri ERP</text>
      <text x="${X[0] + 14}" y="${re + 42}" font-size="12" fill="var(--ink-2)">vendor master, POs, goods receipts: read by API with a token</text></g>
    ${arrow(`M${X[0] + w / 2} ${re}V${r2 + 72}`)}${arrow(`M${X[1] + w / 2} ${re}V${r2 + 72}`)}
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
  const pick = ["S01", "E2", "E4", "I1", "I2"].map((id) => state.samples.find((x) => x.id === id)).filter(Boolean);
  $("#sample-downloads").innerHTML = pick.map((x) => `<a href="/samples/${x.id}.pdf" download>${esc(x.title)}</a>`).join(", ");
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
  $("#local-scan").addEventListener("click", () => findLocal(""));
  $("#local-form").addEventListener("submit", (e) => { e.preventDefault(); findLocal($("#local-url").value.trim()); });
  $("#local-disconnect").addEventListener("click", async () => { showReader(await LocalModel.disconnect()); $("#local-status").textContent = "Disconnected. Scans go to the reader selected above."; });
  LocalModel.onChange(renderLocal);
  LocalModel.resume();  // a connection made earlier in this browser carries on; nothing local is touched
  $("#reader-find").addEventListener("submit", (e) => { e.preventDefault(); const u = $("#reader-url").value.trim(); if (u) findReaders(u); });
  await loadReaders();
  loadErpStatus();
  await loadSamples();
  if (!location.hash) location.hash = "#/process";
  route();
})();
