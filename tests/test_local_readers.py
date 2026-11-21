"""Private readers: discovery, the NuExtract3 and Qwen3-VL clients, switching, and the launcher.

Real models are not run here; fake servers speak the same HTTP dialects (vLLM / llama.cpp
OpenAI-style, and Ollama) and answer known pages with their answer keys.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import date

import pytest

from apflow import local_readers as lr
from apflow.extract.routes import LazyPages, extract_llm_route
from apflow.models import CachedClient, ModelError, _png_b64
from apflow.service import ActionError, Service

sys.path.insert(0, os.path.dirname(__file__))
from fake_reader_server import serve_in_thread  # noqa: E402

HERE = os.path.dirname(__file__)
FAKE = os.path.join(HERE, "fake_reader_server.py")
LAUNCHER = os.path.join(HERE, "..", "scripts", "serve_reader.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def svc_and_answers(tmp_path_factory):
    svc = Service(str(tmp_path_factory.mktemp("lr")), as_of=date(2026, 11, 20), model_mode="standin")
    answers = {}
    for sid in ("S05", "E3", "S01"):
        s = svc._samples[sid]
        key = hashlib.sha256(_png_b64(LazyPages(s["file"])[0]).encode()).hexdigest()
        ans = json.loads(svc.standin.for_file(s["sha256"]).complete_json("", []))
        ans.pop("confidence")
        answers[key] = ans
    return svc, answers


@pytest.fixture
def registry(tmp_path, monkeypatch):
    d = tmp_path / "readers"
    d.mkdir()
    monkeypatch.setenv("APFLOW_READERS_DIR", str(d))
    return d


def reg(d, url, model="x"):
    (d / (url.split(":")[-1] + ".json")).write_text(json.dumps({"url": url, "model": model}))


# ---------------- addresses and names ----------------

@pytest.mark.parametrize("given,want", [
    ("8137", "http://127.0.0.1:8137"), ("localhost:8137", "http://127.0.0.1:8137"),
    ("http://127.0.0.1:8137/v1", "http://127.0.0.1:8137"), (" http://[::1]:9000 ", "http://[::1]:9000")])
def test_addresses_are_normalised(given, want):
    assert lr.normalise_url(given, env={}) == want


@pytest.mark.parametrize("bad", ["http://example.com:80", "http://169.254.169.254:80", "10.0.0.5:8000",
                                 "http://127.0.0.1", "ftp://127.0.0.1:21", "", "not a url"])
def test_other_hosts_and_bad_addresses_are_refused(bad):
    with pytest.raises(ValueError):
        lr.normalise_url(bad, env={})


def test_listed_hosts_are_allowed():
    assert lr.normalise_url("gpu-box:8000", env={"APFLOW_READER_HOSTS": "GPU-box, other"}) == "http://gpu-box:8000"


@pytest.mark.parametrize("model,fam", [
    ("numind/NuExtract3", "nuextract"), ("hf.co/numind/NuExtract3-GGUF:Q4_K_M", "nuextract"),
    ("Qwen/Qwen3-VL-8B-Instruct", "qwen-vl"), ("qwen3-vl:8b-instruct", "qwen-vl"),
    ("qwen3-vl-4b-instruct", "qwen-vl"), ("gemma3:4b", "vision"), ("llama3.1:8b", None),
    ("qwen3:8b", None), ("nomic-embed-text", None)])
def test_model_families(model, fam):
    assert lr.family(model) == fam


# ---------------- discovery ----------------

def test_discovery_finds_registered_servers_on_any_port_and_skips_text_models(registry):
    a = serve_in_thread(free_port(), "numind/NuExtract3", {})
    b = serve_in_thread(free_port(), "qwen3-vl:8b-instruct", {}, flavor="ollama")
    c = serve_in_thread(free_port(), "llama3.1:8b", {})
    try:
        for s in (a, b, c):
            reg(registry, f"http://127.0.0.1:{s.server_port}")
        (registry / "junk.json").write_text("{not json")          # ignored, not fatal
        (registry / "foreign.json").write_text(json.dumps({"url": "http://example.com:80"}))
        found = {r["model"]: r for r in lr.discover()}
        assert set(found) == {"numind/NuExtract3", "qwen3-vl:8b-instruct"}
        assert found["numind/NuExtract3"]["flavor"] == "openai" and found["numind/NuExtract3"]["registered"]
        assert found["qwen3-vl:8b-instruct"]["flavor"] == "ollama"
        assert found["qwen3-vl:8b-instruct"]["label"].startswith("Qwen3-VL on 127.0.0.1:")
    finally:
        for s in (a, b, c):
            s.shutdown()


def test_discovery_is_quick_when_nothing_is_running(registry):
    t = time.time()
    assert lr.discover() == []
    assert time.time() - t < 5


def test_probe_ignores_servers_that_are_not_model_servers():
    import http.server
    import threading
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert lr.probe(f"http://127.0.0.1:{srv.server_port}") is None
    finally:
        srv.shutdown()


# ---------------- clients ----------------

def _field_errors(svc, sid, ex):
    from apflow.evaluate import FIELDS, field_ok, lines_ok
    t = svc._samples[sid]["truth"]
    wrong = [f for f in FIELDS if not field_ok(f, t.get(f), ex.get(f))]
    hit, n = lines_ok(t["lines"], ex.lines)
    return wrong, hit, n


def test_nuextract_gets_its_template_and_reads_scans_exactly(svc_and_answers):
    svc, answers = svc_and_answers
    srv = serve_in_thread(free_port(), "numind/NuExtract3", answers, require_template=True)
    try:
        c = lr.LocalClient(f"http://127.0.0.1:{srv.server_port}", "numind/NuExtract3")
        for sid in ("S05", "E3"):
            ex = extract_llm_route(svc._samples[sid]["file"], c)
            wrong, hit, n = _field_errors(svc, sid, ex)
            assert wrong == [] and hit == n, (sid, wrong)
        body = srv.seen[0]
        kw = body["chat_template_kwargs"]
        assert json.loads(kw["template"]) == lr.NUEXTRACT_TEMPLATE and kw["enable_thinking"] is False
        assert all(p["type"] == "image_url" for p in body["messages"][0]["content"])  # no prompt text needed
        assert body["temperature"] == 0.2
    finally:
        srv.shutdown()


def test_nuextract_on_ollama_gets_the_template_in_the_text(svc_and_answers):
    svc, answers = svc_and_answers
    srv = serve_in_thread(free_port(), "hf.co/numind/NuExtract3-GGUF:Q4_K_M", answers, flavor="ollama")
    try:
        c = lr.LocalClient(f"http://127.0.0.1:{srv.server_port}", "hf.co/numind/NuExtract3-GGUF:Q4_K_M", flavor="ollama")
        extract_llm_route(svc._samples["E3"]["file"], c)
        first = srv.seen[0]["messages"][0]["content"][0]
        assert first["type"] == "text" and first["text"].startswith("# Template:")
    finally:
        srv.shutdown()


def test_qwen_retries_without_unsupported_fields_and_strips_thinking(svc_and_answers):
    svc, answers = svc_and_answers
    srv = serve_in_thread(free_port(), "Qwen/Qwen3-VL-8B-Instruct", answers, reject_rf=True, think=True)
    try:
        c = lr.LocalClient(f"http://127.0.0.1:{srv.server_port}", "Qwen/Qwen3-VL-8B-Instruct")
        ex = extract_llm_route(svc._samples["E3"]["file"], c)
        wrong, hit, n = _field_errors(svc, "E3", ex)
        assert wrong == [] and hit == n
        assert "response_format" in srv.seen[0] and "response_format" not in srv.seen[1]
        assert srv.seen[0]["messages"][0]["content"][0]["type"] == "text"  # Qwen gets the cloud prompt
        assert c.name == f"Qwen3-VL (Qwen/Qwen3-VL-8B-Instruct) on 127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


def test_a_stopped_server_is_a_model_error_and_the_run_falls_back_to_ocr(svc_and_answers):
    from apflow.pipeline import run_invoice
    svc, _ = svc_and_answers
    c = lr.LocalClient(f"http://127.0.0.1:{free_port()}", "numind/NuExtract3", timeout=2)
    with pytest.raises(ModelError):
        c.complete_json("p", [])
    rr = run_invoice(svc._samples["E3"]["file"], svc._repo(), llm=c, as_of=date(2026, 11, 20))
    routes = [l["route"] for l in rr.route_log]
    assert routes[:2] == ["llm", "ocr"] and "error" in rr.route_log[0]


def test_cached_answers_are_kept_apart_per_reader(tmp_path, svc_and_answers):
    svc, _ = svc_and_answers
    pages = LazyPages(svc._samples["E3"]["file"])
    a = CachedClient(lr.LocalClient("http://127.0.0.1:1", "numind/NuExtract3"), str(tmp_path))
    b = CachedClient(lr.LocalClient("http://127.0.0.1:1", "qwen3-vl:8b-instruct"), str(tmp_path))

    class Cloud:  # no cache_id: keys stay as before, so saved cloud answers still hit
        name = "cloud"
    c = CachedClient(Cloud(), str(tmp_path))
    keys = {a._key("p", pages), b._key("p", pages), c._key("p", pages)}
    assert len(keys) == 3
    assert c._key("p", pages) == hashlib.sha256(b"p" + pages.key.encode()).hexdigest()


# ---------------- switching in the service and the app ----------------

def test_service_finds_switches_and_records_the_reader(tmp_path, registry, svc_and_answers):
    _, answers = svc_and_answers
    srv = serve_in_thread(free_port(), "numind/NuExtract3", answers, require_template=True)
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        reg(registry, url)
        svc = Service(str(tmp_path / "s"), as_of=date(2026, 11, 20), model_mode="standin")
        assert svc.reader_id == "standin" and [o["id"] for o in svc.readers()["options"]] == ["standin", "ocr"]
        d = svc.find_readers()
        rid = f"local|{url}|numind/NuExtract3"
        assert d["added"] == [rid] and svc.find_readers()["added"] == []  # found once
        priv = next(o for o in d["options"] if o["id"] == rid)
        assert priv["kind"] == "private" and "never leave" in priv["note"]
        svc.select_reader(rid)
        assert svc.model_mode == "real" and svc.model is None and svc.model_label().startswith("NuExtract3")
        run = svc.wait(svc.start_sample("E3"))
        read = next(e for e in run["events"] if e["stage"] == "read")
        assert f"NuExtract3 (numind/NuExtract3) on 127.0.0.1:{srv.server_port}" in read["detail"]
        assert run["outcome"] == "HOLD"  # E3's designed outcome: confirm the inferred PO
        chk = svc.check_model()
        assert chk["ok"] and all(x["fields_right"] == x["fields"] and x["lines_right"] == x["lines"] for x in chk["docs"])
        assert svc.check_model()["docs"][0]["cached"]  # second time from the cache
        svc.select_reader("ocr")
        assert svc.model_mode == "none" and not svc.check_model()["ok"]
        with pytest.raises(ActionError):
            svc.select_reader("local|http://127.0.0.1:1|nope")
    finally:
        srv.shutdown()


def test_connecting_by_address(tmp_path, registry, svc_and_answers):
    _, answers = svc_and_answers
    srv = serve_in_thread(free_port(), "Qwen/Qwen3-VL-4B-Instruct", answers)
    svc = Service(str(tmp_path / "s"), model_mode="none")
    try:
        d = svc.find_readers(str(srv.server_port))  # just the port works
        assert any(o.get("model") == "Qwen/Qwen3-VL-4B-Instruct" for o in d["options"])
        with pytest.raises(ActionError, match="not this machine"):
            svc.find_readers("http://example.com:8000")
        with pytest.raises(ActionError, match="No vision model answered"):
            svc.find_readers(str(free_port()))
    finally:
        srv.shutdown()


def test_reader_api(tmp_path, registry, monkeypatch, svc_and_answers):
    from apflow.web.app import create_app
    _, answers = svc_and_answers
    srv = serve_in_thread(free_port(), "numind/NuExtract3", answers)
    try:
        reg(registry, f"http://127.0.0.1:{srv.server_port}")
        app = create_app(Service(str(tmp_path / "a"), model_mode="standin"))
        cl = app.test_client()
        r = cl.get("/api/readers").get_json()
        assert r["current"] == "standin"
        f = cl.post("/api/readers/find", json={}).get_json()
        rid = f["found"][0]
        s = cl.post("/api/readers/select", json={"id": rid}).get_json()
        assert s["current"] == rid and cl.get("/api/config").get_json()["reader"] == rid
        assert cl.post("/api/readers/select", json={"id": "bogus"}).status_code == 400
        assert cl.post("/api/readers/find", json={"url": "http://10.1.2.3:80"}).status_code == 400
        monkeypatch.setenv("APFLOW_READER_LOCKED", "1")
        assert cl.post("/api/readers/select", json={"id": "ocr"}).status_code == 400
    finally:
        srv.shutdown()


def test_start_on_a_private_reader_from_the_environment(tmp_path, registry, monkeypatch):
    srv = serve_in_thread(free_port(), "numind/NuExtract3", {})
    try:
        reg(registry, f"http://127.0.0.1:{srv.server_port}")
        monkeypatch.setenv("APFLOW_READER", "local")
        svc = Service(str(tmp_path / "e"), model_mode="standin")
        assert svc.reader_id.startswith("local|")
    finally:
        srv.shutdown()


# ---------------- launcher ----------------

@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("serve_reader", LAUNCHER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_launcher_skips_ports_in_use(launcher):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    p = busy.getsockname()[1]
    try:
        assert not launcher.port_is_free("127.0.0.1", p)
        got = launcher.find_free_port("127.0.0.1", p, p + 30)
        assert got != p and launcher.port_is_free("127.0.0.1", got)
        with pytest.raises(SystemExit):
            launcher.find_free_port("127.0.0.1", p, p)
    finally:
        busy.close()


class A:  # parsed arguments
    def __init__(self, **k):
        self.model, self.size, self.backend, self.cmd, self.hf = "nuextract", "auto", "auto", None, None
        self.__dict__.update(k)


GPU_IDLE = {"index": 1, "free_gb": 30.0, "total_gb": 40.0, "busy_pct": 5}
GPU_BUSY = {"index": 0, "free_gb": 3.0, "total_gb": 24.0, "busy_pct": 97}


def test_launcher_picks_the_least_busy_gpu_and_claims_only_free_memory(launcher):
    p = launcher.plan(A(), gpu_list=[GPU_BUSY, GPU_IDLE], which=lambda x: None, vllm_ok=True)
    assert p["backend"] == "vllm" and p["env"] == {"CUDA_VISIBLE_DEVICES": "1"}
    assert p["gpu_util"] == round((30 - 1) / 40, 2)
    cmd = launcher.command(p, "127.0.0.1", 8105, A())
    assert "numind/NuExtract3" in cmd and "--trust-remote-code" in cmd and cmd[cmd.index("--port") + 1] == "8105"


def test_launcher_falls_back_when_the_gpu_is_busy(launcher):
    which = {"llama-server": "/usr/bin/llama-server"}.get
    p = launcher.plan(A(), gpu_list=[GPU_BUSY], which=which, vllm_ok=True)
    assert p["backend"] == "llama.cpp"
    cmd = launcher.command(p, "127.0.0.1", 8101, A())
    assert cmd[:3] == ["llama-server", "-hf", "numind/NuExtract3-GGUF:Q4_K_M"] and "--jinja" in cmd
    assert cmd[cmd.index("--alias") + 1] == "NuExtract3" and cmd[cmd.index("-ngl") + 1] == "0"
    p = launcher.plan(A(), gpu_list=[], which={"ollama": "/usr/bin/ollama"}.get, vllm_ok=False)
    assert p["backend"] == "ollama"
    with pytest.raises(SystemExit, match="No model server software"):
        launcher.plan(A(), gpu_list=[], which=lambda x: None, vllm_ok=False)


def test_launcher_sizes_qwen_to_the_machine(launcher):
    p = launcher.plan(A(model="qwen3-vl"), gpu_list=[GPU_IDLE], which=lambda x: None, vllm_ok=True)
    assert p["model_key"] == "qwen3-vl:8b" and "Qwen/Qwen3-VL-8B-Instruct" in launcher.command(p, "127.0.0.1", 1, A())
    small = launcher.plan(A(model="qwen3-vl"), gpu_list=[], which={"ollama": "x"}.get, vllm_ok=False)
    if (launcher.ram_gb() or 0) < 16:
        assert small["model_key"] == "qwen3-vl:4b"
    assert launcher.plan(A(model="qwen3-vl", size="4b"), gpu_list=[GPU_IDLE], which=lambda x: None,
                         vllm_ok=True)["model_key"] == "qwen3-vl:4b"


def test_launcher_recognises_port_clashes(launcher):
    assert launcher.port_clash("OSError: [Errno 98] Address already in use")
    assert launcher.port_clash("error while attempting to bind on address ('127.0.0.1', 8100): only one usage of each socket address")
    assert not launcher.port_clash("CUDA out of memory")


def test_launcher_end_to_end_on_a_busy_machine(tmp_path, svc_and_answers):
    """A port in the range is taken; the launcher starts the server on the next free one,
    registers it, the app finds it there, and Ctrl+C removes the registration."""
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    p = busy.getsockname()[1]
    env = {**os.environ, "APFLOW_READERS_DIR": str(tmp_path / "readers")}
    cmd = f'"{sys.executable}" "{FAKE}" --port {{port}} --host {{host}} --model numind/NuExtract3 --loading 1.5'
    proc = subprocess.Popen([sys.executable, LAUNCHER, "--cmd", cmd, "--ports", f"{p}-{p + 40}"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        end = time.time() + 30
        files = []
        while time.time() < end and not files:
            time.sleep(0.3)
            files = list((tmp_path / "readers").glob("*.json")) if (tmp_path / "readers").exists() else []
        assert files, proc.stdout.read() if proc.poll() is not None else "no registration"
        entry = json.loads(files[0].read_text())
        assert entry["url"] != f"http://127.0.0.1:{p}" and entry["model"] == "numind/NuExtract3"
        found = lr.discover(env={"APFLOW_READERS_DIR": str(tmp_path / "readers")})
        assert [r["url"] for r in found if r["registered"]] == [entry["url"]]
        proc.send_signal(signal.SIGINT)
        out, _ = proc.communicate(timeout=20)
        assert "ready: numind/NuExtract3" in out
        assert not list((tmp_path / "readers").glob("*.json"))
        assert lr.probe(entry["url"], timeout=0.5) is None  # the model server was stopped too
    finally:
        busy.close()
        if proc.poll() is None:
            proc.kill()
