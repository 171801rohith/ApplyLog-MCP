"""Runtime configuration: data directory, timezone offset, 'today'."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_TZ_OFFSET_MINUTES = 330  # IST


@dataclass(frozen=True)
class Paths:
    home: Path
    db: Path
    backups: Path
    logs: Path


def data_paths() -> Paths:
    """Resolve APPLYLOG_HOME (default %LOCALAPPDATA%\\ApplyLog) and create its subfolders."""
    home_env = os.environ.get("APPLYLOG_HOME")
    if home_env:
        home = Path(home_env)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        home = Path(base) / "ApplyLog"
    paths = Paths(home, home / "applylog.db", home / "backups", home / "logs")
    for d in (paths.home, paths.backups, paths.logs):
        d.mkdir(parents=True, exist_ok=True)
    return paths


def tz_offset_minutes() -> int:
    raw = os.environ.get("APPLYLOG_TZ_OFFSET_MINUTES", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TZ_OFFSET_MINUTES
    return value if -14 * 60 <= value <= 14 * 60 else DEFAULT_TZ_OFFSET_MINUTES


def utc_now() -> datetime:
    return datetime.now(UTC)


def local_today(now_utc: datetime | None = None, offset_minutes: int | None = None) -> date:
    """The user's local calendar date for the given UTC instant (default: now)."""
    if offset_minutes is None:
        offset_minutes = tz_offset_minutes()
    now_utc = now_utc or utc_now()
    return now_utc.astimezone(timezone(timedelta(minutes=offset_minutes))).date()


def utc_stamp(now_utc: datetime | None = None) -> str:
    """Machine timestamp (UTC ISO, seconds precision)."""
    return (now_utc or utc_now()).astimezone(UTC).isoformat(timespec="seconds")
