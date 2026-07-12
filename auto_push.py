"""
auto_push.py
============

Long-running loop that keeps your data destinations fresh from Schwab so you
never have to re-run an export by hand.

Each cycle pulls READ-ONLY from Schwab (reusing schwab_client) and pushes to:
  - the Next.js app  ->  export_to_app.main()     (writes data/snapshot.json)
  - Google Sheets    ->  export_to_sheets.main()  (optional)

The two targets keep independent cadences, so you can refresh the app often
while writing to Sheets less frequently (Sheets has tighter API quotas).

Run with:
    python auto_push.py

Stop with Ctrl+C. It is self-contained: it pulls Schwab itself and does NOT
depend on the Streamlit dashboard being open.

.env knobs (all optional):
    APP_PUSH_INTERVAL=60        # seconds between app pushes   (0 = disable)
    SHEETS_PUSH_INTERVAL=300    # seconds between Sheets pushes (0 = disable)
    AM_POST_OPEN_TIME=09:40     # ET HH:MM — one forced full am_report run after the
                                # open settles (live premiums/greeks/OI). Empty = off.
    AM_POST_OPEN_GRACE_MIN=30   # catch window after that time (so a late start still fires)

Read-only throughout — this never places or cancels an order.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None

# Where to write refresh-status.json (the per-feed last/next times the app counts
# down to). Same folder the app reads its data from. None disables status writing.
try:
    from research_sync import _app_data_dir as _resolve_data_dir
    _STATUS_DIR = _resolve_data_dir()
except Exception:
    _STATUS_DIR = None

TICK_SECONDS = 5  # how often the loop wakes to check what's due


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _write_status(status: dict) -> None:
    """Persist the per-feed last/next refresh times for the app's countdowns. Best
    effort — never let a status-file hiccup disturb the push loop."""
    if not _STATUS_DIR:
        return
    try:
        with open(os.path.join(_STATUS_DIR, "refresh-status.json"), "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
    except Exception:
        pass


def _interval(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _run(label: str, fn) -> tuple[float, object]:
    """Run one export. Never raises — a failure in one target must not stop
    the loop or block the other target. Returns (elapsed seconds, fn result)."""
    start = time.time()
    result = None
    try:
        result = fn()
        _log(f"{label}: ok ({time.time() - start:.1f}s)")
    except SystemExit as exc:
        # Export mains raise SystemExit on config problems (missing env, etc.).
        _log(f"{label}: skipped — {exc}")
    except Exception as exc:  # e.g. AuthError when the token needs a refresh
        _log(f"{label}: ERROR — {exc}")
    return time.time() - start, result


_ENV_KEY_FOR_LABEL = {
    "app": "APP_PUSH_INTERVAL",
    "sheets": "SHEETS_PUSH_INTERVAL",
    "history": "HISTORY_PUSH_INTERVAL",
    "research": "RESEARCH_PUSH_INTERVAL",
    "am_report": "AM_REPORT_PUSH_INTERVAL",
    "am_ladder": "AM_LADDER_PUSH_INTERVAL",
}


def _reload_intervals(targets):
    """Re-read *_PUSH_INTERVAL from .env each tick and apply any change to
    already-running targets, so interval edits from the app's Settings page
    take effect without restarting this process. Only targets enabled at
    startup (interval > 0) are adjusted; flipping a target 0<->nonzero still
    needs a restart. A changed interval takes effect on that target's next
    scheduled run (next_run is intentionally left alone)."""
    load_dotenv(override=True)
    for t in targets:
        env_key = _ENV_KEY_FOR_LABEL.get(t[0])
        if not env_key:
            continue
        new_interval = _interval(env_key, t[2])
        if new_interval > 0 and new_interval != t[2]:
            _log(f"{t[0]}: interval changed {t[2]}s -> {new_interval}s (picked up from .env)")
            t[2] = new_interval


def main() -> None:
    app_interval = _interval("APP_PUSH_INTERVAL", 60)
    sheets_interval = _interval("SHEETS_PUSH_INTERVAL", 300)
    history_interval = _interval("HISTORY_PUSH_INTERVAL", 60)
    research_interval = _interval("RESEARCH_PUSH_INTERVAL", 900)
    am_report_interval = _interval("AM_REPORT_PUSH_INTERVAL", 1800)
    am_ladder_interval = _interval("AM_LADDER_PUSH_INTERVAL", 300)

    # Each target: [label, callable, interval_seconds, next_run_epoch].
    targets: list[list] = []
    if app_interval > 0:
        import export_to_app
        targets.append(["app", export_to_app.main, app_interval, 0.0])
    if history_interval > 0:
        # Rolling order/txn sync + closed-trade rebuild. Runs a light 2-day pull
        # each tick (deep backfill only on first run or `sync_trade_history.py --full`).
        import sync_trade_history
        targets.append(["history", sync_trade_history.main, history_interval, 0.0])
    if research_interval > 0:
        # Approved-universe indicators (Bollinger/RSI/MACD) + setup flags. Daily
        # indicators only move at the close, so a slow cadence is plenty — the
        # live quote is folded in for an intraday read each run.
        import research_sync
        targets.append(["research", research_sync.main, research_interval, 0.0])
    if am_report_interval > 0:
        # Pre-open market briefing (regime gate + CSP board + VRP + gamma walls),
        # market data only. A 30-min cadence keeps it fresh without hammering chains.
        import am_report
        targets.append(["am_report", am_report.main, am_report_interval, 0.0])
    if am_ladder_interval > 0:
        # Light intraday pass: refresh just the put premiums / VRP on board names
        # (puts-only chains, no candles/trend/gamma). Cheap enough for a few minutes.
        import am_report as _amr
        targets.append(["am_ladder", _amr.refresh_ladders, am_ladder_interval, 0.0])
    if sheets_interval > 0:
        try:
            import export_to_sheets
            targets.append(["sheets", export_to_sheets.main, sheets_interval, 0.0])
        except Exception as exc:
            _log(f"sheets: disabled (could not import: {exc})")

    if not targets:
        raise SystemExit(
            "Nothing to push. Set APP_PUSH_INTERVAL and/or SHEETS_PUSH_INTERVAL > 0."
        )

    # Daily post-open full run: pin ONE fresh, fully-live board right after the open
    # settles (default 9:40 ET). Premiums, greeks, the VIX family, and OI-based gamma
    # walls are only real once the session is open, so this is the morning board worth
    # reading — independent of where the 30-min am_report interval happens to land.
    # Set AM_POST_OPEN_TIME= (empty) to disable.
    import am_report as _amr_post
    post_open_raw = os.environ.get("AM_POST_OPEN_TIME", "09:40").strip()
    post_open_min: int | None = None
    if post_open_raw and _ET is not None:
        try:
            _ph, _pm = post_open_raw.split(":")
            post_open_min = int(_ph) * 60 + int(_pm)
        except Exception:
            _log(f"AM_POST_OPEN_TIME='{post_open_raw}' invalid — want HH:MM (ET); post-open run off.")
    post_open_grace = _interval("AM_POST_OPEN_GRACE_MIN", 30)
    post_open_fired = None  # date the post-open run last fired

    _log(
        "auto_push started — "
        + ", ".join(f"{t[0]} every {t[2]}s" for t in targets)
        + ". Press Ctrl+C to stop."
    )
    status: dict = {}
    try:
        while True:
            now = time.time()
            ran_any = False
            _reload_intervals(targets)

            # Daily post-open forced run (e.g. 9:40 ET). Fires once per trading day
            # within the catch window. _run_window() is active only on trading days
            # 9:00–4:10 ET, so it covers weekends/holidays for free.
            if post_open_min is not None:
                try:
                    et = datetime.now(_ET)
                    active, _why = _amr_post._run_window()
                    et_min = et.hour * 60 + et.minute
                    if (active and post_open_min <= et_min < post_open_min + post_open_grace
                            and post_open_fired != et.date()):
                        _run(f"am_report (post-open {post_open_raw} ET)",
                             lambda: _amr_post.main(force=True))
                        post_open_fired = et.date()
                        # Don't let the 30-min interval double-fire right after this.
                        for _t in targets:
                            if _t[0] == "am_report":
                                _t[3] = time.time() + _t[2]
                except Exception as exc:
                    _log(f"post-open: error — {exc}")

            for t in targets:
                label, fn, interval, next_run = t
                if now >= next_run:
                    elapsed, result = _run(label, fn)
                    ran_any = True
                    if elapsed > interval:
                        _log(
                            f"{label}: warning — took {elapsed:.0f}s, longer than its "
                            f"{interval}s interval; this target is slipping behind"
                        )
                    # The ladder refresh picks its own next cadence (tighter on a hot
                    # tape); honor it. Everything else uses its fixed interval.
                    if label == "am_ladder" and isinstance(result, int) and result > 0:
                        used = result
                    else:
                        used = interval
                    t[3] = time.time() + used  # schedule the next run
                    status[label] = {"lastAt": _iso(time.time()), "nextAt": _iso(t[3]),
                                     "intervalSec": used}
            if ran_any:
                _write_status(status)
            time.sleep(TICK_SECONDS)
    except KeyboardInterrupt:
        _log("Stopped.")


if __name__ == "__main__":
    main()
