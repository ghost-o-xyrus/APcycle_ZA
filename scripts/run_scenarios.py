import json, sys
from datetime import date
from apflow.scenarios import scenarios
from apflow.generator import build, truth
from apflow.company import Repo
from apflow.pipeline import run_invoice
from apflow.extract.routes import extraction_from_llm_json

class Oracle:
    """Stands in for the vision model: returns the ground truth as model JSON."""
    def __init__(self, t): self.t = t
    def complete_json(self, prompt, images):
        d = {k: v for k, v in self.t.items() if k not in ("layout", "scanned")}
        d["confidence"] = {k: 0.9 for k in d}
        return json.dumps(d)

AS_OF = date(2026, 11, 20)
for s in scenarios():
    pdf, t = build(s.spec, "out/scenarios", s.id)
    repo = Repo().with_ledger(s.extra_ledger)
    rr = run_invoice(pdf, repo, llm=Oracle(t) if s.spec.scanned else None, as_of=AS_OF)
    fails = {c.id for c in rr.checks if c.status == "fail"}
    ok = rr.decision.outcome == s.expect and fails == s.expect_fail
    print(f"{'PASS' if ok else 'FAIL'} {s.id:4s} expect {s.expect:7s} got {rr.decision.outcome:7s} fails={sorted(fails)} exp={sorted(s.expect_fail)} route={rr.extraction.route}")
    if not ok:
        for c in rr.checks:
            if c.status=='fail': print('     ', c.id, c.message)
    if '-v' in sys.argv: print('      ', rr.decision.summary, '|', rr.decision.question)
