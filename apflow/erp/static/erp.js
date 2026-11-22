"use strict";
/* Kaveri ERP screens. Records live in the ERP; the invoice desk reads them through the API. */
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const money = (n, cur) => n == null ? "–" : new Intl.NumberFormat(!cur || cur === "INR" ? "en-IN" : "en-US", { style: "currency", currency: cur || "INR" }).format(n);
const qty = (n) => n == null ? "–" : (+n).toLocaleString("en-IN", { maximumFractionDigits: 3 });
const COMPANIES = { IN: "Kaveri Instruments Pvt Ltd (India)", US: "Kaveri Instruments Inc. (US)", DE: "Kaveri Instruments GmbH (Germany)" };
const SOURCE = { seed: "demo", tester: "tester", reference: "reference" };

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `Request failed (${r.status})`);
  return body;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const tag = (src) => `<span class="tag ${esc(src)}">${esc(SOURCE[src] || src)}</span>`;
const docLink = (id, text = "Open document") => id ? `<a href="/erp/docs/${esc(id)}" target="_blank" rel="noopener">${esc(text)}</a>` : '<span class="muted">no document</span>';

/* ---------------- routes ---------------- */
async function route() {
  const h = location.hash.replace(/^#\/?/, "");
  const [path, query] = h.split("?");
  const parts = path.split("/").filter(Boolean);
  const q = new URLSearchParams(query || "");
  document.querySelectorAll(".bar nav a").forEach((a) => a.classList.toggle("on", a.dataset.nav === (parts[0] || "")));
  const main = $("#main");
  try {
    if (!parts.length) await overview(main);
    else if (parts[0] === "pos" && parts[1] === "new") await newPO(main);
    else if (parts[0] === "pos" && parts[1]) await poDetail(main, decodeURIComponent(parts[1]));
    else if (parts[0] === "pos") await poList(main);
    else if (parts[0] === "receipts" && parts[1] === "new") await newReceipt(main, q.get("po"));
    else if (parts[0] === "receipts") await receiptList(main);
    else if (parts[0] === "vendors" && parts[1] === "new") await newVendor(main);
    else if (parts[0] === "vendors") await vendorList(main);
    else if (parts[0] === "integration") await integration(main);
    else main.innerHTML = '<h1>Not found</h1><p><a href="#/">Back to the overview</a></p>';
  } catch (e) {
    main.innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  }
  main.focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);

/* ---------------- overview ---------------- */
async function overview(main) {
  const o = await api("/erp/ui/overview");
  const when = (t) => new Date(t * 1000).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
  main.innerHTML = `
    <h1>Kaveri ERP</h1>
    <p class="lead">The group's system of record. Purchasing raises purchase orders here and Stores record what arrives and is accepted. The invoice desk does not keep its own copy: it reads vendors, POs and goods receipts from here, through the ERP's API, to match every invoice three ways.</p>
    <section class="panel" aria-labelledby="t-own">
      <h2 id="t-own">Test the desk with your own invoice</h2>
      <p class="muted" style="margin:0">An invoice is paid only if its PO and goods receipt agree with it. Put them here first, then upload the invoice.</p>
      <ol class="steps">
        <li><b>Add the vendor</b><p>Skip this if the supplier is already listed. Indian suppliers need a valid GSTIN.</p><a class="btn" href="#/vendors/new">Add a vendor</a></li>
        <li><b>Add the purchase order</b><p>Upload the PO's PDF. The ERP reads it; you check and save.</p><a class="btn primary" href="#/pos/new">Add a PO</a></li>
        <li><b>Record the goods receipt</b><p>Upload the receipt, or enter what arrived and what was accepted.</p><a class="btn primary" href="#/receipts/new">Record a receipt</a></li>
        <li><b>Upload the invoice</b><p>At the invoice desk. It checks the invoice against this PO and receipt.</p><a class="btn" href="/" target="_blank" rel="noopener">Open the invoice desk ↗</a></li>
      </ol>
    </section>
    <div class="stats">
      <a class="stat" href="#/vendors" style="text-decoration:none;color:inherit"><span class="muted">Vendors</span><b>${o.counts.vendors}</b></a>
      <a class="stat" href="#/pos" style="text-decoration:none;color:inherit"><span class="muted">Purchase orders</span><b>${o.counts.purchase_orders}</b></a>
      <a class="stat" href="#/receipts" style="text-decoration:none;color:inherit"><span class="muted">Goods receipts</span><b>${o.counts.receipts}</b></a>
    </div>
    <section class="panel">
      <div class="row spread"><h2>Recent activity</h2>
        <span id="reset-box"><button class="btn danger" id="reset">Remove tester records</button></span></div>
      <div class="wide"><table><thead><tr><th>When</th><th>Who</th><th>What</th><th>Record</th></tr></thead><tbody>
      ${o.audit.map((a) => `<tr><td>${when(a.at)}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td><td>${esc(a.record)}</td></tr>`).join("")}
      </tbody></table></div>
      <p class="msg" id="reset-msg" role="status"></p>
    </section>`;
  $("#reset").addEventListener("click", () => {
    $("#reset-box").innerHTML = 'Remove everything testers added? <button class="btn danger" id="reset-yes">Remove</button> <button class="btn" id="reset-no">Keep</button>';
    $("#reset-no").onclick = () => overview(main);
    $("#reset-yes").onclick = async () => { await post("/erp/ui/reset", {}); overview(main); };
  });
}

/* ---------------- purchase orders ---------------- */
function receivedTag(p) {
  if (p.external_buyer) return '<span class="tag reference">another buyer</span>';
  if (p.kind === "services") return '<span class="tag">service: no receipt</span>';
  const full = p.lines.filter((l) => l.accepted >= l.qty_ordered - 1e-9).length;
  const any = p.lines.some((l) => l.received > 0);
  if (full === p.lines.length) return '<span class="tag ok">all received</span>';
  return any ? `<span class="tag part">${full} of ${p.lines.length} lines</span>` : '<span class="tag none">nothing yet</span>';
}

async function poList(main) {
  const pos = await api("/erp/ui/pos");
  main.innerHTML = `<div class="row spread"><div><h1>Purchase orders</h1><p class="lead">What each company ordered, from whom, at what price. Invoices are matched against these lines.</p></div>
    <a class="btn primary" href="#/pos/new">Add a purchase order</a></div>
    <div class="panel wide"><table><thead><tr><th>PO</th><th>Supplier</th><th>Company</th><th>Date</th><th class="num">Value (pre-tax)</th><th>Goods received</th><th>Status</th><th>Source</th></tr></thead><tbody>
    ${pos.map((p) => `<tr class="click" data-href="#/pos/${encodeURIComponent(p.po_number)}"><td><a href="#/pos/${encodeURIComponent(p.po_number)}">${esc(p.po_number)}</a></td>
      <td>${esc(p.vendor_name)}</td><td>${esc(p.external_buyer || COMPANIES[p.entity] || p.entity)}</td><td>${esc(p.po_date)}</td>
      <td class="num">${money(p.value, p.currency)}</td><td>${receivedTag(p)}</td><td>${esc(p.status)}</td><td>${tag(p.source)}</td></tr>`).join("")}
    </tbody></table></div>`;
  main.querySelectorAll("tr.click").forEach((tr) => tr.addEventListener("click", (e) => { if (e.target.tagName !== "A") location.hash = tr.dataset.href; }));
}

async function poDetail(main, num) {
  const [pos, recs] = await Promise.all([api("/erp/ui/pos"), api("/erp/ui/receipts")]);
  const p = pos.find((x) => x.po_number === num);
  if (!p) { main.innerHTML = `<p class="msg err">No purchase order ${esc(num)}.</p>`; return; }
  const mine = recs.filter((r) => r.po_number === num);
  main.innerHTML = `<p><a href="#/pos">← Purchase orders</a></p>
    <div class="row spread"><h1>${esc(p.po_number)}</h1>
      ${p.external_buyer || p.status !== "open" ? "" : `<a class="btn primary" href="#/receipts/new?po=${encodeURIComponent(p.po_number)}">Record a goods receipt</a>`}</div>
    ${p.external_buyer ? `<p class="read">A real public order placed by ${esc(p.external_buyer)}, kept for reference with its original document. It is not one of our orders, so the invoice desk does not match invoices against it.</p>` : ""}
    <section class="panel"><dl class="kv">
      <dt>Supplier</dt><dd>${esc(p.vendor_name)} (${esc(p.vendor_id)})</dd>
      <dt>Ordered by</dt><dd>${esc(p.external_buyer || COMPANIES[p.entity] || p.entity)}</dd>
      <dt>Date</dt><dd>${esc(p.po_date)}</dd><dt>Kind</dt><dd>${esc(p.kind)}${p.kind === "services" ? " (no goods receipt needed; the desk matches two ways)" : ""}</dd>
      <dt>Status</dt><dd>${esc(p.status)}</dd><dt>Source</dt><dd>${tag(p.source)}</dd><dt>Document</dt><dd>${docLink(p.doc_id, "Open the PO document")}</dd>
    </dl></section>
    <section class="panel wide"><h2>Lines</h2><table><thead><tr><th>#</th><th>Description</th><th>HSN</th><th class="num">Ordered</th><th class="num">Rate</th><th>Prices</th><th class="num">Tax</th><th class="num">Received</th><th class="num">Accepted</th><th class="num">Still to come</th></tr></thead><tbody>
    ${p.lines.map((l) => `<tr><td>${l.line_no}</td><td>${esc(l.description)}</td><td>${esc(l.hsn || "")}</td><td class="num">${qty(l.qty_ordered)} ${esc(l.uom)}</td>
      <td class="num">${money(l.unit_price, p.currency)}</td><td>${l.price_basis === "incl_tax" ? "incl. tax" : "excl. tax"}</td><td class="num">${l.tax_rate}%</td>
      <td class="num">${l.requires_receipt ? qty(l.received) : "–"}</td><td class="num">${l.requires_receipt ? qty(l.accepted) : "–"}</td><td class="num">${l.requires_receipt ? qty(Math.max(0, l.qty_ordered - l.received)) : "–"}</td></tr>`).join("")}
    </tbody></table></section>
    <section class="panel wide"><h2>Goods receipts against this PO</h2>
    ${mine.length ? `<table><thead><tr><th>Receipt</th><th>Date</th><th>By</th><th>Lines</th><th>Document</th></tr></thead><tbody>
      ${mine.map((r) => `<tr><td>${esc(r.receipt_no)}</td><td>${esc(r.receipt_date)}</td><td>${esc(r.received_by)}</td>
        <td>${r.lines.map((l) => `line ${l.line_no}: ${qty(l.received)} received, ${qty(l.accepted)} accepted`).join("<br>")}</td><td>${docLink(r.doc_id)}</td></tr>`).join("")}
      </tbody></table>` : '<p class="muted">None yet.</p>'}</section>`;
}

/* reading an uploaded document into a draft */
function dropZone(el, kind, onRead) {
  el.innerHTML = `<div class="drop" id="drop-${kind}"><p style="margin:0 0 8px">Drop the ${kind === "po" ? "purchase order" : "goods receipt"} PDF here, or</p>
    <label class="btn">Choose a PDF<input type="file" accept="application/pdf" hidden></label>
    <p class="msg" role="status"></p></div>`;
  const zone = el.querySelector(".drop"), input = el.querySelector("input"), msg = el.querySelector(".msg");
  async function send(file) {
    msg.className = "msg"; msg.textContent = `Reading ${file.name}...`;
    const fd = new FormData(); fd.append("file", file);
    try { const r = await api(`/erp/ui/read/${kind === "po" ? "po" : "receipt"}`, { method: "POST", body: fd }); msg.textContent = ""; onRead(r, file.name); }
    catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
  }
  input.addEventListener("change", () => input.files[0] && send(input.files[0]));
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("over"); e.dataTransfer.files[0] && send(e.dataTransfer.files[0]); });
}

async function newPO(main) {
  const vendors = (await api("/erp/ui/vendors")).filter((v) => v.source !== "reference");
  main.innerHTML = `<p><a href="#/pos">← Purchase orders</a></p><h1>Add a purchase order</h1>
    <p class="lead">Upload the PO and the ERP reads it into the form below. Check every field before saving: the invoice desk will hold invoices to exactly what is saved here.</p>
    <section class="panel" id="upload"></section>
    <form class="panel" id="po-form" novalidate>
      <p class="read" id="read-by" hidden></p>
      <div class="grid">
        <label class="f">PO number<input name="po_number" placeholder="Blank: numbered automatically"></label>
        <label class="f">PO date<input name="po_date" type="date" required></label>
        <label class="f">Ordered by<select name="entity">${Object.entries(COMPANIES).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select></label>
        <label class="f">Currency<select name="currency">${["INR", "USD", "EUR", "GBP"].map((c) => `<option>${c}</option>`).join("")}</select></label>
        <label class="f">Kind<select name="kind"><option value="goods">Goods (needs a goods receipt)</option><option value="services">Services (no goods receipt)</option></select></label>
        <label class="f">Prices on the PO are<select name="price_basis"><option value="excl_tax">Excluding tax</option><option value="incl_tax">Including tax</option></select></label>
      </div>
      <h2 style="margin-top:18px">Supplier</h2>
      <div class="grid">
        <label class="f">Vendor<select name="vendor_id"><option value="">New vendor (enter below)</option>
          ${vendors.map((v) => `<option value="${esc(v.vendor_id)}">${esc(v.name)} (${esc(v.vendor_id)})</option>`).join("")}</select></label>
      </div>
      <div class="grid" id="new-vendor" style="margin-top:12px">
        <label class="f">Vendor name<input name="v_name"></label>
        <label class="f">Country<select name="v_country"><option value="IN">India</option><option value="US">United States</option><option value="DE">Germany</option><option value="FR">France</option><option value="GB">United Kingdom</option></select></label>
        <label class="f">GSTIN (India) or tax number (EIN, VAT ID)<input name="v_taxno"></label>
        <label class="f">Bank account or IBAN<input name="v_bank" placeholder="As on their invoices"></label>
        <label class="f">Bank code (IFSC, BIC or routing)<input name="v_code"></label>
      </div>
      <h2 style="margin-top:18px">Lines</h2>
      <div class="wide"><table><thead><tr><th>#</th><th>Description</th><th>HSN</th><th class="num">Qty</th><th>Unit</th><th class="num">Rate</th><th class="num">Tax %</th><th></th></tr></thead><tbody id="lines"></tbody></table></div>
      <div class="row" style="margin-top:10px"><button class="btn" type="button" id="add-line">Add a line</button></div>
      <div class="row" style="margin-top:18px"><button class="btn primary" type="submit">Save purchase order</button><a class="btn" href="#/pos">Cancel</a></div>
      <p class="msg" id="po-msg" role="alert"></p>
    </form>`;
  const form = $("#po-form");
  let docId = null;
  const lineRow = (l = {}, i = 1) => `<tr><td>${i}</td><td><input name="d" value="${esc(l.description || "")}" aria-label="Description"></td>
    <td><input name="h" value="${esc(l.hsn || "")}" style="width:80px" aria-label="HSN"></td><td><input class="n" name="q" value="${l.qty_ordered ?? ""}" style="width:80px" aria-label="Quantity"></td>
    <td><input name="u" value="${esc(l.uom || "pcs")}" style="width:64px" aria-label="Unit"></td><td><input class="n" name="r" value="${l.unit_price ?? ""}" style="width:110px" aria-label="Rate"></td>
    <td><input class="n" name="t" value="${l.tax_rate ?? 18}" style="width:64px" aria-label="Tax percent"></td><td><button class="btn" type="button" data-rm aria-label="Remove line">×</button></td></tr>`;
  const renumber = () => [...$("#lines").rows].forEach((r, i) => (r.cells[0].textContent = i + 1));
  $("#lines").innerHTML = lineRow();
  $("#add-line").onclick = () => { $("#lines").insertAdjacentHTML("beforeend", lineRow({}, $("#lines").rows.length + 1)); };
  $("#lines").addEventListener("click", (e) => { if (e.target.dataset.rm !== undefined && $("#lines").rows.length > 1) { e.target.closest("tr").remove(); renumber(); } });
  const toggleVendor = () => ($("#new-vendor").hidden = !!form.vendor_id.value);
  form.vendor_id.addEventListener("change", toggleVendor);
  form.po_date.value = new Date().toISOString().slice(0, 10);
  dropZone($("#upload"), "po", ({ doc_id, draft }, fname) => {
    docId = doc_id;
    $("#read-by").hidden = false;
    $("#read-by").textContent = `Read ${fname} by ${draft.read_by}. ${draft.notes.join(" ")} Check each field, then save.`;
    if (draft.po_number) form.po_number.value = draft.po_number;
    if (draft.po_date) form.po_date.value = draft.po_date;
    if (draft.entity) form.entity.value = draft.entity;
    if (draft.currency) form.currency.value = draft.currency;
    form.kind.value = draft.kind || "goods"; form.price_basis.value = draft.price_basis || "excl_tax";
    const v = draft.vendor || {};
    form.vendor_id.value = v.match || "";
    form.v_name.value = v.name || ""; form.v_taxno.value = v.gstin || v.tax_id || ""; form.v_country.value = v.country || "IN";
    toggleVendor();
    if (draft.lines.length) $("#lines").innerHTML = draft.lines.map((l, i) => lineRow(l, i + 1)).join("");
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#po-msg"); msg.className = "msg"; msg.textContent = "Saving...";
    const lines = [...$("#lines").rows].map((r, i) => ({ line_no: i + 1, description: r.querySelector('[name="d"]').value, hsn: r.querySelector('[name="h"]').value,
      qty_ordered: r.querySelector('[name="q"]').value, uom: r.querySelector('[name="u"]').value, unit_price: r.querySelector('[name="r"]').value,
      tax_rate: r.querySelector('[name="t"]').value, price_basis: form.price_basis.value })).filter((l) => l.description || l.qty_ordered);
    const country = form.v_country.value, taxno = form.v_taxno.value.trim();
    const body = { po_number: form.po_number.value.trim(), po_date: form.po_date.value, entity: form.entity.value, currency: form.currency.value,
      kind: form.kind.value, vendor_id: form.vendor_id.value, lines, doc_id: docId,
      new_vendor: form.vendor_id.value ? null : { name: form.v_name.value, country, gstin: country === "IN" ? taxno : "", tax_id: country === "IN" ? "" : taxno,
        currency: { IN: "INR", US: "USD", GB: "GBP" }[country] || "EUR", bank_account: form.v_bank.value, bank_code: form.v_code.value } };
    try {
      const p = await post("/erp/ui/pos", body);
      location.hash = `#/pos/${encodeURIComponent(p.po_number)}`;
    } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
  });
}

/* ---------------- goods receipts ---------------- */
async function receiptList(main) {
  const recs = await api("/erp/ui/receipts");
  main.innerHTML = `<div class="row spread"><div><h1>Goods receipts</h1><p class="lead">What Stores counted and accepted against each PO. Only accepted quantities can be invoiced.</p></div>
    <a class="btn primary" href="#/receipts/new">Record a goods receipt</a></div>
    <div class="panel wide"><table><thead><tr><th>Receipt</th><th>PO</th><th>Supplier</th><th>Date</th><th>What arrived</th><th>By</th><th>Source</th><th>Document</th></tr></thead><tbody>
    ${recs.map((r) => `<tr><td>${esc(r.receipt_no)}</td><td><a href="#/pos/${encodeURIComponent(r.po_number)}">${esc(r.po_number)}</a></td><td>${esc(r.vendor_name || "")}</td><td>${esc(r.receipt_date)}</td>
      <td>${r.lines.map((l) => `${esc(l.description || "line " + l.line_no)}: ${qty(l.received)} received, ${qty(l.accepted)} accepted`).join("<br>")}</td>
      <td>${esc(r.received_by)}</td><td>${tag(r.source)}</td><td>${docLink(r.doc_id)}</td></tr>`).join("")}
    </tbody></table></div>`;
}

async function newReceipt(main, poParam) {
  const pos = (await api("/erp/ui/pos")).filter((p) => p.status === "open" && !p.external_buyer);
  main.innerHTML = `<p><a href="#/receipts">← Goods receipts</a></p><h1>Record a goods receipt</h1>
    <p class="lead">What arrived against a PO, and how much passed inspection. Upload the receipt to fill the form, or enter the quantities.</p>
    <section class="panel" id="upload"></section>
    <form class="panel" id="r-form" novalidate>
      <p class="read" id="read-by" hidden></p>
      <div class="grid">
        <label class="f">Against PO<select name="po_number"><option value="">Choose a PO</option>${pos.map((p) => `<option value="${esc(p.po_number)}">${esc(p.po_number)} · ${esc(p.vendor_name)}</option>`).join("")}</select></label>
        <label class="f">Receipt number<input name="receipt_no" placeholder="Blank: numbered automatically"></label>
        <label class="f">Date received<input name="receipt_date" type="date"></label>
        <label class="f">Received and inspected by<input name="received_by" value="Stores"></label>
      </div>
      <div class="wide" style="margin-top:16px"><table><thead><tr><th>#</th><th>Description</th><th class="num">Ordered</th><th class="num">Received before</th><th class="num">Received now</th><th class="num">Accepted now</th></tr></thead><tbody id="r-lines"><tr><td colspan="6" class="muted">Choose a PO to see its lines.</td></tr></tbody></table></div>
      <div class="row" style="margin-top:18px"><button class="btn primary" type="submit">Save goods receipt</button><a class="btn" href="#/receipts">Cancel</a></div>
      <p class="msg" id="r-msg" role="alert"></p>
    </form>`;
  const form = $("#r-form");
  form.receipt_date.value = new Date().toISOString().slice(0, 10);
  let docId = null, readLines = {};
  function showLines() {
    const p = pos.find((x) => x.po_number === form.po_number.value);
    if (!p) { $("#r-lines").innerHTML = '<tr><td colspan="6" class="muted">Choose a PO to see its lines.</td></tr>'; return; }
    $("#r-lines").innerHTML = p.lines.map((l) => {
      const got = readLines[l.line_no] || {};
      return `<tr data-line="${l.line_no}"><td>${l.line_no}</td><td>${esc(l.description)}</td><td class="num">${qty(l.qty_ordered)} ${esc(l.uom)}</td><td class="num">${qty(l.received)}</td>
        <td><input class="n" name="rec" value="${got.received ?? ""}" style="width:90px" aria-label="Received now, line ${l.line_no}"></td>
        <td><input class="n" name="acc" value="${got.accepted ?? ""}" style="width:90px" aria-label="Accepted now, line ${l.line_no}"></td></tr>`;
    }).join("");
    $("#r-lines").querySelectorAll('[name="rec"]').forEach((inp) => inp.addEventListener("input", () => {
      const acc = inp.closest("tr").querySelector('[name="acc"]'); if (!acc.dataset.touched) acc.value = inp.value;
    }));
    $("#r-lines").querySelectorAll('[name="acc"]').forEach((inp) => inp.addEventListener("input", () => (inp.dataset.touched = "1")));
  }
  form.po_number.addEventListener("change", showLines);
  if (poParam) { form.po_number.value = poParam; showLines(); }
  dropZone($("#upload"), "receipt", ({ doc_id, draft }, fname) => {
    docId = doc_id;
    $("#read-by").hidden = false;
    const known = pos.some((p) => p.po_number === draft.po_number);
    $("#read-by").textContent = `Read ${fname} by ${draft.read_by}. ${draft.notes.join(" ")}${draft.po_number && !known ? ` It names ${draft.po_number}, which is not an open PO here: add that PO first.` : ""} Check the quantities, then save.`;
    if (known) form.po_number.value = draft.po_number;
    if (draft.receipt_no) form.receipt_no.value = draft.receipt_no;
    if (draft.receipt_date) form.receipt_date.value = draft.receipt_date;
    if (draft.received_by) form.received_by.value = draft.received_by;
    readLines = Object.fromEntries((draft.lines || []).filter((l) => l.line_no).map((l) => [l.line_no, l]));
    showLines();
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("#r-msg"); msg.className = "msg"; msg.textContent = "Saving...";
    const lines = [...$("#r-lines").querySelectorAll("tr[data-line]")].map((r) => ({ line_no: +r.dataset.line,
      received: r.querySelector('[name="rec"]').value || 0, accepted: r.querySelector('[name="acc"]').value || 0 }));
    try {
      const r = await post("/erp/ui/receipts", { po_number: form.po_number.value, receipt_no: form.receipt_no.value.trim(), receipt_date: form.receipt_date.value,
        received_by: form.received_by.value, lines, doc_id: docId });
      location.hash = `#/pos/${encodeURIComponent(r.po_number)}`;
    } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
  });
}

/* ---------------- vendors ---------------- */
async function vendorList(main) {
  const vs = await api("/erp/ui/vendors");
  main.innerHTML = `<div class="row spread"><div><h1>Vendors</h1><p class="lead">The vendor master: who may be paid, and to which bank account. An invoice whose bank details differ from these is held.</p></div>
    <a class="btn primary" href="#/vendors/new">Add a vendor</a></div>
    <div class="panel wide"><table><thead><tr><th>ID</th><th>Name</th><th>Country</th><th>GSTIN or tax number</th><th>Currency</th><th>Bank account</th><th>Bank code</th><th>Status</th><th>Source</th></tr></thead><tbody>
    ${vs.map((v) => `<tr><td>${esc(v.vendor_id)}</td><td>${esc(v.name)}</td><td>${esc(v.country)}</td><td>${esc(v.gstin || v.tax_id || "")}</td><td>${esc(v.currency)}</td>
      <td>${esc(v.bank_account)}</td><td>${esc(v.bank_code)}</td><td>${esc(v.status)}</td><td>${tag(v.source)}</td></tr>`).join("")}
    </tbody></table></div>`;
}

async function newVendor(main) {
  main.innerHTML = `<p><a href="#/vendors">← Vendors</a></p><h1>Add a vendor</h1>
    <p class="lead">Copy the bank details from the supplier's invoice or onboarding form: the desk pays only to the account on file.</p>
    <form class="panel" id="v-form" novalidate><div class="grid">
      <label class="f">Name<input name="name" required></label>
      <label class="f">Country<select name="country"><option value="IN">India</option><option value="US">United States</option><option value="DE">Germany</option><option value="FR">France</option><option value="GB">United Kingdom</option></select></label>
      <label class="f">GSTIN (India) or tax number (EIN, VAT ID)<input name="taxno"></label>
      <label class="f">Bank account or IBAN<input name="bank_account"></label>
      <label class="f">Bank code (IFSC, BIC or routing)<input name="bank_code"></label>
      <label class="f">Payment terms (days)<input name="terms_days" value="30" inputmode="numeric"></label>
    </div><div class="row" style="margin-top:16px"><button class="btn primary">Save vendor</button><a class="btn" href="#/vendors">Cancel</a></div>
    <p class="msg" id="v-msg" role="alert"></p></form>`;
  $("#v-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, country = f.country.value, taxno = f.taxno.value.trim();
    try {
      await post("/erp/ui/vendors", { name: f.name.value, country, gstin: country === "IN" ? taxno : "", tax_id: country === "IN" ? "" : taxno,
        currency: { IN: "INR", US: "USD", GB: "GBP" }[country] || "EUR", bank_account: f.bank_account.value, bank_code: f.bank_code.value, terms_days: f.terms_days.value });
      location.hash = "#/vendors";
    } catch (err) { $("#v-msg").className = "msg err"; $("#v-msg").textContent = err.message; }
  });
}

/* ---------------- integration ---------------- */
async function integration(main) {
  const o = await api("/erp/ui/overview"), i = o.integration;
  const when = i.desk_last_sync ? new Date(i.desk_last_sync * 1000).toLocaleString("en-IN", { timeStyle: "medium", dateStyle: "medium" }) : "not yet";
  main.innerHTML = `<h1>Integration</h1>
    <p class="lead">How the invoice desk reads this ERP: the way an AP automation product connects to a client's NetSuite or SAP. The client's administrator issues an integration token; the product reads vendors, POs and receipts through the API with it, and never sees the ERP's screens.</p>
    <section class="panel"><dl class="kv">
      <dt>API base</dt><dd><code>${esc(i.base)}</code></dd>
      <dt>Records</dt><dd><code>vendor</code>, <code>purchaseOrder</code>, <code>itemReceipt</code>; receive goods with <code>POST purchaseOrder/{id}/!transform/itemReceipt</code></dd>
      <dt>Token</dt><dd><code>${esc(i.token_hint)}</code> (held by the desk as <code>APFLOW_ERP_TOKEN</code>; requests without it get 401)</dd>
      <dt>Invoice desk</dt><dd>${i.desk_mode === "api" ? `reads over HTTP from <code>${esc(i.desk_url)}</code>` : "reads this ERP's database directly (local mode)"}; last read ${esc(when)}; ${i.desk_calls} API calls so far</dd>
    </dl></section>
    <section class="panel"><h2>Try it</h2><pre>curl -H "Authorization: Bearer &lt;token&gt;" ${esc(i.base)}/purchaseOrder/PO-2026-0101</pre>
      <p class="muted" style="margin:8px 0 0">NetSuite's equivalent is <code>/services/rest/record/v1/purchaseOrder</code> with a token-based or OAuth 2.0 integration; SAP S/4HANA's is the <code>API_PURCHASEORDER_PROCESS_SRV</code> and <code>API_MATERIAL_DOCUMENT_SRV</code> OData services. Connecting either means writing one client with the same snapshot of vendors, POs and receipts.</p></section>`;
}

route();
