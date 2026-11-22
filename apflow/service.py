"""Service layer between the web app and the engine: runs, samples, human actions."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict
from datetime import date

from .company import Repo
from .generator import build
from .pipeline import reevaluate, run_invoice, sha256_of
from .scenarios import scenarios
from .schema import Extraction
from .store import FINAL_FROM_OUTCOME, Store

SAMPLE_GROUPS = [
    ("Should pass", ("S",)),
    ("Edge cases", ("E",)),
    ("Should be held", ("H",)),
    ("Should be rejected", ("R",)),
    ("Other countries", ("I",)),
]
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


class ActionError(ValueError):
    pass


class StandinModel:
    """Stand-in for the vision model, for the sample scans only: returns their answer key.

    Labelled as such in the interface. A real Gemini or Claude client replaces it
    in part 6; uploads never use it.
    """

    name = "stand-in model (sample answer keys)"

    def __init__(self, truths: dict):
        self.truths = truths  # sha256 -> truth dict

    def for_file(self, sha: str):
        t = self.truths.get(sha)
        if t is None:
            return None
        model = self

        class _One:
            def complete_json(self, prompt, images):
                d = {k: v for k, v in t.items() if k not in ("layout", "scanned")}
                d["confidence"] = {k: 0.9 for k in d}
                return json.dumps(d)
        return _One()


class Service:
    def __init__(self, data_dir: str, db_path: str | None = None, model_mode: str = "standin",
                 as_of: date | None = None, server_readers: bool | None = None):
        self.data_dir = data_dir
        self.samples_dir = os.path.join(data_dir, "samples")
        self.uploads_dir = os.path.join(data_dir, "uploads")
        self.pages_dir = os.path.join(data_dir, "pages")
        for d in (self.samples_dir, self.uploads_dir, self.pages_dir):
            os.makedirs(d, exist_ok=True)
        self.store = Store(db_path or os.path.join(data_dir, "apflow.db"))
        # The ERP (system of record for vendors, POs, goods receipts) and the desk's connection to it.
        from .erp.api import api_token
        from .erp.db import ErpDB
        from .erp_client import ErpClient
        self.erp_db = ErpDB(os.path.join(data_dir, "erp"))
        url = os.environ.get("APFLOW_ERP_URL", "").strip()
        if url == "self":  # the ERP hosted in this service, reached over HTTP like any other
            url = f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"
        self.erp = ErpClient(self.erp_db, url or None, os.environ.get("APFLOW_ERP_TOKEN") or api_token(self.erp_db))
        self.as_of = as_of
        self._samples = self._build_samples()
        # Readers: the cloud model (Gemini, Groq as backup) when a key is set; private models on
        # this company's own servers, found or added at run time; the labelled stand-in for the
        # sample scans; or none (OCR only). One is current; every new run uses it.
        from .models import CachedClient, model_from_env
        from .relay import BrowserRelay, RelayClient
        self._cache_dir = os.path.join(data_dir, "model_cache")
        self.cloud_model = model_from_env(self._cache_dir) if model_mode in ("auto", "real") else None
        self.standin = StandinModel({s["sha256"]: s["truth"] for s in self._samples.values()})
        self._allow_standin = model_mode in ("auto", "standin")
        self._local: dict[str, dict] = {}
        self._reader_lock = threading.Lock()
        # A model on the viewer's own computer, reached through the page they have open (see relay.py).
        self.relay = BrowserRelay()
        self._relay_client = CachedClient(RelayClient(self.relay), self._cache_dir)
        self.default_reader = "cloud" if self.cloud_model else ("standin" if self._allow_standin else "ocr")
        self.reader_id = self.default_reader
        want = os.environ.get("APFLOW_READER", "").strip()
        # Looking for model servers from the server itself only makes sense when the desk runs on the
        # same machine (or network) as the models: self-hosted, "host" and "local" are then the same
        # machine. On a hosted demo (Render) the server would only search Render's own machine, so it
        # is off and the viewer's browser looks on the viewer's computer instead. Kept, not deleted:
        # set APFLOW_SERVER_READERS=1 (or APFLOW_READER) when self-hosting next to the models.
        self.server_readers = (os.environ.get("APFLOW_SERVER_READERS", "") == "1" or bool(want)
                               if server_readers is None else server_readers)
        if want:  # local = first private model found; or a server address
            try:
                self.find_readers(None if want == "local" else want)
                if self._local:
                    self.select_reader(next(iter(self._local)))
            except ActionError:
                pass
        self._threads: dict[str, threading.Thread] = {}

    # ---------- samples ----------
    def _build_samples(self) -> dict:
        out = {}
        for s in scenarios():
            pdf = os.path.join(self.samples_dir, f"{s.id}.pdf")
            tpath = os.path.join(self.samples_dir, f"{s.id}.truth.json")
            if not (os.path.exists(pdf) and os.path.exists(tpath)):
                build(s.spec, self.samples_dir, s.id)
            truth = json.load(open(tpath))
            group = next(g for g, prefixes in SAMPLE_GROUPS if s.id[0] in prefixes)
            out[s.id] = {"id": s.id, "title": s.title, "group": group, "scanned": s.spec.scanned,
                         "file": pdf, "sha256": sha256_of(pdf), "truth": truth,
                         "vendor": s.spec.vendor.name, "expect": s.expect}
        return out

    def samples(self) -> list[dict]:
        return [{k: v for k, v in s.items() if k in ("id", "title", "group", "scanned", "vendor", "expect")}
                for s in self._samples.values()]

    # ---------- runs ----------
    def _repo(self) -> Repo:
        return self.erp.repo(self.store.ledger_entries(), self.store.approved_files())

    def start_sample(self, sample_id: str) -> str:
        s = self._samples.get(sample_id)
        if not s:
            raise ActionError(f"No sample called {sample_id}")
        return self._start(s["file"], f"{sample_id}.pdf", sample_id)

    def start_upload(self, data: bytes, file_name: str) -> str:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ActionError("That file is over 15 MB. Upload a single invoice PDF.")
        if not data.startswith(b"%PDF"):
            raise ActionError("That file is not a PDF. Upload the invoice as a PDF.")
        safe = "".join(ch for ch in os.path.basename(file_name or "upload.pdf") if ch.isalnum() or ch in "._- ")[:80]
        path = os.path.join(self.uploads_dir, f"{uuid.uuid4().hex[:8]}_{safe or 'upload.pdf'}")
        with open(path, "wb") as f:
            f.write(data)
        return self._start(path, safe or "upload.pdf", None)

    def _start(self, path: str, name: str, sample_id: str | None) -> str:
        run_id = uuid.uuid4().hex[:10]
        sha = sha256_of(path)
        self.store.start_run(run_id, name, path, sha, sample_id)
        th = threading.Thread(target=self._work, args=(run_id, path, sha), daemon=True)
        self._threads[run_id] = th
        th.start()
        return run_id

    def _work(self, run_id: str, path: str, sha: str):
        try:
            seen = self.store.find_sha(sha, exclude=run_id)
            real = self.real_model  # the reader is fixed for the whole run, even if someone switches it
            llm = real if real else (self.model.for_file(sha) if self.model else None)

            def on_event(e):
                d = asdict(e)
                if e.stage == "read" and real and "the vision model" in d["detail"]:
                    d["detail"] = d["detail"].replace("the vision model", real.name)  # which reader saw the file
                if e.stage == "intake" and seen:
                    d["detail"] += f"; this exact file was processed before (run {seen['run_id']})"
                self.store.add_event(run_id, d)

            rr = run_invoice(path, self._repo(), llm=llm, as_of=self.as_of or date.today(),
                             on_event=on_event, run_id=run_id)
            self.store.finish_run(rr)
        except Exception as e:  # the run fails visibly, never silently
            self.store.add_event(run_id, {"stage": "error", "label": "Run failed", "status": "fail", "ms": 0,
                                          "detail": f"{type(e).__name__}: {e}"})
            self.store.fail_run(run_id, f"{type(e).__name__}: {e}")

    def wait(self, run_id: str, timeout: float = 120) -> dict:
        th = self._threads.get(run_id)
        if th:
            th.join(timeout)
        return self.store.get_run(run_id)

    def run_all_samples(self) -> list[str]:
        """Process every sample in order (used to fill the dashboard for a demo)."""
        ids = []
        for sid in self._samples:
            rid = self.start_sample(sid)
            self.wait(rid)
            ids.append(rid)
        return ids

    # ---------- readers ----------
    @property
    def real_model(self):
        """The current model client (cloud or private), or None."""
        if self.reader_id == "cloud":
            return self.cloud_model
        if self.reader_id == "browser":
            return self._relay_client  # fails at once (and the run uses OCR) if no page is connected
        if self.reader_id in self._local:
            return self._local[self.reader_id]["client"]
        return None

    @property
    def model(self):
        return self.standin if self.reader_id == "standin" else None

    @property
    def model_mode(self) -> str:
        return "real" if self.real_model else ("standin" if self.reader_id == "standin" else "none")

    def readers(self) -> dict:
        opts = []
        if self.cloud_model:
            opts.append({"id": "cloud", "kind": "cloud", "label": f"Cloud: {self.cloud_model.name}",
                         "note": "Pages are sent to the cloud provider. Free tiers may keep and review what is sent."})
        if self.relay.model:
            live = self.relay.alive()
            opts.append({"id": "browser", "kind": "browser", "connected": live,
                         "label": self.relay.model["label"] + ("" if live else " (not connected)"),
                         "model": self.relay.model["model"],
                         "note": ("Scans go from this server to your open desk page and on to the model on your "
                                  "computer. No AI company sees them. Keep the desk tab open while invoices run."
                                  if live else "The page that connected it is closed or offline. Open the desk "
                                  "on that computer and connect again; until then scans go to OCR.")})
        for rid, r in self._local.items():
            opts.append({"id": rid, "kind": "private", "label": r["label"], "model": r["model"], "url": r["url"],
                         "note": "Runs on your own server: pages never leave it."})
        if self._allow_standin:
            opts.append({"id": "standin", "kind": "standin", "label": self.standin.name,
                         "note": "Demo only: answers for the built-in sample scans; other scans go to OCR."})
        opts.append({"id": "ocr", "kind": "ocr", "label": "No model: OCR on this server",
                     "note": "Nothing leaves this server. Slower, and more scans go to a person."})
        return {"current": self.reader_id, "label": self.model_label(), "options": opts,
                "server_scan": self.server_readers, "relay": self.relay.status()}

    # ---------- a model on the viewer's computer, through their browser ----------
    def relay_connect(self, url: str, model: str, flavor: str = "openai") -> dict:
        from .local_readers import family
        url, model = (url or "").strip(), (model or "").strip()
        if not url.startswith(("http://", "https://")) or not model:
            raise ActionError("Give the model server's address (for example http://127.0.0.1:8100) and a model name.")
        if not family(model):
            raise ActionError(f"{model} does not look like a vision model. Use NuExtract3, Qwen3-VL or another "
                              "model that reads images.")
        token = self.relay.connect(url, model, flavor if flavor in ("openai", "ollama") else "openai")
        with self._reader_lock:
            self.reader_id = "browser"
        return {**self.readers(), "token": token}

    def relay_disconnect(self, token: str) -> dict:
        if self.relay.check(token):
            self.relay.disconnect()
            with self._reader_lock:
                if self.reader_id == "browser":
                    self.reader_id = self.default_reader
        return self.readers()

    def find_readers(self, url: str | None = None) -> dict:
        """Look for private models on the server's own machine (and at `url` if given); add what is found.

        Off unless self.server_readers (see __init__): on a hosted demo the browser searches instead."""
        if not self.server_readers:
            raise ActionError("This desk is hosted, so it cannot see your computer. Use \"Find models on my "
                              "computer\": your browser looks for them and passes scans to them.")
        from .local_readers import LocalClient, discover, normalise_url
        from .models import CachedClient
        extra = []
        if url:
            try:
                extra = [normalise_url(url)]
            except ValueError as e:
                raise ActionError(str(e))
        own = int(os.environ.get("PORT", 8000))
        found = discover(extra, own_port=own)
        if extra and not any(r["url"] == extra[0] for r in found):
            raise ActionError(f"No vision model answered at {extra[0]}. Check the server is running and has "
                              "a vision model loaded (for example NuExtract3 or Qwen3-VL).")
        added = []
        with self._reader_lock:
            for r in found:
                if r["id"] not in self._local:
                    client = CachedClient(LocalClient(r["url"], r["model"], r["family"], r["flavor"]), self._cache_dir)
                    self._local[r["id"]] = {**r, "client": client}
                    added.append(r["id"])
        out = self.readers()
        out["found"] = [r["id"] for r in found]
        out["added"] = added
        return out

    def select_reader(self, reader_id: str) -> dict:
        valid = {o["id"] for o in self.readers()["options"]}
        if reader_id not in valid:
            raise ActionError("That reader is not available. Use Find models on my computer first.")
        with self._reader_lock:
            self.reader_id = reader_id
        return self.readers()

    def model_label(self) -> str:
        if self.reader_id == "browser" and not self.relay.alive():
            return "Model on your computer, not connected: scans go to OCR"
        if self.real_model:
            return self.real_model.name
        if self.model:
            return self.model.name
        return "OCR only (no model): scans go to a person"

    def check_model(self, sample_ids=("S05", "E3")) -> dict:
        """Run the real model on sample scans and score it against their answer keys."""
        from .evaluate import FIELDS, field_ok, lines_ok
        from .extract.routes import extract_llm_route
        m = self.real_model
        if not m:
            return {"ok": False, "error": "The current reader is not a model. Choose a cloud or private model under Reader, or set GEMINI_API_KEY and restart."}
        out = {"ok": True, "docs": []}
        for sid in sample_ids:
            s = self._samples[sid]
            t0 = time.time()
            hits0 = getattr(m, "hits", 0)
            try:
                ex = extract_llm_route(s["file"], m)
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"[:600], "model": m.name}
            wrong = [f for f in FIELDS if not field_ok(f, s["truth"].get(f), ex.get(f))]
            hit, n = lines_ok(s["truth"]["lines"], ex.lines)
            out["docs"].append({"sample": sid, "seconds": round(time.time() - t0, 1),
                                "fields_right": len(FIELDS) - len(wrong), "fields": len(FIELDS),
                                "wrong": {f: {"expected": s["truth"].get(f), "got": ex.get(f)} for f in wrong},
                                "lines_right": hit, "lines": n,
                                "cached": getattr(m, "hits", 0) > hits0})
        out["model"] = m.name
        return out

    # ---------- human actions ----------
    def act(self, run_id: str, action: str, reason: str = "", actor: str = "AP clerk", po: str | None = None) -> dict:
        run = self.store.get_run(run_id)
        if not run:
            raise ActionError("No such run")
        if run["state"] != "done":
            raise ActionError("This run has not finished yet")
        if run["final_status"] != "on_hold":
            raise ActionError(f"Only invoices on hold can be acted on; this one is {run['final_status'].replace('_', ' ')}")
        reason = (reason or "").strip()
        if action == "approve":
            if not reason:
                raise ActionError("Give a reason for approving an invoice the checks held")
            failed = {c["check_id"]: c for c in run["checks"] if c["status"] == "fail"}
            if "C07" in failed and not (failed["C07"].get("evidence") or {}).get("vendor_id"):
                raise ActionError("This vendor is not in the vendor master, so there are no verified bank details "
                                  "to pay. Add the vendor in the ERP, then use Check again.")
            if "C02" in failed:
                raise ActionError("The invoice number, date or total could not be read, so this payment could not be "
                                  "checked for duplicates later. Ask the vendor for a corrected invoice, or reject it.")
            self.store.add_action(run_id, actor, "approve", reason, {"overrode": run["outcome"]}, "approved")
        elif action == "reject":
            if not reason:
                raise ActionError("Give a reason the vendor will see")
            self.store.add_action(run_id, actor, "reject", reason, {}, "rejected")
        elif action == "approve_part":
            if run["approvable"] is None:
                raise ActionError("This invoice has no part that can be approved on its own")
            self.store.add_action(run_id, actor, "approve_part", reason or "Approved up to the PO balance",
                                  {"approved": run["approvable"], "held": run["held"]}, "part_approved")
        elif action == "confirm_po":
            if not po:
                raise ActionError("Choose the PO to confirm")
            repo = self._repo()
            if po not in repo.pos:
                raise ActionError(f"{po} is not a known PO")
            ex = Extraction.from_dict(run["extraction"])
            from .schema import Field
            ex.fields["po_reference"] = Field(po, 1.0, f"confirmed by {actor}")
            ev = (run["evaluation"] or 1) + 1
            self.store.add_action(run_id, actor, "confirm_po", reason or f"Confirmed {po}", {"po": po})
            self.store.add_event(run_id, {"stage": "human", "label": "PO confirmed by a person", "status": "ok",
                                          "ms": 0, "detail": f"{actor} confirmed {po}; checks run again"}, ev)
            rr = reevaluate(run_id, run["file_path"], run["sha256"], ex, repo, self.as_of or date.today(),
                            on_event=lambda e: self.store.add_event(run_id, asdict(e), ev))
            self.store.finish_run(rr, evaluation=ev)
        elif action == "recheck":  # a PO, goods receipt or vendor was added in the ERP since
            repo = self._repo()
            ex = Extraction.from_dict(run["extraction"])
            ev = (run["evaluation"] or 1) + 1
            self.store.add_action(run_id, actor, "recheck", reason or "Checked again against the ERP")
            self.store.add_event(run_id, {"stage": "human", "label": "Checked again against the ERP", "status": "ok",
                                          "ms": 0, "detail": f"{actor} re-ran the checks with the ERP's current vendors, "
                                          "POs and goods receipts"}, ev)
            rr = reevaluate(run_id, run["file_path"], run["sha256"], ex, repo, self.as_of or date.today(),
                            on_event=lambda e: self.store.add_event(run_id, asdict(e), ev))
            self.store.finish_run(rr, evaluation=ev)
        else:
            raise ActionError(f"Unknown action {action}")
        return self.store.get_run(run_id)

    # ---------- document image ----------
    def page_png(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        if not run:
            raise ActionError("No such run")
        out = os.path.join(self.pages_dir, f"{run['sha256'][:16]}.png")
        if not os.path.exists(out):
            base = out[:-4]
            subprocess.run(["pdftoppm", "-r", "110", "-png", "-singlefile", "-f", "1", "-l", "1",
                            run["file_path"], base], check=True, timeout=60)
        return out

    def reset(self):
        self.store.reset()
