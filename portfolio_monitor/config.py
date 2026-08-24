"""Runtime configuration for the portfolio monitor."""

from dataclasses import dataclass


@dataclass
class Config:
    risk_free_rate: float = 0.045
    positions_path: str = "positions.json"
    db_path: str = "portfolio.db"
    snapshots_dir: str = "snapshots"
