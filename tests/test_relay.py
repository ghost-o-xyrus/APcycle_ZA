"""A model on the viewer's computer, reached through their open desk page (apflow/relay.py).

The page is simulated here in Python: it polls the server for requests, calls the model server
on 127.0.0.1 the way the page's JavaScript does, and posts the answer back. The browser itself is
covered in test_ui.py."""
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date

import pytest

from apflow.extract.routes import LazyPages
from apflow.models import ModelError, _png_b64
from apflow.relay import BrowserRelay
from apflow.service import ActionError, Service
from tests.fake_reader_server import serve_in_thread
from tests.test_local_readers import free_port

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call_model(req):
    """What the page does with one request: POST it, retry once without refused fields."""
    body = dict(req["body"])
    for attempt in (0, 1):
        r = urllib.request.Request(req["endpoint"], json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with DIRECT.open(r, timeout=30) as resp:
                return json.loads(resp.read())["choices"][0]["message"]["content"], None
        except urllib.error.HTTPError as e:
            if attempt == 0 and e.code in (400, 422):
                for k in req["retry_without"]:
                    body.pop(k, None)
                continue
            return None, f"HTTP {e.code}"
        except OSError as e:
            return None, str(e)


class Page:
    """A desk tab that connected a model and relays requests until stopped."""

    def __init__(self, relay: BrowserRelay, token: str):
        self.relay, self.token, self.stop, self.handled = relay, token, threading.Event(), 0
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def loop(self):
        while not self.stop.is_set():
            if not self.relay.check(self.token):
                return  # replaced by another page
            req = self.relay.next_request()
            if req:
                content, err = call_model(req)
                self.relay.answer(req["id"], content, err)
                self.handled += 1
            else:
                time.sleep(0.05)

    def close(self):
        self.stop.set()
        self.t.join(5)


@pytest.fixture(scope="module")
def answers(tmp_path_factory):
    svc = Service(str(tmp_path_factory.mktemp("rl")), as_of=date(2026, 11, 20), model_mode="standin")
    out = {}
    for sid in ("S05", "E3"):
        s = svc._samples[sid]
        key = hashlib.sha256(_png_b64(LazyPages(s["file"])[0]).encode()).hexdigest()
        ans = json.loads(svc.standin.for_file(s["sha256"]).complete_json("", []))
        ans.pop("confidence")
        out[key] = ans
    return out


def test_no_page_connected_fails_at_once():
    r = BrowserRelay()
    t0 = time.time()
    with pytest.raises(ModelError, match="No browser is connected"):
        r.ask("read this", [])
    assert time.time() - t0 < 0.5
    assert not r.status()["connected"]


def test_only_the_connecting_page_gets_requests_and_a_new_page_replaces_it():
    r = BrowserRelay()
    t1 = r.connect("http://127.0.0.1:8100", "NuExtract3")
    assert r.check(t1) and not r.check("guess") and not r.check("")
    t2 = r.connect("http://127.0.0.1:8101", "qwen3-vl:4b", "ollama")
    assert r.check(t2) and not r.check(t1)
    assert r.model["family"] == "qwen-vl" and r.model["label"] == "Qwen3-VL on your computer (qwen3-vl:4b)"
    r.disconnect()
    assert not r.check(t2) and r.model is None


def test_a_page_that_stops_polling_is_not_connected(monkeypatch):
    import apflow.relay as relay
    r = BrowserRelay()
    r.connect("http://127.0.0.1:8100", "NuExtract3")
    assert r.alive()
    monkeypatch.setattr(relay, "ALIVE_SECONDS", 0)
    assert not r.alive()


def test_requests_carry_the_model_body_and_errors_come_back(answers):
    r = BrowserRelay(timeout=5)
    token = r.connect("http://127.0.0.1:9", "numind/NuExtract3")
    out = {}

    def ask():
        try:
            out["ok"] = r.ask("prompt", [])
        except ModelError as e:
            out["err"] = str(e)
    th = threading.Thread(target=ask)
    th.start()
    req = None
    for _ in range(100):
        req = r.next_request()
        if req:
            break
        time.sleep(0.02)
    assert req["endpoint"] == "http://127.0.0.1:9/v1/chat/completions"
    assert "template" in req["body"]["chat_template_kwargs"]  # NuExtract's template travels with it
    assert req["retry_without"] == ["response_format"]  # never drop NuExtract's template
    assert r.next_request() is None  # handed out once
    r.answer(req["id"], None, "Failed to fetch")
    th.join(5)
    assert "Failed to fetch" in out["err"] and r.check(token)


def test_answer_with_thinking_is_cleaned():
    r = BrowserRelay(timeout=5)
    r.connect("http://127.0.0.1:9", "qwen3-vl:4b", "ollama")
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("v", r.ask("p", [])))
    th.start()
    while not (req := r.next_request()):
        time.sleep(0.02)
    assert req["retry_without"] == ["response_format", "chat_template_kwargs"]
    assert req["body"]["response_format"] == {"type": "json_object"}
    r.answer(req["id"], '<think>sum {a}</think>\n```json\n{"invoice_number": "X1"}\n```')
    th.join(5)
    assert json.loads(out["v"]) == {"invoice_number": "X1"}


def test_unclaimed_request_gives_up_when_the_page_vanished(monkeypatch):
    import apflow.relay as relay
    r = BrowserRelay(timeout=30)
    r.connect("http://127.0.0.1:9", "NuExtract3")
    monkeypatch.setattr(relay, "ALIVE_SECONDS", -4.0)  # claimed-by deadline: created + 1 s
    r.last_seen = time.time() + 100  # still "alive" but never polls
    t0 = time.time()
    with pytest.raises(ModelError, match="did not pick up"):
        r.ask("p", [])
    assert time.time() - t0 < 5


def test_a_scan_is_read_by_the_model_on_your_computer(tmp_path, answers):
    """End to end: the desk's reader is the page's model; the run says which reader saw the file."""
    srv = serve_in_thread(free_port(), "numind/NuExtract3", answers, require_template=True)
    svc = Service(str(tmp_path / "s"), as_of=date(2026, 11, 20), model_mode="standin")
    url = f"http://127.0.0.1:{srv.server_port}"
    with pytest.raises(ActionError):
        svc.relay_connect("127.0.0.1:8100", "NuExtract3")  # needs a full address
    with pytest.raises(ActionError, match="vision model"):
        svc.relay_connect(url, "llama3.1:8b")
    d = svc.relay_connect(url, "numind/NuExtract3")
    page = Page(svc.relay, d["token"])
    try:
        assert d["current"] == "browser"
        opt = next(o for o in d["options"] if o["id"] == "browser")
        assert opt["connected"] and "No AI company sees them" in opt["note"]
        run = svc.wait(svc.start_sample("E3"))
        read = next(e for e in run["events"] if e["stage"] == "read")
        assert "NuExtract3 on your computer (numind/NuExtract3), through your browser" in read["detail"]
        assert run["outcome"] == "HOLD" and page.handled >= 1  # E3's designed outcome
        chk = svc.check_model(("S05",))
        assert chk["ok"] and chk["docs"][0]["fields_right"] == chk["docs"][0]["fields"]
        assert len(srv.seen) >= 2
    finally:
        page.close()
        srv.shutdown()
    # The page closed: the reader stays chosen but says so, and scans go to OCR instead of waiting.
    svc.relay.last_seen = 0
    assert svc.model_label().startswith("Model on your computer, not connected")
    assert "not connected" in next(o for o in svc.readers()["options"] if o["id"] == "browser")["label"]
    svc._relay_client.cache_dir = str(tmp_path)  # no saved answers: the model must really be asked
    t0 = time.time()
    run = svc.wait(svc.start_sample("S05"))
    read = next(e for e in run["events"] if e["stage"] == "read")
    assert run["state"] == "done" and time.time() - t0 < 60 and "OCR" in read["detail"], read["detail"]
    svc.relay_disconnect("wrong token")
    assert svc.reader_id == "browser"
    svc.relay_disconnect(d["token"])
    assert svc.reader_id == "standin" and all(o["id"] != "browser" for o in svc.readers()["options"])


def test_relay_api(tmp_path, monkeypatch):
    from apflow.web.app import create_app
    svc = Service(str(tmp_path / "a"), model_mode="standin")
    cl = create_app(svc).test_client()
    assert cl.post("/api/relay/next", json={"token": "x"}).status_code == 409
    d = cl.post("/api/relay/connect", json={"url": "http://127.0.0.1:8100", "model": "NuExtract3"}).get_json()
    tok = d["token"]
    assert d["current"] == "browser" and d["relay"]["connected"]
    assert cl.post("/api/relay/next", json={"token": tok}).get_json() == {"request": None}
    assert cl.post("/api/relay/answer", json={"token": tok, "id": "nope", "content": "{}"}).get_json() == {"ok": False}
    assert cl.post("/api/relay/answer", json={"token": "bad", "id": "nope"}).status_code == 409
    assert "token" not in cl.get("/api/readers").get_json()  # the token is only given to the page that connected
    assert cl.post("/api/relay/disconnect", json={"token": tok}).get_json()["current"] == "standin"
    monkeypatch.setenv("APFLOW_READER_LOCKED", "1")
    assert cl.post("/api/relay/connect", json={"url": "http://127.0.0.1:8100", "model": "NuExtract3"}).status_code == 400


def test_the_launcher_can_be_downloaded_from_the_desk(tmp_path):
    from apflow.web.app import create_app
    cl = create_app(Service(str(tmp_path / "d"), model_mode="none")).test_client()
    r = cl.get("/downloads/serve_reader.py")
    assert r.status_code == 200 and b"def main(" in r.data and "attachment" in r.headers["Content-Disposition"]
