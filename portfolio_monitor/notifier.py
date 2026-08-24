"""Push the day's alerts to Telegram or iMessage. Off by default -- a no-op unless
explicitly enabled, and a no-op whenever there's nothing to send.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

import requests

SEVERITY_EMOJI = {"high": "\U0001F534", "warn": "\U0001F7E1", "info": "\U0001F535"}
TELEGRAM_MAX_CHARS = 4000


@dataclass
class NotifierConfig:
    telegram: bool = False
    imessage: bool = False
    imessage_recipient: Optional[str] = None


def _format_digest(alerts, asof_str: str) -> str:
    lines = [f"Portfolio monitor -- {asof_str}", f"{len(alerts)} alert(s)", ""]
    for a in alerts:
        emoji = SEVERITY_EMOJI.get(a.severity, "")
        lines.append(f"{emoji} [{a.severity.upper()}] {a.message}")
    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_CHARS:
        text = text[:TELEGRAM_MAX_CHARS] + "\n... (truncated)"
    return text


def _send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("warning: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set -- skipping Telegram notification",
              file=sys.stderr)
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"warning: Telegram notification failed: {exc}", file=sys.stderr)


def _applescript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _send_imessage(text: str, recipient: str) -> None:
    if sys.platform != "darwin":
        print("warning: iMessage notification requested but not running on macOS -- skipping", file=sys.stderr)
        return
    script = (
        'tell application "Messages"\n'
        '  set targetService to 1st service whose service type = iMessage\n'
        f'  set targetBuddy to buddy "{_applescript_escape(recipient)}" of targetService\n'
        f'  send "{_applescript_escape(text)}" to targetBuddy\n'
        "end tell"
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        print(f"warning: iMessage notification failed: {exc.stderr}", file=sys.stderr)


def maybe_send(alerts, asof_str: str, cfg: NotifierConfig) -> None:
    """No-op unless cfg.telegram or cfg.imessage is enabled and there's at least one alert."""
    if not alerts or not (cfg.telegram or cfg.imessage):
        return

    text = _format_digest(alerts, asof_str)

    if cfg.telegram:
        _send_telegram(text)
    if cfg.imessage:
        if not cfg.imessage_recipient:
            print("warning: iMessage notification enabled but no recipient configured -- skipping", file=sys.stderr)
        else:
            _send_imessage(text, cfg.imessage_recipient)
