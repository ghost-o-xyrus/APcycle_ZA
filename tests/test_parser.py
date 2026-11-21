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
