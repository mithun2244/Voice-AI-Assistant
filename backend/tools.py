"""
Agent tools — currently the human-in-the-loop hand-off.

`transfer_to_author` lets the voice agent escalate to a real person: when it
can't answer a hiring manager's question and they accept the offer to be
connected, it pings the author on Discord with a link back to the live app.
"""

from __future__ import annotations

import logging
import os

import requests
from langchain_core.tools import tool

logger = logging.getLogger("voice-agent")

# Where to send the author so they can join the caller. Overridable via env.
APP_URL = os.getenv("APP_URL", "http://localhost:5173/")
_WEBHOOK_TIMEOUT = 10  # seconds


@tool
def transfer_to_author(reason: str = "") -> str:
    """Notify the author that a hiring manager wants to talk to them directly.

    Call this ONLY after the caller has said yes to being connected to the
    author, typically because their question could not be answered from the
    resume/GitHub context.

    Args:
        reason: A short summary of what the caller asked / why they want the
            author (e.g. "wants to discuss compensation and start date").

    Returns:
        A short, speakable confirmation to read back to the caller.
    """
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook:
        logger.warning("DISCORD_WEBHOOK_URL is not set — cannot notify the author.")
        return (
            "I wasn't able to reach the author automatically just now, but I've "
            "noted your request and they'll follow up."
        )

    content = (
        "🔔 **A hiring manager wants to connect!**\n"
        f"**They asked about:** {reason.strip() or '(no detail captured)'}\n"
        f"**Join them here:** {APP_URL}"
    )
    try:
        resp = requests.post(webhook, json={"content": content}, timeout=_WEBHOOK_TIMEOUT)
        resp.raise_for_status()
        logger.info("Notified author via Discord webhook (reason=%r)", reason)
    except Exception as exc:  # noqa: BLE001 — surface any webhook failure gracefully
        logger.error("Discord webhook failed: %s", exc)
        return (
            "I tried to reach the author but the notification didn't go through. "
            "Please leave your contact details and they'll get back to you."
        )

    return (
        "Great — I've pinged the author and shared this link with them, so they "
        "can jump in and talk with you directly."
    )
