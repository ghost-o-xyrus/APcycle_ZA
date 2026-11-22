"""Run store (part 4): every run, its stage events, checks, decision and human actions, in SQLite.

The store is also the system's memory: approved runs are fed back as invoice
history, so a resubmitted invoice is caught as a duplicate and running PO
balances include what this app has already approved.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict

from .company import LedgerInvoice

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, created_at REAL, file_name TEXT, file_path TEXT, sha256 TEXT, sample_id TEXT,
  state TEXT,                       -- running | done | error
  error TEXT,
  route TEXT, vendor_id TEXT, vendor_name TEXT, invoice_number TEXT, invoice_date TEXT,
  po_number TEXT, po_how TEXT, subtotal REAL, total REAL, currency TEXT DEFAULT 'INR', entity TEXT,
  outcome TEXT,                     -- system decision: APPROVE | HOLD | REJECT
  final_status TEXT,                -- approved | part_approved | rejected | on_hold
  owner TEXT, question TEXT, summary TEXT, approvable REAL, held REAL, approval_level TEXT,
  total_ms INTEGER, evaluation INTEGER DEFAULT 1,
  extraction_json TEXT, decision_json TEXT, matches_json TEXT, route_log_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT, seq INTEGER, ts REAL, stage TEXT, label TEXT, status TEXT, ms INTEGER, detail TEXT,
  checks_json TEXT, evaluation INTEGER, PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS checks (
  run_id TEXT, evaluation INTEGER, check_id TEXT, name TEXT, status TEXT, severity TEXT, message TEXT,
  owner TEXT, question TEXT, evidence_json TEXT
);
CREATE TABLE IF NOT EXISTS actions (
  run_id TEXT, ts REAL, actor TEXT, action TEXT, reason TEXT, detail_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_created ON runs(created_at);
"""

FINAL_FROM_OUTCOME = {"APPROVE": "approved", "HOLD": "on_hold", "REJECT": "rejected"}


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
        for col, decl in (("currency", "TEXT DEFAULT 'INR'"), ("entity", "TEXT"), ("first_outcome", "TEXT")):  # stores made before these existed
            if col not in cols:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---------- writing ----------
    def start_run(self, run_id, file_name, file_path, sha256, sample_id=None):
        with self.tx() as c:
            c.execute("INSERT INTO runs (run_id, created_at, file_name, file_path, sha256, sample_id, state) "
                      "VALUES (?,?,?,?,?,?, 'running')", (run_id, time.time(), file_name, file_path, sha256, sample_id))

    def add_event(self, run_id, ev: dict, evaluation=1):
        with self.tx() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
            c.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (run_id, seq, time.time(), ev["stage"], ev.get("label", ev["stage"]), ev["status"],
                       ev.get("ms", 0), ev.get("detail", ""), json.dumps(ev.get("checks", [])), evaluation))
            return seq

    def finish_run(self, rr, evaluation=1):
        ex, d = rr.extraction, rr.decision
        with self.tx() as c:
            c.execute("DELETE FROM checks WHERE run_id=? AND evaluation=?", (rr.run_id, evaluation))
            for ch in rr.checks:
                c.execute("INSERT INTO checks VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (rr.run_id, evaluation, ch.id, ch.name, ch.status, ch.severity, ch.message, ch.owner,
                           ch.question, json.dumps(ch.evidence, default=str)))
            c.execute("""UPDATE runs SET state='done', route=?, vendor_id=?, vendor_name=?, invoice_number=?,
                         invoice_date=?, po_number=?, po_how=?, subtotal=?, total=?, outcome=?, final_status=?,
                         owner=?, question=?, summary=?, approvable=?, held=?, approval_level=?, total_ms=?,
                         evaluation=?, extraction_json=?, decision_json=?, matches_json=?, route_log_json=?,
                         currency=?, entity=? WHERE run_id=?""",
                      (ex.route, rr.vendor_id, rr.vendor_name, ex.get("invoice_number"), ex.get("invoice_date"),
                       rr.po_number, rr.po_how, rr.subtotal, ex.get("total"), d.outcome,
                       FINAL_FROM_OUTCOME[d.outcome], d.owner, d.question, d.summary, d.approvable_amount,
                       d.held_amount, d.approval_level, sum(e.ms for e in rr.events), evaluation,
                       json.dumps(ex.to_dict()), json.dumps(asdict(d)), json.dumps(rr.matches),
                       json.dumps(rr.route_log), getattr(rr, "currency", "INR"), getattr(rr, "entity", None), rr.run_id))
            if evaluation == 1:  # the machine's own first decision, kept when a person's action re-evaluates
                c.execute("UPDATE runs SET first_outcome=? WHERE run_id=?", (d.outcome, rr.run_id))

    def fail_run(self, run_id, error: str):
        with self.tx() as c:
            c.execute("UPDATE runs SET state='error', error=? WHERE run_id=?", (error[:500], run_id))

    def add_action(self, run_id, actor, action, reason, detail=None, final_status=None):
        with self.tx() as c:
            c.execute("INSERT INTO actions VALUES (?,?,?,?,?,?)",
                      (run_id, time.time(), actor, action, reason, json.dumps(detail or {}, default=str)))
            if final_status:
                c.execute("UPDATE runs SET final_status=? WHERE run_id=?", (final_status, run_id))

    def reset(self):
        with self.tx() as c:
            for t in ("runs", "events", "checks", "actions"):
                c.execute(f"DELETE FROM {t}")

    # ---------- reading ----------
    def get_run(self, run_id) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not r:
                return None
            run = dict(r)
            for k in ("extraction_json", "decision_json", "matches_json", "route_log_json"):
                run[k[:-5]] = json.loads(run.pop(k)) if run.get(k) else None
            ev = run["evaluation"] or 1
            run["checks"] = [dict(x, evidence=json.loads(x["evidence_json"])) for x in
                             (dict(y) for y in self._conn.execute(
                                 "SELECT * FROM checks WHERE run_id=? AND evaluation=? ORDER BY rowid", (run_id, ev)))]
            for ch in run["checks"]:
                ch.pop("evidence_json", None)
                ch["id"] = ch["check_id"]
            run["events"] = self.events(run_id)
            run["actions"] = [dict(a, detail=json.loads(a["detail_json"])) for a in
                              (dict(y) for y in self._conn.execute(
                                  "SELECT * FROM actions WHERE run_id=? ORDER BY ts", (run_id,)))]
            for a in run["actions"]:
                a.pop("detail_json", None)
            return run

    def events(self, run_id, after_seq=0) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq",
                                      (run_id, after_seq)).fetchall()
            return [dict(dict(r), checks=json.loads(r["checks_json"])) for r in rows]

    def list_runs(self, outcome=None, owner=None, final_status=None, limit=500) -> list[dict]:
        q, args = "SELECT run_id, created_at, file_name, sample_id, state, route, vendor_name, invoice_number, " \
                  "po_number, po_how, subtotal, total, currency, entity, outcome, COALESCE(first_outcome, outcome) AS first_outcome, " \
                  "final_status, owner, summary, total_ms, error " \
                  "FROM runs WHERE 1=1", []
        for col, val in (("outcome", outcome), ("owner", owner), ("final_status", final_status)):
            if val:
                q += f" AND {col}=?"
                args.append(val)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def find_sha(self, sha256, exclude=None) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT run_id, created_at, final_status FROM runs WHERE sha256=? AND run_id<>? "
                                   "AND state='done' ORDER BY created_at LIMIT 1", (sha256, exclude or "")).fetchone()
            return dict(r) if r else None

    def approved_files(self) -> dict:
        """sha256 -> the earlier run, for every file a person or the rules approved (fully or in part)."""
        with self._lock:
            rows = self._conn.execute("SELECT run_id, sha256, created_at, invoice_number, final_status FROM runs "
                                      "WHERE state='done' AND final_status IN ('approved','part_approved')").fetchall()
        return {r["sha256"]: dict(r) for r in rows}

    # ---------- feedback into the checks ----------
    def ledger_entries(self) -> list[LedgerInvoice]:
        """Approved (or part-approved) runs become invoice history for later runs."""
        out = []
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runs WHERE state='done' AND final_status IN "
                                      "('approved','part_approved') AND vendor_id IS NOT NULL").fetchall()
        for r in rows:
            ex = json.loads(r["extraction_json"])
            matches = json.loads(r["matches_json"] or "[]")
            share = 1.0
            sub = r["subtotal"] or 0.0
            if r["final_status"] == "part_approved" and r["approvable"] is not None and sub:
                share = r["approvable"] / sub
            lines = []
            for m in matches:
                if m.get("po_line") is None:
                    continue
                li = ex["lines"][m["line"]]
                lines.append({"line_no": m["po_line"], "qty": li["qty"] * share, "amount": li["amount"] * share,
                              "description": li["description"]})
            out.append(LedgerInvoice(r["vendor_id"], r["invoice_number"] or "", r["invoice_date"] or "",
                                     r["po_number"] or "", round(sub * share, 2), round((r["total"] or 0) * share, 2),
                                     "posted", lines))
        return out

    # ---------- dashboard ----------
    def metrics(self) -> dict:
        with self._lock:
            runs = [dict(r) for r in self._conn.execute("SELECT * FROM runs WHERE state='done'").fetchall()]
            acted = {r[0] for r in self._conn.execute("SELECT DISTINCT run_id FROM actions").fetchall()}
            fails = self._conn.execute(
                "SELECT c.check_id, c.name, COUNT(*) n FROM checks c JOIN runs r ON r.run_id=c.run_id "
                "AND c.evaluation=r.evaluation WHERE c.status='fail' AND r.outcome='HOLD' "
                "GROUP BY c.check_id, c.name ORDER BY n DESC, c.check_id").fetchall()
            stage_ms = self._conn.execute(
                "SELECT stage, AVG(ms) FROM events GROUP BY stage").fetchall()
        from .money import to_inr
        inr = lambda r, x: to_inr(x, r.get("currency") or "INR")  # noqa: E731  one scale for all companies
        n = len(runs)
        no_touch = [r for r in runs if r["outcome"] == "APPROVE" and r["run_id"] not in acted]
        open_holds = [r for r in runs if r["final_status"] == "on_hold"]
        by_owner = {}
        for r in open_holds:
            o = by_owner.setdefault(r["owner"] or "Unassigned", {"count": 0, "amount": 0.0})
            o["count"] += 1
            o["amount"] += inr(r, r["total"] or 0.0)
        route = {}
        for r in runs:
            route[r["route"]] = route.get(r["route"], 0) + 1
        outcomes = {k: sum(1 for r in runs if r["outcome"] == k) for k in ("APPROVE", "HOLD", "REJECT")}
        ms = sorted(r["total_ms"] or 0 for r in runs)
        return {
            "processed": n,
            "outcomes": outcomes,
            "no_touch_rate": (len(no_touch) / n) if n else None,
            "open_holds": len(open_holds),
            "amount_waiting": round(sum(inr(r, r["total"] or 0.0) for r in open_holds), 2),
            "amount_approved": round(sum(inr(r, r["total"] or 0.0) * ((r["approvable"] or 0) / r["subtotal"]
                                         if r["final_status"] == "part_approved" and r["subtotal"] else 1.0)
                                         for r in runs if r["final_status"] in ("approved", "part_approved")), 2),
            "median_ms": ms[len(ms) // 2] if ms else None,
            "hold_reasons": [{"check_id": f[0], "name": f[1], "count": f[2]} for f in fails],
            "queue_by_owner": by_owner,
            "routes": route,
            "stage_ms": {s: round(v or 0) for s, v in stage_ms},
            "currencies": sorted({r.get("currency") or "INR" for r in runs}),
        }
