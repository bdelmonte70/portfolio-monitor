"""Condition alerts: surface facts about the book. Never a buy/sell recommendation.

Each check compares today's stored valuation against the most recent prior
run so severity-"high" alerts (target/stop) fire once on the crossing, not
every day the condition remains true. Informational/warn alerts (near-target,
IV move, new strikes/expiries, a Claude news flag) are level-triggered --
they fire whenever the condition holds today, since re-seeing them each run
is a feature, not spam.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import db as db_mod
from .diff import ChainDiffEntry
from .models import OptionPosition, Position
from .news_analysis import NewsAnalysis
from .valuation import Valuation

SEVERITY_ORDER = {"high": 0, "warn": 1, "info": 2}


@dataclass
class Alert:
    alert_type: str
    severity: str  # "high" | "warn" | "info"
    ticker: Optional[str]
    position_id: Optional[str]
    message: str
    detail: Optional[dict] = None

    def to_row(self, asof_str: str, run_timestamp: str) -> dict:
        return {
            "asof_date": asof_str, "run_timestamp": run_timestamp, "alert_type": self.alert_type,
            "severity": self.severity, "ticker": self.ticker, "position_id": self.position_id,
            "message": self.message, "detail": json.dumps(self.detail) if self.detail is not None else None,
        }


def _position_label(pos: Position) -> str:
    if isinstance(pos, OptionPosition):
        return f"{pos.ticker} {pos.option_type} ${pos.strike:g} {pos.expiry}"
    return f"{pos.ticker} shares"


def check_target_stop_alerts(positions: list[Position], valuations: list[Valuation], conn: sqlite3.Connection,
                              asof_str: str, target_near_pct: float = 0.80) -> list[Alert]:
    val_by_id = {v.position_id: v for v in valuations}
    alerts: list[Alert] = []

    for pos in positions:
        v = val_by_id.get(pos.id)
        if v is None:
            continue
        prior = db_mod.get_prior_valuation(conn, pos.id, asof_str)
        label = _position_label(pos)

        if v.progress_to_target_pct is not None:
            prior_pct = prior["progress_to_target_pct"] if prior else None
            was_hit = prior_pct is not None and prior_pct >= 100.0
            now_hit = v.progress_to_target_pct >= 100.0
            if now_hit and not was_hit:
                alerts.append(Alert("target_hit", "high", pos.ticker, pos.id,
                                     f"{label} hit your target level (mark {v.mark:g} vs target {pos.target_price:g})"))
            elif not now_hit and v.progress_to_target_pct >= target_near_pct * 100.0:
                alerts.append(Alert("target_near", "info", pos.ticker, pos.id,
                                     f"{label} is {v.progress_to_target_pct:.0f}% of the way to your target"))

        if v.progress_to_stop_pct is not None:
            prior_pct = prior["progress_to_stop_pct"] if prior else None
            was_hit = prior_pct is not None and prior_pct >= 100.0
            now_hit = v.progress_to_stop_pct >= 100.0
            if now_hit and not was_hit:
                alerts.append(Alert("stop_hit", "high", pos.ticker, pos.id,
                                     f"{label} hit your stop level (mark {v.mark:g} vs stop {pos.stop_price:g})"))

    return alerts


def check_iv_change_alerts(positions: list[Position], valuations: list[Valuation], conn: sqlite3.Connection,
                            asof_str: str, iv_change_pct: float = 0.20) -> list[Alert]:
    val_by_id = {v.position_id: v for v in valuations}
    alerts: list[Alert] = []

    for pos in positions:
        if not isinstance(pos, OptionPosition):
            continue
        v = val_by_id.get(pos.id)
        if v is None or v.iv is None:
            continue
        prior = db_mod.get_prior_valuation(conn, pos.id, asof_str)
        if not prior or not prior["iv"]:
            continue
        rel_change = (v.iv - prior["iv"]) / prior["iv"]
        if abs(rel_change) >= iv_change_pct:
            direction = "up" if rel_change > 0 else "down"
            alerts.append(Alert("iv_change", "warn", pos.ticker, pos.id,
                                 f"{_position_label(pos)} IV moved {direction} {abs(rel_change) * 100:.0f}% "
                                 f"({prior['iv'] * 100:.1f}% -> {v.iv * 100:.1f}%)"))
    return alerts


def check_diff_alerts(diff_entries: list[ChainDiffEntry]) -> list[Alert]:
    alerts: list[Alert] = []
    strikes_by_group: dict[tuple, list[float]] = {}

    for e in diff_entries:
        if e.kind == "new_expiry":
            alerts.append(Alert("new_expiry", "info", e.ticker, None,
                                 f"New expiry listed for {e.ticker}: {e.expiry}"))
        else:
            strikes_by_group.setdefault((e.ticker, e.expiry, e.option_type), []).append(e.strike)

    for (ticker, expiry, option_type), strikes in strikes_by_group.items():
        strikes_str = ", ".join(f"${s:g}" for s in sorted(strikes))
        alerts.append(Alert("new_strikes", "info", ticker, None,
                             f"New {option_type} strikes near your position in {ticker} {expiry}: {strikes_str}"))
    return alerts


def check_news_flag_alerts(news_results: list[NewsAnalysis]) -> list[Alert]:
    return [Alert("news_flag", "warn", r.ticker, None, r.position_flag)
            for r in news_results if r.position_flag]


def collect_alerts(positions: list[Position], valuations: list[Valuation], conn: sqlite3.Connection,
                    asof_str: str, diff_entries: list[ChainDiffEntry], news_results: list[NewsAnalysis],
                    target_near_pct: float = 0.80, iv_change_pct: float = 0.20) -> list[Alert]:
    alerts: list[Alert] = []
    alerts += check_target_stop_alerts(positions, valuations, conn, asof_str, target_near_pct)
    alerts += check_iv_change_alerts(positions, valuations, conn, asof_str, iv_change_pct)
    alerts += check_diff_alerts(diff_entries)
    alerts += check_news_flag_alerts(news_results)
    return sorted(alerts, key=lambda a: SEVERITY_ORDER.get(a.severity, 3))


def store_alerts(conn: sqlite3.Connection, asof_str: str, run_timestamp: str, alerts: list[Alert]) -> None:
    db_mod.insert_alerts(conn, asof_str, [a.to_row(asof_str, run_timestamp) for a in alerts])


def print_alerts(alerts: list[Alert]) -> None:
    print("\nAlerts:")
    if not alerts:
        print("  none")
        return
    for a in alerts:
        print(f"  [{a.severity.upper():<4}] {a.alert_type:<12} {a.message}")
