"""
report_refresh.py
=================

App-triggered, WRITE-ONLY rebuild of the Morning Brief (am_report).

The dashboard's Morning Brief "Refresh" button drops a single empty marker file:

  * task_inbox/refresh_report   — "please rebuild the morning brief now"

The auto_push loop calls process() each tick; when the marker is present it runs
am_report.main(force=True) once (the same job the scheduler runs on
AM_REPORT_PUSH_INTERVAL, forced so it ignores the freshness gate) and reports
progress back ONE WAY through a sanitized report-status.json in the app's data/
folder. No secrets flow app->bridge; the app only reads its own data/ folder.

Mirrors backfill.py — same write-only, poll-a-status-file contract.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

INBOX_DIR = "task_inbox"
REFRESH_MARKER = os.path.join(INBOX_DIR, "refresh_report")
STATUS_FILE = "report-status.json"  # written into APP_DATA_DIR (app reads it)


def _status_path() -> str | None:
    data_dir = os.environ.get("APP_DATA_DIR")
    if not data_dir:
        try:
            from research_sync import _app_data_dir
            data_dir = _app_data_dir()
        except Exception:
            data_dir = None
    return os.path.join(data_dir, STATUS_FILE) if data_dir else None


def _write_status(status: str, error: str | None = None) -> None:
    """Publish a sanitized status the app can poll. Never contains secrets."""
    path = _status_path()
    if not path:
        return
    payload = {
        "status": status,          # idle | running | done | error
        "error": error,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass


def process(log=None) -> None:
    """Service an app-dropped morning-brief refresh. Safe to call every loop tick —
    it only acts when the marker is present. Runs synchronously (like backfill),
    so the loop pauses other pushes until am_report finishes."""
    if not os.path.exists(REFRESH_MARKER):
        return
    try:
        os.remove(REFRESH_MARKER)
    except OSError:
        pass

    _write_status("running")
    if log:
        log("am_report: refresh requested from the app — rebuilding")
    try:
        import am_report
        am_report.main(force=True)
        _write_status("done")
        if log:
            log("am_report: refresh done")
    except (SystemExit, Exception) as exc:  # noqa: BLE001 — surface any failure to the UI
        msg = str(exc) or exc.__class__.__name__
        _write_status("error", error=msg)
        if log:
            log(f"am_report: refresh ERROR — {msg}")
