"""Diagnostic (REDACTED OUTPUT): dump the *shape* of Schwab transaction records
so we can build a parser, with zero sensitive data in the output.

It keeps field names and enum-type values (type, assetType, instruction,
positionEffect, putCall, status) but replaces account numbers, dollar amounts,
quantities, real tickers, and dates with placeholders. Symbols are mapped to
SYM1/SYM2/... consistently within the run (the mapping is never printed).

Run from the Schwab folder with the venv active and network/creds available:
    python inspect_transactions.py
Paste the whole output back -- it will contain no real figures or identifiers.
"""
import datetime
import json
import re
from collections import Counter

from dotenv import load_dotenv

import schwab_client as sc

# Values we keep as-is because they're non-sensitive enums and we need them.
KEEP_STR_KEYS = {
    "type", "activitytype", "assettype", "status", "instruction",
    "positioneffect", "putcall", "feetype", "transactiontype", "subaccount",
    "amountindicator", "side", "settlementtype", "ordertype",
}
SYMBOL_KEYS = {"symbol", "underlyingsymbol", "ticker", "cusip"}

_symbol_map = {}


def _pseudo(sym):
    if sym not in _symbol_map:
        _symbol_map[sym] = "SYM{}".format(len(_symbol_map) + 1)
    return _symbol_map[sym]


def redact(obj, key=None):
    kl = (key or "").lower()
    if isinstance(obj, dict):
        return {k: redact(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, key) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        # keep only the sign so we can tell received vs delivered, debit vs credit
        return "<pos>" if obj > 0 else "<neg>" if obj < 0 else "<zero>"
    if isinstance(obj, str):
        if kl in SYMBOL_KEYS:
            return _pseudo(obj)
        if kl in KEEP_STR_KEYS:
            return obj
        if re.match(r"^\d{4}-\d{2}-\d{2}", obj):  # date/time -> keep format, hide value
            return re.sub(r"\d", "#", obj)
        # default: redact anything we didn't explicitly whitelist
        return "<redacted>"
    return obj


load_dotenv()
c = sc.get_client()
accounts = sc.list_accounts(c)
if not accounts:
    raise SystemExit("No linked Schwab accounts found.")
aid = accounts[0]["hash"]

start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=58)
resp = c.get_transactions(aid, start_date=start)
resp.raise_for_status()
txns = resp.json()


def jstr(t):
    return json.dumps(t)


print("Transactions in last 58 days: {}".format(len(txns)))
if txns:
    print("Top-level keys of first record: {}".format(sorted(txns[0].keys())))
print("Count by 'type':", dict(Counter(t.get("type") for t in txns)))
print("Count by 'activityType':", dict(Counter(t.get("activityType") for t in txns)))

eq = [redact(t) for t in txns if "EQUITY" in jstr(t)][:3]
rd = [redact(t) for t in txns if "RECEIVE_AND_DELIVER" in jstr(t).upper()][:2]

print("\n========== up to 3 EQUITY-involving transactions (redacted) ==========")
print(json.dumps(eq, indent=2)[:4500])
print("\n========== up to 2 RECEIVE_AND_DELIVER transactions (redacted) ==========")
print(json.dumps(rd, indent=2)[:4500])
