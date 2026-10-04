import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CSV = ROOT / "tests" / "fixtures" / "sheet.csv"


def load_import_script():
    """scripts/import_csv.py isn't a package module; load it by path."""
    spec = importlib.util.spec_from_file_location("import_csv", ROOT / "scripts" / "import_csv.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["import_csv"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db_path(tmp_path):
    from applylog import db

    path = tmp_path / "applylog.db"
    db.init_db(path)
    return path


@pytest.fixture
def imported_db(db_path):
    """A temp DB loaded with the fixture sheet."""
    import csv
    from datetime import date

    from applylog import db

    importer = load_import_script()
    with FIXTURE_CSV.open(encoding="utf-8-sig", newline="") as fh, db.connect(db_path) as conn:
        importer.import_rows(conn, csv.DictReader(fh), year=2026, today=date(2026, 10, 2))
    return db_path
