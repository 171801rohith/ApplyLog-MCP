import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from applylog import db

T = date(2026, 10, 2)


def _add(conn, company, ctc=None, **kw):
    return db.insert_drive(conn, company=company, ctc_text=ctc, today=T, **kw)


def test_schema_version_and_pragmas(db_path):
    with db.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db.init_db(db_path)  # idempotent


def test_ctc_columns_stored(db_path):
    with db.connect(db_path) as conn:
        d = db.get_drive(conn, _add(conn, "AeroAegis", "8K (Intern)"))
    assert d["ctc_text"] == "8K (Intern)"
    assert (d["is_internship"], d["stipend_month"], d["ctc_min_lpa"]) == (1, 8000, None)


def test_dedupe_same_company_same_ctc(db_path):
    with db.connect(db_path) as conn:
        _add(conn, "Infosys", "7-21LPA")
        assert db.find_duplicates(conn, "INFOSYS", "7-21 LPA", None)
        assert not db.find_duplicates(conn, "Infosys", "3.5LPA", None)
        assert db.find_duplicates(conn, "Infosys", None, None)  # unknown CTC: possible duplicate
        assert not db.find_duplicates(conn, "TCS", "7-21LPA", None)


def test_dedupe_ignores_archived(db_path):
    with db.connect(db_path) as conn:
        drive_id = _add(conn, "LTM", "4LPA")
        db.update_drive(conn, drive_id, changes={}, archive=True, today=T)
        assert not db.find_duplicates(conn, "LTM", "4LPA", None)


def test_status_rule(db_path):
    with db.connect(db_path) as conn:
        drive_id = _add(conn, "LTM", "4LPA")
        assert db.get_drive(conn, drive_id)["status"] == "registered"
        db.insert_round(conn, drive_id=drive_id, round_type="aptitude_mcq", today=T)
        assert db.get_drive(conn, drive_id)["status"] == "in_progress"
        db.update_round(conn, db.find_round_id(conn, drive_id, 1), changes={"result": "passed"}, today=T)
        assert db.get_drive(conn, drive_id)["status"] == "in_progress"
        r2 = db.insert_round(conn, drive_id=drive_id, round_type="dsa_coding", today=T)
        db.update_round(conn, r2, changes={"result": "failed"}, today=T)
        assert db.get_drive(conn, drive_id)["status"] == "rejected"


def test_failed_round_keeps_offer(db_path):
    with db.connect(db_path) as conn:
        drive_id = _add(conn, "X", status="offer")
        db.insert_round(conn, drive_id=drive_id, round_type="other", result="failed", today=T)
        assert db.get_drive(conn, drive_id)["status"] == "offer"


def test_duplicate_round_no_is_conflict(db_path):
    with db.connect(db_path) as conn:
        drive_id = _add(conn, "X")
        db.insert_round(conn, drive_id=drive_id, round_no=1, round_type="other", today=T)
        with pytest.raises(db.Conflict):
            db.insert_round(conn, drive_id=drive_id, round_no=1, round_type="other", today=T)


def test_missing_ids_raise_not_found(db_path):
    with db.connect(db_path) as conn:
        with pytest.raises(db.NotFound):
            db.get_drive(conn, 999)
        with pytest.raises(db.NotFound):
            db.insert_round(conn, drive_id=999, round_type="other", today=T)
        with pytest.raises(db.NotFound):
            db.update_round(conn, 999, changes={"result": "passed"}, today=T)


def test_archive_and_restore(db_path):
    with db.connect(db_path) as conn:
        keep, gone = _add(conn, "Keep"), _add(conn, "Gone")
        db.update_drive(conn, gone, changes={}, archive=True, today=T)
        assert [r["id"] for r in db.list_drives(conn)[1]] == [keep]
        assert {r["id"] for r in db.list_drives(conn, include_archived=True)[1]} == {keep, gone}
        db.update_drive(conn, gone, changes={}, archive=False, today=T)
        assert {r["id"] for r in db.list_drives(conn)[1]} == {keep, gone}


def test_no_delete_anywhere():
    src = Path(db.__file__).read_text()
    assert not re.search(r"\bDELETE\s+FROM\b", src, re.IGNORECASE)
    assert not [name for name in dir(db) if "delete" in name.lower()]


def test_sql_injection_text_is_stored_verbatim(db_path):
    evil = "Robert'); DROP TABLE drives;--"
    with db.connect(db_path) as conn:
        drive_id = _add(conn, evil, note="x' OR '1'='1")
        d = db.get_drive(conn, drive_id)
        assert d["company"] == evil and d["notes"].endswith("x' OR '1'='1")
        assert db.list_drives(conn, company="'); DROP")[0] == 1
        assert db.list_drives(conn, company="%")[0] == 0  # LIKE wildcards are escaped
        assert conn.execute("SELECT COUNT(*) FROM drives").fetchone()[0] == 1


def test_notes_are_appended(db_path):
    with db.connect(db_path) as conn:
        drive_id = _add(conn, "X", note="first")
        db.update_drive(conn, drive_id, changes={}, note="second", today=T + timedelta(days=1))
        assert db.get_drive(conn, drive_id)["notes"] == "[2026-10-02] first\n[2026-10-03] second"


def test_update_drive_reparses_ctc(db_path):
    with db.connect(db_path) as conn:
        drive_id = _add(conn, "X", "?")
        db.update_drive(conn, drive_id, changes={"ctc_text": "6-9 LPA", "company": "X Pvt Ltd"}, today=T)
        d = db.get_drive(conn, drive_id)
        assert (d["ctc_min_lpa"], d["ctc_max_lpa"], d["company_norm"]) == (6, 9, "x")


def test_backup_daily_and_pruned(db_path, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(20):
        (backups / f"applylog-202601{i + 1:02d}.db").write_bytes(b"")
    made = db.backup_if_needed(db_path, backups, T)
    assert made and made.name == "applylog-20261002.db"
    assert db.backup_if_needed(db_path, backups, T) is None  # already have today's
    files = sorted(p.name for p in backups.glob("applylog-*.db"))
    assert len(files) == db.BACKUPS_KEPT and files[-1] == "applylog-20261002.db"
    with sqlite3.connect(made) as conn:  # a real, readable SQLite copy
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()
