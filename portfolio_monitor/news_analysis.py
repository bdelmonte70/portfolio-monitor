"""Daily Claude-powered news analysis per held underlying.

Pulls recent headlines via yfinance, sends them to Claude for a structured
summary/sentiment/driver/position-flag read, and caches the result per
ticker per day so re-running the same day never re-bills the API.

This is explicitly NOT a trade signal: Claude is asked to summarize and flag
facts, never to recommend buying, selling, holding, or rolling a position.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import yfinance as yf

from .models import OptionPosition, Position

DEFAULT_MODEL = "claude-sonnet-5"

NEWS_TOOL = {
    "name": "report_news_analysis",
    "description": "Report a structured analysis of recent headlines for one held ticker.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-4 sentence factual summary of what actually happened, based only on the provided headlines.",
            },
            "sentiment": {
                "type": "string",
                "enum": ["positive", "neutral", "negative"],
                "description": "Overall sentiment of the news for this name.",
            },
            "key_drivers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short bullet phrases naming the main drivers behind the news/sentiment.",
            },
            "position_flag": {
                "type": "string",
                "description": (
                    "If any headline specifically and concretely affects the held position(s) "
                    "described (e.g. an earnings date, a catalyst near the strike/expiry, a "
                    "guidance change, a downgrade/upgrade), describe it in one sentence. "
                    "Otherwise an empty string. Informational only -- never a buy/sell/hold/roll "
                    "recommendation."
                ),
            },
        },
        "required": ["summary", "sentiment", "key_drivers", "position_flag"],
    },
}

SYSTEM_PROMPT = (
    "You help a trader monitor news for positions they already hold. You will be given "
    "recent headlines for one ticker and a description of the position(s) currently held "
    "in it. Summarize only what the headlines actually say -- no speculation, no invented "
    "facts. Then flag anything that concretely affects the held position(s). "
    "You are NOT a trading advisor: never recommend buying, selling, holding, or rolling. "
    "Report your analysis using the report_news_analysis tool."
)


@dataclass
class Headline:
    title: str
    summary: str
    published: str  # ISO 8601
    publisher: str
    url: str


@dataclass
class NewsAnalysis:
    ticker: str
    asof_date: str
    window_days: int
    headline_count: int
    summary: Optional[str]
    sentiment: Optional[str]
    key_drivers: list[str]
    position_flag: Optional[str]
    model: Optional[str]
    cached: bool

    def to_dict(self) -> dict:
        return asdict(self)


def get_recent_headlines(ticker: str, asof_date: date, window_days: int = 3) -> list[Headline]:
    try:
        raw = yf.Ticker(ticker).news
    except Exception as exc:
        print(f"warning: could not fetch news for {ticker}: {exc}", file=sys.stderr)
        return []

    cutoff = datetime.combine(asof_date, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=window_days)
    headlines = []
    for item in raw or []:
        content = item.get("content", item)  # tolerate older/newer yfinance news shapes
        pub_date_str = content.get("pubDate") or content.get("providerPublishTime")
        if not pub_date_str:
            continue
        try:
            pub_dt = datetime.fromisoformat(str(pub_date_str).replace("Z", "+00:00"))
        except ValueError:
            continue
        if pub_dt < cutoff:
            continue

        title = content.get("title", "")
        summary = content.get("summary") or content.get("description") or ""
        publisher = (content.get("provider") or {}).get("displayName", "")
        url = (content.get("canonicalUrl") or {}).get("url", "")
        if title:
            headlines.append(Headline(title=title, summary=summary, published=pub_dt.isoformat(),
                                       publisher=publisher, url=url))

    return headlines


def _describe_positions(positions: list[Position], ticker: str) -> str:
    lines = []
    for p in positions:
        if p.ticker != ticker:
            continue
        if isinstance(p, OptionPosition):
            lines.append(f"- {p.contracts} contract(s) {p.option_type} @ strike ${p.strike:g}, "
                         f"expiry {p.expiry}, entry premium ${p.entry_price:g}/share")
        else:
            lines.append(f"- {p.contracts:g} shares, entry ${p.entry_price:g}/share")
    return "\n".join(lines) if lines else "(no matching position found)"


def _build_user_message(ticker: str, positions: list[Position], headlines: list[Headline]) -> str:
    position_desc = _describe_positions(positions, ticker)
    headline_lines = []
    for h in headlines:
        headline_lines.append(f"- [{h.published}] ({h.publisher}) {h.title}\n  {h.summary}".rstrip())
    headlines_block = "\n".join(headline_lines) if headline_lines else "(no recent headlines)"

    return (
        f"Ticker: {ticker}\n\n"
        f"Currently held position(s) in {ticker}:\n{position_desc}\n\n"
        f"Recent headlines:\n{headlines_block}"
    )


def _call_claude(client, model: str, ticker: str, positions: list[Position],
                  headlines: list[Headline]) -> dict:
    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[NEWS_TOOL],
        tool_choice={"type": "tool", "name": "report_news_analysis"},
        messages=[{"role": "user", "content": _build_user_message(ticker, positions, headlines)}],
    )
    for block in message.content:
        if block.type == "tool_use" and block.name == "report_news_analysis":
            return block.input
    raise RuntimeError(f"Claude did not return a report_news_analysis tool call for {ticker}")


def get_cached_analysis(conn: sqlite3.Connection, ticker: str, asof_str: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT window_days, headline_count, summary, sentiment, key_drivers, position_flag, model "
        "FROM news_analysis WHERE ticker = ? AND asof_date = ?",
        (ticker, asof_str),
    ).fetchone()
    if row is None:
        return None
    return {
        "window_days": row[0], "headline_count": row[1], "summary": row[2], "sentiment": row[3],
        "key_drivers": json.loads(row[4]) if row[4] else [], "position_flag": row[5], "model": row[6],
    }


def _headlines_to_json(headlines: list[Headline]) -> str:
    return json.dumps([asdict(h) for h in headlines])


def analyze_ticker_news(client, model: str, ticker: str, positions: list[Position], conn: sqlite3.Connection,
                         asof_date: date, run_timestamp: str, window_days: int = 3,
                         force_refresh: bool = False) -> NewsAnalysis:
    asof_str = asof_date.isoformat()

    if not force_refresh:
        cached = get_cached_analysis(conn, ticker, asof_str)
        if cached is not None:
            return NewsAnalysis(ticker=ticker, asof_date=asof_str, cached=True, **cached)

    headlines = get_recent_headlines(ticker, asof_date, window_days)

    result = _call_claude(client, model, ticker, positions, headlines)
    key_drivers = list(result.get("key_drivers") or [])
    position_flag = result.get("position_flag") or None

    analysis = NewsAnalysis(
        ticker=ticker, asof_date=asof_str, window_days=window_days, headline_count=len(headlines),
        summary=result.get("summary"), sentiment=result.get("sentiment"), key_drivers=key_drivers,
        position_flag=position_flag, model=model, cached=False,
    )

    conn.execute(
        """
        INSERT INTO news_analysis (ticker, asof_date, run_timestamp, window_days, headline_count,
                                    summary, sentiment, key_drivers, position_flag, model, headlines_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, asof_date) DO UPDATE SET
            run_timestamp=excluded.run_timestamp, window_days=excluded.window_days,
            headline_count=excluded.headline_count, summary=excluded.summary, sentiment=excluded.sentiment,
            key_drivers=excluded.key_drivers, position_flag=excluded.position_flag, model=excluded.model,
            headlines_json=excluded.headlines_json
        """,
        (ticker, asof_str, run_timestamp, window_days, len(headlines), analysis.summary, analysis.sentiment,
         json.dumps(key_drivers), position_flag, model, _headlines_to_json(headlines)),
    )
    conn.commit()

    return analysis


def run_news_analysis(positions: list[Position], conn: sqlite3.Connection, asof_date: date, run_timestamp: str,
                       model: str = DEFAULT_MODEL, window_days: int = 3,
                       force_refresh: bool = False) -> list[NewsAnalysis]:
    tickers = sorted({p.ticker for p in positions})

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("warning: ANTHROPIC_API_KEY is not set -- skipping news analysis. "
              "Set it in your environment to enable this layer.", file=sys.stderr)
        return []

    import anthropic
    client = anthropic.Anthropic()

    results = []
    for ticker in tickers:
        try:
            analysis = analyze_ticker_news(client, model, ticker, positions, conn, asof_date, run_timestamp,
                                            window_days, force_refresh)
            results.append(analysis)
        except Exception as exc:
            print(f"warning: news analysis failed for {ticker}: {exc}", file=sys.stderr)

    return results


def print_news_summary(results: list[NewsAnalysis]) -> None:
    print("\nNews Analysis:")
    if not results:
        print("  none")
        return
    for r in results:
        cache_note = " (cached)" if r.cached else ""
        print(f"\n  {r.ticker}{cache_note}  [{r.headline_count} headlines, {r.window_days}d window, sentiment: {r.sentiment}]")
        print(f"    {r.summary}")
        if r.key_drivers:
            print(f"    Drivers: {'; '.join(r.key_drivers)}")
        if r.position_flag:
            print(f"    Position flag: {r.position_flag}")
