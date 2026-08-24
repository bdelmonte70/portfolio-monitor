"""Position models and positions.json loading/validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, Union


class PositionError(ValueError):
    """Raised when a row in positions.json is malformed."""


def _parse_date(value: str, field_name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise PositionError(f"{field_name!r} must be YYYY-MM-DD, got {value!r}") from exc


def _fmt_strike(strike: float) -> str:
    # 180 -> "180", 182.5 -> "182.5"
    return f"{strike:g}"


@dataclass
class OptionPosition:
    id: str
    ticker: str
    option_type: str  # "call" | "put"
    strike: float
    expiry: str  # YYYY-MM-DD
    entry_price: float  # per-share premium
    contracts: int
    entry_date: str
    target_price: Optional[float] = None  # per-share premium
    stop_price: Optional[float] = None  # per-share premium
    asset_type: str = field(default="option", init=False)

    @property
    def expiry_date(self) -> date:
        return _parse_date(self.expiry, "expiry")

    @property
    def entry_date_date(self) -> date:
        return _parse_date(self.entry_date, "entry_date")

    @classmethod
    def from_dict(cls, row: dict) -> "OptionPosition":
        try:
            ticker = str(row["ticker"]).upper()
            option_type = str(row["option_type"]).lower()
            if option_type not in ("call", "put"):
                raise PositionError(f"option_type must be 'call' or 'put', got {option_type!r}")
            strike = float(row["strike"])
            expiry = str(row["expiry"])
            _parse_date(expiry, "expiry")
            entry_price = float(row["entry_price"])
            contracts = int(row["contracts"])
            entry_date = str(row["entry_date"])
            _parse_date(entry_date, "entry_date")
        except KeyError as exc:
            raise PositionError(f"option row missing required field {exc}") from exc

        row_id = row.get("id")
        if not row_id:
            side = "C" if option_type == "call" else "P"
            row_id = f"{ticker}_{_fmt_strike(strike)}{side}_{expiry}"

        return cls(
            id=str(row_id),
            ticker=ticker,
            option_type=option_type,
            strike=strike,
            expiry=expiry,
            entry_price=entry_price,
            contracts=contracts,
            entry_date=entry_date,
            target_price=(float(row["target_price"]) if row.get("target_price") is not None else None),
            stop_price=(float(row["stop_price"]) if row.get("stop_price") is not None else None),
        )


@dataclass
class SharePosition:
    id: str
    ticker: str
    entry_price: float  # per share
    contracts: float  # = shares
    entry_date: str
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    asset_type: str = field(default="shares", init=False)

    @property
    def entry_date_date(self) -> date:
        return _parse_date(self.entry_date, "entry_date")

    @classmethod
    def from_dict(cls, row: dict) -> "SharePosition":
        try:
            ticker = str(row["ticker"]).upper()
            entry_price = float(row["entry_price"])
            contracts = float(row["contracts"])
            entry_date = str(row["entry_date"])
            _parse_date(entry_date, "entry_date")
        except KeyError as exc:
            raise PositionError(f"shares row missing required field {exc}") from exc

        row_id = row.get("id") or f"{ticker}_SHARES_{entry_date}"

        return cls(
            id=str(row_id),
            ticker=ticker,
            entry_price=entry_price,
            contracts=contracts,
            entry_date=entry_date,
            target_price=(float(row["target_price"]) if row.get("target_price") is not None else None),
            stop_price=(float(row["stop_price"]) if row.get("stop_price") is not None else None),
        )


Position = Union[OptionPosition, SharePosition]


def load_positions(path: str) -> list[Position]:
    with open(path) as f:
        rows = json.load(f)

    if not isinstance(rows, list):
        raise PositionError("positions.json must contain a JSON array of rows")

    positions: list[Position] = []
    seen_ids: set[str] = set()
    for i, row in enumerate(rows):
        asset_type = str(row.get("asset_type", "")).lower()
        if asset_type == "option":
            pos: Position = OptionPosition.from_dict(row)
        elif asset_type == "shares":
            pos = SharePosition.from_dict(row)
        else:
            raise PositionError(f"row {i}: asset_type must be 'option' or 'shares', got {row.get('asset_type')!r}")

        if pos.id in seen_ids:
            raise PositionError(f"duplicate position id {pos.id!r}")
        seen_ids.add(pos.id)
        positions.append(pos)

    return positions
