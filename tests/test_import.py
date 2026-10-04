import csv
from datetime import date

from conftest import FIXTURE_CSV, load_import_script

from applylog import db


def _rounds(conn, company):
    return [dict(r) for r in conn.execute(
        "SELECT r.round_no, r.round_type, r.label, r.round_date, r.result FROM rounds r "
        "JOIN drives d ON d.id = r.drive_id WHERE d.company = ? ORDER BY r.round_no", (company,))]


def test_fixture_import_counts(imported_db):
    with db.connect(imported_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM drives").fetchone()[0] == 13
        infosys = conn.execute("SELECT ctc_text FROM drives WHERE company = 'Infosys' ORDER BY id")
        assert [r[0] for r in infosys] == ["7-21LPA", "3.5LPA"]
        failed_r1 = conn.execute(
            "SELECT COUNT(*) FROM rounds WHERE round_no = 1 AND result = 'failed'").fetchone()[0]
        assert failed_r1 == 8
        assert conn.execute("SELECT COUNT(*) FROM drives WHERE status = 'rejected'").fetchone()[0] == 8


def test_ltm_round_two_implies_round_one_passed(imported_db):
    with db.connect(imported_db) as conn:
        assert _rounds(conn, "LTM") == [
            {"round_no": 1, "round_type": "other", "label": "unknown", "round_date": None,
             "result": "passed"},
            {"round_no": 2, "round_type": "aptitude_mcq", "label": "Online Aptitude",
             "round_date": "2026-10-04", "result": "pending"},
        ]
        assert conn.execute("SELECT status FROM drives WHERE company='LTM'").fetchone()[0] == "in_progress"


def test_rows_without_rounds(imported_db):
    with db.connect(imported_db) as conn:
        rows = {r["company"]: dict(r) for r in conn.execute(
            "SELECT company, ctc_text, status, registered_on, is_internship, stipend_month FROM drives "
            "WHERE id NOT IN (SELECT drive_id FROM rounds)")}
    assert set(rows) == {"Infosys", "AeroAegis", "Cognizant"}
    assert all(r["status"] == "registered" for r in rows.values())
    assert rows["AeroAegis"]["registered_on"] == "2026-10-02"
    assert (rows["AeroAegis"]["is_internship"], rows["AeroAegis"]["stipend_month"]) == (1, 8000)
    assert rows["Cognizant"]["ctc_text"] == "?"


def test_round_types_and_dates(imported_db):
    with db.connect(imported_db) as conn:
        assert _rounds(conn, "MuSigma")[0]["round_date"] == "2026-07-10"
        assert _rounds(conn, "Accorian(Cybersecurity)")[0]["round_type"] == "resume_screening"
        assert _rounds(conn, "Delloite")[0]["round_type"] == "dsa_coding"
        assert _rounds(conn, "RE Financial") == [
            {"round_no": 1, "round_type": "aptitude_mcq", "label": "Online Aptitude",
             "round_date": "2026-10-05", "result": "pending"}]


def test_reimport_skips_duplicates(imported_db):
    importer = load_import_script()
    with FIXTURE_CSV.open(encoding="utf-8-sig", newline="") as fh, db.connect(imported_db) as conn:
        summary = importer.import_rows(conn, csv.DictReader(fh), year=2026, today=date(2026, 10, 3))
        assert summary["drives"] == 0 and summary["skipped_duplicate"] == 13
        assert conn.execute("SELECT COUNT(*) FROM drives").fetchone()[0] == 13


def test_blank_rows_skipped(db_path):
    importer = load_import_script()
    rows = [{"Company": "", "Offer / CTC": "", "Date": "", "Round": "", "Round Result": ""},
            {"Company": "X", "Offer / CTC": "5LPA", "Date": "?", "Round": "", "Round Result": ""}]
    with db.connect(db_path) as conn:
        summary = importer.import_rows(conn, rows, year=2026, today=date(2026, 10, 2))
    assert summary == {"drives": 1, "rounds": 0, "skipped_blank": 1, "skipped_duplicate": 0}


def test_cli_main(tmp_path, capsys):
    importer = load_import_script()
    target = tmp_path / "cli.db"
    assert importer.main([str(FIXTURE_CSV), "--db", str(target)]) == 0
    err = capsys.readouterr()
    assert "13 drives" in err.err and err.out == ""
    bad = tmp_path / "bad.csv"
    bad.write_text("Name,CTC\nx,y\n")
    assert importer.main([str(bad), "--db", str(target)]) == 2


# ---------- .xlsx workbooks ----------

def _write_xlsx(path):
    """A minimal two-sheet workbook shaped like the real export: shared strings, an inline string,
    trailing spaces, real date cells (styled mmm-d) next to text dates, and gap rows."""
    import zipfile

    strings = ["Company", "Offer / CTC", "Date", "Round", "Round Result", "MuSigma", "5LPA",
               "MCQ Tech n Apt (1)", "Failed ", "Delloite ", "? ", "?", "Amazon", "? (Intern)",
               "DSA-Repo (1)", "Pending", "Rubrik", "Sep-22 (Reg)", "Resume Screening (1)"]
    sst = "".join(f"<si><t>{s}</t></si>" for s in strings)

    def row(n, *cells):
        out = []
        for col, (kind, val) in zip("ABCDE", cells, strict=False):
            if kind == "s":
                out.append(f'<c r="{col}{n}" t="s"><v>{strings.index(val)}</v></c>')
            elif kind == "d":
                out.append(f'<c r="{col}{n}" s="1"><v>{val}</v></c>')
            elif kind == "i":
                out.append(f'<c r="{col}{n}" t="inlineStr"><is><t>{val}</t></is></c>')
        return f'<row r="{n}">{"".join(out)}</row>'

    header = row(1, *[("s", h) for h in strings[:5]])
    college = "".join([header,
                       row(2, ("s", "MuSigma"), ("s", "5LPA"), ("d", "46213.0"), ("s", "MCQ Tech n Apt (1)"),
                           ("s", "Failed ")),
                       row(9, ("s", "Delloite "), ("s", "? "), ("s", "?"), ("", ""), ("i", "Pending"))])
    out = "".join([header,
                   row(2, ("s", "Amazon"), ("s", "? (Intern)"), ("d", "46278"), ("s", "DSA-Repo (1)"),
                       ("s", "Pending")),
                   row(3, ("s", "Rubrik"), ("s", "? (Intern)"), ("s", "Sep-22 (Reg)"),
                       ("s", "Resume Screening (1)"), ("s", "Pending"))])
    m = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {m} {r}><sheets>'
                   '<sheet name="College" sheetId="1" r:id="rId1"/><sheet name="Out" sheetId="2" r:id="rId2"/>'
                   "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f"<sst {m}>{sst}</sst>")
        z.writestr("xl/styles.xml", f'<styleSheet {m}><numFmts><numFmt numFmtId="164" formatCode="mmm-d"/>'
                   '</numFmts><cellXfs><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs></styleSheet>')
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {m}><sheetData>{college}</sheetData></worksheet>")
        z.writestr("xl/worksheets/sheet2.xml", f"<worksheet {m}><sheetData>{out}</sheetData></worksheet>")
    return path


def test_read_xlsx(tmp_path):
    importer = load_import_script()
    sheets = importer.read_xlsx(_write_xlsx(tmp_path / "book.xlsx"))
    assert [name for name, _ in sheets] == ["College", "Out"]
    college = sheets[0][1]
    assert college[0] == {"Company": "MuSigma", "Offer / CTC": "5LPA", "Date": "2026-07-10",
                          "Round": "MCQ Tech n Apt (1)", "Round Result": "Failed "}
    assert college[1]["Company"] == "Delloite " and college[1]["Round"] == ""
    assert college[1]["Round Result"] == "Pending"  # inline string
    assert sheets[1][1][0]["Date"] == "2026-09-13"


def test_source_for_sheet():
    importer = load_import_script()
    assert importer.source_for_sheet("College") == "campus"
    assert importer.source_for_sheet("Out") == "off_campus"
    assert importer.source_for_sheet("Off campus") == "off_campus"
    assert importer.source_for_sheet("Referrals") == "referral"


def test_cli_xlsx_import_and_dry_run(tmp_path, capsys):
    importer = load_import_script()
    book, target = _write_xlsx(tmp_path / "book.xlsx"), tmp_path / "x.db"
    assert importer.main([str(book), "--db", str(target), "--dry-run"]) == 0
    assert "Would import" in capsys.readouterr().err
    with db.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM drives").fetchone()[0] == 0

    assert importer.main([str(book), "--db", str(target)]) == 0
    with db.connect(target) as conn:
        rows = {r["company"]: dict(r) for r in conn.execute(
            "SELECT company, ctc_text, source, registered_on, status, is_internship FROM drives")}
        assert set(rows) == {"MuSigma", "Delloite", "Amazon", "Rubrik"}  # trailing spaces stripped
        assert rows["MuSigma"]["status"] == "rejected" and rows["MuSigma"]["source"] == "campus"
        assert rows["Delloite"]["ctc_text"] == "?" and rows["Delloite"]["status"] == "registered"
        assert rows["Amazon"]["source"] == "off_campus" and rows["Amazon"]["is_internship"] == 1
        assert rows["Rubrik"]["registered_on"] == "2026-09-22"
        assert _rounds(conn, "Amazon") == [{"round_no": 1, "round_type": "dsa_coding", "label": "DSA-Repo",
                                            "round_date": "2026-09-13", "result": "pending"}]
        assert _rounds(conn, "Rubrik")[0]["round_date"] is None  # (Reg) date is not a round date

    assert importer.main([str(book), "--db", str(target)]) == 0  # re-run: all duplicates
    assert "skipped 4 duplicates" in capsys.readouterr().err


def test_same_company_twice_in_sheet_is_kept(db_path):
    """Symtten appears twice in the 'Out' sheet with different CTC text and dates."""
    importer = load_import_script()
    rows = [{"Company": "Symtten", "Offer / CTC": "? (Probation)", "Date": "Sep-24 (Reg)",
             "Round": "Resume Screening (1)", "Round Result": "Pending"},
            {"Company": "Symtten", "Offer / CTC": "?", "Date": "Sep-29 (Reg)",
             "Round": "Resume Screening (1)", "Round Result": "Pending"}]
    with db.connect(db_path) as conn:
        assert importer.import_rows(conn, rows, year=2026, today=date(2026, 10, 2))["drives"] == 2
        assert importer.import_rows(conn, rows, year=2026, today=date(2026, 10, 2))["skipped_duplicate"] == 2
