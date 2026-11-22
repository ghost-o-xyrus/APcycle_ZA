"""Vendor and PO resolution, line matching, and the check catalogue (C01-C16, X01).

Every check returns a CheckResult with evidence. Checks never decide; decide.py
does, from the results alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher

from .company import ENTITIES, EU_COUNTRIES, Entity, Repo, POLine, Vendor, regime
from .gstin import is_valid, repair, state_code
from .money import fmt
from .taxid import country_of, validate as validate_tax_id
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
    sha256: str | None = None
    entity: Entity | None = None       # which of our companies is billed
    entity_how: str = ""
    vendor_country: str | None = None
    regime: str | None = None          # gst | import_india | us_sales_tax | vat_domestic | eu_reverse_charge | import
    cur: str = "INR"                   # currency the amounts are shown in
    matches: list = field(default_factory=list)  # [(invoice Line, POLine | None, score)]


def resolve_vendor(ctx: Context) -> None:
    g = ctx.ex.get("vendor_gstin")
    if g:
        v = ctx.repo.vendor_by_gstin(g)
        if v:
            ctx.vendor, ctx.vendor_how = v, "exact GSTIN"
            return
        near = [(d, v) for v in ctx.repo.vendors.values() if (d := _ocr_distance(g, v.gstin)) is not None and d <= 2]
        if len(near) == 1:
            ctx.vendor, ctx.vendor_how = near[0][1], f"GSTIN after {near[0][0]} OCR correction(s)"
            return
    t = ctx.ex.get("vendor_tax_id")
    if t:
        v = ctx.repo.vendor_by_tax_id(t)
        if v:
            ctx.vendor, ctx.vendor_how = v, f"exact {'EIN' if v.country == 'US' else 'VAT ID'}"
            return
    name = ctx.ex.get("vendor_name")
    if name:
        scored = sorted(((_sim(name, v.name), v) for v in ctx.repo.vendors.values()), key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.85:
            ctx.vendor, ctx.vendor_how = scored[0][1], f"name match {scored[0][0]:.2f} (tax number did not match)"


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


def resolve_entity(ctx: Context) -> None:
    """Which of our companies is billed, where the supplier is, and so which tax rules apply."""
    ex = ctx.ex
    ent, how = None, ""
    if ctx.po_number and ctx.po_number in ctx.repo.pos:
        ent, how = ENTITIES.get(ctx.repo.pos[ctx.po_number].entity), "from the PO"
    elif ex.get("buyer_gstin"):
        ent, how = ENTITIES["IN"], "buyer GSTIN"
    elif ex.get("buyer_tax_id"):
        from .taxid import clean
        ent = next((e for e in ENTITIES.values() if clean(e.tax_id) == clean(ex.get("buyer_tax_id"))), None)
        how = "buyer tax number" if ent else ""
    if ent is None and ex.get("buyer_name"):
        best = max(ENTITIES.values(), key=lambda e: _sim(ex.get("buyer_name"), e.name))
        if _sim(ex.get("buyer_name"), best.name) >= 0.85:
            ent, how = best, "buyer name"
    if ent is None and not ex.get("buyer_tax_id") and not ex.get("buyer_name") and (
            (ctx.vendor and ctx.vendor.country == "IN") or ex.get("vendor_gstin")):
        ent, how = ENTITIES["IN"], "Indian supplier"
    ctx.entity, ctx.entity_how = ent, how
    vc = ctx.vendor.country if ctx.vendor else (
        "IN" if ex.get("vendor_gstin") else country_of(ex.get("vendor_tax_id")))
    ctx.vendor_country = vc
    ctx.regime = regime(vc, ent.country) if (vc and ent) else None
    po = ctx.repo.pos.get(ctx.po_number) if ctx.po_number else None
    ctx.cur = (po.currency if po else None) or (ctx.vendor.currency if ctx.vendor else None) or \
        ex.get("currency") or (ent.currency if ent else "INR")


def m(ctx, x) -> str:
    return fmt(x, ctx.cur)


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
    if t == "debit_note":
        return R("C01", "Document type", "fail", "hold", "This is a supplier debit note, not an invoice",
                 {"doc_type": t}, "AP", "Which invoice does this debit note adjust, and is the extra charge agreed?")
    if t in (None, "unknown"):
        return R("C01", "Document type", "fail", "hold", "Could not tell what this document is", {"doc_type": t},
                 "AP", "Is this an invoice? If so, check the scan; if not, discard it")
    word = {"purchase_order": "purchase order or order confirmation",
            "receipt": "receipt for a payment already made"}.get(t, t.replace("_", " "))
    confident = ctx.ex.conf("doc_type") >= 0.9 and ctx.ex.route != "ocr"
    if confident:
        return R("C01", "Document type", "fail", "reject", f"Not an invoice: this is a {word}", {"doc_type": t},
                 "Vendor", None)
    return R("C01", "Document type", "fail", "hold", f"Looks like a {word}, not an invoice (read with low confidence)",
             {"doc_type": t}, "AP", f"Is this really a {word}? If so, reject it; if it is an invoice, check the scan")


def c02_required(ctx):
    req = ctx.repo.settings.required_fields
    missing = [f for f in req if not ctx.ex.get(f)]
    if not missing:
        return R("C02", "Required fields", "pass", "info", "Invoice number, date and total present")
    return R("C02", "Required fields", "fail", "hold", "Missing: " + ", ".join(missing), {"missing": missing},
             "AP", f"Ask the vendor for a corrected invoice showing the {', '.join(m.replace('_', ' ') for m in missing)}")


def _gst_rules(ctx) -> bool:
    """Indian GST rules apply: an Indian supplier billing our Indian company, or GSTINs on an unplaced invoice."""
    if ctx.regime:
        return ctx.regime == "gst"
    return bool(ctx.ex.get("vendor_gstin") or ctx.ex.get("buyer_gstin")) or ctx.entity is not None and \
        ctx.entity.country == "IN" and ctx.vendor_country in (None, "IN")


def c03_gstin_valid(ctx):
    if not _gst_rules(ctx):
        return c03_tax_ids(ctx)
    bad = []
    for k in ("vendor_gstin", "buyer_gstin"):
        g = ctx.ex.get(k)
        if not is_valid(g):
            bad.append((k, g, ctx.ex.conf(k)))
    if not bad:
        return R("C03", "GSTIN valid", "pass", "info", "Both GSTINs pass the checksum")
    # Reject (telling the vendor their invoice is wrong) only when the PDF's own text says so.
    # From a scan, a model's or OCR's confidence is not proof of what is printed.
    confident = ctx.ex.route == "text" and all(c >= 0.9 for _, _, c in bad)
    msg = "; ".join(f"{k.replace('_', ' ')} '{g}' invalid" for k, g, _ in bad)
    if confident:
        return R("C03", "GSTIN valid", "fail", "reject", msg, {"invalid": bad}, "Vendor")
    return R("C03", "GSTIN valid", "fail", "hold", msg + " (low-confidence read)", {"invalid": bad}, "AP",
             "Check the GSTIN on the scan against the vendor master")


def c03_tax_ids(ctx):
    """Outside Indian GST: the supplier's EIN or VAT ID (and ours, when printed) must be well formed."""
    ex, bad, notes = ctx.ex, [], []
    for k, who in (("vendor_tax_id", "supplier"), ("buyer_tax_id", "our")):
        v = ex.get(k)
        if v and country_of(v) is None:
            notes.append((k, v, "format not checked (scheme not supported)", ex.conf(k), who))
        elif v:
            ok, why = validate_tax_id(v)
            (notes if ok else bad).append((k, v, why, ex.conf(k), who))
    if ctx.regime == "import_india" and ex.get("buyer_gstin") and not is_valid(ex.get("buyer_gstin")):
        bad.append(("buyer_gstin", ex.get("buyer_gstin"), "GSTIN checksum", ex.conf("buyer_gstin"), "our"))
    if bad:
        msg = "; ".join(f"{who} tax number '{v}' fails the {why.replace(' wrong', '')}" for _, v, why, _, who in bad)
        if ctx.ex.route == "text" and all(c >= 0.9 for *_, c, _ in bad):
            return R("C03", "Tax numbers valid", "fail", "reject", msg, {"invalid": bad}, "Vendor")
        return R("C03", "Tax numbers valid", "fail", "hold", msg + " (low-confidence read)", {"invalid": bad}, "AP",
                 "Check the tax number on the scan against the vendor master")
    if ctx.vendor_country in EU_COUNTRIES and not ex.get("vendor_tax_id"):
        return R("C03", "Tax numbers valid", "fail", "hold", "EU invoice without the supplier's VAT ID",
                 {"vendor_country": ctx.vendor_country}, "AP",
                 "Ask the supplier for a corrected invoice: EU law requires their VAT ID on it")
    if not notes:
        return R("C03", "Tax numbers valid", "pass", "info", "No tax numbers printed; the vendor master has them")
    def said(v, why, who):
        kind = "EIN" if why.startswith("EIN") else "GSTIN" if why.startswith("GSTIN") else \
            "tax number" if "not supported" in why else "VAT ID"
        verdict = why if "not supported" in why else "well formed" if kind == "EIN" or "format" in why \
            else "check digits valid"
        return f"{who} {kind} {v} {verdict}"
    return R("C03", "Tax numbers valid", "pass", "info",
             "; ".join(said(v, why, who) for _, v, why, _, who in notes).capitalize())


def c04_billed_to_us(ctx):
    if not _gst_rules(ctx) and not (ctx.regime == "import_india" and ctx.ex.get("buyer_gstin")):
        return c04_billed_abroad(ctx)
    g, ours = ctx.ex.get("buyer_gstin"), ctx.repo.settings.company_gstin
    if g == ours:
        return R("C04", "Billed to us", "pass", "info", f"Buyer GSTIN {g} is ours")
    if g and ctx.ex.conf("buyer_gstin") >= 0.9:
        return R("C04", "Billed to us", "fail", "reject", f"Billed to GSTIN {g}, ours is {ours}",
                 {"buyer_gstin": g, "ours": ours}, "Vendor")
    return R("C04", "Billed to us", "fail", "hold", f"Buyer GSTIN unreadable or different ({g})", {"buyer_gstin": g},
             "AP", "Confirm the buyer GSTIN on the document")


def c04_billed_abroad(ctx):
    from .taxid import clean
    ex, ent = ctx.ex, ctx.entity
    bt, bn = ex.get("buyer_tax_id"), ex.get("buyer_name")
    ours = {clean(e.tax_id): e for e in ENTITIES.values()}
    if ent is not None:
        if bt and clean(bt) == clean(ent.tax_id):
            return R("C04", "Billed to us", "pass", "info", f"Billed to {ent.name} ({ent.tax_label} {ent.tax_id})")
        if bt and clean(bt) in ours:
            other = ours[clean(bt)]
            return R("C04", "Billed to us", "fail", "hold", f"Billed to {other.name}, but {ctx.po_number} was raised by "
                     f"{ent.name}", {"billed": other.code, "po_entity": ent.code}, "AP",
                     f"Should {other.name} or {ent.name} pay this? Ask the supplier to re-address it if needed")
        if bt and ex.conf("buyer_tax_id") >= 0.9:
            return R("C04", "Billed to us", "fail", "reject", f"Billed to tax number {bt}; {ent.name} is {ent.tax_id}",
                     {"buyer_tax_id": bt}, "Vendor")
        if bt:
            return R("C04", "Billed to us", "fail", "hold", f"Billed-to tax number reads {bt}, not {ent.tax_id} "
                     "(low-confidence read)", {"buyer_tax_id": bt}, "AP",
                     f"Check the billed-to tax number on the scan; is this addressed to {ent.name}?")
        if bn and _sim(bn, ent.name) >= 0.85:
            return R("C04", "Billed to us", "pass", "info", f"Billed to {ent.name} (by name)")
        return R("C04", "Billed to us", "fail", "hold", f"Cannot confirm the invoice is addressed to {ent.name}",
                 {"buyer_name": bn, "buyer_tax_id": bt}, "AP", f"Is this invoice addressed to {ent.name}?")
    if bt and clean(bt) not in ours and ex.conf("buyer_tax_id") >= 0.9:
        return R("C04", "Billed to us", "fail", "reject", f"Billed to tax number {bt}, which is none of our companies",
                 {"buyer_tax_id": bt}, "Vendor")
    names = ", ".join(e.name for e in ENTITIES.values())
    if bn:
        return R("C04", "Billed to us", "fail", "hold", f"Billed to '{bn}', which is not one of our companies",
                 {"buyer_name": bn}, "AP", "Was this sent to us by mistake? If so, reject it")
    return R("C04", "Billed to us", "fail", "hold", "Cannot tell which of our companies this is billed to",
             {"ours": names}, "AP", f"Which of our companies ({names}) is this invoice for?")


def c05_arithmetic(ctx):
    tol = ctx.repo.settings.money_abs_tol
    ex, issues = ctx.ex, []
    for l in ex.lines:
        if abs(l.qty * l.unit_price - l.amount) > tol:
            issues.append(f"'{l.description}': {l.qty:g} x {m(ctx, l.unit_price)} != {m(ctx, l.amount)}")
    sub, tot = ex.get("subtotal"), ex.get("total")
    tax = sum(ex.get(k, 0.0) or 0.0 for k in ("cgst", "sgst", "igst", "tax"))
    if ex.lines and sub is not None and abs(sum(l.amount for l in ex.lines) - sub) > tol:
        issues.append(f"lines sum to {m(ctx, sum(l.amount for l in ex.lines))}, subtotal says {m(ctx, sub)}")
    rated = [(l.amount, pl.gst_rate) for l, pl, _ in ctx.matches if pl is not None]
    zero_rated = ctx.regime in ("eu_reverse_charge", "import_india", "import")  # C06 says what is wrong there
    if rated and len(rated) == len(ex.lines) and not zero_rated:
        exp_tax = sum(a * r / 100 for a, r in rated)
        if abs(exp_tax - tax) > tol:
            issues.append(f"tax charged {m(ctx, tax)}, PO {'GST' if _gst_rules(ctx) else 'tax'} rates give {m(ctx, exp_tax)}")
    if sub is not None and tot is not None and abs(sub + tax - tot) > tol:
        issues.append(f"subtotal {m(ctx, sub)} + tax {m(ctx, tax)} = {m(ctx, sub + tax)}, total says {m(ctx, tot)}")
    if not issues:
        return R("C05", "Arithmetic", "pass", "info", "Lines, subtotal, tax and total reconcile")
    return R("C05", "Arithmetic", "fail", "hold", "; ".join(issues), {"issues": issues}, "AP",
             "Ask the vendor to correct the totals; which figure is right?")


STATES = {"07": "Delhi", "09": "Uttar Pradesh", "23": "Madhya Pradesh", "24": "Gujarat", "27": "Maharashtra",
          "29": "Karnataka", "32": "Kerala", "33": "Tamil Nadu", "36": "Telangana", "37": "Andhra Pradesh"}


def _state(code):
    return f"{STATES[code]} ({code})" if code in STATES else f"state {code}"


def c06_tax_type(ctx):
    if not _gst_rules(ctx):
        return c06_tax_abroad(ctx)
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
        return R("C06", "Tax type", "pass", "info", f"{exp} as expected for {_state(vs)} to {_state(bs)}")
    return R("C06", "Tax type", "fail", "hold", f"Expected {exp} for {_state(vs)} to {_state(bs)}; invoice has "
             f"CGST {m(ctx, c)}, SGST {m(ctx, s_)}, IGST {m(ctx, i)}", {"vendor_state": vs, "buyer_state": bs}, "AP",
             "Ask the vendor to reissue with the correct tax type (we cannot claim credit as billed)")


def c06_tax_abroad(ctx):
    ex, reg, ent = ctx.ex, ctx.regime, ctx.entity
    gst = sum(ex.get(k, 0.0) or 0.0 for k in ("cgst", "sgst", "igst"))
    tax = (ex.get("tax") or 0.0) + gst
    po_rated = any(pl is not None and pl.gst_rate > 0 for _, pl, _ in ctx.matches)
    where = f"{ctx.vendor_country} supplier to {ent.name}" if ent else ""
    if reg is None:
        return R("C06", "Tax type", "skip", "info", "Supplier country or our billed company unknown")
    if reg == "import_india":
        if tax == 0:
            return R("C06", "Tax type", "pass", "info", f"Import into India ({where}): no GST on the supplier's invoice; "
                     "IGST is paid at customs (goods) or under reverse charge (services)")
        return R("C06", "Tax type", "fail", "hold", f"Foreign supplier charged tax ({m(ctx, tax)}) on an import into India",
                 {"tax": tax}, "AP", "Ask the supplier to reissue without tax: IGST is paid at customs, not to them")
    if reg == "us_sales_tax":
        if tax > 0:
            return R("C06", "Tax type", "pass", "info", f"US sales tax charged ({m(ctx, tax)})")
        return R("C06", "Tax type", "pass", "info", "No sales tax charged" +
                 ("; self-assess use tax on these items" if po_rated else ""))
    if reg == "vat_domestic":
        if tax > 0 or not po_rated:
            return R("C06", "Tax type", "pass", "info", f"Domestic VAT ({where})" + (f": {m(ctx, tax)}" if tax else ""))
        return R("C06", "Tax type", "fail", "hold", f"Domestic supply ({where}) without VAT", {"tax": 0}, "AP",
                 "Ask the supplier why no VAT was charged, or for a corrected invoice")
    if reg == "eu_reverse_charge":
        if tax == 0:
            return R("C06", "Tax type", "pass", "info", f"Cross-border EU supply ({where}): reverse charge, "
                     "we account for the VAT")
        return R("C06", "Tax type", "fail", "hold", f"Cross-border EU supply ({where}) should be reverse-charged, but the "
                 f"supplier charged {m(ctx, tax)} VAT", {"tax": tax}, "AP",
                 "Ask the supplier to reissue with reverse charge and both VAT IDs (we cannot reclaim VAT charged this way)")
    if tax == 0:
        return R("C06", "Tax type", "pass", "info", f"Import ({where}): no tax on the invoice; import VAT is paid at the border")
    return R("C06", "Tax type", "fail", "hold", f"Supplier abroad charged tax ({m(ctx, tax)}) on an import", {"tax": tax},
             "AP", "Ask the supplier to reissue without tax")


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
        code = {"IN": "IFSC", "US": "routing no."}.get(v.country, "BIC")
        diff.append(f"{code} {ifsc} vs master {v.ifsc}")
    if not diff:
        return R("C08", "Bank details", "pass", "info", "Bank details match the vendor master")
    return R("C08", "Bank details", "fail", "hold", "Bank details differ from master: " + "; ".join(diff),
             {"differences": diff, "fraud_flag": True}, "Treasury",
             f"Call {v.name} on the number in the vendor master (not the one on the invoice) to confirm the bank change")


def c09_po(ctx):
    if ctx.po_how == "printed":
        return R("C09", "PO resolved", "pass", "info", f"{ctx.po_number} printed on the invoice")
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
        return R("C11", "Price", "pass", "info", ("The line price is" if len(rows) == 1 else f"All {len(rows)} line prices are") + " within tolerance of the PO", {"lines": rows})
    b = max(bad, key=lambda r: r["impact"])
    return R("C11", "Price", "fail", "hold",
             "; ".join(f"'{r['line']}' billed at {m(ctx, r['invoice_price'])} vs PO {m(ctx, r['po_price_pre_tax'])} "
                       f"(+{r['diff_pct']}%, +{m(ctx, r['impact'])})" for r in bad),
             {"lines": rows, "over": bad}, "Procurement",
             f"Was a price increase on '{b['line']}' agreed? If not, short-pay {m(ctx, sum(r['impact'] for r in bad))}")


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
                       f"({r['basis_qty']:g} {r['basis']}, {r['billed_before']:g} already billed)" for r in bad),
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
                 f"{m(ctx, sub_now)} fits in the {m(ctx, remaining)} left on {ctx.po_number}", ev)
    ev["approvable"] = round(max(0.0, remaining), 2)
    ev["excess"] = round(sub_now - max(0.0, remaining), 2)
    return R("C13", "PO balance", "fail", "hold",
             f"{ctx.po_number}: {m(ctx, before)} already billed of {m(ctx, value)}; this invoice ({m(ctx, sub_now)}) "
             f"exceeds the balance by {m(ctx, ev['excess'])}", ev, "Procurement",
             f"Approve {m(ctx, ev['approvable'])} now; is a PO amendment coming for the {m(ctx, ev['excess'])} excess?")


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
    seen = ctx.repo.approved_files.get(ctx.sha256) if ctx.sha256 else None
    if seen:  # the same file, byte for byte: no reading needed to know it is a repeat
        return R("C14", "Duplicate", "fail", "reject",
                 f"This exact file was already {seen['final_status'].replace('_', ' ')} (run {seen['run_id']})",
                 {"run_id": seen["run_id"], "invoice_number": seen.get("invoice_number")}, "Vendor")
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
        if d < po_d and ctx.ex.route != "text" and d.day <= 12:
            # 06/11/2026 is 6 November here; a reader that took it as June 11 put the date before the PO.
            try:
                swapped = date(d.year, d.day, d.month)
            except ValueError:
                swapped = None
            if swapped and po_d <= swapped <= ctx.as_of:
                f = ctx.ex.fields.get("invoice_date")
                if f:
                    f.value = str(swapped)
                ctx.ex.notes.append(f"invoice date read as {d}; as day/month it is {swapped}, after the PO")
                return R("C15", "Dates", "pass", "info",
                         f"Invoice date {swapped} (read as {d}: day and month swapped; the printed date is day/month)",
                         {"read": str(d), "date": str(swapped), "po_date": str(po_d)})
        if d < po_d:
            return R("C15", "Dates", "fail", "hold", f"Invoice {d} predates PO {ctx.po_number} ({po_d})",
                     {"date": str(d), "po_date": str(po_d)}, "Procurement", "Was this bought before the PO was raised?")
    return R("C15", "Dates", "pass", "info", f"Invoice date {d} is plausible")


def x01_extraction(ctx, route_issues):
    ex = ctx.ex
    # Never approve what was not fully read: skipped pages or bundled invoices go to a person.
    gaps = [n for n in ex.notes if "pages" in n and "first" in n] + [n for n in ex.notes if "different invoice numbers" in n]
    if gaps:
        bundled = any("invoice numbers" in g for g in gaps)
        return R("X01", "Extraction confidence", "fail", "hold", "Not fully read: " + "; ".join(gaps),
                 {"gaps": gaps}, "AP",
                 "Split the file into one PDF per invoice and upload each" if bundled
                 else "Check the pages that were not read before approving")
    if ex.route == "text":
        return R("X01", "Extraction confidence", "pass", "info", "Read from the PDF text layer")
    mc = ex.mean_confidence()
    if mc >= ctx.repo.settings.escalate_below_confidence and not route_issues:
        return R("X01", "Extraction confidence", "pass", "info", f"Read by {ex.route}, mean confidence {mc:.2f}")
    return R("X01", "Extraction confidence", "fail", "hold",
             f"Read by {ex.route} with mean confidence {mc:.2f}" + (f"; {', '.join(route_issues)}" if route_issues else ""),
             {"mean_confidence": round(mc, 2), "issues": route_issues}, "AP",
             "Check the highlighted values against the scan before this goes further")


def c16_currency(ctx):
    cur = ctx.ex.get("currency")
    po = ctx.repo.pos.get(ctx.po_number) if ctx.po_number else None
    expected, basis = ((po.currency, f"{po.po_number}") if po else
                       (ctx.vendor.currency, f"{ctx.vendor.name}'s vendor record") if ctx.vendor else (None, ""))
    if expected is None:
        if cur in (None, "INR") and (ctx.entity is None or ctx.entity.country == "IN"):
            return R("C16", "Currency", "pass", "info", "Amounts in rupees")
        if ctx.entity is not None and cur in (None, ctx.entity.currency):
            return R("C16", "Currency", "pass", "info", f"Amounts in {ctx.entity.currency}")
        return R("C16", "Currency", "fail", "hold", f"Invoice is in {cur}, with no PO or vendor record to check it against",
                 {"currency": cur}, "AP", f"Is a {cur} payment expected? Confirm the rate with Treasury before paying")
    if cur in (None, expected):
        return R("C16", "Currency", "pass", "info", f"Amounts in {expected}, as on {basis}" +
                 ("" if cur else " (currency not printed)"))
    return R("C16", "Currency", "fail", "hold", f"Invoice is in {cur} but {basis} is in {expected}",
             {"currency": cur, "expected": expected}, "AP",
             f"Ask the supplier to bill in {expected}, or get the PO amended and the rate confirmed by Treasury")


def run_checks(ex: Extraction, repo: Repo, as_of: date, route_issues=(), sha256: str | None = None) -> tuple[list, Context]:
    ctx = Context(ex, repo, as_of, sha256=sha256)
    first = c01_doc_type(ctx)
    if first.status == "fail":
        return [first], ctx
    resolve_vendor(ctx)
    resolve_po(ctx)
    resolve_entity(ctx)
    res = [first, x01_extraction(ctx, list(route_issues)), c02_required(ctx), c03_gstin_valid(ctx),
           c04_billed_to_us(ctx), c05_arithmetic(ctx), c06_tax_type(ctx), c07_vendor(ctx), c08_bank(ctx),
           c09_po(ctx), c10_po_usable(ctx)]
    usable = ctx.po_number and res[-1].status == "pass"
    sub_now = ex.get("subtotal") or sum(l.amount for l in ex.lines)
    if usable:
        res += [c11_price(ctx), c12_quantity(ctx), c13_po_balance(ctx, sub_now)]
    else:
        res += [R(i, n, "skip", "info", "No usable PO") for i, n in (("C11", "Price"), ("C12", "Quantity"), ("C13", "PO balance"))]
    res += [c14_duplicate(ctx), c15_dates(ctx), c16_currency(ctx)]
    return res, ctx
