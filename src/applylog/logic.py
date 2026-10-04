"""Pure functions: parsing, normalisation, the drive-status rule and stats shaping."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal, get_args

RoundType = Literal["aptitude_mcq", "dsa_coding", "resume_screening", "technical_interview",
                    "hr_interview", "group_discussion", "other"]
Result = Literal["pending", "passed", "failed", "skipped"]
Status = Literal["registered", "in_progress", "rejected", "offer", "accepted", "declined", "withdrawn"]
Source = Literal["campus", "off_campus", "referral", "linkedin", "company_site", "other"]
ROUND_TYPES: tuple[str, ...] = get_args(RoundType)
CTC_BANDS = ("<5", "5-8", "8+", "internship", "unknown")

_NUM = r"\d+(?:\.\d+)?"
_COMPANY_SUFFIXES = {
    "pvt", "private", "ltd", "limited", "inc", "llp", "llc", "corp", "corporation",
    "co", "company", "plc", "gmbh",
}


# ---------- company / CTC ----------

def normalize_company(name: str) -> str:
    """Casefold, drop punctuation and legal suffixes; 'Accorian (Cybersecurity)' == 'Accorian(Cybersecurity)'."""
    tokens = re.findall(r"[0-9a-z]+", name.casefold())
    while len(tokens) > 1 and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def ctc_key(text: str | None) -> str:
    """Comparison key for dedupe: whitespace/case-insensitive, '?' counts as blank."""
    key = re.sub(r"\s+", "", (text or "").casefold())
    return "" if key in ("", "?", "-", "na", "n/a") else key


@dataclass(frozen=True)
class Ctc:
    min_lpa: float | None = None
    max_lpa: float | None = None
    is_internship: bool = False
    stipend_month: int | None = None


def parse_ctc(text: str | None) -> Ctc:
    """'5LPA' -> 5/5, '4.5-6LPA' -> 4.5/6, '8K (Intern)' -> internship 8000/month, '?' -> nothing."""
    t = (text or "").strip().casefold()
    if not t or t == "?":
        return Ctc()
    stipend_m = re.search(rf"({_NUM})\s*k\b", t)
    stipend = round(float(stipend_m.group(1)) * 1000) if stipend_m else None
    internship = stipend is not None or "intern" in t or "stipend" in t
    lo = hi = None
    lpa_m = re.search(
        rf"({_NUM})\s*(?:lpa|lakhs?|l)?\s*(?:-|–|to)\s*({_NUM})\s*(?:lpa|lakhs?|l)\b"
        rf"|({_NUM})\s*(?:lpa|lakhs?|l)\b",
        t,
    )
    if lpa_m:
        if lpa_m.group(3):
            lo = hi = float(lpa_m.group(3))
        else:
            lo, hi = sorted((float(lpa_m.group(1)), float(lpa_m.group(2))))
    return Ctc(lo, hi, internship, stipend)


def ctc_band(min_lpa: float | None, is_internship: bool) -> str:
    """Band by the lower bound of the CTC range."""
    if is_internship and min_lpa is None:
        return "internship"
    if min_lpa is None:
        return "unknown"
    if min_lpa < 5:
        return "<5"
    return "5-8" if min_lpa < 8 else "8+"


# ---------- rounds ----------

_LABEL_RULES = (
    (r"\b(dsa|coding|code)\b", "dsa_coding"),
    (r"\b(scanning|screening|shortlist(ing)?|resume)\b", "resume_screening"),
    (r"\btech(nical)?\s+interview\b", "technical_interview"),
    (r"\b(hr)\b", "hr_interview"),
    (r"\b(gd|group\s+discussion)\b", "group_discussion"),
    (r"\b(mcq|apt|aptitude)\b", "aptitude_mcq"),
)


def round_type_for_label(label: str | None) -> str:
    t = (label or "").casefold()
    for pattern, round_type in _LABEL_RULES:
        if re.search(pattern, t):
            return round_type
    return "other"


def parse_round_label(raw: str | None) -> tuple[str | None, int | None]:
    """'MCQ Tech n Apt (1)' -> ('MCQ Tech n Apt', 1); 'Online Aptitude' -> ('Online Aptitude', 1)."""
    text = (raw or "").strip()
    if not text:
        return None, None
    m = re.fullmatch(r"(.*?)\s*\(\s*(\d+)\s*\)", text)
    if m:
        return (m.group(1).strip() or None), int(m.group(2))
    return text, 1


# ---------- dates ----------

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def parse_sheet_date(raw: str | None, year: int) -> tuple[str | None, bool]:
    """'July-10' -> ('YYYY-07-10', False); 'Oct-2 (Reg)' -> (..., True); '?' -> (None, False).

    Already-ISO dates (e.g. from real Excel date cells) pass through unchanged.
    """
    text = (raw or "").strip()
    is_reg = bool(re.search(r"\(\s*reg", text, re.IGNORECASE))
    iso = re.match(r"\d{4}-\d{2}-\d{2}\b", text)
    if iso:
        try:
            return check_iso_date(iso.group(0)), is_reg
        except ValueError:
            return None, is_reg
    m = re.match(r"([A-Za-z]+)\s*[-/ ]\s*(\d{1,2})\b", text)
    if not m or m.group(1)[:3].casefold() not in _MONTHS:
        return None, is_reg
    try:
        d = date(year, _MONTHS[m.group(1)[:3].casefold()], int(m.group(2)))
    except ValueError:
        return None, is_reg
    return d.isoformat(), is_reg


def check_iso_date(value: str) -> str:
    """Validate a YYYY-MM-DD string (raises ValueError)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    date.fromisoformat(value)
    return value


# ---------- notes / status ----------

def append_note(existing: str, note: str | None, today: date) -> str:
    note = (note or "").strip()
    if not note:
        return existing
    entry = f"[{today.isoformat()}] {note}"
    return f"{existing}\n{entry}" if existing else entry


def status_after_round(status: str, archived: bool, results: list[str]) -> str:
    """Drive status after a round change, given all of the drive's round results.

    Any failed round -> rejected (unless archived or already offer/accepted).
    Otherwise a registered drive with rounds, or a 'rejected' drive whose failure was
    corrected, goes back to in_progress. Every other status is left alone.
    """
    if archived or status in ("offer", "accepted", "declined", "withdrawn"):
        return status
    if "failed" in results:
        return "rejected"
    if results and status in ("registered", "rejected"):
        return "in_progress"
    return status
