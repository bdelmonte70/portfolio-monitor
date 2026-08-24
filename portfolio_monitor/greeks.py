"""Black-Scholes pricing and greeks, computed with only the stdlib `math` module."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class Greeks:
    delta: Optional[float]
    gamma: Optional[float]
    theta: Optional[float]  # per calendar day
    vega: Optional[float]  # per 1 vol point (1.00 -> 0.01 change in IV)


_EMPTY = Greeks(delta=None, gamma=None, theta=None, vega=None)


def compute_greeks(
    spot: float,
    strike: float,
    days_to_expiry: float,
    risk_free_rate: float,
    iv: float,
    option_type: str,
) -> Greeks:
    """Black-Scholes greeks for a single European-style option contract.

    spot: current underlying price.
    strike: option strike.
    days_to_expiry: actual calendar days remaining (>0).
    risk_free_rate: annualized, e.g. 0.045 for 4.5%.
    iv: annualized implied volatility as a decimal, e.g. 0.30 for 30%.
    option_type: "call" or "put".
    """
    if spot is None or iv is None or iv <= 0 or days_to_expiry is None or days_to_expiry <= 0 or spot <= 0 or strike <= 0:
        return _EMPTY

    t = days_to_expiry / 365.0
    sigma = iv
    r = risk_free_rate

    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t

    pdf_d1 = _norm_pdf(d1)
    is_call = option_type == "call"

    if is_call:
        delta = _norm_cdf(d1)
    else:
        delta = _norm_cdf(d1) - 1.0

    gamma = pdf_d1 / (spot * sigma * sqrt_t)

    vega = spot * pdf_d1 * sqrt_t / 100.0  # per 1 vol point

    discounted_k = strike * math.exp(-r * t)
    if is_call:
        theta_annual = (-spot * pdf_d1 * sigma / (2 * sqrt_t)) - r * discounted_k * _norm_cdf(d2)
    else:
        theta_annual = (-spot * pdf_d1 * sigma / (2 * sqrt_t)) + r * discounted_k * _norm_cdf(-d2)
    theta = theta_annual / 365.0  # per calendar day

    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega)


def intrinsic_value(spot: float, strike: float, option_type: str) -> float:
    return max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)


def bs_price(
    spot: float,
    strike: float,
    days_to_expiry: float,
    risk_free_rate: float,
    iv: float,
    option_type: str,
) -> Optional[float]:
    """Black-Scholes theoretical price (per share). At/after expiry, returns intrinsic value."""
    if spot is None or iv is None or iv <= 0 or spot <= 0 or strike <= 0:
        return None
    if days_to_expiry is None or days_to_expiry <= 0:
        return intrinsic_value(spot, strike, option_type)

    t = days_to_expiry / 365.0
    sigma = iv
    r = risk_free_rate

    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    discounted_k = strike * math.exp(-r * t)

    if option_type == "call":
        return spot * _norm_cdf(d1) - discounted_k * _norm_cdf(d2)
    return discounted_k * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
