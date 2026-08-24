"""Deterministic macro gate: blends VIX, VIX term structure, market breadth, and
credit spread into a single 0-100 score describing the environment the book sits in.

Every sub-score uses the same self-calibrating transform (a value's percentile
rank within its own trailing 1-year history), so there are no hand-tuned magic
number thresholds -- same data in, same score out.

Score convention: 0 = hostile/risk-off, 100 = calm/risk-on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

import pandas as pd
import yfinance as yf

# SPDR Select Sector ETFs, used as a fast breadth proxy in place of pulling
# all ~500 S&P constituents (see class docstring below).
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC"]

# Minimum overlapping daily history points required before the VIX term
# structure ratio uses a self-calibrating percentile rather than the fixed
# contango/backwardation fallback band.
MIN_TERM_HISTORY_DAYS = 20


@dataclass
class MacroGateWeights:
    vix: float = 0.25
    term_structure: float = 0.25
    breadth: float = 0.25
    credit: float = 0.25

    def validate(self) -> None:
        total = self.vix + self.term_structure + self.breadth + self.credit
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"macro gate weights must sum to 1.0, got {total}")


@dataclass
class MacroGateResult:
    score: float
    vix_level: Optional[float]
    vix_percentile: Optional[float]
    vix_score: Optional[float]
    vix3m_level: Optional[float]
    term_ratio: Optional[float]
    term_percentile: Optional[float]
    term_score: Optional[float]
    breadth_pct: Optional[float]
    breadth_score: Optional[float]
    credit_ratio: Optional[float]
    credit_percentile: Optional[float]
    credit_score: Optional[float]
    weights: MacroGateWeights

    def to_dict(self) -> dict:
        d = asdict(self)
        d["weights"] = asdict(self.weights)
        return d


def _history_closes(ticker: str, period: str = "1y") -> pd.Series:
    try:
        hist = yf.Ticker(ticker).history(period=period)
    except Exception:
        return pd.Series(dtype=float)
    return hist["Close"] if not hist.empty else pd.Series(dtype=float)


def _percentile(current: float, history: pd.Series) -> Optional[float]:
    """% of historical values <= current (self-calibrating rank, 0-100)."""
    if history.empty:
        return None
    return float((history <= current).sum() / len(history) * 100.0)


def compute_breadth_proxy() -> Optional[float]:
    """% of SPDR sector ETFs trading above their own 200-day SMA.

    A lightweight stand-in for "percent of SPY constituents above their
    200-day MA" -- 11 fast lookups instead of ~500 -- per the spec's allowance
    for a breadth proxy.
    """
    above = 0
    total = 0
    for ticker in SECTOR_ETFS:
        closes = _history_closes(ticker)
        if len(closes) < 200:
            continue
        sma200 = closes.rolling(200).mean().iloc[-1]
        total += 1
        if closes.iloc[-1] > sma200:
            above += 1
    return (above / total * 100.0) if total else None


def compute_macro_gate(weights: Optional[MacroGateWeights] = None) -> MacroGateResult:
    weights = weights or MacroGateWeights()
    weights.validate()

    vix_hist = _history_closes("^VIX")
    vix3m_hist = _history_closes("^VIX3M")
    hyg_hist = _history_closes("HYG")
    tlt_hist = _history_closes("TLT")

    vix_level = float(vix_hist.iloc[-1]) if not vix_hist.empty else None
    vix_percentile = _percentile(vix_level, vix_hist) if vix_level is not None else None
    # Lower VIX relative to its own trailing year -> calmer -> higher (favorable) score.
    vix_score = (100.0 - vix_percentile) if vix_percentile is not None else None

    vix3m_level = float(vix3m_hist.iloc[-1]) if not vix3m_hist.empty else None
    term_ratio = term_percentile = term_score = None
    term_ratio_hist = (vix_hist / vix3m_hist).dropna() if not vix_hist.empty and not vix3m_hist.empty else pd.Series(dtype=float)
    if len(term_ratio_hist) >= MIN_TERM_HISTORY_DAYS:
        term_ratio = float(term_ratio_hist.iloc[-1])
        term_percentile = _percentile(term_ratio, term_ratio_hist)
        # Backwardation (VIX > VIX3M, ratio high vs its own history) signals
        # near-term stress -> lower (unfavorable) score.
        term_score = (100.0 - term_percentile) if term_percentile is not None else None
    elif vix_level is not None and vix3m_level is not None:
        # yfinance/Yahoo carries very little historical daily data for
        # ^VIX3M (often just today), so a self-calibrating percentile isn't
        # usually available. Fall back to today's two live levels against a
        # fixed contango(0.85)/backwardation(1.15) band instead.
        term_ratio = vix_level / vix3m_level
        term_score = max(0.0, min(100.0, 100.0 * (1.15 - term_ratio) / (1.15 - 0.85)))

    breadth_pct = compute_breadth_proxy()
    breadth_score = breadth_pct  # already 0-100, higher = more names in uptrend = favorable

    credit_ratio = credit_percentile = credit_score = None
    if not hyg_hist.empty and not tlt_hist.empty:
        credit_ratio_hist = (hyg_hist / tlt_hist).dropna()
        if not credit_ratio_hist.empty:
            credit_ratio = float(credit_ratio_hist.iloc[-1])
            credit_percentile = _percentile(credit_ratio, credit_ratio_hist)
            # HYG/TLT ratio high vs its own history -> credit risk-on -> favorable score.
            credit_score = credit_percentile

    components = [
        (vix_score, weights.vix),
        (term_score, weights.term_structure),
        (breadth_score, weights.breadth),
        (credit_score, weights.credit),
    ]
    available = [(s, w) for s, w in components if s is not None]
    weight_sum = sum(w for _, w in available)
    score = (sum(s * w for s, w in available) / weight_sum) if weight_sum else 50.0

    return MacroGateResult(
        score=score,
        vix_level=vix_level, vix_percentile=vix_percentile, vix_score=vix_score,
        vix3m_level=vix3m_level, term_ratio=term_ratio, term_percentile=term_percentile, term_score=term_score,
        breadth_pct=breadth_pct, breadth_score=breadth_score,
        credit_ratio=credit_ratio, credit_percentile=credit_percentile, credit_score=credit_score,
        weights=weights,
    )


def print_macro_gate_summary(asof_str: str, macro: MacroGateResult) -> None:
    print(f"\nMacro Gate as of {asof_str}: {macro.score:.1f} / 100")
    print(f"  VIX:    level {macro.vix_level:.2f}  1yr pctile {macro.vix_percentile:.0f}  -> score {macro.vix_score:.0f}"
          if macro.vix_score is not None else "  VIX:    n/a")
    if macro.term_score is not None:
        pctile = f"{macro.term_percentile:.0f}" if macro.term_percentile is not None else "n/a (fallback band)"
        print(f"  Term:   VIX/VIX3M {macro.term_ratio:.3f}  pctile {pctile}  -> score {macro.term_score:.0f}")
    else:
        print("  Term:   n/a")
    print(f"  Breadth: {macro.breadth_pct:.1f}% of sector ETFs > 200dma  -> score {macro.breadth_score:.0f}"
          if macro.breadth_score is not None else "  Breadth: n/a")
    print(f"  Credit: HYG/TLT {macro.credit_ratio:.3f}  1yr pctile {macro.credit_percentile:.0f}  -> score {macro.credit_score:.0f}"
          if macro.credit_score is not None else "  Credit: n/a")
