#!/usr/bin/env python3
"""Start a private reader (a vision model on this computer) for the invoice desk.

Two models are supported, both about 4 billion parameters and Apache-2.0 licensed:
  NuExtract3 (Numind)    --model nuextract   default; runs best under llama.cpp
  Qwen3-VL 4B (Alibaba)  --model qwen3-vl    runs best under Ollama

It never assumes a fixed port or an idle machine:
  * picks the first free port in a range (default 8100-8120, where the desk's page looks),
    skipping anything already listening, and moves on if another program grabs the port first;
  * on NVIDIA machines, picks the GPU with the most free memory and only claims what is free;
  * chooses a backend that fits: vLLM when a GPU has room, else llama.cpp (llama-server), else Ollama;
  * lets the hosted desk's page talk to the model (CORS): --origin is the desk's web address;
  * registers the server in ~/.apflow/readers, so a desk running on this same computer also finds
    it, and removes the registration when stopped (Ctrl+C).

Examples:
  python serve_reader.py                           # NuExtract3
  python serve_reader.py --model qwen3-vl          # Qwen3-VL 4B
  python serve_reader.py --backend ollama
  python serve_reader.py --origin http://localhost:8000    # a desk you run yourself
  python serve_reader.py --dry-run                 # show the plan, start nothing
  python serve_reader.py --list                    # registered readers and whether they answer
  python serve_reader.py --cmd "my-server --port {port} --host {host}"    # any other server

Then open the desk, go to Dashboard > Reader and press "Find models on my computer".
Standard library only (Python 3.9+), so it runs without the app installed.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

DESK = "https://apcycle-za.onrender.com"  # the hosted desk; its page calls the model from your browser

MODELS = {
    "nuextract": {
        "label": "NuExtract3 (Numind, 4B, Apache-2.0)",
        "vllm": "numind/NuExtract3",
        "vllm_args": ["--trust-remote-code", "--limit-mm-per-prompt", '{"image": 6, "video": 0}',
                      "--chat-template-content-format", "openai", "--generation-config", "vllm",
                      "--max-model-len", "16384"],
        "vllm_gb": 16,
        "gguf": "numind/NuExtract3-GGUF:Q4_K_M",
        "ollama": "hf.co/numind/NuExtract3-GGUF:Q4_K_M",
        "alias": "NuExtract3",
        "ram_gb": 6,
        "download": "about 3.4 GB (2.7 GB model and 0.7 GB image encoder)",
        "prefer": "llama.cpp",
    },
    "qwen3-vl": {
        "label": "Qwen3-VL 4B Instruct (Alibaba, Apache-2.0)",
        "vllm": "Qwen/Qwen3-VL-4B-Instruct",
        "vllm_args": ["--limit-mm-per-prompt", '{"image": 2, "video": 0}', "--max-model-len", "16384"],
        "vllm_gb": 14,
        "gguf": "Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M",
        "ollama": "qwen3-vl:4b",
        "alias": "qwen3-vl-4b-instruct",
        "ram_gb": 6,
        "download": "about 3.3 GB",
        "prefer": "ollama",
    },
}

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local calls never use a proxy


def say(msg: str):
    print(f"[reader] {msg}", flush=True)


def registry_dir() -> str:
    return os.environ.get("APFLOW_READERS_DIR") or os.path.join(os.path.expanduser("~"), ".apflow", "readers")


# ---------------- machine ----------------

def port_is_free(host: str, port: int) -> bool:
    """Free = nothing answers there and we can bind it ourselves."""
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return False
    except OSError:
        pass
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(host: str, start: int, end: int, skip=()) -> int:
    for p in range(start, end + 1):
        if p not in skip and port_is_free(host, p):
            return p
    raise SystemExit(f"No free port between {start} and {end}. Pass a wider range with --ports.")


def gpus(run=subprocess.run) -> list[dict]:
    """NVIDIA GPUs with free and total memory in GB; [] when there are none or nvidia-smi is missing."""
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = run(["nvidia-smi", "--query-gpu=index,memory.free,memory.total,utilization.gpu",
                   "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    res = []
    for line in out.strip().splitlines():
        try:
            i, free, total, util = [x.strip() for x in line.split(",")]
            res.append({"index": int(i), "free_gb": float(free) / 1024, "total_gb": float(total) / 1024,
                        "busy_pct": float(util)})
        except ValueError:
            continue
    return res


def ram_gb() -> float | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (ValueError, OSError, AttributeError):
        pass
    try:  # Windows
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("avail", ctypes.c_ulonglong), ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong),
                        ("c", ctypes.c_ulonglong), ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
        m = MS(); m.l = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.total / 1024 ** 3
    except Exception:
        return None


def have_vllm() -> bool:
    if shutil.which("vllm"):
        return True
    import importlib.util
    return importlib.util.find_spec("vllm") is not None


def get_json(url: str, timeout: float = 2.0):
    try:
        with DIRECT.open(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return None, None


# ---------------- plan ----------------

def model_key(name: str) -> str:
    return "qwen3-vl" if name.lower().startswith("qwen") else "nuextract"


def origins(args) -> list[str]:
    """Web pages allowed to call the model: the desk (hosted, or your own on port 8000)."""
    given = [o.strip().rstrip("/") for o in (getattr(args, "origin", None) or DESK).split(",") if o.strip()]
    return list(dict.fromkeys(given + ["http://localhost:8000", "http://127.0.0.1:8000"]))


def plan(args, gpu_list=None, which=shutil.which, vllm_ok=None) -> dict:
    gpu_list = gpus() if gpu_list is None else gpu_list
    best = max(gpu_list, key=lambda g: g["free_gb"], default=None)
    best_free = best["free_gb"] if best else 0.0
    ram = ram_gb()
    key = model_key(args.model)
    m = MODELS[key]
    vllm_ok = have_vllm() if vllm_ok is None else vllm_ok
    backend = args.backend
    if args.cmd:
        backend = "custom"
    elif backend == "auto":
        tools = [b for b in ("llama.cpp", "ollama") if which("llama-server" if b == "llama.cpp" else b)]
        if vllm_ok and best and best_free >= m["vllm_gb"]:
            backend = "vllm"
        elif tools:
            backend = m["prefer"] if m["prefer"] in tools else tools[0]
        else:
            raise SystemExit(
                "No model server software found. Install one of:\n"
                "  llama.cpp: winget install llama.cpp   (Windows)  |  brew install llama.cpp   (macOS, Linux)\n"
                "  Ollama (Windows, macOS, Linux): https://ollama.com/download\n"
                "  vLLM (Linux + NVIDIA GPU): pip install vllm\n"
                "then open a new terminal and run this script again.")
    p = {"model_key": key, "model": m, "backend": backend, "gpu": best, "ram_gb": ram, "env": {}}
    if backend == "vllm":
        if not best:
            raise SystemExit("vLLM needs an NVIDIA GPU; none was found. Use --backend llama.cpp or ollama.")
        # Claim only what is free on the least busy GPU, leaving 1 GB headroom for its other users.
        util = max(0.1, min(0.90, (best["free_gb"] - 1.0) / best["total_gb"]))
        if best["free_gb"] < m["vllm_gb"]:
            say(f"warning: GPU {best['index']} has {best['free_gb']:.1f} GB free; {m['label']} wants about "
                f"{m['vllm_gb']} GB under vLLM. It may fail to start; llama.cpp or Ollama need less.")
        p["env"] = {"CUDA_VISIBLE_DEVICES": str(best["index"])}
        p["gpu_util"] = round(util, 2)
    if backend == "ollama" and key == "nuextract":
        say("note: NuExtract3 is most reliable under llama.cpp (winget install llama.cpp, or brew install "
            "llama.cpp). Trying Ollama.")
    if ram and ram < m["ram_gb"] and backend in ("llama.cpp", "ollama") and best_free < 4:
        say(f"warning: {ram:.0f} GB of memory; {m['label']} needs about {m['ram_gb']} GB. Close other programs.")
    return p


def command(p: dict, host: str, port: int, args) -> list[str]:
    m, b = p["model"], p["backend"]
    if b == "custom":
        return shlex.split(args.cmd.format(port=port, host=host), posix=os.name != "nt")
    if b == "vllm":
        exe = ([shutil.which("vllm"), "serve", m["vllm"]] if shutil.which("vllm")
               else [sys.executable, "-m", "vllm.entrypoints.openai.api_server", "--model", m["vllm"]])
        return exe + ["--host", host, "--port", str(port), "--allowed-origins", json.dumps(origins(args)),
                      "--gpu-memory-utilization", str(p["gpu_util"])] + m["vllm_args"]
    if b == "llama.cpp":
        ngl = "99" if p.get("gpu") and p["gpu"]["free_gb"] >= 4 else "0"
        # llama-server answers requests from any web page (CORS) by default
        return ["llama-server", "-hf", args.hf or m["gguf"], "--host", host, "--port", str(port),
                "--jinja", "--alias", m["alias"], "-c", "16384", "-ngl", ngl]
    if b == "ollama":
        return ["ollama", "serve"]  # its address and allowed pages come from OLLAMA_HOST and OLLAMA_ORIGINS
    raise SystemExit(f"Unknown backend {b}")


def backend_env(p: dict, hostpart: str, port: int, args) -> dict:
    env = {**os.environ, **p["env"]}
    if p["backend"] == "ollama":
        # Our own Ollama on its own port, allowing the desk's page. An Ollama already running on this
        # computer keeps running; the two share downloaded models.
        env["OLLAMA_HOST"] = f"{hostpart}:{port}"
        env["OLLAMA_ORIGINS"] = ",".join(origins(args))
    return env


def cors_check(url: str, origin: str) -> tuple[bool, str]:
    """Would a page at `origin` be allowed to call this server? (What the browser checks.)"""
    def allowed(headers) -> bool:
        a = (headers.get("Access-Control-Allow-Origin") or "").strip()
        return a in ("*", origin)
    try:
        req = urllib.request.Request(url + "/v1/models", headers={"Origin": origin})
        with DIRECT.open(req, timeout=5) as r:
            if not allowed(r.headers):
                return False, "the server does not allow requests from that page (no CORS header)"
        req = urllib.request.Request(url + "/v1/chat/completions", method="OPTIONS", headers={
            "Origin": origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        try:
            with DIRECT.open(req, timeout=5) as r:
                ok = allowed(r.headers)
        except urllib.error.HTTPError as e:
            ok = e.code < 400 and allowed(e.headers)
        return (True, "ok") if ok else (False, "the server refuses the browser's pre-check for sending a page")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} for a request from that page"
    except OSError as e:
        return False, f"could not reach the server ({e})"


# ---------------- registry ----------------

def register(url: str, model: str, backend: str, pid, log: str | None) -> str:
    d = registry_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, url.split("://", 1)[1].replace(":", "_").replace("[", "").replace("]", "") + ".json")
    with open(path, "w") as f:
        json.dump({"url": url, "model": model, "backend": backend, "pid": pid,
                   "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "log": log}, f, indent=2)
    return path


def list_registry(prune: bool = False) -> int:
    d = registry_dir()
    files = sorted(f for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []
    if not files:
        say(f"No readers registered in {d}.")
        return 0
    for f in files:
        path = os.path.join(d, f)
        try:
            r = json.load(open(path))
        except (OSError, ValueError):
            continue
        code, body = get_json(r["url"] + "/v1/models", 1.5)
        alive = code == 200
        say(f"{'answering' if alive else 'not answering'}: {r['url']}  {r.get('model')}  ({r.get('backend')})")
        if prune and not alive:
            os.remove(path)
            say(f"  removed {f}")
    return 0


# ---------------- run ----------------

def served_model(url: str) -> str | None:
    """The model id the server reports once it is ready."""
    code, body = get_json(url + "/v1/models", 2.0)
    if code != 200 or not isinstance(body, dict) or not body.get("data"):
        return None
    hc, _ = get_json(url + "/health", 2.0)  # llama.cpp says 503 while it is still loading
    if hc not in (200, 404, None):
        return None
    return body["data"][0].get("id")


def wait_ready(url: str, proc, timeout: float, log_path: str | None) -> str | None:
    end, last_note = time.time() + timeout, time.time()
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            return None
        mid = served_model(url)
        if mid:
            return mid
        if time.time() - last_note > 20:
            say("still starting (the first start downloads the model, which can take several minutes)"
                + (f"; log: {log_path}" if log_path else ""))
            last_note = time.time()
        time.sleep(1.0)
    return None


def tail(path: str | None, n: int = 15) -> str:
    if not path or not os.path.exists(path):
        return ""
    with open(path, errors="replace") as f:
        return "".join(f.readlines()[-n:])


def port_clash(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ("address already in use", "only one usage of each socket address",
                                "couldn't bind", "could not bind", "bind: address", "errno 98", "errno 48",
                                "error 10048"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Start a private reader for the invoice desk on a free port.")
    ap.add_argument("--model", choices=["nuextract", "qwen3-vl"], default="nuextract")
    ap.add_argument("--backend", choices=["auto", "vllm", "llama.cpp", "ollama"], default="auto")
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 keeps the model private to this computer")
    ap.add_argument("--ports", default="8100-8120", help="range to pick a free port from (the desk looks in 8100-8120)")
    ap.add_argument("--origin", default=DESK, help="web address of the desk whose page will call the model "
                    "(comma-separated for several)")
    ap.add_argument("--hf", help="override the GGUF repo for llama.cpp, e.g. owner/repo:Q4_K_M")
    ap.add_argument("--cmd", help="start any other server; {port} and {host} are filled in")
    ap.add_argument("--wait", type=float, default=1800, help="seconds to wait for the model to load")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="show registered readers")
    ap.add_argument("--prune", action="store_true", help="with --list: remove readers that do not answer")
    args = ap.parse_args(argv)

    if args.list:
        return list_registry(args.prune)

    start, _, end = args.ports.partition("-")
    start, end = int(start), int(end or start)
    p = plan(args)
    m = p["model"]
    gpu = p["gpu"]
    say(f"model: {m['label']}; first start downloads {m['download']}")
    say(f"backend: {p['backend']}" + (f" on GPU {gpu['index']} ({gpu['free_gb']:.1f} of {gpu['total_gb']:.1f} GB free, "
                                      f"{gpu['busy_pct']:.0f}% busy)" if gpu and p["backend"] == "vllm" else ""))
    say("pages allowed to use it: " + ", ".join(origins(args)))

    log_dir = os.path.join(os.path.dirname(registry_dir()), "logs")
    os.makedirs(log_dir, exist_ok=True)
    tried = set()
    for attempt in range(5):
        port = find_free_port(args.host, start, end, skip=tried)
        tried.add(port)
        hostpart = f"[{args.host}]" if ":" in args.host else args.host
        url = f"http://{hostpart}:{port}"
        cmd = command(p, args.host, port, args)
        env = backend_env(p, hostpart, port, args)
        say(f"port {port} is free")
        say("command: " + " ".join(shlex.quote(c) for c in cmd)
            + (f"  (with OLLAMA_HOST={env['OLLAMA_HOST']} OLLAMA_ORIGINS={env['OLLAMA_ORIGINS']})"
               if p["backend"] == "ollama" else ""))
        if args.dry_run:
            if p["backend"] == "ollama":
                say(f"then: ollama pull {m['ollama']}")
            return 0
        log_path = os.path.join(log_dir, f"reader-{port}.log")
        log = open(log_path, "w")
        try:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
        except FileNotFoundError:
            say(f"could not start {cmd[0]}: not found")
            return 1
        stopper = lambda: stop(proc)  # noqa: E731
        atexit.register(stopper)
        if p["backend"] == "ollama":
            if not wait_for(lambda: get_json(url + "/api/version", 1.0)[0] == 200, 60, proc):
                stop(proc)
                if port_clash(tail(log_path, 40)):
                    say(f"port {port} was taken meanwhile; trying another")
                    continue
                say("Ollama did not start:\n" + tail(log_path))
                return 1
            say(f"downloading {m['ollama']} if needed ({m['download']}, first time only)")
            if subprocess.run(["ollama", "pull", m["ollama"]], env=env).returncode != 0:
                stop(proc)
                say("ollama pull failed")
                return 1
            mid = m["ollama"]
        else:
            mid = wait_ready(url, proc, args.wait, log_path)
        if not mid:
            out = tail(log_path, 40)
            stop(proc)
            atexit.unregister(stopper)
            if port_clash(out):
                say(f"port {port} was taken meanwhile; trying another")
                continue
            say("the server did not come up. Last lines of its log:\n" + out)
            return 1
        reg = register(url, mid, p["backend"], proc.pid, log_path)
        ok, why = cors_check(url, origins(args)[0])
        if ok:
            say(f"checked: a page at {origins(args)[0]} may call this model")
        else:
            say(f"warning: a page at {origins(args)[0]} will be refused: {why}. "
                "See How it works > Private models > Troubleshooting on the desk.")
        return hold(reg, proc, url, mid, origins(args)[0])
    say("gave up after 5 ports were taken while starting")
    return 1


def wait_for(cond, timeout: float, proc) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        if cond():
            return True
        time.sleep(0.5)
    return False


def stop(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()


def hold(reg_path: str, proc, url: str, model: str, desk: str = DESK) -> int:
    """Keep the reader registered until it stops or the user presses Ctrl+C."""
    def cleanup(*_):
        try:
            os.remove(reg_path)
        except OSError:
            pass
        stop(proc)
    atexit.register(cleanup)

    def on_signal(signum, frame):
        cleanup()
        raise SystemExit(0)
    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_signal)

    say(f"ready: {model} at {url}")
    say(f"Now open {desk}/#/dashboard and press \"Find models on my computer\" under Reader")
    say(f"(or type {url} in its address box). If Chrome asks to let the site reach devices on your")
    say("network or this computer, choose Allow. Keep this window open; press Ctrl+C to stop the model.")
    while True:
        if proc is not None and proc.poll() is not None:
            say(f"the server stopped (exit code {proc.returncode})")
            cleanup()
            return 1
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
