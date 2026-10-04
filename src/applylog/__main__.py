"""Entry point: `python -m applylog` runs the stdio MCP server."""

import logging
from logging.handlers import RotatingFileHandler

from . import db
from .config import data_paths, local_today


def main() -> None:
    paths = data_paths()
    # Configure logging before the SDK does, so nothing goes to stdout (reserved for the protocol).
    handler = RotatingFileHandler(paths.logs / "applylog.log", maxBytes=1_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    log = logging.getLogger("applylog")

    db.init_db(paths.db)
    try:
        made = db.backup_if_needed(paths.db, paths.backups, local_today())
        if made:
            log.info("backup written: %s", made.name)
    except Exception:
        log.exception("backup failed; continuing")

    from .server import create_server

    log.info("starting applylog server")
    create_server(paths.db).run("stdio")


if __name__ == "__main__":
    main()
