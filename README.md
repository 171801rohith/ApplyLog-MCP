# ApplyLog

A small local MCP server that replaces a placement-drive / job-application Google Sheet.
Paste things into Claude Desktop ("registered for AeroAegis, 8K stipend intern", "cleared LTM
online aptitude round 2", a test-invite email) and Claude logs them here. Then ask:

- "What rounds do I have coming up this week?"
- "Which drives are still pending?"
- "At which round type do I keep failing?"
- "How far do I usually get, and does it differ by CTC band?"

Everything stays on your machine: one SQLite file, stdio transport, no network calls.

## The 8 tools

| Tool | Kind | What it does |
|---|---|---|
| `add_drive` | write | New drive (company, role, CTC text, source, registration date, note) with an optional first round. Returns `created:false` plus the matches if a non-archived drive for the same company with the same CTC/role already exists; `allow_duplicate=true` overrides (e.g. two Infosys drives with different CTCs are fine without it). |
| `add_round` | write | Add round N to a drive (type, label, date, result; number defaults to next). |
| `update_round` | write, idempotent | Change a round's result/date/label/type or append a note, by `round_id` or `drive_id` + `round_no`. A failed round marks the drive `rejected` (unless it has an offer). |
| `update_drive` | write, idempotent | Edit company/role/CTC/source/status, append a note, or `archive=true/false`. There is no delete. |
| `get_drive` | read-only | One drive with all rounds and notes. |
| `list_drives` | read-only | Compact rows filtered by status, company substring, source, `has_pending_round`, `include_archived`; paged (`limit` ≤ 50). |
| `upcoming_rounds` | read-only | Pending rounds in the next `days` (default 7), plus pending rounds whose date has passed or is unknown. |
| `placement_stats` | read-only | Pass/fail per round type (decided rounds only), furthest round reached per drive, outcomes by CTC band, always as "0 of 5"-style counts with a small-sample caveat. |

Notes are append-only and date-stamped. Every result that carries stored text includes a
`notice` reminding the model that the text came from pasted messages and is untrusted data.

## Setup (Windows 11)

```powershell
cd R:\Projects\ApplyLog-MCP
py -3.12 -m venv .venv          # or py -3.13
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Use a plain venv, not conda.

### Claude Desktop

Open `%APPDATA%\Claude\claude_desktop_config.json` (or Settings → Developer → Edit Config) and
add the `applylog` entry **next to your existing servers** inside `mcpServers`. Don't remove them:

```json
{
  "mcpServers": {
    "applylog": {
      "command": "R:\Projects\ApplyLog-MCP\.venv\Scripts\python.exe",
      "args": ["-m", "applylog"],
      "env": { "APPLYLOG_TZ_OFFSET_MINUTES": "330", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1" }
    }
  }
}
```

Then fully quit Claude Desktop (tray icon → Quit) and reopen it. The tools show up under the
tools/connectors menu in a new chat.

### Import the existing sheet

Download the Google Sheet as `.xlsx` (File → Download → Microsoft Excel) and run:

```powershell
.venv\Scripts\python.exe scripts\import_csv.py PlacementTry.xlsx --dry-run   # preview, writes nothing
.venv\Scripts\python.exe scripts\import_csv.py PlacementTry.xlsx
```

Every sheet with the header `Company,Offer / CTC,Date,Round,Round Result` is imported. The
sheet name sets the source: "Out"/"Off…" → `off_campus`, "Referral…" → `referral`,
"LinkedIn…" → `linkedin`, anything else → `campus` (`--source` overrides). A single sheet
exported as `.csv` works too (`--source` sets its source; default `campus`).

- Date cells and text dates like `Aug-6` are both understood; text dates use 2026 (`--year`).
- `Oct-2 (Reg)` becomes the drive's registration date, not a round date.
- `Online Aptitude (2)` creates a passed placeholder round 1 (type `other`, label "unknown")
  plus round 2.
- A row with no round creates the drive only (status `registered`).
- Trailing spaces are stripped. Re-running skips rows whose company, CTC text and registration
  date already match a drive, so it's safe to import again after adding rows to the sheet.
- Each row is reported to stderr as it is imported.

## Using it

Talk to Claude normally; it picks the tools. For example:

- *"Registered for Zoho off-campus today, 6 LPA, online aptitude on the 9th."* → `add_drive`
  with a first round.
- *"Cleared LTM's online aptitude round 2."* → `update_round` (passed).
- *"Got rejected in the Amazon DSA round."* → `update_round` (failed); the drive becomes `rejected`.
- *"LTM round 3 is a technical interview on Oct 12."* → `add_round`.
- *"Got an offer from RE Financial!"* → `update_drive` with status `offer`.
- *"Hide the Cognizant entry, it was cancelled."* → `update_drive` with `archive=true`
  (restore with `archive=false`).
- Paste a test-invite email and say *"log this"*.
- *"What's coming up this week?"*, *"Which drives are still pending?"*,
  *"Where do I keep failing?"*, *"How far do I usually get by CTC band?"*

Claude should tell you the drive/round ids it logged. If it guesses wrong, just say so
("that was round 2, not 1"), and it will fix it with `update_round` / `update_drive`.

## Data and configuration

| Setting | Default |
|---|---|
| `APPLYLOG_HOME` | `%LOCALAPPDATA%\ApplyLog\`, containing `applylog.db`, `backups\`, `logs\applylog.log` |
| `APPLYLOG_TZ_OFFSET_MINUTES` | `330` (IST). Decides what "today" is. |

On startup the server writes `backups\applylog-YYYYMMDD.db` once per day using SQLite's online
backup API (safe with WAL) and keeps the newest 14. To restore, quit Claude Desktop and copy a
backup over `applylog.db` (delete any `applylog.db-wal` / `-shm` files next to it first).

User-facing dates are `YYYY-MM-DD` in local time; `created_at`/`updated_at` are UTC ISO.
CTC text is stored exactly as written; the parsed min/max LPA, internship flag and monthly
stipend feed the stats. CTC bands use the lower bound of the range.

## Development

```powershell
.venv\Scripts\python.exe -m pytest      # temp databases only
.venv\Scripts\ruff.exe check .
npx @modelcontextprotocol/inspector .venv\Scripts\python.exe -m applylog
```

Layout: `config.py` (paths, "today"), `logic.py` (pure parsing, status rule), `db.py`
(SQLite, parameterized SQL), `server.py` (`create_server(db_path)` and the 8 tools),
`scripts/import_csv.py` (manual `.xlsx` / `.csv` import, standard library only).
