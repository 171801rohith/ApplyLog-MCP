"""SQLite storage. Every function takes an open connection; all SQL is parameterized."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from . import logic
from .config import utc_stamp

SCHEMA = """
CREATE TABLE drives (
  id            INTEGER PRIMARY KEY,
  company       TEXT NOT NULL,
  company_norm  TEXT NOT NULL,
  role          TEXT,
  ctc_text      TEXT,
  ctc_min_lpa   REAL,
  ctc_max_lpa   REAL,
  is_internship INTEGER NOT NULL DEFAULT 0,
  stipend_month INTEGER,
  source        TEXT NOT NULL DEFAULT 'campus'
                CHECK (source IN ('campus','off_campus','referral','linkedin','company_site','other')),
  registered_on TEXT,
  status        TEXT NOT NULL DEFAULT 'in_progress'
                CHECK (status IN ('registered','in_progress','rejected','offer','accepted','declined','withdrawn')),
  notes         TEXT NOT NULL DEFAULT '',
  archived_at   TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE TABLE rounds (
  id          INTEGER PRIMARY KEY,
  drive_id    INTEGER NOT NULL REFERENCES drives(id) ON DELETE CASCADE,
  round_no    INTEGER NOT NULL,
  round_type  TEXT NOT NULL CHECK (round_type IN
                ('aptitude_mcq','dsa_coding','resume_screening','technical_interview',
                 'hr_interview','group_discussion','other')),
  label       TEXT,
  round_date  TEXT,
  result      TEXT NOT NULL DEFAULT 'pending'
                CHECK (result IN ('pending','passed','failed','skipped')),
  notes       TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE (drive_id, round_no)
);
CREATE INDEX ix_rounds_date ON rounds(round_date);
CREATE INDEX ix_drives_norm ON drives(company_norm);
PRAGMA user_version = 1;
"""

BACKUPS_KEPT = 14

# Enum -> hard-coded ORDER BY clauses (never interpolate user text).
SORTS = {
    "updated": "d.updated_at DESC, d.id DESC",
    "company": "d.company COLLATE NOCASE ASC, d.id ASC",
    "registered_on": "d.registered_on IS NULL, d.registered_on DESC, d.id DESC",
}


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


@contextmanager
def connect(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Fresh connection per call; commits on success, rolls back on error, always closes."""
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str) -> None:
    with connect(db_path) as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            conn.executescript(SCHEMA)


def backup_if_needed(db_path: Path, backups_dir: Path, today: date) -> Path | None:
    """Online backup via sqlite3 backup API (safe with WAL); keep the newest BACKUPS_KEPT."""
    target = backups_dir / f"applylog-{today:%Y%m%d}.db"
    made = None
    if not target.exists():
        with connect(db_path) as src:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        made = target
    for old in sorted(backups_dir.glob("applylog-*.db"), reverse=True)[BACKUPS_KEPT:]:
        old.unlink()
    return made


# ---------- reads ----------

def _drive_row(conn: sqlite3.Connection, drive_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM drives WHERE id = ?", (drive_id,)).fetchone()
    if row is None:
        raise NotFound(f"No drive with id {drive_id}.")
    return row


def get_drive(conn: sqlite3.Connection, drive_id: int) -> dict[str, Any]:
    drive = dict(_drive_row(conn, drive_id))
    drive["rounds"] = [dict(r) for r in conn.execute(
        "SELECT * FROM rounds WHERE drive_id = ? ORDER BY round_no", (drive_id,))]
    return drive


def find_duplicates(conn: sqlite3.Connection, company: str, ctc_text: str | None,
                    role: str | None) -> list[dict[str, Any]]:
    """Non-archived drives for the same company whose CTC and role don't conflict (blank = wildcard)."""
    rows = conn.execute(
        "SELECT id, company, role, ctc_text, status, registered_on FROM drives "
        "WHERE company_norm = ? AND archived_at IS NULL ORDER BY id",
        (logic.normalize_company(company),),
    ).fetchall()
    want_ctc, want_role = logic.ctc_key(ctc_text), logic.ctc_key(role)
    out = []
    for r in rows:
        have_ctc, have_role = logic.ctc_key(r["ctc_text"]), logic.ctc_key(r["role"])
        if (not want_ctc or not have_ctc or want_ctc == have_ctc) and \
           (not want_role or not have_role or want_role == have_role):
            out.append(dict(r))
    return out


_LATEST_ROUND = (
    "SELECT round_no, label, round_type, round_date, result FROM rounds "
    "WHERE drive_id = ? ORDER BY round_no DESC LIMIT 1"
)


def list_drives(conn: sqlite3.Connection, *, statuses: list[str] | None = None,
                company: str | None = None, source: str | None = None,
                has_pending_round: bool | None = None, include_archived: bool = False,
                sort: str = "updated", limit: int = 20, offset: int = 0
                ) -> tuple[int, list[dict[str, Any]]]:
    where, args = [], []
    if not include_archived:
        where.append("d.archived_at IS NULL")
    if statuses:
        where.append(f"d.status IN ({','.join('?' * len(statuses))})")
        args += statuses
    if company:
        where.append("d.company LIKE ? ESCAPE '\\'")
        args.append("%" + company.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if source:
        where.append("d.source = ?")
        args.append(source)
    if has_pending_round is not None:
        where.append(("" if has_pending_round else "NOT ") +
                     "EXISTS (SELECT 1 FROM rounds p WHERE p.drive_id = d.id AND p.result = 'pending')")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM drives d {clause}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT d.id, d.company, d.role, d.ctc_text, d.status, d.archived_at FROM drives d "
        f"{clause} ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        latest = conn.execute(_LATEST_ROUND, (r["id"],)).fetchone()
        item["latest_round"] = dict(latest) if latest else None
        items.append(item)
    return total, items


def pending_rounds(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT r.id AS round_id, r.drive_id, d.company, d.ctc_text, r.round_no, r.round_type, "
        "r.label, r.round_date FROM rounds r JOIN drives d ON d.id = r.drive_id "
        "WHERE r.result = 'pending' AND d.archived_at IS NULL "
        "ORDER BY r.round_date IS NULL, r.round_date, d.company")]


def stats_rows(conn: sqlite3.Connection) -> dict[str, Any]:
    by_type = [dict(r) for r in conn.execute(
        "SELECT r.round_type, SUM(r.result = 'passed') AS passed, SUM(r.result = 'failed') AS failed, "
        "SUM(r.result = 'pending') AS pending FROM rounds r JOIN drives d ON d.id = r.drive_id "
        "WHERE d.archived_at IS NULL GROUP BY r.round_type ORDER BY r.round_type")]
    drives = [dict(r) for r in conn.execute(
        "SELECT d.id, d.status, d.ctc_min_lpa, d.is_internship, MAX(r.round_no) AS furthest "
        "FROM drives d LEFT JOIN rounds r ON r.drive_id = d.id "
        "WHERE d.archived_at IS NULL GROUP BY d.id")]
    return {"by_type": by_type, "drives": drives,
            "furthest": Counter(d["furthest"] or 0 for d in drives)}


# ---------- writes ----------

def _ctc_columns(ctc_text: str | None) -> dict[str, Any]:
    c = logic.parse_ctc(ctc_text)
    return {"ctc_text": ctc_text, "ctc_min_lpa": c.min_lpa, "ctc_max_lpa": c.max_lpa,
            "is_internship": int(c.is_internship), "stipend_month": c.stipend_month}


def insert_drive(conn: sqlite3.Connection, *, company: str, today: date, role: str | None = None,
                 ctc_text: str | None = None, source: str = "campus",
                 registered_on: str | None = None, status: str = "registered",
                 note: str | None = None) -> int:
    now = utc_stamp()
    cols = {"company": company, "company_norm": logic.normalize_company(company), "role": role,
            **_ctc_columns(ctc_text), "source": source, "registered_on": registered_on,
            "status": status, "notes": logic.append_note("", note, today),
            "created_at": now, "updated_at": now}
    cur = conn.execute(
        f"INSERT INTO drives ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()))
    return cur.lastrowid


def _apply_status_rule(conn: sqlite3.Connection, drive_id: int) -> None:
    d = _drive_row(conn, drive_id)
    results = [r[0] for r in conn.execute("SELECT result FROM rounds WHERE drive_id = ?", (drive_id,))]
    new = logic.status_after_round(d["status"], d["archived_at"] is not None, results)
    conn.execute("UPDATE drives SET status = ?, updated_at = ? WHERE id = ?",
                 (new, utc_stamp(), drive_id))


def insert_round(conn: sqlite3.Connection, *, drive_id: int, round_type: str, today: date,
                 round_no: int | None = None, label: str | None = None,
                 round_date: str | None = None, result: str = "pending",
                 note: str | None = None) -> int:
    _drive_row(conn, drive_id)
    if round_no is None:
        round_no = conn.execute("SELECT COALESCE(MAX(round_no), 0) + 1 FROM rounds WHERE drive_id = ?",
                                (drive_id,)).fetchone()[0]
    now = utc_stamp()
    try:
        cur = conn.execute(
            "INSERT INTO rounds (drive_id, round_no, round_type, label, round_date, result, notes, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (drive_id, round_no, round_type, label, round_date, result,
             logic.append_note("", note, today), now, now))
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"Drive {drive_id} already has a round {round_no}; use update_round.") from exc
    _apply_status_rule(conn, drive_id)
    return cur.lastrowid


def find_round_id(conn: sqlite3.Connection, drive_id: int, round_no: int) -> int:
    row = conn.execute("SELECT id FROM rounds WHERE drive_id = ? AND round_no = ?",
                       (drive_id, round_no)).fetchone()
    if row is None:
        raise NotFound(f"Drive {drive_id} has no round {round_no}.")
    return row[0]


def update_round(conn: sqlite3.Connection, round_id: int, *, today: date,
                 changes: dict[str, Any], note: str | None = None) -> int:
    """changes: subset of result/round_date/label/round_type. Returns the drive id."""
    row = conn.execute("SELECT drive_id, notes FROM rounds WHERE id = ?", (round_id,)).fetchone()
    if row is None:
        raise NotFound(f"No round with id {round_id}.")
    allowed = {k: changes[k] for k in ("result", "round_date", "label", "round_type") if k in changes}
    allowed["notes"] = logic.append_note(row["notes"], note, today)
    allowed["updated_at"] = utc_stamp()
    conn.execute(f"UPDATE rounds SET {', '.join(f'{k} = ?' for k in allowed)} WHERE id = ?",
                 [*allowed.values(), round_id])
    if "result" in allowed:
        _apply_status_rule(conn, row["drive_id"])
    return row["drive_id"]


def update_drive(conn: sqlite3.Connection, drive_id: int, *, today: date,
                 changes: dict[str, Any], note: str | None = None,
                 archive: bool | None = None) -> None:
    """changes: subset of company/role/ctc_text/source/status. archive=True/False archives/restores."""
    d = _drive_row(conn, drive_id)
    cols: dict[str, Any] = {k: changes[k] for k in ("company", "role", "source", "status") if k in changes}
    if "company" in cols:
        cols["company_norm"] = logic.normalize_company(cols["company"])
    if "ctc_text" in changes:
        cols.update(_ctc_columns(changes["ctc_text"]))
    if archive is not None:
        cols["archived_at"] = utc_stamp() if archive else None
    cols["notes"] = logic.append_note(d["notes"], note, today)
    cols["updated_at"] = utc_stamp()
    conn.execute(f"UPDATE drives SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                 [*cols.values(), drive_id])
