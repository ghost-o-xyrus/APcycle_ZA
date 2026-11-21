"""Vision model clients (part 6): Gemini first, Groq as backup, every answer cached.

All clients implement `complete_json(prompt, images) -> str`, the interface the
extraction router already uses. Standard library only (urllib), no SDKs.

Env:
  GEMINI_API_KEY   - Google AI Studio key (free tier works)
  GEMINI_MODEL     - pin one model to try first (model names change often; defaults below)
  GROQ_API_KEY     - optional backup
  GROQ_MODEL       - default qwen/qwen3.6-27b (vision)
  APFLOW_MODEL     - auto (default) | standin | none
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.request

from PIL import Image

DEFAULT_GEMINI_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash"]
DEFAULT_GROQ_MODEL = "qwen/qwen3.6-27b"
MAX_SIDE = 1600   # px; keeps image tokens small without losing printed text
MAX_PAGES = 2


class ModelError(RuntimeError):
    pass


def _png_b64(im: Image.Image) -> str:
    im = im.convert("RGB")
    scale = MAX_SIDE / max(im.size)
    if scale < 1:
        im = im.resize((int(im.width * scale), int(im.height * scale)))
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def _post(url: str, body: dict, headers: dict, timeout: float, opener=urllib.request.urlopen) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with opener(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise ModelError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise ModelError(f"network: {e.reason}") from e


def _json_text(text: str) -> str:
    """The model's answer, checked to be a JSON object (fences tolerated). Raises ModelError if not."""
    t = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        if not isinstance(json.loads(t), dict):
            raise ValueError
    except ValueError:
        raise ModelError(f"answer is not a JSON object: {text[:80]!r}")
    return t


class GeminiClient:
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key: str, models=None, timeout: float = 60, opener=urllib.request.urlopen,
                 retry_wait: float = 4.0):
        if not api_key:
            raise ModelError("GEMINI_API_KEY is not set")
        self.api_key, self.timeout, self.opener, self.retry_wait = api_key, timeout, opener, retry_wait
        pinned = os.environ.get("GEMINI_MODEL", "").strip()
        self.models = list(models or ([pinned] if pinned else []) + [m for m in DEFAULT_GEMINI_MODELS if m != pinned])
        self.last_model = None

    @property
    def name(self):
        return f"Gemini ({self.last_model or self.models[0]})"

    def complete_json(self, prompt: str, images: list) -> str:
        parts = [{"text": prompt}] + [{"inline_data": {"mime_type": "image/png", "data": _png_b64(im)}}
                                      for im in images[:MAX_PAGES]]
        body = {"contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
        errors = []
        for m in self.models:
            for attempt in range(2):
                try:
                    data = _post(self.URL.format(model=m), body, {"x-goog-api-key": self.api_key}, self.timeout, self.opener)
                    texts = [p.get("text", "") for c in data.get("candidates", [])[:1]
                             for p in c.get("content", {}).get("parts", []) if not p.get("thought")]
                    out = _json_text("".join(texts))
                    self.last_model = m
                    return out
                except ModelError as e:
                    if "HTTP 429" in str(e) and attempt == 0:
                        time.sleep(self.retry_wait)  # free-tier per-minute limit: one short retry
                        continue
                    errors.append(f"{m}: {e}")
                    break  # missing model, quota, bad answer or network: try the next model
        raise ModelError("; ".join(errors) or "no Gemini model configured")


class GroqClient:
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str, model: str | None = None, timeout: float = 60, opener=urllib.request.urlopen):
        self.api_key, self.timeout, self.opener = api_key, timeout, opener
        self.model = model or DEFAULT_GROQ_MODEL
        self.last_model = None

    @property
    def name(self):
        return f"Groq ({self.model})"

    def complete_json(self, prompt: str, images: list) -> str:
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_png_b64(im)}"}} for im in images[:MAX_PAGES]]
        body = {"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": content}]}
        data = _post(self.URL, body, {"Authorization": f"Bearer {self.api_key}"}, self.timeout, self.opener)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ModelError(f"unexpected Groq answer: {str(data)[:200]}") from e
        out = _json_text(text or "")
        self.last_model = self.model
        return out


class ChainClient:
    """Try each client in order; the first answer wins."""

    def __init__(self, clients):
        self.clients = clients
        self.last = None

    @property
    def name(self):
        return " then ".join(c.name for c in self.clients)

    def complete_json(self, prompt, images):
        errors = []
        for c in self.clients:
            try:
                out = c.complete_json(prompt, images)
                self.last = c
                return out
            except ModelError as e:
                errors.append(f"{c.name}: {e}")
        raise ModelError(" | ".join(errors))


class CachedClient:
    """Caches answers by (prompt, image bytes): repeat demo runs cost no quota and give the same result."""

    def __init__(self, inner, cache_dir: str):
        self.inner, self.cache_dir = inner, cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.hits = self.misses = 0

    @property
    def name(self):
        return self.inner.name

    def _key(self, prompt, images):
        h = hashlib.sha256(prompt.encode())
        for im in images[:MAX_PAGES]:
            h.update(im.tobytes())
        return h.hexdigest()

    def complete_json(self, prompt, images):
        path = os.path.join(self.cache_dir, self._key(prompt, images) + ".json")
        if os.path.exists(path):
            self.hits += 1
            return open(path).read()
        out = self.inner.complete_json(prompt, images)  # clients only return validated JSON
        self.misses += 1
        with open(path, "w") as f:
            f.write(out)
        return out


def model_from_env(cache_dir: str, env=os.environ):
    """The real model chain if any key is set, else None."""
    clients = []
    if env.get("GEMINI_API_KEY", "").strip():
        clients.append(GeminiClient(env["GEMINI_API_KEY"].strip()))
    if env.get("GROQ_API_KEY", "").strip():
        clients.append(GroqClient(env["GROQ_API_KEY"].strip(), env.get("GROQ_MODEL") or None))
    if not clients:
        return None
    return CachedClient(clients[0] if len(clients) == 1 else ChainClient(clients), cache_dir)
