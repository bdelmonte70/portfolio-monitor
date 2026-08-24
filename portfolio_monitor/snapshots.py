"""Persist the full pulled option chain per underlying: dated JSON file + SQLite mirror.

This is what later layers diff (today vs prior run) to detect new strikes,
new expiries, and IV history per contract.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

from . import db as db_mod


def save_chain_snapshot(chain: dict, ticker: str, asof_date: str, snapshots_dir: str,
                         conn: sqlite3.Connection) -> str:
    os.makedirs(snapshots_dir, exist_ok=True)
    run_timestamp = datetime.now().isoformat(timespec="seconds")

    payload = {
        "ticker": ticker,
        "asof_date": asof_date,
        "run_timestamp": run_timestamp,
        "chain": chain["expiries"],
    }
    path = os.path.join(snapshots_dir, f"{ticker}_{asof_date}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    flat_rows = []
    for expiry, sides in chain["expiries"].items():
        for option_type, key in (("call", "calls"), ("put", "puts")):
            for row in sides.get(key, []):
                if row.get("strike") is None:
                    continue
                flat_rows.append({
                    "expiry": expiry,
                    "option_type": option_type,
                    "strike": row["strike"],
                    "bid": row.get("bid"),
                    "ask": row.get("ask"),
                    "last": row.get("last"),
                    "iv": row.get("iv"),
                    "volume": row.get("volume"),
                    "open_interest": row.get("open_interest"),
                })

    if flat_rows:
        db_mod.upsert_chain_snapshot_rows(conn, ticker, asof_date, run_timestamp, flat_rows)

    return path
