/* A model on the viewer's own computer, used by a desk that may be hosted far away.
 *
 * The hosted server cannot reach this computer; this page can. It looks for model servers on
 * 127.0.0.1 only when the person presses a button (the browser may ask permission first), then
 * relays: it fetches the server's requests, calls the local model, and returns the answers.
 * The desk and the ERP pages both load this file; any open tab keeps the model connected.
 */
(function () {
  const KEY = "apflow.localModel";
  const PORTS = [...Array.from({ length: 21 }, (_, i) => 8100 + i), 11434, 1234, 8080, 8081, 8000, 8001, 30000, 5000, 5001];
  const listeners = new Set();
  const st = { saved: load(), connected: false, handled: 0, busy: 0, error: "", polling: false };

  function load() { try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { return null; } }
  function save(v) { try { v ? localStorage.setItem(KEY, JSON.stringify(v)) : localStorage.removeItem(KEY); } catch (e) { /* private mode */ } }
  function emit() { listeners.forEach((f) => { try { f(status()); } catch (e) { console.error(e); } }); }
  function status() { return { ...st, saved: st.saved && { url: st.saved.url, model: st.saved.model, flavor: st.saved.flavor } }; }

  // Same families as apflow/local_readers.py
  function family(id) {
    const m = String(id).toLowerCase();
    if (m.includes("nuextract")) return "nuextract";
    if (m.includes("qwen") && m.includes("vl")) return "qwen-vl";
    if (["-vl", "_vl", "vision", "llava", "gemma-3", "gemma3", "pixtral", "minicpm-v", "internvl", "granite-vision", "moondream", "ocr"].some((k) => m.includes(k))) return "vision";
    return null;
  }

  async function getJSON(url, ms) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms);
    try {
      const r = await fetch(url, { signal: ctl.signal, cache: "no-store" });
      if (!r.ok) return null;
      return await r.json();
    } catch (e) { return null; } finally { clearTimeout(t); }
  }

  // What is serving at base: {url, flavor, models} or null. Mirrors local_readers.probe().
  async function probe(base, ms = 1500) {
    const v = await getJSON(base + "/api/version", ms);
    if (v && typeof v === "object" && "version" in v) {
      const tags = (await getJSON(base + "/api/tags", ms)) || {};
      return { url: base, flavor: "ollama", models: (tags.models || []).map((m) => m.name || m.model).filter(Boolean) };
    }
    const d = await getJSON(base + "/v1/models", ms);
    if (d && Array.isArray(d.data)) return { url: base, flavor: "openai", models: d.data.map((m) => m.id).filter(Boolean) };
    return null;
  }

  function normalise(input) {
    let s = String(input || "").trim();
    if (!s) throw new Error("Type the model server's address, for example http://127.0.0.1:8100");
    if (/^\d+$/.test(s)) s = "127.0.0.1:" + s;
    if (!/^https?:\/\//i.test(s)) s = "http://" + s;
    const u = new URL(s);
    return `${u.protocol}//${u.host}`;
  }

  // The browser's own permission for this site to reach this computer (Chrome 142+). null if unknown.
  async function permission() {
    if (!navigator.permissions) return null;
    for (const name of ["loopback-network", "local-network-access", "local-network"]) {
      try { return (await navigator.permissions.query({ name })).state; } catch (e) { /* not this name */ }
    }
    return null;
  }

  // Look on this computer. Only call from a click: the browser may show a permission prompt.
  async function scan(extra) {
    const own = location.hostname === "localhost" || location.hostname === "127.0.0.1" ? Number(location.port || 80) : null;
    const bases = [...new Set([...(extra ? [normalise(extra)] : []), ...PORTS.filter((p) => p !== own).map((p) => `http://127.0.0.1:${p}`)])];
    const found = (await Promise.all(bases.map((b) => probe(b)))).filter(Boolean);
    const models = [];
    for (const f of found) for (const m of f.models) {
      const fam = family(m);
      models.push({ url: f.url, flavor: f.flavor, model: m, family: fam, vision: !!fam });
    }
    return { servers: found, models, permission: await permission() };
  }

  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error(d.error || `HTTP ${r.status}`); e.status = r.status; e.data = d; throw e; }
    return d;
  }

  async function connect(entry) {
    const d = await post("/api/relay/connect", { url: entry.url, model: entry.model, flavor: entry.flavor });
    st.saved = { url: entry.url, model: entry.model, flavor: entry.flavor, token: d.token };
    save(st.saved);
    st.connected = true; st.error = ""; st.handled = 0;
    emit();
    loop();
    return d;
  }

  async function disconnect() {
    const tok = st.saved && st.saved.token;
    st.saved = null; save(null); st.connected = false; st.error = "";
    emit();
    return post("/api/relay/disconnect", { token: tok || "" });
  }

  // Send one request to the local model the way apflow/local_readers.LocalClient would.
  async function callModel(req) {
    let body = { ...req.body };
    for (let attempt = 0; attempt < 2; attempt++) {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 15 * 60 * 1000);
      let r;
      try {
        r = await fetch(req.endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: ctl.signal });
      } catch (e) {
        throw new Error(`could not reach ${req.endpoint.replace("/v1/chat/completions", "")} from this page (is the model still running, and does it allow this site? see How it works, part 3)`);
      } finally { clearTimeout(t); }
      if ((r.status === 400 || r.status === 422) && attempt === 0) {
        for (const k of req.retry_without || []) delete body[k];
        continue;
      }
      const text = await r.text();
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${text.slice(0, 200)}`);
      let data;
      try { data = JSON.parse(text); } catch (e) { throw new Error("the model server did not answer with JSON"); }
      let content = data && data.choices && data.choices[0] && data.choices[0].message && data.choices[0].message.content;
      if (Array.isArray(content)) content = content.map((p) => (p && p.text) || "").join("");
      if (typeof content !== "string") throw new Error("unexpected answer: " + text.slice(0, 200));
      return content;
    }
  }

  async function handle(req, token) {
    st.busy++; emit();
    let content = null, error = null;
    try { content = await callModel(req); } catch (e) { error = e.message || String(e); st.error = error; }
    try {
      await post("/api/relay/answer", { token, id: req.id, content, error });
      if (!error) { st.handled++; st.error = ""; }
    } catch (e) { /* the server restarted; the run already gave up */ }
    st.busy--; emit();
  }

  // Long-poll the desk for requests. A chain of fetches, not timers, so a background tab keeps up.
  async function loop() {
    if (st.polling) return;
    st.polling = true;
    let failures = 0;
    try {
      while (st.saved && st.saved.token) {
        const token = st.saved.token;
        try {
          const d = await post("/api/relay/next", { token, wait: 15 });
          failures = 0;
          if (!st.connected) { st.connected = true; emit(); }
          if (d.request) handle(d.request, token);  // not awaited: keep the heartbeat going meanwhile
        } catch (e) {
          if (e.status === 409) {  // the desk restarted, or another page connected a model
            st.connected = false;
            st.error = "The desk no longer has this model connected (it restarted, or another tab connected one). Connect it again.";
            st.saved = null; save(null); emit();
            return;
          }
          failures++;
          if (st.connected) { st.connected = false; emit(); }
          await new Promise((r) => setTimeout(r, Math.min(15000, 1000 * failures)));
        }
      }
    } finally { st.polling = false; }
  }

  // A page that loads with a saved connection resumes relaying. This talks only to the desk,
  // never to 127.0.0.1, so nothing local is touched until a scan actually needs the model.
  function resume() { if (st.saved && st.saved.token) loop(); }

  window.LocalModel = { scan, connect, disconnect, resume, status, family, normalise, permission, onChange: (f) => { listeners.add(f); f(status()); } };
})();
