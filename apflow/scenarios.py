"""Scenario catalogue: each scenario = invoice spec + expected decision + expected failed checks.

Plus a seeded factory of clean invoices (all layouts, text and scanned) for the
extraction evaluation.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

from .company import VENDORS, PO_LINES, POS, LedgerInvoice, Repo
from .generator import InvoiceSpec, SpecLine
from .gstin import with_checksum

V = {v.vendor_id: v for v in VENDORS}
PL = {(l.po_number, l.line_no): l for l in PO_LINES}


def line(po: str, n: int, qty: float, price: float | None = None) -> SpecLine:
    l = PL[(po, n)]
    p = round(l.unit_price_pre_tax, 2) if price is None else price
    return SpecLine(l.description, l.hsn, l.uom, qty, p, l.gst_rate)


@dataclass
class Scenario:
    id: str
    title: str
    spec: InvoiceSpec
    expect: str  # APPROVE | HOLD | REJECT
    expect_fail: set = field(default_factory=set)  # check ids expected to fail
    extra_ledger: list = field(default_factory=list)  # history to add before running
    tags: tuple = ()


def scenarios() -> list[Scenario]:
    S = []
    S.append(Scenario("S01", "Happy path: goods, same state, PO printed",
        InvoiceSpec(V["V001"], "DLS/2026/1187", "2026-09-22", "PO-2026-0101",
                    [line("PO-2026-0101", 1, 200), line("PO-2026-0101", 2, 50)], layout="A"),
        "APPROVE"))
    S.append(Scenario("S02", "Happy path: services, other state (IGST), two-way match",
        InvoiceSpec(V["V008"], "GCL/1243", "2026-11-03", "PO-2026-0107",
                    [line("PO-2026-0107", 1, 3)], layout="B"),
        "APPROVE"))
    S.append(Scenario("S03", "Happy path: GeM-style tax-inclusive PO price",
        InvoiceSpec(V["V002"], "NSW/INV/7731", "2026-10-03", "PO-2026-0102",
                    [line("PO-2026-0102", 1, 1), line("PO-2026-0102", 2, 2)], layout="C"),
        "APPROVE", tags=("normalisation",)))
    S.append(Scenario("S04", "High value: approve with manager sign-off",
        InvoiceSpec(V["V002"], "NSW/INV/7802", "2026-11-04", "PO-2026-0110",
                    [line("PO-2026-0110", 1, 1)], layout="A"),
        "APPROVE", tags=("tier",)))
    S.append(Scenario("S05", "Happy path, scanned",
        InvoiceSpec(V["V001"], "DLS/2026/1301", "2026-11-06", "PO-2026-0112",
                    [line("PO-2026-0112", 1, 10)], layout="B", scanned=True, seed=5),
        "APPROVE", tags=("scan",)))
    # --- edge cases ---
    S.append(Scenario("E1", "Split PO: third part-bill takes contract past its value",
        InvoiceSpec(V["V003"], "SFS/26-27/0633", "2026-11-15", "PO-2026-0103",
                    [line("PO-2026-0103", 1, 4)], layout="A"),
        "HOLD", {"C12", "C13"}, tags=("edge",)))
    S.append(Scenario("E2", "Reissued invoice: same bill, new number",
        InvoiceSpec(V["V005"], "MOS-2026-0431", "2026-11-12", "PO-2026-0105",
                    [line("PO-2026-0105", 1, 20)], layout="B"),
        "HOLD", {"C14"}, tags=("edge",)))
    S.append(Scenario("E3", "Scanned invoice with no PO reference",
        InvoiceSpec(V["V001"], "DLS/2026/1290", "2026-10-30", None,
                    [line("PO-2026-0106", 1, 6), line("PO-2026-0106", 2, 30)], layout="C", scanned=True, seed=3),
        "HOLD", {"C09"}, tags=("edge", "scan")))
    S.append(Scenario("E4", "Total looks fine, but one unit price is up 12%",
        InvoiceSpec(V["V004"], "CE/0912", "2026-10-12", "PO-2026-0104",
                    [line("PO-2026-0104", 1, 180, price=369.60), line("PO-2026-0104", 2, 50)], layout="A"),
        "HOLD", {"C11"}, tags=("edge",)))
    # --- controls ---
    S.append(Scenario("R01", "Exact duplicate of a paid invoice",
        InvoiceSpec(V["V005"], "MOS-2026-0398", "2026-10-20", "PO-2026-0105",
                    [line("PO-2026-0105", 1, 20)], layout="C"),
        "REJECT", {"C14"}))
    S.append(Scenario("R02", "Billed to another company's GSTIN",
        InvoiceSpec(V["V001"], "DLS/2026/1322", "2026-11-08", "PO-2026-0101",
                    [line("PO-2026-0101", 3, 4)], layout="A", buyer_gstin=with_checksum("29AAACB7710E1Z")),
        "REJECT", {"C04"}))
    S.append(Scenario("R03", "Blocked vendor",
        InvoiceSpec(V["V007"], "VT/88/2026", "2026-11-01", "PO-2026-0109",
                    [line("PO-2026-0109", 1, 8)], layout="B"),
        "REJECT", {"C07"}))
    S.append(Scenario("R04", "Invoice against a cancelled PO",
        InvoiceSpec(V["V004"], "CE/0955", "2026-11-02", "PO-2026-0108",
                    [line("PO-2026-0108", 1, 10)], layout="C"),
        "REJECT", {"C10"}))
    S.append(Scenario("R05", "Not an invoice (quotation)",
        InvoiceSpec(V["V001"], "Q-2026-77", "2026-11-09", None,
                    [line("PO-2026-0101", 1, 50)], layout="A", title="QUOTATION"),
        "REJECT", {"C01"}))
    S.append(Scenario("H01", "Bank account differs from vendor master",
        InvoiceSpec(V["V001"], "DLS/2026/1340", "2026-11-10", "PO-2026-0101",
                    [line("PO-2026-0101", 3, 4)], layout="B", bank_account="91827364550011", ifsc="KKBK0000811"),
        "HOLD", {"C08"}))
    S.append(Scenario("H02", "Billed more than the goods accepted",
        InvoiceSpec(V["V004"], "CE/0930", "2026-10-15", "PO-2026-0104",
                    [line("PO-2026-0104", 1, 200)], layout="C"),
        "HOLD", {"C12"}))
    S.append(Scenario("H03", "Totals do not add up",
        InvoiceSpec(V["V005"], "MOS-2026-0450", "2026-11-14", "PO-2026-0105",
                    [line("PO-2026-0105", 2, 100)], layout="A", total_override=31100.00),
        "HOLD", {"C05"}))
    S.append(Scenario("H04", "CGST+SGST charged on an inter-state supply",
        InvoiceSpec(V["V004"], "CE/0961", "2026-10-20", "PO-2026-0104",
                    [line("PO-2026-0104", 2, 10)], layout="B", tax_mode="cgst_sgst"),
        "HOLD", {"C06"}))
    S.append(Scenario("H05", "Invoice number missing",
        InvoiceSpec(V["V001"], None, "2026-11-11", "PO-2026-0101",
                    [line("PO-2026-0101", 2, 10)], layout="A"),
        "HOLD", {"C02"}))
    S.append(Scenario("H06", "Vendor still pending onboarding",
        InvoiceSpec(V["V006"], "KPH-221", "2026-11-02", "PO-2026-0111",
                    [line("PO-2026-0111", 1, 500)], layout="C"),
        "HOLD", {"C07"}))
    return S


# ---------- seeded clean invoices for the extraction evaluation ----------

INV_PREFIX = {"V001": "DLS/2026/", "V002": "NSW/INV/", "V003": "SFS/26-27/", "V004": "CE/",
              "V005": "MOS-2026-", "V008": "GCL/"}


def random_clean(seed: int, layout: str | None = None, scanned: bool | None = None) -> InvoiceSpec:
    rng = random.Random(seed)
    repo = Repo()
    pos = [p for p in POS if p.status == "open" and repo.vendors[p.vendor_id].status == "approved"]
    po = rng.choice(pos)
    pls = repo.lines_of(po.po_number)
    chosen = rng.sample(pls, k=rng.randint(1, len(pls)))
    lines = []
    for l in chosen:
        q = max(1, int(l.qty_ordered * rng.uniform(0.1, 0.5))) if l.qty_ordered > 1 else 1
        lines.append(line(po.po_number, l.line_no, q))
    v = repo.vendors[po.vendor_id]
    num = INV_PREFIX.get(v.vendor_id, "INV-") + str(rng.randint(1000, 9999))
    d = f"2026-{rng.randint(10, 11)}-{rng.randint(1, 28):02d}"
    return InvoiceSpec(v, num, d, po.po_number, lines,
                       layout=layout or rng.choice("ABC"),
                       scanned=rng.random() < 0.35 if scanned is None else scanned, seed=seed)
