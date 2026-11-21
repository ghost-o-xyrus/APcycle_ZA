"""GSTIN: anchored on real GSTINs printed in public GeM contracts."""
import pytest

from apflow.gstin import CHARS, checksum_char, is_valid, repair, with_checksum

REAL = ["36AAAAI2661J1ZP", "07AAACN4089K1Z5", "37AABAI0046C1ZA"]  # IIT Hyderabad, Narang Scientific, IIPE (GeM PDFs)


@pytest.mark.parametrize("g", REAL)
def test_real_gstins_validate(g):
    assert is_valid(g)


@pytest.mark.parametrize("g", REAL)
def test_every_single_character_substitution_is_caught(g):
    # The mod-36 Luhn-style checksum must catch any one-character error in the first 14.
    for i in range(14):
        for c in CHARS:
            if c == g[i]:
                continue
            bad = g[:i] + c + g[i + 1:]
            assert checksum_char(bad[:14]) != g[14] or not is_valid(bad), (i, c)


def test_with_checksum_roundtrip():
    for g in REAL:
        assert with_checksum(g[:14]) == g


@pytest.mark.parametrize("bad,expected", [
    ("O7AAACN4089K1Z5", "07AAACN4089K1Z5"),   # O read for 0 in the state code
    ("37AABAIOO46C1ZA", "37AABAI0046C1ZA"),   # two O's in the PAN digits
    ("36AAAAI2661J12P", "36AAAAI2661J1ZP"),   # Z read as 2
])
def test_repair_fixes_ocr_confusions(bad, expected):
    fixed, did = repair(bad)
    assert did and fixed == expected


def test_repair_does_not_touch_valid():
    assert repair(REAL[0]) == (REAL[0], False)


def test_repair_refuses_when_ambiguous():
    # '1' at entity and checksum positions: two equally small repairs are valid -> refuse to guess
    fixed, did = repair("29AADCD4417K1Z1")
    assert not did


@pytest.mark.parametrize("g", [None, "", "07AAACN4089K1Z", "07AAACN4089K1Z55", "07aaacn4089k1z5x"])
def test_malformed(g):
    assert not is_valid(g)
