"""A stand-in model server for tests: speaks the OpenAI-style API (vLLM, llama.cpp) or Ollama's.

Answers come from a JSON file mapping sha256(base64 of the first page image) -> answer JSON,
so a test gets the right answer for a known page and an empty answer otherwise.

Run: python tests/fake_reader_server.py --port 8137 --model numind/NuExtract3 --answers a.json
Flags imitate real behaviour worth testing:
  --flavor ollama          also serves /api/version and /api/tags
  --require-template       400 unless chat_template_kwargs.template is sent (NuExtract)
  --reject-response-format 400 when response_format is sent (some servers)
  --think                  wraps the answer in <think>...</think> and a ```json fence
  --loading N              /health answers 503 for the first N seconds
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_server(port, model, answers, flavor="openai", require_template=False, reject_rf=False,
                think=False, loading=0.0, host="127.0.0.1"):
    started = time.time()
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/v1/models":
                return self._send(200, {"object": "list", "data": [{"id": model, "object": "model"}]})
            if self.path == "/health":
                return self._send(503 if time.time() - started < loading else 200, {"status": "ok"})
            if flavor == "ollama" and self.path == "/api/version":
                return self._send(200, {"version": "0.13.0"})
            if flavor == "ollama" and self.path == "/api/tags":
                return self._send(200, {"models": [{"name": model, "model": model}]})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                return self._send(404, {"error": "not found"})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            seen.append(body)
            if reject_rf and "response_format" in body:
                return self._send(400, {"error": {"message": "response_format is not supported"}})
            tmpl = (body.get("chat_template_kwargs") or {}).get("template")
            if require_template and not tmpl:
                return self._send(400, {"error": {"message": "template is required"}})
            pics = [p for p in body["messages"][0]["content"] if p.get("type") == "image_url"]
            key = hashlib.sha256(pics[0]["image_url"]["url"].split(",", 1)[1].encode()).hexdigest() if pics else ""
            ans = answers.get(key, {"doc_type": "unknown"})
            text = json.dumps(ans)
            if think:
                text = "<think>check {subtotal} + {tax} = {total}</think>\n```json\n" + text + "\n```"
            self._send(200, {"choices": [{"message": {"role": "assistant", "content": text}}]})

    srv = ThreadingHTTPServer((host, port), H)
    srv.seen = seen
    return srv


def serve_in_thread(*a, **k):
    srv = make_server(*a, **k)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", default="numind/NuExtract3")
    ap.add_argument("--answers")
    ap.add_argument("--flavor", default="openai")
    ap.add_argument("--require-template", action="store_true")
    ap.add_argument("--reject-response-format", action="store_true")
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--loading", type=float, default=0.0)
    a = ap.parse_args()
    answers = json.load(open(a.answers)) if a.answers else {}
    make_server(a.port, a.model, answers, a.flavor, a.require_template, a.reject_response_format,
                a.think, a.loading, a.host).serve_forever()
