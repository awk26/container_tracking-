import re

_LETTER_VALUES = {
    "A": 10, "B": 12, "C": 13, "D": 14, "E": 15, "F": 16, "G": 17, "H": 18,
    "I": 19, "J": 20, "K": 21, "L": 23, "M": 24, "N": 25, "O": 26, "P": 27,
    "Q": 28, "R": 29, "S": 30, "T": 31, "U": 32, "V": 34, "W": 35, "X": 36,
    "Y": 37, "Z": 38,
}

_FORMAT_RE = re.compile(r"^[A-Z]{4}\d{7}$")


def normalize(raw: str) -> str:
    return re.sub(r"\s+", "", raw or "").upper()


def is_valid_container_number(raw: str) -> bool:
    """Validate ISO 6346 format (4-letter owner code + 6-digit serial + 1 check digit)."""
    number = normalize(raw)
    if not _FORMAT_RE.match(number):
        return False

    total = 0
    for i, ch in enumerate(number[:10]):
        value = _LETTER_VALUES[ch] if ch.isalpha() else int(ch)
        total += value * (2 ** i)

    check_digit = total % 11
    if check_digit == 10:
        check_digit = 0

    return check_digit == int(number[10])
