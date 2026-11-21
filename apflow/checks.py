"""Vendor and PO resolution, line matching, and the check catalogue (C01-C15, X01).

Every check returns a CheckResult with evidence. Checks never decide; decide.py
does, from the results alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher

from .company import Repo, POLine, Vendor
from .gstin import is_valid, repair, state_code
from .schema import CheckResult, Extraction, Line

CONFUSABLE = {frozenset(p) for p in [("0", "O"), ("1", "I"), ("5", "S"), ("8", "B"), ("2", "Z"), ("6", "G"), ("0", "D")]}


def _sim(a: str, b: str) -> float:
    norm = lambda s: re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()
    return SequenceMatcher(None, " ".join(norm(a)), " ".join(norm(b))).ratio()


def _norm_no(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _ocr_distance(a: str, b: str) -> int | None:
    """Number of OCR-confusable substitutions turning a into b; None if any other difference."""
    if len(a) != len(b):
        return None
    d = 0
    for x, y in zip(a, b):
        if x != y:
            if frozenset((x, y)) not in CONFUSABLE:
                return None
            d += 1
    return d


# ---------------- resolution ----------------

@dataclass
class Context:
    ex: Extraction
    repo: Repo
    as_of: date
    vendor: Vendor | None = None
    vendor_how: str = ""
    po_number: str | None = None
    po_how: str = ""  # printed | inferred | none
    po_candidates: list = field(default_factory=list)
    matches: list = field(default_factory=list)  # [(invoice Line, POLine | None, score)]


def resolve_vendor(ctx: Context) -> None:
    g = ctx.ex.get("vendor_gstin")
    if g:
        v = ctx.repo.vendor_by_gstin(g)
        if v:
            ctx.vendor, ctx.vendor_how = v, "GSTIN exact"
            return
        near = [(d, v) for v in ctx.repo.vendors.values() if (d := _ocr_distance(g, v.gstin)) is not None and d <= 2]
        if len(near) == 1:
            ctx.vendor, ctx.vendor_how = near[0][1], f"GSTIN after {near[0][0]} OCR correction(s)"
            return
    name = ctx.ex.get("vendor_name")
    if name:
        scored = sorted(((_sim(name, v.name), v) for v in ctx.repo.vendors.values()), key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.85:
            ctx.vendor, ctx.vendor_how = scored[0][1], f"name match {scored[0][0]:.2f} (GSTIN did not match)"


def _match_lines(inv_lines: list[Line], po_lines: list[POLine]) -> list:
    out, free = [], list(po_lines)
    for l in inv_lines:
        best, bs = None, 0.0
        for pl in free:
            s = 0.65 * _sim(l.description, pl.description) + 0.2 * (1.0 if l.hsn and l.hsn == pl.hsn else 0.0) \
                + 0.15 * (1.0 if abs(l.unit_price - pl.unit_price_pre_tax) <= max(1.0, 0.02 * pl.unit_price_pre_tax) else 0.0)
            if s > bs:
                best, bs = pl, s
        if best is not None and bs >= 0.5:
            free.remove(best)
            out.append((l, best, round(bs, 3)))
        else:
            out.append((l, None, round(bs, 3)))
    return out


def resolve_po(ctx: Context) -> None:
    ref = ctx.ex.get("po_reference")
    s = ctx.repo.settings
    if ref:
        key = _norm_no(ref)
        hit = next((p for p in ctx.repo.pos.values() if _norm_no(p.po_number) == key), None)
        if hit:
            confirmed = (ctx.ex.fields.get("po_reference").evidence or "").startswith("confirmed by")
            ctx.po_number, ctx.po_how = hit.po_number, "confirmed" if confirmed else "printed"
            ctx.matches = _match_lines(ctx.ex.lines, ctx.repo.lines_of(hit.po_number))
            return
    if not ctx.vendor:
        ctx.po_how = "none"
        return
    cands = []
    for p in ctx.repo.open_pos_of(ctx.vendor.vendor_id):
        m = _match_lines(ctx.ex.lines, ctx.repo.lines_of(p.po_number))
        line_score = sum(x[2] for x in m) / len(m) if m else 0.0
        remaining = ctx.repo.po_value_pre_tax(p.po_number) - ctx.repo.billed_amount(p.po_number)
        sub = ctx.ex.get("subtotal") or sum(l.amount for l in ctx.ex.lines)
        fit = 1.0 if sub <= remaining + s.money_abs_tol else 0.4
        cands.append((round(0.8 * line_score + 0.2 * fit, 3), p.po_number, m))
    cands.sort(key=lambda c: -c[0])
    ctx.po_candidates = [(sc, po) for sc, po, _ in cands]
    if cands and cands[0][0] >= s.po_infer_min_score and \
            (len(cands) == 1 or cands[0][0] - cands[1][0] >= s.po_infer_min_margin):
        ctx.po_number, ctx.po_how, ctx.matches = cands[0][1], "inferred", cands[0][2]
    else:
        ctx.po_how = "none"


# ---------------- checks ----------------

def R(id, name, status, severity, message, evidence=None, owner=None, question=None):
    return CheckResult(id, name, status, severity, message, evidence or {}, owner, question)


def c01_doc_type(ctx):
    t = ctx.ex.get("doc_type")
    if t == "invoice":
        return R("C01", "Document type", "pass", "info", "Tax invoice")
    if t == "credit_note":
        return R("C01", "Document type", "fail", "hold", "This is a credit note; it goes to the credit-note process",
                 {"doc_type": t}, "AP", "Apply this credit note against which open invoice?")
    return R("C01", "Document type", "fail", "reject", f"Not an invoice (looks like: {t})", {"doc_type": t},
             "Vendor", None)


def c02_required(ctx):
    req = ctx.repo.settings.required_fields
    missing = [f for f in req if not ctx.ex.get(f)]
    if not missing:
        return R("C02", "Required fields", "pass", "info", "Invoice number, date and total present")
    return R("C02", "Required fields", "fail", "hold", "Missing: " + ", ".join(missing), {"missing": missing},
             "AP", f"Ask the vendor for a corrected invoice showing the {', '.join(m.replace('_', ' ') for m in missing)}")


def c03_gstin_valid(ctx):
    bad = []
    for k in ("vendor_gstin", "buyer_gstin"):
        g = ctx.ex.get(k)
        if not is_valid(g):
            bad.append((k, g, ctx.ex.conf(k)))
    if not bad:
        return R("C03", "GSTIN valid", "pass", "info", "Both GSTINs pass the checksum")
    confident = all(c >= 0.9 for _, _, c in bad)
    msg = "; ".join(f"{k.replace('_', ' ')} '{g}' invalid" for k, g, _ in bad)
    if confident:
        return R("C03", "GSTIN valid", "fail", "reject", msg, {"invalid": bad}, "Vendor")
    return R("C03", "GSTIN valid", "fail", "hold", msg + " (low-confidence read)", {"invalid": bad}, "AP",
             "Check the GSTIN on the scan against the vendor master")


def c04_billed_to_us(ctx):
    g, ours = ctx.ex.get("buyer_gstin"), ctx.repo.settings.company_gstin
    if g == ours:
        return R("C04", "Billed to us", "pass", "info", f"Buyer GSTIN {g} is ours")
    if g and ctx.ex.conf("buyer_gstin") >= 0.9:
        return R("C04", "Billed to us", "fail", "reject", f"Billed to GSTIN {g}, ours is {ours}",
                 {"buyer_gstin": g, "ours": ours}, "Vendor")
    return R("C04", "Billed to us", "fail", "hold", f"Buyer GSTIN unreadable or different ({g})", {"buyer_gstin": g},
             "AP", "Confirm the buyer GSTIN on the document")


def c05_arithmetic(ctx):
    tol = ctx.repo.settings.money_abs_tol
    ex, issues = ctx.ex, []
    for l in ex.lines:
        if abs(l.qty * l.unit_price - l.amount) > tol:
            issues.append(f"'{l.description}': {l.qty:g} x {l.unit_price:,.2f} != {l.amount:,.2f}")
    sub, tot = ex.get("subtotal"), ex.get("total")
    tax = sum(ex.get(k, 0.0) or 0.0 for k in ("cgst", "sgst", "igst"))
    if ex.lines and sub is not None and abs(sum(l.amount for l in ex.lines) - sub) > tol:
        issues.append(f"lines sum to {sum(l.amount for l in ex.lines):,.2f}, subtotal says {sub:,.2f}")
    rated = [(l.amount, pl.gst_rate) for l, pl, _ in ctx.matches if pl is not None]
    if rated and len(rated) == len(ex.lines):
        exp_tax = sum(a * r / 100 for a, r in rated)
        if abs(exp_tax - tax) > tol:
            issues.append(f"tax charged {tax:,.2f}, PO GST rates give {exp_tax:,.2f}")
    if sub is not None and tot is not None and abs(sub + tax - tot) > tol:
        issues.append(f"subtotal {sub:,.2f} + tax {tax:,.2f} = {sub + tax:,.2f}, total says {tot:,.2f}")
    if not issues:
        return R("C05", "Arithmetic", "pass", "info", "Lines, subtotal, tax and total reconcile")
    return R("C05", "Arithmetic", "fail", "hold", "; ".join(issues), {"issues": issues}, "AP",
             "Ask the vendor to correct the totals; which figure is right?")


def c06_tax_type(ctx):
    vs, bs = state_code(ctx.ex.get("vendor_gstin")), ctx.repo.settings.company_state
    c, s_, i = (ctx.ex.get(k, 0.0) or 0.0 for k in ("cgst", "sgst", "igst"))
    if not vs:
        return R("C06", "Tax type", "skip", "info", "Vendor state unknown")
    intra = vs == bs
    ok = (c > 0 and abs(c - s_) <= 1 and i == 0) if intra else (i > 0 and c == 0 and s_ == 0)
    if c == s_ == i == 0:
        ok = True  # nil-rated; arithmetic check covers the amount
    exp = "CGST + SGST (same state)" if intra else "IGST (inter-state)"
    if ok:
        return R("C06", "Tax type", "pass", "info", f"{exp} as expected for state {vs} -> {bs}")
    return R("C06", "Tax type", "fail", "hold", f"Expected {exp} for state {vs} -> {bs}; invoice has "
             f"CGST {c:,.2f}, SGST {s_:,.2f}, IGST {i:,.2f}", {"vendor_state": vs, "buyer_state": bs}, "AP",
             "Ask the vendor to reissue with the correct tax type (we cannot claim credit as billed)")


def c07_vendor(ctx):
    v = ctx.vendor
    if v is None:
        return R("C07", "Vendor approved", "fail", "hold", "Vendor not in the vendor master",
                 {"gstin": ctx.ex.get("vendor_gstin"), "name": ctx.ex.get("vendor_name")}, "Procurement",
                 "Is this a new vendor? Start onboarding before we can pay")
    ev = {"vendor_id": v.vendor_id, "matched_by": ctx.vendor_how, "status": v.status}
    if v.status == "approved":
        return R("C07", "Vendor approved", "pass", "info", f"{v.name} ({v.vendor_id}), matched by {ctx.vendor_how}", ev)
    if v.status == "blocked":
        return R("C07", "Vendor approved", "fail", "reject", f"{v.name} is blocked", ev, "Vendor")
    return R("C07", "Vendor approved", "fail", "hold", f"{v.name} onboarding is still {v.status}", ev, "Procurement",
             f"Finish onboarding {v.name} (KYC and bank verification) before payment")


def c08_bank(ctx):
    v = ctx.vendor
    if v is None:
        return R("C08", "Bank details", "skip", "info", "Vendor unknown")
    acct, ifsc = ctx.ex.get("bank_account"), ctx.ex.get("ifsc")
    if not acct and not ifsc:
        return R("C08", "Bank details", "pass", "info", "No bank details printed; master details will be used")
    diff = []
    if acct and acct != v.bank_account:
        diff.append(f"account {acct} vs master {v.bank_account}")
    if ifsc and ifsc != v.ifsc:
        diff.append(f"IFSC {ifsc} vs master {v.ifsc}")
    if not diff:
        return R("C08", "Bank details", "pass", "info", "Bank details match the vendor master")
    return R("C08", "Bank details", "fail", "hold", "Bank details differ from master: " + "; ".join(diff),
             {"differences": diff, "fraud_flag": True}, "Treasury",
             f"Call {v.name} on the number in the vendor master (not the one on the invoice) to confirm the bank change")


def c09_po(ctx):
    if ctx.po_how == "printed":
        return R("C09", "PO resolved", "pass", "info", f"PO {ctx.po_number} printed on invoice")
    if ctx.po_how == "confirmed":
        return R("C09", "PO resolved", "pass", "info",
                 f"PO {ctx.po_number} {ctx.ex.fields['po_reference'].evidence}", {"confirmed": True})
    ev = {"candidates": ctx.po_candidates[:3], "printed": ctx.ex.get("po_reference")}
    if ctx.po_how == "inferred":
        sc = ctx.po_candidates[0][0]
        return R("C09", "PO resolved", "fail", "hold",
                 f"No usable PO on invoice; best match {ctx.po_number} (score {sc:.2f})", ev, "AP",
                 f"Confirm {ctx.po_number} is the PO for this invoice?")
    return R("C09", "PO resolved", "fail", "hold", "No PO printed and none of the vendor's open POs fit", ev,
             "Procurement", "Which PO covers this purchase, or was it bought without one?")


def c10_po_usable(ctx):
    if not ctx.po_number:
        return R("C10", "PO usable", "skip", "info", "No PO")
    p = ctx.repo.pos[ctx.po_number]
    ev = {"po": p.po_number, "status": p.status, "po_vendor": p.vendor_id}
    if ctx.vendor and p.vendor_id != ctx.vendor.vendor_id:
        return R("C10", "PO usable", "fail", "reject", f"{p.po_number} belongs to another vendor ({p.vendor_id})", ev, "Vendor")
    if p.status != "open":
        return R("C10", "PO usable", "fail", "reject", f"{p.po_number} is {p.status}", ev, "Vendor")
    return R("C10", "PO usable", "pass", "info", f"{p.po_number} is open and belongs to {p.vendor_id}", ev)


def c11_price(ctx):
    s = ctx.repo.settings
    rows, bad, unmatched = [], [], []
    for l, pl, sc in ctx.matches:
        if pl is None:
            unmatched.append(l.description)
            continue
        po_p = pl.unit_price_pre_tax
        tol = max(s.price_abs_tol_per_unit, s.price_rel_tol * po_p)
        diff = l.unit_price - po_p
        row = {"line": l.description, "invoice_price": l.unit_price, "po_price_pre_tax": round(po_p, 2),
               "basis": pl.price_basis, "diff_pct": round(100 * diff / po_p, 2), "impact": round(diff * l.qty, 2)}
        rows.append(row)
        if diff > tol:
            bad.append(row)
    if unmatched:
        return R("C11", "Price", "fail", "hold", "Lines not on the PO: " + "; ".join(unmatched),
                 {"lines": rows, "unmatched": unmatched}, "Procurement", "Were these items ordered outside the PO?")
    if not bad:
        return R("C11", "Price", "pass", "info", f"All {len(rows)} line prices within tolerance of the PO", {"lines": rows})
    b = max(bad, key=lambda r: r["impact"])
    return R("C11", "Price", "fail", "hold",
             "; ".join(f"'{r['line']}' billed at {r['invoice_price']:,.2f} vs PO {r['po_price_pre_tax']:,.2f} "
                       f"(+{r['diff_pct']}%, +₹{r['impact']:,.2f})" for r in bad),
             {"lines": rows, "over": bad}, "Procurement",
             f"Was a price increase on '{b['line']}' agreed? If not, short-pay ₹{sum(r['impact'] for r in bad):,.2f}")


def c12_quantity(ctx):
    rows, bad = [], []
    for l, pl, sc in ctx.matches:
        if pl is None:
            continue
        prior = ctx.repo.billed_qty(pl.po_number, pl.line_no)
        basis = ctx.repo.accepted_qty(pl.po_number, pl.line_no) if pl.requires_grn else pl.qty_ordered
        avail = basis - prior
        row = {"line": l.description, "billed_now": l.qty, "billed_before": prior,
               "basis": "accepted at goods receipt" if pl.requires_grn else "ordered (no goods receipt: services)",
               "basis_qty": basis, "available": avail}
        rows.append(row)
        if l.qty > avail + 1e-9:
            bad.append(row)
    if not rows:
        return R("C12", "Quantity", "skip", "info", "No PO lines matched")
    if not bad:
        return R("C12", "Quantity", "pass", "info", "Billed quantities within what was received / ordered", {"lines": rows})
    goods = any("accepted" in r["basis"] for r in bad)
    return R("C12", "Quantity", "fail", "hold",
             "; ".join(f"'{r['line']}' billed {r['billed_now']:g}, only {r['available']:g} available "
                       f"({r['basis_qty']:g} {r['basis']} - {r['billed_before']:g} already billed)" for r in bad),
             {"lines": rows, "over": bad}, "Stores" if goods else "Procurement",
             "Has the rest been delivered and accepted? Update the goods receipt or short-pay" if goods
             else "Is a contract extension approved for the extra quantity?")


def c13_po_balance(ctx, sub_now):
    if not ctx.po_number:
        return R("C13", "PO balance", "skip", "info", "No PO")
    tol = ctx.repo.settings.money_abs_tol
    value = ctx.repo.po_value_pre_tax(ctx.po_number)
    before = ctx.repo.billed_amount(ctx.po_number)
    remaining = value - before
    ev = {"po_value": round(value, 2), "billed_before": round(before, 2), "remaining": round(remaining, 2),
          "this_invoice": round(sub_now, 2)}
    if sub_now <= remaining + tol:
        return R("C13", "PO balance", "pass", "info",
                 f"₹{sub_now:,.2f} fits in the ₹{remaining:,.2f} left on {ctx.po_number}", ev)
    ev["approvable"] = round(max(0.0, remaining), 2)
    ev["excess"] = round(sub_now - max(0.0, remaining), 2)
    return R("C13", "PO balance", "fail", "hold",
             f"{ctx.po_number}: ₹{before:,.2f} already billed of ₹{value:,.2f}; this invoice (₹{sub_now:,.2f}) "
             f"exceeds the balance by ₹{ev['excess']:,.2f}", ev, "Procurement",
             f"Approve ₹{ev['approvable']:,.2f} now; is a PO amendment coming for the ₹{ev['excess']:,.2f} excess?")


def dup_score(ex: Extraction, inv, as_of: date, window: int, recurring: bool = False) -> tuple[float, dict]:
    """Similarity to a past invoice. For recurring services (rent, housekeeping),
    an identical bill a month later is the next period, not a duplicate: a gap
    of 25+ days damps the score (found by the random-invoice test)."""
    sub = ex.get("subtotal") or sum(l.amount for l in ex.lines)
    amount = 1.0 if abs(sub - inv.subtotal) <= 1.0 else max(0.0, 1 - abs(sub - inv.subtotal) / max(inv.subtotal, 1))
    a = {(l.description.lower(), round(l.qty, 2)) for l in ex.lines}
    b = {(l["description"].lower(), round(l["qty"], 2)) for l in inv.lines}
    lines = len(a & b) / len(a | b) if a | b else 0.0
    try:
        gap = abs((date.fromisoformat(ex.get("invoice_date")) - date.fromisoformat(inv.invoice_date)).days)
    except (TypeError, ValueError):
        gap = window
    near = max(0.0, 1 - gap / window)
    numsim = SequenceMatcher(None, _norm_no(ex.get("invoice_number")), _norm_no(inv.vendor_invoice_no)).ratio()
    score = 0.4 * amount + 0.3 * lines + 0.15 * near + 0.15 * numsim
    parts = {"amount": round(amount, 2), "lines": round(lines, 2), "date_gap_days": gap,
             "number_similarity": round(numsim, 2)}
    if recurring and gap >= 25:
        score *= 0.6
        parts["new_service_period"] = True
    return round(score, 3), parts


def c14_duplicate(ctx):
    if not ctx.vendor:
        return R("C14", "Duplicate", "skip", "info", "Vendor unknown")
    s = ctx.repo.settings
    mine = [inv for inv in ctx.repo.ledger if inv.vendor_id == ctx.vendor.vendor_id]
    no = _norm_no(ctx.ex.get("invoice_number"))
    exact = next((inv for inv in mine if no and _norm_no(inv.vendor_invoice_no) == no), None)
    if exact:
        return R("C14", "Duplicate", "fail", "reject",
                 f"Invoice {exact.vendor_invoice_no} was already received on {exact.invoice_date} ({exact.status})",
                 {"match": exact.vendor_invoice_no, "status": exact.status}, "Vendor")
    recurring = lambda inv: ctx.repo.pos.get(inv.po_number) is not None and ctx.repo.pos[inv.po_number].kind == "services"
    scored = sorted(((dup_score(ctx.ex, inv, ctx.as_of, s.duplicate_window_days, recurring(inv)), inv) for inv in mine),
                    key=lambda x: -x[0][0])
    if scored and scored[0][0][0] >= s.duplicate_hold_score:
        (sc, parts), inv = scored[0]
        return R("C14", "Duplicate", "fail", "hold",
                 f"Looks like {inv.vendor_invoice_no} ({inv.invoice_date}, {inv.status}) reissued: similarity {sc:.2f}",
                 {"match": inv.vendor_invoice_no, "score": sc, "parts": parts, "status": inv.status}, "AP",
                 f"Is {ctx.ex.get('invoice_number')} the same bill as {inv.vendor_invoice_no}, already {inv.status}?")
    top = scored[0][0][0] if scored else 0.0
    return R("C14", "Duplicate", "pass", "info", f"No duplicate (closest similarity {top:.2f})", {"closest": top})


def c15_dates(ctx):
    d = ctx.ex.get("invoice_date")
    try:
        d = date.fromisoformat(d)
    except (TypeError, ValueError):
        return R("C15", "Dates", "skip", "info", "No readable date")
    if d > ctx.as_of:
        return R("C15", "Dates", "fail", "hold", f"Invoice dated {d} is in the future", {"date": str(d)}, "AP",
                 "Confirm the invoice date with the vendor")
    if ctx.po_number:
        po_d = date.fromisoformat(ctx.repo.pos[ctx.po_number].po_date)
        if d < po_d:
            return R("C15", "Dates", "fail", "hold", f"Invoice {d} predates PO {ctx.po_number} ({po_d})",
                     {"date": str(d), "po_date": str(po_d)}, "Procurement", "Was this bought before the PO was raised?")
    return R("C15", "Dates", "pass", "info", f"Invoice date {d} is plausible")


def x01_extraction(ctx, route_issues):
    ex = ctx.ex
    if ex.route == "text":
        return R("X01", "Extraction confidence", "pass", "info", "Read from the PDF text layer")
    mc = ex.mean_confidence()
    if mc >= ctx.repo.settings.escalate_below_confidence and not route_issues:
        return R("X01", "Extraction confidence", "pass", "info", f"Read by {ex.route}, mean confidence {mc:.2f}")
    return R("X01", "Extraction confidence", "fail", "hold",
             f"Read by {ex.route} with mean confidence {mc:.2f}" + (f"; {', '.join(route_issues)}" if route_issues else ""),
             {"mean_confidence": round(mc, 2), "issues": route_issues}, "AP",
             "Check the highlighted values against the scan before this goes further")


def run_checks(ex: Extraction, repo: Repo, as_of: date, route_issues=()) -> tuple[list, Context]:
    ctx = Context(ex, repo, as_of)
    first = c01_doc_type(ctx)
    if first.status == "fail":
        return [first], ctx
    resolve_vendor(ctx)
    resolve_po(ctx)
    res = [first, x01_extraction(ctx, list(route_issues)), c02_required(ctx), c03_gstin_valid(ctx),
           c04_billed_to_us(ctx), c05_arithmetic(ctx), c06_tax_type(ctx), c07_vendor(ctx), c08_bank(ctx),
           c09_po(ctx), c10_po_usable(ctx)]
    usable = ctx.po_number and res[-1].status == "pass"
    sub_now = ex.get("subtotal") or sum(l.amount for l in ex.lines)
    if usable:
        res += [c11_price(ctx), c12_quantity(ctx), c13_po_balance(ctx, sub_now)]
    else:
        res += [R(i, n, "skip", "info", "No usable PO") for i, n in (("C11", "Price"), ("C12", "Quantity"), ("C13", "PO balance"))]
    res += [c14_duplicate(ctx), c15_dates(ctx)]
    return res, ctx
