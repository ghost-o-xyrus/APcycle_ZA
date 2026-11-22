"""A model on the viewer's own computer, reached through their browser.

The desk may run on a hosted server (Render) that can never reach a person's laptop. The page
open on that laptop can: it finds the model server on 127.0.0.1 (after the browser asks the
person's permission) and relays requests. The server queues a request, the page fetches it,
calls the local model, and posts the answer back. If no page is connected, a request fails at
once and the run falls back to OCR, as for any model failure.

Page images therefore travel server -> browser -> local model, and no third-party AI sees them.
The document itself was still uploaded to the server; for full privacy run the desk itself on
the same computer.
"""
from __future__ import annotations

import os
import secrets
import threading
import time

from .local_readers import FAMILY_NAMES, LocalClient, _strip_thinking, family
from .models import ModelError, _json_text

ALIVE_SECONDS = 15  # a page that has not polled for this long is gone


class BrowserRelay:
    def __init__(self, timeout: float | None = None):
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._polling = 0  # pages waiting in next_request right now
        self._pending: dict[str, dict] = {}
        self.model: dict | None = None
        self.token = ""  # only the page that connected receives requests
        self.last_seen = 0.0
        self.answered = 0
        self.timeout = timeout or float(os.environ.get("APFLOW_RELAY_TIMEOUT", 600))  # CPU models are slow

    # ---------- the page's side ----------
    def connect(self, url: str, model: str, flavor: str = "openai") -> str:
        """A page offers its model. A later page replaces an earlier one. Returns the page's token."""
        fam = family(model) or "vision"
        with self._lock:
            self.model = {"url": url.rstrip("/"), "model": model, "flavor": flavor, "family": fam,
                          "label": f"{FAMILY_NAMES[fam]} on your computer ({model})"}
            self.token = secrets.token_urlsafe(18)
            self.last_seen = time.time()
            self._fail_pending("Another page connected a model")
            return self.token

    def check(self, token: str) -> bool:
        return bool(self.token) and secrets.compare_digest(str(token or ""), self.token)

    def disconnect(self):
        with self._cv:
            self.model, self.token = None, ""
            self._fail_pending("The page disconnected the model")
            self._cv.notify_all()

    def _fail_pending(self, why: str):
        for r in self._pending.values():
            if not r["done"].is_set():
                r["error"] = why
                r["done"].set()

    def alive(self) -> bool:
        return bool(self.model) and (self._polling > 0 or time.time() - self.last_seen < ALIVE_SECONDS)

    def next_request(self, wait: float = 0.0, token: str | None = None) -> dict | None:
        """The page asks for work, waiting up to `wait` seconds for some (a long poll: no timers in the
        page, so a background tab is not throttled). Each call is also the page's heartbeat."""
        end = time.time() + max(0.0, wait)
        with self._cv:
            self._polling += 1
            try:
                while True:
                    self.last_seen = time.time()
                    if token is not None and token != self.token:
                        return None  # this page was replaced or disconnected while it waited
                    req = self._claim()
                    if req or time.time() >= end or not self.model:
                        return req
                    self._cv.wait(min(1.0, end - time.time()))
            finally:
                self._polling -= 1
                self.last_seen = time.time()

    def _claim(self) -> dict | None:
        for rid, r in sorted(self._pending.items(), key=lambda kv: kv[1]["created"]):
            if not r["claimed"]:
                r["claimed"] = time.time()
                return {"id": rid, "endpoint": r["url"] + "/v1/chat/completions", "body": r["body"],
                            # fields to drop and retry once if the server refuses them (HTTP 400/422)
                            "retry_without": r["drop"]}
        return None

    def answer(self, rid: str, content: str | None = None, error: str | None = None) -> bool:
        with self._lock:
            r = self._pending.get(rid)
            if not r:
                return False
            r["content"], r["error"] = content, error
            self.answered += 0 if error else 1
            r["done"].set()
            return True

    # ---------- the server's side ----------
    def ask(self, prompt: str, images) -> str:
        if not self.alive():
            raise ModelError("No browser is connected to a model on its computer (open the desk and connect it)")
        m = dict(self.model)
        body = LocalClient(m["url"], m["model"], m["family"], m["flavor"])._body(prompt, images)
        rid = secrets.token_hex(6)
        drop = ["response_format"] + ([] if m["family"] == "nuextract" else ["chat_template_kwargs"])
        req = {"url": m["url"], "body": body, "drop": drop, "created": time.time(), "claimed": 0.0, "done": threading.Event(),
               "content": None, "error": None}
        with self._cv:
            self._pending[rid] = req
            self._cv.notify_all()
        try:
            end = time.time() + self.timeout
            while not req["done"].wait(1.0):
                if not req["claimed"] and time.time() - req["created"] > ALIVE_SECONDS + 5:
                    raise ModelError("The connected page did not pick up the request (is the desk tab still open?)")
                if time.time() > end:
                    raise ModelError(f"The model on your computer did not answer within {int(self.timeout)} s")
            if req["error"]:
                raise ModelError(f"Model on your computer: {req['error'][:300]}")
            return _json_text(_strip_thinking(req["content"] or ""))
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    def status(self) -> dict:
        return {"connected": self.alive(), "model": self.model, "answered": self.answered,
                "waiting": sum(1 for r in self._pending.values() if not r["claimed"])}


class RelayClient:
    """The VisionClient the pipeline sees; every call goes to the viewer's computer through their page."""

    def __init__(self, relay: BrowserRelay):
        self.relay = relay

    @property
    def name(self):
        m = self.relay.model or {}
        return f"{m.get('label', 'Model on your computer')}, through your browser"

    @property
    def cache_id(self):
        return f"browser:{(self.relay.model or {}).get('model', '')}"

    def complete_json(self, prompt: str, images) -> str:
        return self.relay.ask(prompt, images)
