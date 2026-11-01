"""Personal-data match. The result is match or no-match.

The matched substring is not returned and is not logged.
"""

from __future__ import annotations

import re

# Five letters, four digits, one letter. One space may separate the groups.
_PAN = re.compile(r"(?<![A-Za-z])[A-Za-z]{5}\s?[0-9]{4}\s?[A-Za-z](?![A-Za-z])")
# Twelve digits, or three groups of four with a space or hyphen.
_AADHAAR = re.compile(r"(?<!\d)\d{4}(?:[ \-]\d{4}){2}(?!\d)|(?<!\d)\d{12}(?!\d)")
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:[^A-Za-z0-9]|$)"
)
# Ten-digit mobile starting 6–9, optional 0 / 91 / +91, optional 5+5 spacing.
_MOBILE = re.compile(
    r"(?<!\d)(?:(?:\+|00)?91[\s-]?|0)?(?:[6-9]\d{9}|[6-9]\d{4}\s\d{5})(?!\d)"
)
_OTP_WORD = r"(?:otp|one[\s-]*time[\s-]*(?:password|pin|code))"
_OTP = re.compile(
    rf"\b{_OTP_WORD}\b.{{0,40}}?\b\d{{4,8}}\b|\b\d{{4,8}}\b.{{0,40}}?\b{_OTP_WORD}\b",
    re.IGNORECASE,
)
_ACCOUNT = re.compile(
    r"\b(?:account(?:\s+(?:number|no\.?|num))?|folio(?:\s+(?:number|no\.?|num))?|"
    r"dp[\s-]*id|dpid|demat(?:\s+id)?|bo[\s-]*id)\b(?:\W{0,12})(\d(?:[\d\s-]{4,}\d)?)",
    re.IGNORECASE,
)
_LONG_RUN = 6


def contains_pii(text: str) -> bool:
    """Return whether the text matches a personal-data pattern.

    True is a match. The caller gets no substring, and this function writes
    nothing to a log.
    """
    if not text:
        return False
    if _PAN.search(text) or _AADHAAR.search(text) or _EMAIL.search(text):
        return True
    if _MOBILE.search(text) or _OTP.search(text):
        return True
    for match in _ACCOUNT.finditer(text):
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) >= _LONG_RUN:
            return True
    return False
