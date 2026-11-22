"""Part 5 API: the contract the front end relies on, including the live event stream."""
import io
import json
import os
from datetime import date

import pytest

from apflow.service import Service
from apflow.web.app import create_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    d = tmp_path_factory.mktemp("app")
    svc = Service(str(d), as_of=date(2026, 11, 20))
    app = create_app(svc)
    app.config["TESTING"] = True
    c = app.test_client()
    c.svc = svc
    return c


def start(client, sid):
    r = client.post("/api/runs", json={"sample_id": sid})
    assert r.status_code == 202
    rid = r.json["run_id"]
    client.svc.wait(rid)
    return rid


def sse(body: str):
    out = []
    for block in body.strip().split("\n\n"):
        ev = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((ev.get("event"), json.loads(ev["data"]) if "data" in ev else None))
    return out


def test_index_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Invoice desk" in r.data
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/fonts/worksans-400.ttf").status_code == 200


def test_samples(client):
    s = client.get("/api/samples").json["samples"]
    assert len(s) == 25 and {"id", "title", "group", "scanned", "vendor"} <= set(s[0])
    assert s[0]["expect"] in ("APPROVE", "HOLD", "REJECT")  # the designed outcome, for grading runs
    assert "truth" not in s[0] and "file" not in s[0]  # the field-level answer key stays on the server


def test_event_stream_in_order_then_done(client):
    rid = start(client, "E4")
    evs = sse(client.get(f"/api/runs/{rid}/events").data.decode())
    assert [e for e, _ in evs].count("stage") == 8 and evs[-1][0] == "done"
    assert [d["stage"] for e, d in evs if e == "stage"] == ["intake", "read", "classify", "validate", "vendor", "po",
                                                            "risk", "decide"]
    po = next(d for e, d in evs if e == "stage" and d["stage"] == "po")
    assert po["status"] == "warn" and any(c["id"] == "C11" for c in po["checks"])


def test_event_stream_resumes(client):
    rid = start(client, "S02")
    evs = sse(client.get(f"/api/runs/{rid}/events?after=6").data.decode())
    assert [d["seq"] for e, d in evs if e == "stage"] == [7, 8]


def test_run_detail_hides_server_paths(client):
    rid = start(client, "S01")
    r = client.get(f"/api/runs/{rid}").json
    assert "file_path" not in r and r["decision"]["outcome"] == "APPROVE"


def test_page_image(client):
    rid = start(client, "S03")
    r = client.get(f"/api/runs/{rid}/page.png")
    assert r.status_code == 200 and r.mimetype == "image/png" and r.data[:4] == b"\x89PNG"


def test_upload_roundtrip(client):
    data = open(client.svc._samples["S02"]["file"], "rb").read()
    r = client.post("/api/runs", data={"file": (io.BytesIO(data), "my invoice.pdf")}, content_type="multipart/form-data")
    assert r.status_code == 202
    client.svc.wait(r.json["run_id"])
    assert client.get(f"/api/runs/{r.json['run_id']}").json["file_name"] == "my invoice.pdf"


@pytest.mark.parametrize("kwargs,code,msg", [
    ({"json": {}}, 400, "Send a PDF file or a sample_id"),
    ({"json": {"sample_id": "NOPE"}}, 400, "No sample called NOPE"),
    ({"data": {"file": (io.BytesIO(b"hello"), "x.pdf")}, "content_type": "multipart/form-data"}, 400, "not a PDF"),
])
def test_bad_requests_get_clear_errors(client, kwargs, code, msg):
    r = client.post("/api/runs", **kwargs)
    assert r.status_code == code and msg in r.json["error"]


def test_unknown_run_404(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/events").status_code == 404


def test_action_endpoint(client):
    rid = start(client, "H02")
    r = client.post(f"/api/runs/{rid}/actions", json={"action": "approve"})
    assert r.status_code == 400 and "reason" in r.json["error"]
    r = client.post(f"/api/runs/{rid}/actions", json={"action": "reject", "reason": "Only 196 accepted", "actor": "Stores"})
    assert r.status_code == 200 and r.json["final_status"] == "rejected"
    assert "file_path" not in r.json


def test_metrics_and_runs_list(client):
    m = client.get("/api/metrics").json
    assert {"processed", "no_touch_rate", "amount_waiting", "hold_reasons", "queue_by_owner", "outcomes"} <= set(m)
    runs = client.get("/api/runs?status=rejected").json["runs"]
    assert runs and all(r["final_status"] == "rejected" for r in runs)
