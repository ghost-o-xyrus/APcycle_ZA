"""Tax registration numbers outside India: format and, where the scheme has one, the check digit.

- India: GSTIN (see gstin.py).
- US: EIN, NN-NNNNNNN. No check digit; some two-digit prefixes are never issued by the IRS.
- EU VAT IDs: country prefix + national format. Germany (ISO 7064 MOD 11,10) and France
  (key = (12 + 3 x (SIREN mod 97)) mod 97) carry check digits and are verified; other EU
  countries are checked for format only. Whether a number is *registered* needs the EU's
  VIES service, which is an online lookup and outside this demo.
"""
from __future__ import annotations

import re

EU = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU", "IE", "IT", "LT", "LU",
      "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"}
EU_FORMATS = {
    "DE": r"\d{9}", "FR": r"[0-9A-Z]{2}\d{9}", "NL": r"\d{9}B\d{2}", "IT": r"\d{11}", "ES": r"[0-9A-Z]\d{7}[0-9A-Z]",
    "BE": r"[01]\d{9}", "AT": r"U\d{8}", "IE": r"\d{7}[A-W][A-I]?", "PL": r"\d{10}", "SE": r"\d{12}",
}
EIN = re.compile(r"^\d{2}-\d{7}$")
EIN_BAD_PREFIX = {"00", "07", "08", "09", "17", "18", "19", "28", "29", "49", "69", "70", "78", "79", "89", "96", "97"}
TOKEN = re.compile(r"\b(?:(?:AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK)"
                   r"[ ]?[0-9A-Z]{8,12}|\d{2}-\d{7})\b")


def clean(v: str | None) -> str:
    return re.sub(r"[\s.]", "", (v or "").upper())


def country_of(v: str | None) -> str | None:
    """Which country a tax number belongs to, from its shape alone."""
    v = clean(v)
    if not v:
        return None
    if EIN.match(v):
        return "US"
    if v[:2] in EU:
        return v[:2]
    if re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", v):
        return "IN"
    return None


def de_check(nine: str) -> bool:
    if not re.fullmatch(r"\d{9}", nine) or nine[0] == "0":
        return False
    p = 10
    for ch in nine[:8]:
        s = (int(ch) + p) % 10 or 10
        p = (2 * s) % 11
    c = 11 - p
    return (0 if c == 10 else c) == int(nine[8])


def de_with_check(eight: str) -> str:
    for d in "0123456789":
        if de_check(eight + d):
            return "DE" + eight + d
    raise ValueError(eight)


def fr_key(siren: str) -> str:
    return f"{(12 + 3 * (int(siren) % 97)) % 97:02d}"


def validate(v: str | None) -> tuple[bool, str]:
    """(ok, why). Unknown schemes are not failed, only described."""
    v = clean(v)
    if not v:
        return False, "missing"
    cc = country_of(v)
    if cc == "US":
        return (v[:2] not in EIN_BAD_PREFIX), ("EIN" if v[:2] not in EIN_BAD_PREFIX else "EIN prefix never issued")
    if cc in EU:
        body = v[2:]
        fmt = EU_FORMATS.get(cc)
        if fmt and not re.fullmatch(fmt, body):
            return False, f"not a {cc} VAT number format"
        if cc == "DE":
            return (de_check(body), "DE VAT check digit" + ("" if de_check(body) else " wrong"))
        if cc == "FR" and body[:2].isdigit():
            ok = fr_key(body[2:]) == body[:2]
            return ok, "FR VAT key" + ("" if ok else " wrong")
        return True, f"{cc} VAT format (registration not looked up)"
    if cc == "IN":
        from .gstin import is_valid
        return is_valid(v), "GSTIN checksum"
    return False, "unrecognised tax number"
