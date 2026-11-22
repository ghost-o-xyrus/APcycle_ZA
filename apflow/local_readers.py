"""Private readers: vision models running on the company's own machines.

Two models are supported by name, and any other vision model on the same kind of server works too:
  NuExtract3 (Numind, Apache-2.0, ~4B): built for template-based extraction. The JSON
    template is passed as `chat_template_kwargs.template`, as its model card says.
  Qwen3-VL (open weights, Apache-2.0): a general vision model; it gets the same prompt as
    the cloud reader.

Servers: anything with an OpenAI-style /v1/chat/completions endpoint (vLLM, SGLang,
llama.cpp's llama-server, LM Studio), and Ollama (found by its /api/version endpoint).

Finding servers: scripts/serve_reader.py writes one small JSON file per server it starts
into the registry folder (~/.apflow/readers, or APFLOW_READERS_DIR), whatever port it got.
Discovery reads those first, then tries the ports these servers use by default.

Safety: the app only talks to this machine (localhost) unless a host is listed in
APFLOW_READER_HOSTS, so a public deployment cannot be used to probe other networks.
Local requests never go through a proxy.
"""
from __future__ import annotations

import glob
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .models import MAX_PAGES, ModelError, _json_text, _png_b64, _post

DEFAULT_PORTS = [11434,            # Ollama
                 8000, 8001,       # vLLM
                 8080, 8081,       # llama.cpp llama-server
                 1234,             # LM Studio
                 30000,            # SGLang
                 5000, 5001]
LAUNCHER_PORTS = range(8100, 8121)  # where scripts/serve_reader.py looks for a free port first
LOOPBACK = {"localhost", "127.0.0.1", "::1"}

# NuExtract template: same keys as the cloud reader's JSON, so both map through one function.
NUEXTRACT_TEMPLATE = {
    "doc_type": ["invoice", "credit_note", "debit_note", "quotation", "purchase_order", "receipt", "delivery_challan", "unknown"],
    "vendor_name": "verbatim-string",
    "vendor_gstin": "verbatim-string",
    "buyer_gstin": "verbatim-string",
    "invoice_number": "verbatim-string",
    "invoice_date": "date",
    "po_reference": "verbatim-string",
    "subtotal": "number",
    "cgst": "number",
    "sgst": "number",
    "igst": "number",
    "total": "number",
    "bank_account": "verbatim-string",
    "ifsc": "verbatim-string",
    "currency": ["INR", "USD", "EUR", "GBP", "JPY", "other"],
    "lines": [{"description": "verbatim-string", "hsn": "verbatim-string", "qty": "number",
               "uom": "verbatim-string", "unit_price": "number", "amount": "number"}],
}
NUEXTRACT_INSTRUCTIONS = (
    "A supplier tax invoice from India. vendor_gstin is the seller's GSTIN, buyer_gstin the bill-to GSTIN. "
    "subtotal is the pre-tax total; cgst, sgst and igst are tax amounts in rupees, not rates; total includes tax. "
    "unit_price and amount are pre-tax. po_reference is the buyer's purchase order number if printed. "
    "Numbers carry no currency symbol or thousands separators.")

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def registry_dir(env=os.environ) -> str:
    return env.get("APFLOW_READERS_DIR") or os.path.join(os.path.expanduser("~"), ".apflow", "readers")


def allowed_hosts(env=os.environ) -> set[str]:
    extra = {h.strip().lower() for h in env.get("APFLOW_READER_HOSTS", "").split(",") if h.strip()}
    return LOOPBACK | extra


def normalise_url(url: str, env=os.environ) -> str:
    """'8137', '127.0.0.1:8137' or 'http://localhost:8137/v1' -> 'http://127.0.0.1:8137'. Refuses other hosts."""
    u = (url or "").strip()
    if not u:
        raise ValueError("Give a server address, for example http://127.0.0.1:8137")
    if u.isdigit():
        u = f"127.0.0.1:{u}"
    if "://" not in u:
        u = "http://" + u
    p = urllib.parse.urlsplit(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"Not a server address: {url}")
    host = p.hostname.lower()
    if host not in allowed_hosts(env):
        raise ValueError(f"{host} is not this machine. To use a model server on another machine, "
                         "list its host in APFLOW_READER_HOSTS and restart the app.")
    if not p.port:
        raise ValueError("Add the port, for example http://127.0.0.1:8137")
    shown = "127.0.0.1" if host == "localhost" else (f"[{host}]" if ":" in host else host)
    return f"{p.scheme}://{shown}:{p.port}"


def _get_json(url: str, timeout: float, opener=None):
    opener = opener or _NO_PROXY.open
    try:
        with opener(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def family(model_id: str) -> str | None:
    """Which prompt style a model needs; None for models that cannot read images."""
    m = model_id.lower()
    if "nuextract" in m:
        return "nuextract"
    if "qwen" in m and "vl" in m:
        return "qwen-vl"
    if any(k in m for k in ("-vl", "_vl", "vision", "llava", "gemma-3", "gemma3", "pixtral", "minicpm-v",
                            "internvl", "granite-vision", "moondream", "ocr")):
        return "vision"
    return None


FAMILY_NAMES = {"nuextract": "NuExtract3", "qwen-vl": "Qwen3-VL", "vision": "Vision model"}


def probe(base_url: str, timeout: float = 0.8, opener=None) -> dict | None:
    """What is serving at base_url: {'url', 'flavor': 'ollama'|'openai', 'models': [...]}, or None."""
    v = _get_json(base_url + "/api/version", timeout, opener)
    if isinstance(v, dict) and "version" in v:
        tags = _get_json(base_url + "/api/tags", timeout, opener) or {}
        names = [m.get("name") or m.get("model") for m in tags.get("models", []) if isinstance(m, dict)]
        return {"url": base_url, "flavor": "ollama", "models": [n for n in names if n]}
    d = _get_json(base_url + "/v1/models", timeout, opener)
    if isinstance(d, dict) and isinstance(d.get("data"), list):
        return {"url": base_url, "flavor": "openai",
                "models": [m.get("id") for m in d["data"] if isinstance(m, dict) and m.get("id")]}
    return None


def read_registry(env=os.environ) -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(registry_dir(env), "*.json"))):
        try:
            d = json.load(open(path))
            d["url"] = normalise_url(d["url"], env)
            out.append(d)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # a stale or foreign file is ignored, never fatal
    return out


def discover(extra_urls=(), env=os.environ, timeout: float = 0.8, own_port: int | None = None) -> list[dict]:
    """Every vision model on reachable local servers, registered ones first.

    Returns readers: {id, url, flavor, model, family, label, registered}."""
    reg = read_registry(env)
    urls: list[str] = []
    for u in [r["url"] for r in reg] + [normalise_url(u, env) for u in extra_urls]:
        if u not in urls:
            urls.append(u)
    hosts = ["127.0.0.1"] + sorted(allowed_hosts(env) - LOOPBACK)
    for h in hosts:
        for port in list(DEFAULT_PORTS) + list(LAUNCHER_PORTS):
            if own_port and port == own_port and h == "127.0.0.1":
                continue  # that is this app
            u = f"http://{h}:{port}"
            if u not in urls:
                urls.append(u)
    with ThreadPoolExecutor(max_workers=16) as pool:
        found = list(pool.map(lambda u: probe(u, timeout), urls))
    reg_urls = {r["url"] for r in reg}
    readers = []
    for info in found:
        if not info:
            continue
        for model in info["models"]:
            fam = family(model)
            if not fam:
                continue
            readers.append(reader_entry(info["url"], model, info["flavor"], fam, info["url"] in reg_urls))
    return readers


def reader_entry(url, model, flavor, fam, registered=False) -> dict:
    hostport = url.split("://", 1)[1]
    return {"id": f"local|{url}|{model}", "url": url, "flavor": flavor, "model": model, "family": fam,
            "registered": registered,
            "label": f"{FAMILY_NAMES[fam]} on {hostport} ({model})"}


def _strip_thinking(text: str) -> str:
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    t = text.strip()
    if not t.startswith("{") and "{" in t and "}" in t:  # prose around the JSON object
        t = t[t.index("{"): t.rindex("}") + 1]
    return t


class LocalClient:
    """A vision model on a company-run server. Same interface as the cloud clients."""

    def __init__(self, url: str, model: str, fam: str | None = None, flavor: str = "openai",
                 timeout: float | None = None, opener=None):
        self.url, self.model, self.flavor = url.rstrip("/"), model, flavor
        self.family = fam or family(model) or "vision"
        self.timeout = timeout or float(os.environ.get("APFLOW_LOCAL_TIMEOUT", 300))  # CPU inference is slow
        self.opener = opener or _NO_PROXY.open
        self.cache_id = f"local:{self.model}"   # keeps its cached answers apart from other readers
        self.last_model = model

    @property
    def name(self):
        return f"{FAMILY_NAMES[self.family]} ({self.model}) on {self.url.split('://', 1)[1]}"

    def _body(self, prompt: str, images) -> dict:
        pics = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_png_b64(im)}"}}
                for im in list(images)[:MAX_PAGES]]
        if self.family == "nuextract":
            template = json.dumps(NUEXTRACT_TEMPLATE, indent=4)
            content = pics
            if self.flavor == "ollama":  # Ollama ignores chat_template_kwargs: put the template in the text
                content = [{"type": "text", "text": f"# Template:\n{template}\n# Instructions:\n{NUEXTRACT_INSTRUCTIONS}"}] + pics
            return {"model": self.model, "temperature": 0.2, "max_tokens": 4096,
                    "messages": [{"role": "user", "content": content}],
                    "chat_template_kwargs": {"template": template, "instructions": NUEXTRACT_INSTRUCTIONS,
                                             "enable_thinking": False}}
        return {"model": self.model, "temperature": 0, "max_tokens": 4096,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}] + pics}],
                "chat_template_kwargs": {"enable_thinking": False}}

    def complete_json(self, prompt: str, images) -> str:
        body = self._body(prompt, images)
        endpoint = self.url + "/v1/chat/completions"
        try:
            data = _post(endpoint, body, {}, self.timeout, self.opener)
        except ModelError as e:
            # Some servers refuse fields they do not know; retry once with the plain request.
            if "HTTP 400" in str(e) or "HTTP 422" in str(e):
                for k in ("response_format", "chat_template_kwargs"):
                    if self.family == "nuextract" and k == "chat_template_kwargs":
                        continue  # NuExtract needs its template
                    body.pop(k, None)
                data = _post(endpoint, body, {}, self.timeout, self.opener)
            else:
                raise ModelError(f"{self.name}: {e}") from e
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ModelError(f"unexpected answer from {self.name}: {str(data)[:200]}") from e
        if isinstance(text, list):  # some servers return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        return _json_text(_strip_thinking(text or ""))

