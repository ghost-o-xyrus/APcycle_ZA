"""Part 6 model clients with a fake network: request shape, fallbacks, retries, cache, failure handling.

The sandbox has no internet, so the real key is verified on the deployed app
with the "Test the model" button (POST /api/model-check).
"""
import io
import json
import urllib.error
from datetime import date

import pytest
from PIL import Image

from apflow.extract.routes import extract
from apflow.generator import build
from apflow.models import (CachedClient, ChainClient, GeminiClient, GroqClient, ModelError, model_from_env)
from apflow.scenarios import random_clean

IMG = [Image.new("RGB", (3000, 2000), "white")]
GOOD = {"candidates": [{"content": {"parts": [{"text": '{"invoice_number": "A1", "total": 118.0}'}]}}]}
THINKING = {"candidates": [{"content": {"parts": [{"text": "let me look...", "thought": True},
                                                  {"text": '{"invoice_number": "B2"}'}]}}]}
GROQ_GOOD = {"choices": [{"message": {"content": '{"invoice_number": "G1"}'}}]}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code):
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(b'{"error":"x"}'))


class FakeNet:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, req, timeout=None):
        self.calls.append((req.full_url, json.loads(req.data), dict(req.header_items())))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return Resp(json.dumps(step).encode())


def gemini(script, models=("m1", "m2")):
    net = FakeNet(script)
    return GeminiClient("KEY", models=list(models), opener=net, retry_wait=0), net


# ---------------- Gemini ----------------

def test_request_shape_and_key_in_header_not_url():
    c, net = gemini([GOOD])
    assert json.loads(c.complete_json("read this", IMG))["invoice_number"] == "A1"
    url, body, headers = net.calls[0]
    assert "m1:generateContent" in url and "KEY" not in url
    assert headers.get("X-goog-api-key") == "KEY"
    assert body["generationConfig"] == {"temperature": 0, "responseMimeType": "application/json"}
    assert body["contents"][0]["parts"][0]["text"] == "read this"
    assert body["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/png"


def test_large_images_are_downscaled():
    import base64
    c, net = gemini([GOOD])
    c.complete_json("p", IMG)
    png = base64.b64decode(net.calls[0][1]["contents"][0]["parts"][1]["inline_data"]["data"])
    assert max(Image.open(io.BytesIO(png)).size) == 1600


def test_at_most_two_pages_sent():
    c, net = gemini([GOOD])
    c.complete_json("p", IMG * 5)
    assert len(net.calls[0][1]["contents"][0]["parts"]) == 3  # prompt + 2 pages


def test_thinking_parts_are_ignored():
    c, _ = gemini([THINKING])
    assert json.loads(c.complete_json("p", IMG)) == {"invoice_number": "B2"}


def test_fenced_json_is_accepted():
    fenced = {"candidates": [{"content": {"parts": [{"text": '```json\n{"a": 1}\n```'}]}}]}
    c, _ = gemini([fenced])
    assert json.loads(c.complete_json("p", IMG)) == {"a": 1}


def test_model_not_available_falls_back_to_next():
    c, net = gemini([http_error(404), GOOD])
    c.complete_json("p", IMG)
    assert "m2:generateContent" in net.calls[1][0] and c.last_model == "m2"
    assert c.name == "Gemini (m2)"


def test_rate_limit_retried_once_then_next_model():
    c, net = gemini([http_error(429), GOOD])
    c.complete_json("p", IMG)
    assert len(net.calls) == 2 and c.last_model == "m1"
    c, net = gemini([http_error(429), http_error(429), GOOD])
    c.complete_json("p", IMG)
    assert c.last_model == "m2"


def test_non_json_answer_tries_next_model():
    bad = {"candidates": [{"content": {"parts": [{"text": "Sure! Here is the invoice..."}]}}]}
    c, _ = gemini([bad, GOOD])
    assert json.loads(c.complete_json("p", IMG))["invoice_number"] == "A1"
    c, _ = gemini([bad, bad])
    with pytest.raises(ModelError, match="not a JSON object"):
        c.complete_json("p", IMG)


def test_blocked_or_empty_answer_is_an_error():
    c, _ = gemini([{"promptFeedback": {"blockReason": "OTHER"}}, {"candidates": []}])
    with pytest.raises(ModelError):
        c.complete_json("p", IMG)


def test_everything_down_raises_with_reasons():
    c, _ = gemini([http_error(500), urllib.error.URLError("no route")])
    with pytest.raises(ModelError, match="m1: HTTP 500.*m2: network: no route"):
        c.complete_json("p", IMG)


def test_empty_key_refused():
    with pytest.raises(ModelError):
        GeminiClient("")


def test_pinned_model_tried_first(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-9-ultra")
    c = GeminiClient("KEY")
    assert c.models[0] == "gemini-9-ultra" and len(c.models) == len(set(c.models))


# ---------------- Groq, chain, cache ----------------

def test_groq_request_shape():
    net = FakeNet([GROQ_GOOD])
    g = GroqClient("GK", "qwen/qwen3.6-27b", opener=net)
    assert json.loads(g.complete_json("p", IMG))["invoice_number"] == "G1"
    url, body, headers = net.calls[0]
    assert url.endswith("/chat/completions") and headers.get("Authorization") == "Bearer GK"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_chain_falls_back_to_groq():
    g_net = FakeNet([GROQ_GOOD])
    chain = ChainClient([gemini([http_error(500), http_error(500)])[0], GroqClient("GK", opener=g_net)])
    assert json.loads(chain.complete_json("p", IMG))["invoice_number"] == "G1"
    assert chain.last.name.startswith("Groq")


def test_cache_avoids_second_call(tmp_path):
    c, net = gemini([GOOD])
    cc = CachedClient(c, str(tmp_path))
    a, b = cc.complete_json("p", IMG), cc.complete_json("p", IMG)  # second would fail: script is empty
    assert a == b and len(net.calls) == 1 and (cc.hits, cc.misses) == (1, 1)


def test_cache_does_not_store_failures(tmp_path):
    c, _ = gemini([http_error(500), http_error(500), GOOD])
    cc = CachedClient(c, str(tmp_path))
    with pytest.raises(ModelError):
        cc.complete_json("p", IMG)
    assert json.loads(cc.complete_json("p", IMG))["invoice_number"] == "A1"


def test_model_from_env(tmp_path):
    assert model_from_env(str(tmp_path), env={}) is None
    assert model_from_env(str(tmp_path), env={"GEMINI_API_KEY": "  "}) is None
    m = model_from_env(str(tmp_path), env={"GEMINI_API_KEY": "k"})
    assert isinstance(m, CachedClient) and isinstance(m.inner, GeminiClient)
    m = model_from_env(str(tmp_path), env={"GEMINI_API_KEY": "k", "GROQ_API_KEY": "g"})
    assert isinstance(m.inner, ChainClient) and "then Groq" in m.name


# ---------------- integration: router and service ----------------

def test_router_survives_model_failure(tmp_path):
    pdf, _ = build(random_clean(31, layout="B", scanned=True), str(tmp_path), "s")
    c, _ = gemini([urllib.error.URLError("offline")] * 2)
    ex, log = extract(pdf, llm=c)
    assert ex.route == "ocr" and "offline" in log[0]["error"]


class TruthNet:
    """Fake Gemini that answers with each sample's answer key (identified by the order of calls)."""

    def __init__(self, truths):
        self.truths = list(truths)

    def __call__(self, req, timeout=None):
        t = self.truths.pop(0)
        d = {k: v for k, v in t.items() if k not in ("layout", "scanned")}
        return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(d)}]}}]}).encode())


def test_service_uses_real_model_and_check_reports_accuracy(tmp_path, monkeypatch):
    from apflow.service import Service
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    svc = Service(str(tmp_path), as_of=date(2026, 11, 20), model_mode="auto")
    assert svc.model_mode == "real" and svc.model is None and svc.model_label().startswith("Gemini")
    truths = [svc._samples["S05"]["truth"], svc._samples["E3"]["truth"]]
    svc.real_model.inner.opener = TruthNet(truths)
    r = svc.check_model()
    assert r["ok"] and [d["fields_right"] for d in r["docs"]] == [12, 12]
    assert all(d["lines_right"] == d["lines"] for d in r["docs"])
    # the same scans in a real run now use the (cached) real model, not the stand-in
    run = svc.wait(svc.start_sample("S05"))
    assert run["route"] == "llm" and run["final_status"] == "approved"


def test_check_model_without_key(tmp_path, monkeypatch):
    from apflow.service import Service
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    svc = Service(str(tmp_path), model_mode="auto")
    assert svc.model_mode == "standin"
    assert not svc.check_model()["ok"] and "GEMINI_API_KEY" in svc.check_model()["error"]


# ---------------- found on the live deployment (21 Nov) ----------------

class Timeout(Exception):
    pass


def test_timeout_mid_read_moves_to_next_model():
    # Live log: "The read operation timed out" escaped as a bare TimeoutError and skipped every other model.
    c, net = gemini([TimeoutError("The read operation timed out"), GOOD])
    assert json.loads(c.complete_json("p", IMG))["invoice_number"] == "A1"
    assert c.last_model == "m2"


def test_high_demand_503_is_retried_once():
    # Live log: gemini-3.6-flash answered 503 "experiencing high demand".
    c, net = gemini([http_error(503), GOOD])
    c.complete_json("p", IMG)
    assert len(net.calls) == 2 and c.last_model == "m1"


def test_error_messages_are_short_and_readable():
    body = json.dumps({"error": {"code": 503, "message": "This model is currently experiencing high demand. " * 5}})
    errs = [urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(body.encode())) for _ in range(4)]
    c, _ = gemini(errs, models=("m1", "m2"))
    with pytest.raises(ModelError) as e:
        c.complete_json("p", IMG)
    msg = str(e.value)
    assert "m1: HTTP 503: This model is currently experiencing high demand" in msg and "m2:" in msg
    assert '{"error"' not in msg and len(msg) < 400


def test_total_time_budget_is_respected(monkeypatch):
    import apflow.models as M
    ticks = iter(range(0, 1000, 40))  # every clock reading is 40 s later than the last
    monkeypatch.setattr(M.time, "monotonic", lambda: next(ticks))
    c, net = gemini([http_error(500), http_error(500), GOOD], models=("m1", "m2", "m3"))
    c.budget = 75
    with pytest.raises(ModelError, match="stopped after 75 s"):
        c.complete_json("p", IMG)
    assert not any("m3:" in url for url, _, _ in net.calls)  # out of time before m3


def test_cache_hit_never_renders_the_pdf(tmp_path, monkeypatch):
    import apflow.extract.routes as routes
    pdf, t = build(random_clean(32, layout="A", scanned=True), str(tmp_path), "s")
    c, net = gemini([GOOD])
    cc = CachedClient(c, str(tmp_path / "cache"))
    routes.extract_llm_route(pdf, cc)
    calls = []
    monkeypatch.setattr(routes, "page_images", lambda *a, **k: calls.append(1) or [])
    routes.extract_llm_route(pdf, cc)  # same file again
    assert calls == [] and (cc.hits, cc.misses) == (1, 1)


def test_model_reads_at_most_two_pages(tmp_path, monkeypatch):
    import apflow.extract.routes as routes
    seen = {}
    real = routes.page_images
    monkeypatch.setattr(routes, "page_images", lambda path, dpi=300, last_page=None: seen.setdefault("lp", last_page) and real(path, dpi, last_page))
    pdf, _ = build(random_clean(33, layout="A", scanned=True), str(tmp_path), "s2")
    c, _ = gemini([GOOD])
    routes.extract_llm_route(pdf, c)
    assert seen["lp"] == 2
