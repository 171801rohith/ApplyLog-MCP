from datetime import UTC, date, datetime

import pytest

from applylog import logic
from applylog.config import local_today


@pytest.mark.parametrize(("text", "lo", "hi", "intern", "stipend"), [
    ("5LPA", 5, 5, False, None),
    ("4.5-6LPA", 4.5, 6, False, None),
    ("9LPA", 9, 9, False, None),
    ("7-21LPA", 7, 21, False, None),
    ("4-6LPA", 4, 6, False, None),
    ("6.6LPA", 6.6, 6.6, False, None),
    ("3-6LPA", 3, 6, False, None),
    ("3.5LPA", 3.5, 3.5, False, None),
    ("6-9 LPA", 6, 9, False, None),
    ("4LPA", 4, 4, False, None),
    ("8K (Intern)", None, None, True, 8000),
    ("? (Intern)", None, None, True, None),
    ("25k - 35k / month (Intern)", None, None, True, 25000),
    ("25K / month (Intern)", None, None, True, 25000),
    ("? (Probation)", None, None, False, None),
    ("?", None, None, False, None),
    ("", None, None, False, None),
    (None, None, None, False, None),
    ("12 lakhs", 12, 12, False, None),
    ("competitive", None, None, False, None),  # no number -> don't invent
])
def test_parse_ctc(text, lo, hi, intern, stipend):
    c = logic.parse_ctc(text)
    assert (c.min_lpa, c.max_lpa, c.is_internship, c.stipend_month) == (lo, hi, intern, stipend)


@pytest.mark.parametrize(("raw", "label", "no"), [
    ("MCQ Tech n Apt (1)", "MCQ Tech n Apt", 1),
    ("DSA ASSESSMENT (1)", "DSA ASSESSMENT", 1),
    ("Online Aptitude (2)", "Online Aptitude", 2),
    ("Online Aptitude", "Online Aptitude", 1),
    ("Scanning (1)", "Scanning", 1),
    ("", None, None),
    (None, None, None),
])
def test_parse_round_label(raw, label, no):
    assert logic.parse_round_label(raw) == (label, no)


@pytest.mark.parametrize(("label", "round_type"), [
    ("MCQ Tech n Apt", "aptitude_mcq"),
    ("Apt", "aptitude_mcq"),
    ("Aptitude and Technical", "aptitude_mcq"),
    ("Online Aptitude", "aptitude_mcq"),
    ("DSA Assessment", "dsa_coding"),
    ("DSA ASSESSMENT", "dsa_coding"),
    ("Scanning", "resume_screening"),
    ("Resume Screening", "resume_screening"),
    ("DSA-Repo", "dsa_coding"),
    ("Resume shortlist", "resume_screening"),
    ("Technical interview", "technical_interview"),
    ("HR", "hr_interview"),
    ("GD", "group_discussion"),
    ("Hackathon", "other"),
    (None, "other"),
])
def test_round_type_for_label(label, round_type):
    assert logic.round_type_for_label(label) == round_type


@pytest.mark.parametrize(("raw", "expected"), [
    ("July-10", ("2026-07-10", False)),
    ("Aug-6", ("2026-08-06", False)),
    ("Sep-7", ("2026-09-07", False)),
    ("Oct-2 (Reg)", ("2026-10-02", True)),
    ("2026-09-13", ("2026-09-13", False)),  # real Excel date cell, already converted
    ("2026-02-30", (None, False)),
    ("?", (None, False)),
    ("", (None, False)),
    ("Feb-30", (None, False)),
    ("Foo-3", (None, False)),
])
def test_parse_sheet_date(raw, expected):
    assert logic.parse_sheet_date(raw, 2026) == expected


def test_check_iso_date():
    assert logic.check_iso_date("2026-10-05") == "2026-10-05"
    for bad in ("2026-13-01", "5 Oct", "2026-10-5"):
        with pytest.raises(ValueError):
            logic.check_iso_date(bad)


def test_normalize_company():
    assert logic.normalize_company("Accorian (Cybersecurity)") == logic.normalize_company(
        "Accorian(Cybersecurity)")
    assert logic.normalize_company("Infosys Ltd.") == logic.normalize_company("INFOSYS")
    assert logic.normalize_company("Recruit CRM Pvt Ltd") == "recruitcrm"


def test_ctc_key_treats_question_mark_as_blank():
    assert logic.ctc_key("?") == logic.ctc_key(None) == ""
    assert logic.ctc_key("6-9 LPA") == logic.ctc_key("6-9lpa")


@pytest.mark.parametrize(("lo", "intern", "band"), [
    (None, False, "unknown"), (None, True, "internship"), (3.5, False, "<5"),
    (5, False, "5-8"), (7.9, False, "5-8"), (8, False, "8+"), (9, True, "8+"),
])
def test_ctc_band(lo, intern, band):
    assert logic.ctc_band(lo, intern) == band


@pytest.mark.parametrize(("status", "archived", "results", "expected"), [
    ("in_progress", False, ["passed", "failed"], "rejected"),
    ("registered", False, ["failed"], "rejected"),
    ("offer", False, ["failed"], "offer"),
    ("accepted", False, ["failed"], "accepted"),
    ("in_progress", True, ["failed"], "in_progress"),  # archived: untouched
    ("registered", False, ["pending"], "in_progress"),
    ("in_progress", False, ["passed"], "in_progress"),
    ("rejected", False, ["passed"], "in_progress"),  # failure corrected
    ("registered", False, [], "registered"),
])
def test_status_after_round(status, archived, results, expected):
    assert logic.status_after_round(status, archived, results) == expected


def test_append_note():
    d = date(2026, 10, 2)
    assert logic.append_note("", "hello", d) == "[2026-10-02] hello"
    assert logic.append_note("[2026-10-01] a", "b", d) == "[2026-10-01] a\n[2026-10-02] b"
    assert logic.append_note("x", "  ", d) == "x"


def test_local_today_ist_midnight_boundary():
    before = datetime(2026, 10, 4, 18, 29, tzinfo=UTC)  # 23:59 IST
    after = datetime(2026, 10, 4, 18, 31, tzinfo=UTC)  # 00:01 IST next day
    assert local_today(before, 330) == date(2026, 10, 4)
    assert local_today(after, 330) == date(2026, 10, 5)
