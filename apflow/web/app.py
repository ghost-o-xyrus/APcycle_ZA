"""Flask app (part 5): the invoice desk.

Run locally:  python -m apflow.web.app   (http://localhost:8000)
Env: APFLOW_DATA (data dir), APFLOW_MODEL (auto | standin | none), GEMINI_API_KEY, GROQ_API_KEY, PORT.
"""
from __future__ import annotations

import json
import os
import time

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from ..service import ActionError, Service

HERE = os.path.dirname(__file__)


def create_app(service: Service | None = None) -> Flask:
    app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    svc = service or Service(os.environ.get("APFLOW_DATA", os.path.join(os.getcwd(), "data", "app")),
                             model_mode=os.environ.get("APFLOW_MODEL", "auto"))
    app.extensions["apflow"] = svc

    @app.errorhandler(ActionError)
    def _bad(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(413)
    def _too_big(e):
        return jsonify(error="That file is over 15 MB. Upload a single invoice PDF."), 413

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/config")
    def config():
        return jsonify(model=svc.model_label(), model_mode=svc.model_mode, company="Kaveri Instruments Pvt Ltd")

    @app.post("/api/model-check")
    def model_check():
        return jsonify(svc.check_model())

    @app.get("/api/samples")
    def samples():
        return jsonify(samples=svc.samples())

    @app.post("/api/runs")
    def create_run():
        if "file" in request.files:
            f = request.files["file"]
            rid = svc.start_upload(f.read(), f.filename)
        else:
            body = request.get_json(silent=True) or {}
            if not body.get("sample_id"):
                raise ActionError("Send a PDF file or a sample_id")
            rid = svc.start_sample(body["sample_id"])
        return jsonify(run_id=rid), 202

    @app.get("/api/runs")
    def list_runs():
        a = request.args
        return jsonify(runs=svc.store.list_runs(a.get("outcome"), a.get("owner"), a.get("status")))

    @app.get("/api/runs/<run_id>")
    def get_run(run_id):
        r = svc.store.get_run(run_id)
        if not r:
            return jsonify(error="No such run"), 404
        r.pop("file_path", None)
        return jsonify(r)

    @app.get("/api/runs/<run_id>/events")
    def stream(run_id):
        if not svc.store.get_run(run_id):
            return jsonify(error="No such run"), 404
        after = int(request.args.get("after", 0))

        def gen():
            last, start = after, time.time()
            while time.time() - start < 180:
                for e in svc.store.events(run_id, last):
                    last = e["seq"]
                    yield f"id: {e['seq']}\nevent: stage\ndata: {json.dumps(e)}\n\n"
                run = svc.store.get_run(run_id)
                if run["state"] != "running" and not svc.store.events(run_id, last):
                    yield f"event: done\ndata: {json.dumps({'state': run['state']})}\n\n"
                    return
                time.sleep(0.15)
            yield "event: done\ndata: {\"state\": \"timeout\"}\n\n"

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/<run_id>/actions")
    def act(run_id):
        b = request.get_json(silent=True) or {}
        r = svc.act(run_id, b.get("action", ""), b.get("reason", ""), b.get("actor", "AP clerk"), b.get("po"))
        r.pop("file_path", None)
        return jsonify(r)

    @app.get("/api/runs/<run_id>/page.png")
    def page(run_id):
        return send_file(svc.page_png(run_id), mimetype="image/png", max_age=3600)

    @app.get("/api/metrics")
    def metrics():
        return jsonify(svc.store.metrics())

    @app.post("/api/demo/run-all")
    def run_all():
        import threading
        threading.Thread(target=svc.run_all_samples, daemon=True).start()
        return jsonify(started=len(svc.samples())), 202

    @app.post("/api/demo/reset")
    def reset():
        svc.reset()
        return jsonify(ok=True)

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
