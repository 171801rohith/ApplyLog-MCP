"""Import placement data from the Google Sheet (run manually; not an MCP tool).

    python scripts/import_csv.py PlacementTry.xlsx          # every sheet in the workbook
    python scripts/import_csv.py export.csv --source campus  # one sheet exported as CSV
    options: --dry-run  --year 2026  --db PATH

Each sheet / CSV needs the header: Company,Offer / CTC,Date,Round,Round Result
In a workbook the sheet name picks the source: names containing "out"/"off" -> off_campus,
"referral" -> referral, "linkedin" -> linkedin, anything else -> campus (--source overrides).
Re-running is safe: a row whose company, CTC text and registration date match an existing
drive is skipped.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import get_args

try:
    import applylog  # noqa: F401
except ImportError:  # running from a checkout without `pip install -e .`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from applylog import db, logic
from applylog.config import data_paths, local_today

HEADER = ("Company", "Offer / CTC", "Date", "Round", "Round Result")
RESULTS = {"failed": "failed", "pending": "pending", "passed": "passed",
           "cleared": "passed", "skipped": "skipped"}
SOURCES = get_args(logic.Source)


def source_for_sheet(name: str) -> str:
    n = name.casefold()
    if "referral" in n:
        return "referral"
    if "linkedin" in n:
        return "linkedin"
    return "off_campus" if re.search(r"\b(out|off)", n) else "campus"


# ---------- import ----------

def import_rows(conn: sqlite3.Connection, rows: Iterable[dict[str, str]], *, year: int, today: date,
                source: str = "campus", report: Callable[[str], None] | None = None) -> dict[str, int]:
    report = report or (lambda _line: None)
    summary = {"drives": 0, "rounds": 0, "skipped_blank": 0, "skipped_duplicate": 0}
    for row in rows:
        company = (row.get("Company") or "").strip()
        if not company:
            summary["skipped_blank"] += 1
            continue
        ctc = (row.get("Offer / CTC") or "").strip() or None
        day, is_reg = logic.parse_sheet_date(row.get("Date"), year)
        registered_on = day if is_reg else None
        if any(logic.ctc_key(d["ctc_text"]) == logic.ctc_key(ctc) and d["registered_on"] == registered_on
               for d in db.find_duplicates(conn, company, ctc, None)):
            summary["skipped_duplicate"] += 1
            report(f"  = {company} [{ctc or '-'}]: already logged, skipped")
            continue
        label, round_no = logic.parse_round_label(row.get("Round"))
        result = RESULTS.get((row.get("Round Result") or "").strip().casefold(), "pending")

        drive_id = db.insert_drive(
            conn, company=company, ctc_text=ctc, source=source, today=today,
            note="Imported from sheet", registered_on=registered_on,
            status="rejected" if label is None and result == "failed" else "registered")
        summary["drives"] += 1
        if label is None:
            report(f"  + {company} [{ctc or '-'}] {source}: no rounds yet")
            continue
        # "Round (2)" means the earlier rounds were cleared, even if the sheet doesn't list them.
        for earlier in range(1, round_no):
            db.insert_round(conn, drive_id=drive_id, round_no=earlier, round_type="other",
                            label="unknown", result="passed", today=today)
            summary["rounds"] += 1
        round_type = logic.round_type_for_label(label)
        db.insert_round(conn, drive_id=drive_id, round_no=round_no, round_type=round_type, label=label,
                        round_date=None if is_reg else day, result=result, today=today)
        summary["rounds"] += 1
        report(f"  + {company} [{ctc or '-'}] {source}: round {round_no} {round_type} "
               f"({label}) {result}" + (f" on {day}" if day and not is_reg else ""))
    return summary


# ---------- .xlsx reading (standard library only) ----------

_M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS = {"m": _M}
_BUILTIN_DATE_FORMATS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}


def _xml(z: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(z.read(name))  # noqa: S314 - a local workbook the user chose to import


def _date_styles(z: zipfile.ZipFile) -> set[int]:
    """Indexes of cell styles whose number format is a date."""
    if "xl/styles.xml" not in z.namelist():
        return set()
    styles = _xml(z, "xl/styles.xml")
    custom = {int(f.get("numFmtId")): f.get("formatCode", "") for f in styles.iterfind("m:numFmts/m:numFmt", _NS)}

    def is_date(fmt_id: int) -> bool:
        code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", custom.get(fmt_id, "")).casefold()
        return fmt_id in _BUILTIN_DATE_FORMATS or bool(re.search(r"[dmy]", code))

    return {i for i, xf in enumerate(styles.iterfind("m:cellXfs/m:xf", _NS))
            if is_date(int(xf.get("numFmtId", "0")))}


def _column(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]*", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n


def _cell(c: ET.Element, shared: list[str], date_styles: set[int], epoch: date) -> str:
    kind, value = c.get("t"), c.findtext("m:v", None, _NS)
    if kind == "inlineStr":
        return "".join(t.text or "" for t in c.iter(f"{{{_M}}}t"))
    if value is None or kind == "e":
        return ""
    if kind == "s":
        return shared[int(value)]
    if kind in ("str", "b"):
        return value
    if int(c.get("s", "0")) in date_styles:
        return (epoch + timedelta(days=float(value))).isoformat()
    number = float(value)
    return str(int(number)) if number.is_integer() else value


def read_xlsx(path: Path) -> list[tuple[str, list[dict[str, str]]]]:
    """[(sheet name, rows as {header: text})] for every sheet; date cells become YYYY-MM-DD."""
    with zipfile.ZipFile(path) as z:
        workbook = _xml(z, "xl/workbook.xml")
        props = workbook.find("m:workbookPr", _NS)
        epoch = date(1904, 1, 1) if props is not None and props.get("date1904") in ("1", "true") \
            else date(1899, 12, 30)
        rels = {r.get("Id"): r.get("Target") for r in _xml(z, "xl/_rels/workbook.xml.rels")}
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in _xml(z, "xl/sharedStrings.xml").iterfind("m:si", _NS):
                plain = si.find("m:t", _NS)
                rich = "".join(r.findtext("m:t", "", _NS) for r in si.iterfind("m:r", _NS))
                shared.append((plain.text or "") if plain is not None else rich)
        date_styles = _date_styles(z)

        sheets = []
        for sheet in workbook.iterfind("m:sheets/m:sheet", _NS):
            target = rels[sheet.get(f"{{{_R}}}id")]
            part = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            grid = []
            for row in _xml(z, part).iterfind("m:sheetData/m:row", _NS):
                cells: dict[int, str] = {}
                for c in row.iterfind("m:c", _NS):
                    cells[_column(c.get("r", "")) or len(cells) + 1] = _cell(c, shared, date_styles, epoch)
                grid.append(cells)
            header_row = next((g for g in grid if any(v.strip() for v in g.values())), {})
            header = {i: v.strip() for i, v in header_row.items() if v.strip()}
            body = grid[grid.index(header_row) + 1:] if header_row else []
            rows = [{name: cells.get(i, "") for i, name in header.items()} for cells in body]
            sheets.append((sheet.get("name", part), rows))
        return sheets


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path, help=".xlsx workbook (all sheets) or .csv export")
    parser.add_argument("--source", choices=SOURCES, default=None,
                        help="source for every row (default: from the sheet name; campus for CSV)")
    parser.add_argument("--year", type=int, default=2026, help="year for dates like 'Aug-6'")
    parser.add_argument("--db", type=Path, default=None, help="database (default: APPLYLOG_HOME)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be imported; write nothing")
    args = parser.parse_args(argv)

    def err(line: str) -> None:
        print(line, file=sys.stderr)

    if args.file.suffix.casefold() == ".xlsx":
        sheets = [(name, rows, args.source or source_for_sheet(name)) for name, rows in read_xlsx(args.file)]
    else:
        sheets = [(args.file.stem, read_csv(args.file), args.source or "campus")]

    usable = []
    for name, rows, source in sheets:
        missing = [h for h in HEADER if not rows or h not in rows[0]]
        if missing:
            err(f"skipping sheet '{name}': missing columns {', '.join(missing)}")
        else:
            usable.append((name, rows, source))
    if not usable:
        err("error: nothing to import (expected header: " + ",".join(HEADER) + ")")
        return 2

    db_path = args.db or data_paths().db
    db.init_db(db_path)
    total = dict.fromkeys(("drives", "rounds", "skipped_blank", "skipped_duplicate"), 0)
    with db.connect(db_path) as conn:
        for name, rows, source in usable:
            err(f"Sheet '{name}' (source: {source})")
            summary = import_rows(conn, rows, year=args.year, today=local_today(), source=source, report=err)
            total = {k: total[k] + summary[k] for k in total}
        if args.dry_run:
            conn.rollback()
    verb = "Would import" if args.dry_run else "Imported"
    err(f"{verb} into {db_path}: {total['drives']} drives, {total['rounds']} rounds; "
        f"skipped {total['skipped_duplicate']} duplicates, {total['skipped_blank']} blank rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
