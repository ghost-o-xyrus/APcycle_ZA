"""Decision policy: from check results to Approve / Hold / Reject. No AI here."""
from __future__ import annotations

from .money import rs
from .schema import CheckResult, Decision, Extraction

OWNER_PRIORITY = ["Treasury", "Procurement", "Stores", "AP"]  # fraud risk first


def decide(checks: list[CheckResult], ex: Extraction, settings) -> Decision:
    fails = [c for c in checks if c.status == "fail"]
    rejects = [c for c in fails if c.severity == "reject"]
    holds = [c for c in fails if c.severity == "hold"]
    sub = ex.get("subtotal") or sum(l.amount for l in ex.lines)

    if rejects:
        top = rejects[0]
        msg = " ".join(f"{c.message}." for c in rejects)
        return Decision("REJECT", [f"{c.id} {c.message}" for c in rejects], owner="Vendor",
                        vendor_message=f"We cannot process invoice {ex.get('invoice_number') or '(no number)'}: {msg} "
                                       "Please send a corrected invoice if this was a mistake.",
                        summary=f"Rejected: {top.message}.")

    if holds:
        holds.sort(key=lambda c: OWNER_PRIORITY.index(c.owner) if c.owner in OWNER_PRIORITY else 99)
        top = holds[0]
        d = Decision("HOLD", [f"{c.id} {c.message}" for c in holds], owner=top.owner, question=top.question)
        bal = next((c for c in holds if c.id == "C13"), None)
        only_excess = bal and all(c.id in ("C12", "C13") for c in holds)
        if only_excess:
            d.approvable_amount = bal.evidence.get("approvable")
            d.held_amount = bal.evidence.get("excess")
        others = f" (+{len(holds) - 1} more)" if len(holds) > 1 else ""
        d.summary = f"Held for {top.owner}: {top.message}{others}."
        if d.approvable_amount is not None:
            ev = bal.evidence
            d.summary = (f"This bill ({rs(ev['this_invoice'])} pre-tax) is more than the {rs(ev['remaining'])} left on the PO. "
                         f"{rs(d.approvable_amount)} can be approved now; {rs(d.held_amount)} is held for {top.owner}.")
        return d

    level = "Manager sign-off" if sub > settings.manager_approval_above else "Auto (within AP limit)"
    passed = sum(1 for c in checks if c.status == "pass")
    return Decision("APPROVE", [f"All {passed} checks passed"], approval_level=level,
                    summary=f"Approved: {rs(sub)} pre-tax, all {passed} checks passed. Approval: {level}.")
