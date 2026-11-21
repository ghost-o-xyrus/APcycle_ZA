"""Runs one invoice end to end, emitting a stage event as each stage finishes.

Stages (as the live view shows them):
  intake -> read -> classify -> validate -> vendor -> po -> risk -> decide
`reevaluate` reruns classify..decide on a stored extraction after a person
supplies a fact the document lacked (for example, confirms the PO).
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import date

from .checks import run_checks
from .company import Repo
from .decide import decide
from .extract.routes import extract, needs_escalation
from .schema import Decision, Extraction

STAGES = [
    ("intake", "Receive the file", []),
    ("read", "Read the document", []),
    ("classify", "Check it is an invoice", ["C01"]),
    ("validate", "Check the document itself", ["X01", "C02", "C03", "C04", "C05", "C06"]),
    ("vendor", "Match the vendor", ["C07", "C08"]),
    ("po", "Match the purchase order", ["C09", "C10", "C11", "C12", "C13"]),
    ("risk", "Look for duplicates and date problems", ["C14", "C15"]),
    ("decide", "Decide", []),
]
LABEL = {s: l for s, l, _ in STAGES}


@dataclass
class StageEvent:
    stage: str
    status: str  # ok | warn | fail | skip
    ms: int
    detail: str
    label: str = ""
    checks: list = field(default_factory=list)  # [{id, name, status, severity, message}]


@dataclass
class RunResult:
    run_id: str
    file: str
    sha256: str
    events: list[StageEvent] = field(default_factory=list)
    extraction: Extraction | None = None
    checks: list = field(default_factory=list)
    decision: Decision | None = None
    route_log: list = field(default_factory=list)
    vendor_id: str | None = None
    vendor_name: str | None = None
    po_number: str | None = None
    po_how: str | None = None
    subtotal: float | None = None
    matches: list = field(default_factory=list)  # [{line, po_line, score}]

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "file": self.file, "sha256": self.sha256,
                "events": [asdict(e) for e in self.events],
                "extraction": self.extraction.to_dict() if self.extraction else None,
                "checks": [asdict(c) for c in self.checks],
                "decision": asdict(self.decision) if self.decision else None, "route_log": self.route_log,
                "vendor_id": self.vendor_id, "po_number": self.po_number, "po_how": self.po_how}


def sha256_of(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _group_status(results) -> str:
    if not results:
        return "skip"
    if all(c.status == "skip" for c in results):
        return "skip"
    if any(c.status == "fail" and c.severity == "reject" for c in results):
        return "fail"
    if any(c.status == "fail" for c in results):
        return "warn"
    return "ok"


def _brief(c) -> dict:
    return {"id": c.id, "name": c.name, "status": c.status, "severity": c.severity, "message": c.message}


def _evaluate(rr: RunResult, ex: Extraction, repo: Repo, as_of: date, route_issues, emit) -> None:
    t = time.perf_counter()
    checks, ctx = run_checks(ex, repo, as_of, route_issues)
    ms = int((time.perf_counter() - t) * 1000)
    rr.checks = checks
    rr.vendor_id = ctx.vendor.vendor_id if ctx.vendor else None
    rr.vendor_name = ctx.vendor.name if ctx.vendor else ex.get("vendor_name")
    rr.po_number, rr.po_how = ctx.po_number, ctx.po_how or None
    rr.subtotal = ex.get("subtotal") or sum(l.amount for l in ex.lines)
    rr.matches = [{"line": i, "po_line": pl.line_no if pl else None, "score": sc}
                  for i, (l, pl, sc) in enumerate(ctx.matches)]
    by_id = {c.id: c for c in checks}
    details = {
        "classify": lambda: f"{ex.get('doc_type') or 'unknown'}",
        "validate": lambda: f"{sum(1 for i in ('X01','C02','C03','C04','C05','C06') if i in by_id and by_id[i].status == 'pass')} of 6 passed",
        "vendor": lambda: (f"{ctx.vendor.name} ({ctx.vendor.vendor_id}), by {ctx.vendor_how}" if ctx.vendor
                           else "not in vendor master"),
        "po": lambda: ({"printed": f"{ctx.po_number} (printed on invoice)",
                        "confirmed": f"{ctx.po_number} (confirmed by a person)",
                        "inferred": f"{ctx.po_number} (inferred, needs confirming)"}.get(ctx.po_how, "no PO found")),
        "risk": lambda: "no duplicate, dates fine" if all(by_id.get(i) is None or by_id[i].status != "fail"
                                                          for i in ("C14", "C15")) else "needs a look",
    }
    for stage, label, ids in STAGES[2:-1]:
        res = [by_id[i] for i in ids if i in by_id]
        st = _group_status(res)
        emit(StageEvent(stage, st, ms if stage == "classify" else 0,
                        details[stage]() if res else "skipped: not an invoice", label, [_brief(c) for c in res]))
    t = time.perf_counter()
    rr.decision = decide(checks, ex, repo.settings)
    d = rr.decision
    n = len(d.reasons)
    short = {"APPROVE": "approve" + ("; needs manager sign-off" if (d.approval_level or "").startswith("Manager") else ", within AP limit"),
             "HOLD": f"hold for {d.owner}" + (f", {n} reasons" if n > 1 else ""),
             "REJECT": "reject" + (f", {n} reasons" if n > 1 else "")}[d.outcome]
    emit(StageEvent("decide", {"APPROVE": "ok", "HOLD": "warn", "REJECT": "fail"}[d.outcome],
                    int((time.perf_counter() - t) * 1000), short, LABEL["decide"]))


def run_invoice(pdf_path: str, repo: Repo | None = None, llm=None, as_of: date | None = None,
                on_event=None, run_id: str | None = None) -> RunResult:
    repo = repo or Repo()
    as_of = as_of or date.today()
    t = time.perf_counter()
    rr = RunResult(run_id or uuid.uuid4().hex[:10], pdf_path, sha256_of(pdf_path))

    def emit(e: StageEvent):
        rr.events.append(e)
        if on_event:
            on_event(e)

    emit(StageEvent("intake", "ok", int((time.perf_counter() - t) * 1000), f"sha256 {rr.sha256[:12]}", LABEL["intake"]))

    t = time.perf_counter()
    ex, log = extract(pdf_path, llm=llm, settings=repo.settings)
    rr.extraction, rr.route_log = ex, log
    issues = needs_escalation(ex, repo.settings)
    tried = " then ".join(l["route"] + (" (skipped: no model)" if l.get("skipped") else "")
                          + (" (failed)" if l.get("error") else "") for l in log)
    emit(StageEvent("read", "ok" if not issues else "warn", int((time.perf_counter() - t) * 1000),
                    f"read by {ex.route} [{tried}]; {len(ex.lines)} line(s); confidence {ex.mean_confidence():.2f}"
                    + (f"; {', '.join(issues)}" if issues else ""), LABEL["read"]))

    _evaluate(rr, ex, repo, as_of, issues if ex.route != "text" else [], emit)
    return rr


def reevaluate(run_id: str, file: str, sha: str, extraction: Extraction, repo: Repo, as_of: date,
               on_event=None) -> RunResult:
    """Rerun checks and decision on a stored extraction (after a person adds a fact)."""
    rr = RunResult(run_id, file, sha, extraction=extraction)

    def emit(e: StageEvent):
        rr.events.append(e)
        if on_event:
            on_event(e)

    issues = needs_escalation(extraction, repo.settings)
    _evaluate(rr, extraction, repo, as_of, issues if extraction.route != "text" else [], emit)
    return rr
