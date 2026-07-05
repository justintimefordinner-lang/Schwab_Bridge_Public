"""
diagnose_board.py — why is the CSP Board empty?

Reads the am_report.json that am_report.py just wrote and summarizes why each
screened name made or missed the board. Pure file read — makes NO Schwab calls,
so it's safe to run anytime and tells us which gate is tripping.

Run:
    python diagnose_board.py
"""

from __future__ import annotations

import collections
import json
import os

from dotenv import load_dotenv

load_dotenv()


def _ascii(s: str) -> str:
    return s.encode("ascii", "replace").decode()  # Δ -> ? so Windows console won't choke


def main() -> None:
    data_dir = os.environ.get("APP_DATA_DIR")
    if not data_dir:
        raise SystemExit("APP_DATA_DIR not set in .env")
    path = os.path.join(data_dir, "am_report.json")
    with open(path, encoding="utf-8") as f:
        rep = json.load(f)

    meta = rep.get("meta", {})
    board = rep.get("board", [])
    steer = rep.get("steerClear", [])
    landmines = rep.get("landmines", [])

    print(f"board={len(board)}  steerClear={len(steer)}  landmines={len(landmines)}  "
          f"screened={meta.get('count')}")
    print(f"asOf={meta.get('asOf')}  source={meta.get('source')}  "
          f"marketOpen={meta.get('marketOpen')}  earnings={meta.get('earningsLoaded')}")
    print()

    reasons = collections.Counter(fa for r in steer for fa in r.get("fails", []))
    if reasons:
        print("Why names were gated off the board (count | reason):")
        for reason, n in reasons.most_common():
            print(f"  {n:3d} | {_ascii(reason)}")
    else:
        print("No gated names recorded (steerClear is empty).")
    print()

    print("Sample gated names:")
    for r in steer[:10]:
        print(f"  {r.get('sym', '?'):6s} {_ascii(', '.join(r.get('fails', [])))}")


if __name__ == "__main__":
    main()
