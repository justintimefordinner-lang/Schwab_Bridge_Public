"""
backfill.py
===========

App-triggered, WRITE-ONLY full trade-history rebuild.

New users start with an empty P&L. The dashboard's "Build trade history" button
drops a single empty marker file into this folder:

  * task_inbox/backfill_history   — "please run a full history rebuild"

The auto_push loop calls process() each tick; when the marker is present it runs
sync_trade_history.main(full=True) once (the same deep backfill as
`python sync_trade_history.py --full`) and reports progress back ONE WAY through
a sanitized history-status.json in the app's data/ folder. No secrets flow
app→bridge; the app only reads its own data/ folder.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

INBOX_DIR = "task_inbox"
BACKFILL_MARKER = os.path.join(INBOX_DIR, "backfill_history")
STATUS_FILE = "history-status.json"  # written into APP_DATA_DIR (app reads it)


def _status_path() -> str | None:
    data_dir = os.environ.get("APP_DATA_DIR")
    if not data_dir:
        try:
            from research_sync import _app_data_dir
            data_dir = _app_data_dir()
        except Exception:
            data_dir = None
    return os.path.join(data_dir, STATUS_FILE) if data_dir else None


def _write_status(status: str, error: str | None = None, counts: dict | None = None) -> None:
    """Publish a sanitized status the app can poll. Never contains secrets."""
    path = _status_path()
    if not path:
        return
    payload = {
        "status": status,          # idle | running | done | error
        "counts": counts,          # {csp, leap, spread, covered, stock, total} on done
        "error": error,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass


def process(log=None) -> None:
    """Service an app-dropped backfill request. Safe to call every loop tick — it
    only acts when the marker is present. Runs synchronously (like the first-run
    backfill), so the loop pauses other pushes until it finishes."""
    if not os.path.exists(BACKFILL_MARKER):
        return
    try:
        os.remove(BACKFILL_MARKER)
    except OSError:
        pass

    _write_status("running")
    if log:
        log("history backfill: full rebuild requested from the app — starting")
    try:
        import sync_trade_history
        counts = sync_trade_history.main(full=True)
        total = sum(counts.values()) if isinstance(counts, dict) else None
        summary = {**counts, "total": total} if isinstance(counts, dict) else None
        _write_status("done", counts=summary)
        if log:
            log(f"history backfill: done — {total if total is not None else '?'} closed trades")
    except (SystemExit, Exception) as exc:  # noqa: BLE001 — surface any failure to the UI
        msg = str(exc) or exc.__class__.__name__
        _write_status("error", error=msg)
        if log:
            log(f"history backfill: ERROR — {msg}")
