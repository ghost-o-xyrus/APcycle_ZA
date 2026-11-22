"""The invented company: vendors, POs, goods receipts and invoice history.

Built explicitly (not randomly) so every scenario is traceable to a record.
Shapes follow the assumption check: GeM-style tax-inclusive PO prices for some
vendors (A3), services POs without goods receipts (two-way match), partial
receipts (A5), one cancelled PO (A11), one blocked and one pending vendor.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, asdict, field

from .gstin import with_checksum
from .config import SETTINGS


def G(state: str, pan: str, entity: str = "1") -> str:
    return with_checksum(f"{state}{pan}{entity}Z")


@dataclass
class Vendor:
    vendor_id: str
    name: str
    gstin: str
    state: str
    bank_account: str
    ifsc: str
    status: str  # approved | pending | blocked
    terms_days: int = 30
    country: str = "IN"
    tax_id: str = ""      # outside India: EIN (US) or VAT ID (EU). In India the GSTIN above.
    currency: str = "INR"


@dataclass
class PO:
    po_number: str
    vendor_id: str
    po_date: str
    status: str  # open | closed | cancelled
    kind: str  # goods | services
    entity: str = "IN"     # which of our companies raised it
    currency: str = "INR"


@dataclass(frozen=True)
class Entity:
    """One of our companies. Each has its own tax registration, currency and tax rules."""
    code: str
    name: str
    country: str
    tax_id: str
    currency: str
    address: str
    tax_label: str  # what the tax number is called on an invoice


@dataclass
class POLine:
    po_number: str
    line_no: int
    description: str
    hsn: str
    uom: str
    qty_ordered: float
    unit_price: float
    price_basis: str  # excl_tax | incl_tax
    gst_rate: float  # the tax rate on the line: GST in India, sales tax in the US, VAT in the EU
    requires_grn: bool

    @property
    def unit_price_pre_tax(self) -> float:
        if self.price_basis == "incl_tax":
            return round(self.unit_price / (1 + self.gst_rate / 100), 4)
        return self.unit_price


@dataclass
class GRN:
    grn_number: str
    po_number: str
    line_no: int
    qty_received: float
    qty_accepted: float
    receipt_date: str
    received_by: str


@dataclass
class LedgerInvoice:
    vendor_id: str
    vendor_invoice_no: str
    invoice_date: str
    po_number: str
    subtotal: float
    total: float
    status: str  # paid | posted
    lines: list = field(default_factory=list)  # [{line_no, qty, amount, description}]


from .taxid import de_with_check, fr_key  # noqa: E402

ENTITIES = {
    "IN": Entity("IN", SETTINGS.company_name, "IN", SETTINGS.company_gstin, "INR",
                 "Plot 14, KIADB Industrial Area, Bengaluru 560058", "GSTIN"),
    "US": Entity("US", "Kaveri Instruments Inc.", "US", "84-3729106", "USD",
                 "1200 Research Blvd, Austin, TX 78759", "EIN"),
    "DE": Entity("DE", "Kaveri Instruments GmbH", "DE", de_with_check("31845627"), "EUR",
                 "Carl-Zeiss-Strasse 7, 07743 Jena", "VAT ID"),
}
EU_COUNTRIES = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU", "IE", "IT",
                "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"}

def regime(vendor_country: str, entity_country: str) -> str:
    """How tax appears on a supplier's invoice, from where the supplier and our company are."""
    if vendor_country == "IN" and entity_country == "IN":
        return "gst"
    if entity_country == "IN":
        return "import_india"          # no Indian GST on the invoice; IGST at customs or reverse charge
    if vendor_country == entity_country == "US":
        return "us_sales_tax"
    if vendor_country == entity_country:
        return "vat_domestic"
    if vendor_country in EU_COUNTRIES and entity_country in EU_COUNTRIES:
        return "eu_reverse_charge"
    return "import"                    # import VAT or duties are paid at the border, not to the supplier


VENDORS = [
    Vendor("V001", "Deccan Lab Supplies", G("29", "AADCD4417K"), "29", "50200011223344", "HDFC0001234", "approved"),
    Vendor("V002", "Narmada Scientific Works", G("07", "AAACN4089K"), "07", "00911020045566", "ICIC0000091", "approved"),
    Vendor("V003", "Sahyadri Facility Services", G("27", "AAKFS2210P"), "27", "33445566778", "SBIN0004455", "approved"),
    Vendor("V004", "Coromandel Electricals", G("33", "AABCC7781L"), "33", "1234567890123", "IOBA0002201", "approved"),
    Vendor("V005", "Malabar Office Systems", G("32", "AAFCM9902Q"), "32", "77889900112", "FDRL0001122", "approved"),
    Vendor("V006", "Kaveri Print House", G("29", "AAJPK3345R"), "29", "40011223344", "CNRB0003300", "pending"),
    Vendor("V007", "Vindhya Traders", G("23", "ABCFV6612M"), "23", "55667788990", "PUNB0012300", "blocked"),
    Vendor("V008", "Ganga Calibration Labs", G("09", "AAGCG1188H"), "09", "66001122334", "UTIB0000456", "approved"),
    # Outside India. bank_account holds the account or IBAN; ifsc holds the bank code (ABA routing or BIC).
    Vendor("V101", "Lone Star Metrology LLC", "", "", "4410029384", "111000025", "approved",
           country="US", tax_id="74-2918365", currency="USD"),
    Vendor("V102", "Rhein Labortechnik GmbH", "", "", "DE89370400440532013000", "COBADEFFXXX", "approved",
           country="DE", tax_id=de_with_check("29475016"), currency="EUR"),
    Vendor("V103", "Optique de Lyon SAS", "", "", "FR1420041010050500013M02606", "PSSTFRPPLYO", "approved",
           country="FR", tax_id="FR" + fr_key("487215039") + "487215039", currency="EUR"),
]

POS = [
    PO("PO-2026-0101", "V001", "2026-09-02", "open", "goods"),
    PO("PO-2026-0102", "V002", "2026-09-05", "open", "goods"),      # GeM-style tax-inclusive prices
    PO("PO-2026-0103", "V003", "2026-04-01", "open", "services"),   # annual contract, billed monthly
    PO("PO-2026-0104", "V004", "2026-09-20", "open", "goods"),      # partial delivery
    PO("PO-2026-0105", "V005", "2026-10-01", "open", "goods"),
    PO("PO-2026-0106", "V001", "2026-10-03", "open", "goods"),      # second open PO for V001 (PO inference)
    PO("PO-2026-0107", "V008", "2026-08-10", "open", "services"),
    PO("PO-2026-0108", "V004", "2026-07-15", "cancelled", "goods"),
    PO("PO-2026-0109", "V007", "2026-06-01", "open", "goods"),      # vendor later blocked
    PO("PO-2026-0110", "V002", "2026-10-12", "open", "goods"),      # high value
    PO("PO-2026-0111", "V006", "2026-10-15", "open", "goods"),
    PO("PO-2026-0112", "V001", "2026-10-20", "open", "goods"),      # third V001 PO, different items
    PO("PO-2026-0201", "V101", "2026-10-06", "open", "goods", entity="US", currency="USD"),
    PO("PO-2026-0202", "V102", "2026-09-28", "open", "goods", entity="IN", currency="EUR"),  # import into India
    PO("PO-2026-0203", "V103", "2026-10-12", "open", "services", entity="DE", currency="EUR"),  # intra-EU service
    PO("PO-2026-0204", "V102", "2026-10-20", "open", "goods", entity="DE", currency="EUR"),  # domestic Germany
]

PO_LINES = [
    POLine("PO-2026-0101", 1, "Borosilicate Beaker 500 ml", "7017", "pcs", 200, 185.00, "excl_tax", 12, True),
    POLine("PO-2026-0101", 2, "Nitrile Gloves Box of 100", "4015", "box", 50, 420.00, "excl_tax", 12, True),
    POLine("PO-2026-0101", 3, "Digital pH Meter", "9027", "pcs", 4, 6800.00, "excl_tax", 18, True),
    POLine("PO-2026-0102", 1, "Semi-Automatic Bench Type Industrial Oven", "8419", "pcs", 1, 274500.00, "incl_tax", 18, True),
    POLine("PO-2026-0102", 2, "Oven Trolley Tray Set", "8419", "set", 2, 11800.00, "incl_tax", 18, True),
    POLine("PO-2026-0103", 1, "Housekeeping Services per month", "9985", "month", 12, 25000.00, "excl_tax", 18, False),
    POLine("PO-2026-0104", 1, "LED Tube Light 18W", "9405", "pcs", 300, 330.00, "excl_tax", 12, True),
    POLine("PO-2026-0104", 2, "MCB 32A Double Pole", "8536", "pcs", 60, 600.00, "excl_tax", 18, True),
    POLine("PO-2026-0105", 1, "Laser Printer Toner Cartridge", "8443", "pcs", 40, 2950.00, "excl_tax", 18, True),
    POLine("PO-2026-0105", 2, "A4 Copier Paper 75 GSM Ream", "4802", "ream", 400, 255.00, "excl_tax", 12, True),
    POLine("PO-2026-0106", 1, "Micropipette 100-1000 ul", "9027", "pcs", 6, 7400.00, "excl_tax", 18, True),
    POLine("PO-2026-0106", 2, "Pipette Tips 1000 ul Pack", "3926", "pack", 30, 540.00, "excl_tax", 18, True),
    POLine("PO-2026-0107", 1, "Calibration of Weighing Balance", "9983", "job", 10, 3500.00, "excl_tax", 18, False),
    POLine("PO-2026-0108", 1, "Industrial Exhaust Fan 24 inch", "8414", "pcs", 10, 5600.00, "excl_tax", 18, True),
    POLine("PO-2026-0109", 1, "Steel Almirah 2 Shelves", "9403", "pcs", 8, 11400.00, "excl_tax", 18, True),
    POLine("PO-2026-0110", 1, "UPLC Liquid Chromatograph System", "9027", "pcs", 1, 1650000.00, "incl_tax", 18, True),
    POLine("PO-2026-0111", 1, "Printed Lab Notebook A4", "4820", "pcs", 500, 120.00, "excl_tax", 12, True),
    POLine("PO-2026-0112", 1, "Fume Hood Filter Cartridge", "8421", "pcs", 10, 4200.00, "excl_tax", 18, True),
    # Texas sales tax 8.25%
    POLine("PO-2026-0201", 1, "CMM Probe Tip Set", "", "set", 10, 285.00, "excl_tax", 8.25, True),
    POLine("PO-2026-0201", 2, "Granite Surface Plate 24x36 in", "", "pcs", 2, 1450.00, "excl_tax", 8.25, True),
    # Export from Germany: no VAT on the supplier's invoice; IGST is paid at Indian customs.
    POLine("PO-2026-0202", 1, "Analytical Balance 220 g", "", "pcs", 2, 3150.00, "excl_tax", 0, True),
    POLine("PO-2026-0202", 2, "Calibration Weight Set E2", "", "set", 2, 750.00, "excl_tax", 0, True),
    # France to Germany, business to business: reverse charge, no VAT on the invoice.
    POLine("PO-2026-0203", 1, "Optical Bench Alignment Service", "", "job", 4, 1200.00, "excl_tax", 0, False),
    # Within Germany: 19% VAT.
    POLine("PO-2026-0204", 1, "Laboratory Magnetic Stirrer", "", "pcs", 5, 420.00, "excl_tax", 19, True),
]

GRNS = [
    GRN("GRN-5001", "PO-2026-0101", 1, 200, 200, "2026-09-18", "Stores-A"),
    GRN("GRN-5002", "PO-2026-0101", 2, 50, 50, "2026-09-18", "Stores-A"),
    GRN("GRN-5003", "PO-2026-0101", 3, 4, 4, "2026-09-18", "Stores-A"),
    GRN("GRN-5004", "PO-2026-0102", 1, 1, 1, "2026-10-01", "Stores-B"),
    GRN("GRN-5005", "PO-2026-0102", 2, 2, 2, "2026-10-01", "Stores-B"),
    GRN("GRN-5006", "PO-2026-0104", 1, 200, 196, "2026-10-08", "Stores-A"),  # partial, 4 rejected
    GRN("GRN-5007", "PO-2026-0104", 2, 60, 60, "2026-10-08", "Stores-A"),
    GRN("GRN-5008", "PO-2026-0105", 1, 40, 40, "2026-10-14", "Stores-C"),
    GRN("GRN-5009", "PO-2026-0105", 2, 400, 400, "2026-10-14", "Stores-C"),
    GRN("GRN-5010", "PO-2026-0106", 1, 6, 6, "2026-10-25", "Stores-A"),
    GRN("GRN-5011", "PO-2026-0106", 2, 30, 30, "2026-10-25", "Stores-A"),
    GRN("GRN-5012", "PO-2026-0109", 1, 8, 8, "2026-06-20", "Stores-B"),
    GRN("GRN-5013", "PO-2026-0110", 1, 1, 1, "2026-11-02", "Stores-B"),
    GRN("GRN-5014", "PO-2026-0111", 1, 500, 500, "2026-10-28", "Stores-C"),
    GRN("GRN-5015", "PO-2026-0112", 1, 10, 10, "2026-11-05", "Stores-A"),
    GRN("GRN-6001", "PO-2026-0201", 1, 10, 10, "2026-10-24", "Austin Receiving"),
    GRN("GRN-6002", "PO-2026-0201", 2, 2, 2, "2026-10-24", "Austin Receiving"),
    GRN("GRN-6003", "PO-2026-0202", 1, 2, 2, "2026-10-30", "Stores-B"),
    GRN("GRN-6004", "PO-2026-0202", 2, 2, 2, "2026-10-30", "Stores-B"),
    GRN("GRN-6005", "PO-2026-0204", 1, 5, 5, "2026-11-06", "Jena Wareneingang"),
]

# History: what has already been invoiced (for duplicates and running balances).
LEDGER = [
    # Services contract billed in parts: 5 + 5 months already billed (E1 setup).
    LedgerInvoice("V003", "SFS/26-27/0412", "2026-08-31", "PO-2026-0103", 125000.00, 147500.00, "paid",
                  [{"line_no": 1, "qty": 5, "amount": 125000.00, "description": "Housekeeping Services per month"}]),
    LedgerInvoice("V003", "SFS/26-27/0519", "2026-10-31", "PO-2026-0103", 125000.00, 147500.00, "posted",
                  [{"line_no": 1, "qty": 5, "amount": 125000.00, "description": "Housekeeping Services per month"}]),
    # Already-paid invoice that a vendor later reissues under a new number (E2 setup).
    LedgerInvoice("V005", "MOS-2026-0398", "2026-10-20", "PO-2026-0105", 59000.00, 69620.00, "paid",
                  [{"line_no": 1, "qty": 20, "amount": 59000.00, "description": "Laser Printer Toner Cartridge"}]),
    # Calibration lab: 4 of 10 jobs billed.
    LedgerInvoice("V008", "GCL/1187", "2026-09-30", "PO-2026-0107", 14000.00, 16520.00, "paid",
                  [{"line_no": 1, "qty": 4, "amount": 14000.00, "description": "Calibration of Weighing Balance"}]),
]


class Repo:
    """Read-only view of the company data used by the checks."""

    def __init__(self, vendors=VENDORS, pos=POS, po_lines=PO_LINES, grns=GRNS, ledger=LEDGER, settings=SETTINGS):
        self.settings = settings
        self.vendors = {v.vendor_id: v for v in vendors}
        self.pos = {p.po_number: p for p in pos}
        self.po_lines = {}
        for l in po_lines:
            self.po_lines.setdefault(l.po_number, []).append(l)
        self.grns = list(grns)
        self.ledger = list(ledger)
        self.approved_files = {}  # sha256 -> earlier approved run (filled by the service)

    # --- lookups ---
    def vendor_by_gstin(self, gstin):
        return next((v for v in self.vendors.values() if gstin and v.gstin == gstin), None)

    def vendor_by_tax_id(self, tax_id):
        from .taxid import clean
        t = clean(tax_id)
        return next((v for v in self.vendors.values() if t and clean(v.tax_id) == t), None)

    def lines_of(self, po_number):
        return self.po_lines.get(po_number, [])

    def accepted_qty(self, po_number, line_no):
        return sum(g.qty_accepted for g in self.grns if g.po_number == po_number and g.line_no == line_no)

    def billed_qty(self, po_number, line_no):
        return sum(l["qty"] for inv in self.ledger if inv.po_number == po_number
                   for l in inv.lines if l["line_no"] == line_no)

    def billed_amount(self, po_number):
        return sum(inv.subtotal for inv in self.ledger if inv.po_number == po_number)

    def po_value_pre_tax(self, po_number):
        return sum(l.qty_ordered * l.unit_price_pre_tax for l in self.lines_of(po_number))

    def open_pos_of(self, vendor_id):
        return [p for p in self.pos.values() if p.vendor_id == vendor_id and p.status == "open"]

    def with_ledger(self, extra, approved_files=None):
        r = Repo(list(self.vendors.values()), list(self.pos.values()),
                 [l for ls in self.po_lines.values() for l in ls], self.grns, self.ledger + list(extra), self.settings)
        r.approved_files = dict(approved_files or {})
        return r


def export_csv(folder: str) -> None:
    """Write the company data as CSVs (the 'PO spreadsheet' an AP clerk would use)."""
    os.makedirs(folder, exist_ok=True)
    for name, rows in [("vendors", VENDORS), ("purchase_orders", POS), ("po_lines", PO_LINES),
                       ("goods_receipts", GRNS)]:
        with open(os.path.join(folder, f"{name}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
            w.writeheader()
            for r in rows:
                w.writerow(asdict(r))
    with open(os.path.join(folder, "invoice_ledger.json"), "w") as f:
        json.dump([asdict(x) for x in LEDGER], f, indent=2)
    with open(os.path.join(folder, "company.json"), "w") as f:
        json.dump(asdict(SETTINGS), f, indent=2, default=str)
