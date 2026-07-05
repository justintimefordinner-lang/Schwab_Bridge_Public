"""Diagnostic: what non-option (equity) activity is in trade-history.json?

Run from the Schwab folder with the venv active:
    python inspect_equities.py
Paste the output back so we can see why closed stocks came out empty.
"""
import collections
import json
import os

from dotenv import load_dotenv

import closed_trades as ct

load_dotenv()
data_dir = ct._data_dir()
path = os.path.join(data_dir, ct.HISTORY_FILE)
with open(path, encoding="utf-8") as f:
    store = json.load(f)

asset_counts = collections.Counter()
nonopt_instr = collections.Counter()
samples = []
total_records = 0

for acct, records in store.items():
    total_records += len(records)
    for o in records:
        for leg in o.get("legs", []):
            at = (leg.get("assetType") or "").upper() or "(blank)"
            asset_counts[at] += 1
            if at not in ("OPTION",):
                instr = (leg.get("instruction") or "").upper() or "(blank)"
                nonopt_instr[(at, instr)] += 1
                if len(samples) < 3:
                    samples.append({
                        "order": {k: o.get(k) for k in ("orderId", "enteredTime", "status", "orderType", "symbol")},
                        "leg": leg,
                    })

print(f"Accounts: {len(store)} | Total records: {total_records}")
print(f"Leg count by assetType: {dict(asset_counts)}")
print("Non-option (assetType, instruction) counts:")
for k, v in nonopt_instr.most_common():
    print(f"    {k}: {v}")
print("\nUp to 3 sample non-option legs:")
print(json.dumps(samples, indent=2)[:2000])
