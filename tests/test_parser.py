"""Parser units: dates, numbers, line rows, classification, label handling."""
import pytest

from apflow.extract.parser import _parse_line_row, classify, parse, parse_date, to_float


@pytest.mark.parametrize("s,iso", [
    ("19-Nov-2026", "2026-11-19"), ("19 November 2026", "2026-11-19"), ("19/11/2026", "2026-11-19"),
    ("2026-11-19", "2026-11-19"), ("19.11.2026", "2026-11-19"), ("Dated 06/11/2026 hon)", "2026-11-06"),
    ("31/02/2026", None), ("no date here", None),
])
def test_dates(s, iso):
    assert parse_date(s) == iso


@pytest.mark.parametrize("s,v", [("1,47,500.00", 147500.0), ("Rs.2,32,627.12", 232627.12), ("₹ 99", 99.0),
                                 ("₹99", 99.0), ("42.00", 42.0), ("abc", None)])
def test_to_float(s, v):
    assert to_float(s) == v


@pytest.mark.parametrize("row,desc,q,r,a,hsn", [
    ("1 Borosilicate Beaker 500 ml 7017 200 pcs 185.00 37,000.00", "Borosilicate Beaker 500 ml", 200, 185, 37000, "7017"),
    ("2 MCB 32A Double Pole 8536 60 600.00 pcs 36,000.00", "MCB 32A Double Pole", 60, 600, 36000, "8536"),
    ("Pipette Tips 1000 ul Pack 30 540.00 16200.00 3926", "Pipette Tips 1000 ul Pack", 30, 540, 16200, "3926"),
    ("Micropipette 100-1000 ul 9027 6 Rs.7,400.00 Rs.44,400.00", "Micropipette 100-1000 ul", 6, 7400, 44400, "9027"),
    # OCR noise: punctuation stuck to numbers, stray symbols
    ("1. Fume Hood Filter Cartridge 8421 - 10 4,200.00: pcs ~ 42,000.00 sy", "Fume Hood Filter Cartridge", 10, 4200, 42000, "8421"),
    ("Nitrile. Gloves Box of 100 Se 20 420.00 8400.00 | ¢ MIB 8% ¢", "Nitrile. Gloves Box of 100 Se", 20, 420, 8400, None),
])
def test_line_rows(row, desc, q, r, a, hsn):
    l = _parse_line_row(row)
    assert l is not None
    assert (l.description, l.qty, l.unit_price, l.amount) == (desc, q, r, a)
    assert l.hsn == hsn


@pytest.mark.parametrize("row", [
    "Plot 14, KIADB Industrial Area, Bengaluru 560058",       # address with numbers
    "Sub Total 58,000.00",
    "1 Digital pH Meter - . 1 6800.00 . .680000° ##-.9a7",   # unreconcilable OCR garble
    "",
])
def test_non_rows_rejected(row):
    assert _parse_line_row(row) is None


@pytest.mark.parametrize("text,kind", [
    ("Acme\nTAX INVOICE\nInvoice No: 1", "invoice"), ("Acme\nQUOTATION\n", "quotation"),
    ("Acme\nCREDIT NOTE\n", "credit_note"), ("Acme Proforma Invoice", "quotation"),
    ("Acme\nDelivery Challan", "delivery_challan"), ("hello world", "unknown"),
    # From real invoices (DocILE): field labels that name other documents are not titles.
    ("Inv # 7688387 Acct # 13673 Invoice Date: 9/20/2020\nEstimate/HL: 7784/ECR27031398", "invoice"),
    ("BillTo: KELLY Phone#: 423\n8THFLOOR Invoice#: 14969559 EDIINVOICE\nEstimate #: R40/HANE/0004", "invoice"),
    ("PARTNERS\n303 EAST OHIO ST Invoice t 0301 K11049\nPO Number BNY 5930\nEstimate LRL YSPP", "invoice"),
    ("Date: 1/28/22\nSales Order\nEstimate #: 1478", "purchase_order"),
    ("Confirmation\nKHWL-FM Contract # 8718\nEstimate # 1085", "purchase_order"),
    ("PHILIP MORRIS U.S.A.\nPURCHASE REQUISITION\nP.O. NUMBER", "purchase_order"),
    ("BILLING DISCREPANCY/CREDIT REQUEST DATE: 3/31/2020\nINVOICE # 7292256", "credit_note"),
    ("Mountain Wireless\nThank you for your payment. The following\nInvoice #: 8684 PREPAY", "receipt"),
    ("ACME\nDEBIT NOTE\nAgainst Invoice No. 44", "debit_note"),
    ("Acme Ltd\nTAX INVOICE\nQuotation No: Q-17\nPO Number: 4500", "invoice"),
    ("Spot Manager: Placed\nOrder: 1030704", "unknown"),
    ("FOOD LION INC\nTHIS IS NOT A CREDIT MEMO - CHECK ATTACHED", "unknown"),
    ("CA NO : GEMC-5116877\n" + "x\n" * 16 + "with the Accepting Officer within 28 days of receipt", "unknown"),
])
def test_classify(text, kind):
    assert classify(text)[0] == kind


def test_invoice_number_label_not_confused_with_date():
    ex = parse("Acme\nTAX INVOICE\nInvoice Date: 19-Nov-2026\nInvoice No: AB/12\n", "text")
    assert ex.get("invoice_number") == "AB/12"
    assert ex.get("invoice_date") == "2026-11-19"


def test_missing_fields_are_none_not_guessed():
    ex = parse("Acme Traders\nTAX INVOICE\nGrand Total 1,000.00\n", "text")
    assert ex.get("invoice_number") is None
    assert ex.get("po_reference") is None
    assert ex.conf("invoice_number") == 0.0


def test_own_gstin_never_taken_as_vendor():
    ours, theirs = "29AAGCK4421M1Z8", "07AAACN4089K1Z5"
    ex = parse(f"Acme\nTAX INVOICE\nRegd {ours}\nOther {theirs}\n", "ocr", own_gstin=ours)
    assert ex.get("vendor_gstin") == theirs
    assert ex.get("buyer_gstin") == ours


# ---------- real-world formats found on the DocILE invoices ----------
from apflow.extract.parser import detect_currency, parse, parse_date  # noqa: E402


@pytest.mark.parametrize("s,want", [
    ("12/03/2026", "2026-03-12"),          # Indian day-first stays first
    ("9/20/2020", "2020-09-20"),           # impossible day-first -> month-first
    ("01/11/99", "1999-11-01"), ("5/31/20", "2020-05-31"),
    ("MAY 21, 1999", "1999-05-21"), ("Sep 8, 2020", "2020-09-08"),
    ("Invoice 05/13/18 Date:", "2018-05-13"), ("2026-11-02", "2026-11-02"), ("12-Mar-2026", "2026-03-12"),
    ("31/31/2020", None), ("no date here", None),
])
def test_dates(s, want):
    assert parse_date(s) == want


@pytest.mark.parametrize("text,want", [
    ("Total ₹1,47,500.00", "INR"), ("Rs. 4,200 only", "INR"), ("Amount Due $2,325.00\nRate $60.00", "USD"),
    ("Total USD 1,200.00", "USD"), ("¥ 54,223", "JPY"), ("Total 1,200.00", None),
    ("Total ₹1,000 (approx $12)", "INR"),
])
def test_currency(text, want):
    assert detect_currency(text)[0] == want


def test_us_invoice_header_fields():
    text = "\n".join(["Page 1 of 2", "Sinclair Broadcast Group Inc", "Inv # 7688387 Acct # 13673 Invoice Date: 9/20/2020",
                      "Estimate/HL: 7784", "Description Qty Rate Amount", "Spot 30s 2 $60.00 $120.00",
                      "Total Amount Due $2,325.00"])
    ex = parse(text, "text", 0.95, "29AAGCK4421M1Z8")
    assert ex.get("doc_type") == "invoice"
    assert ex.get("vendor_name") == "Sinclair Broadcast Group Inc"
    assert ex.get("invoice_number") == "7688387" and ex.get("invoice_date") == "2020-09-20"
    assert ex.get("total") == 2325.0 and ex.get("currency") == "USD"


@pytest.mark.parametrize("first_lines,want", [
    (["Please Remit To:", "KMOZ 92.3 The Moose"], "KMOZ 92.3 The Moose"),
    (["RemitTo: 3636MomentumPlace", "Station: Cumulus"], None),
    (["Deccan Lab Supplies TAX INVOICE"], "Deccan Lab Supplies"),
    (["MAKEGOOD FORM", "Advertiser: POL"], None),
])
def test_vendor_name_skips_non_names(first_lines, want):
    ex = parse("\n".join(first_lines + ["Invoice No: 1"]), "text", 0.95, "29AAGCK4421M1Z8")
    assert ex.get("vendor_name") == want
