"""Push notifications via ntfy and/or Pushover.

Both are fire-and-forget: a notification failure must never take down a hunt or
throw away a booking we already hold.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import Secrets

log = logging.getLogger("jamesiv.notify")

PRIORITY_LOW = "low"
PRIORITY_DEFAULT = "default"
PRIORITY_HIGH = "high"
PRIORITY_URGENT = "urgent"

_NTFY_PRIORITY = {
    PRIORITY_LOW: "2",
    PRIORITY_DEFAULT: "3",
    PRIORITY_HIGH: "4",
    PRIORITY_URGENT: "5",
}
_PUSHOVER_PRIORITY = {
    PRIORITY_LOW: "-1",
    PRIORITY_DEFAULT: "0",
    PRIORITY_HIGH: "1",
    PRIORITY_URGENT: "1",
}


class Notifier:
    def __init__(self, secrets: Secrets, *, timeout: float = 8.0):
        self.secrets = secrets
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def enabled(self) -> bool:
        return self.secrets.has_notifier

    async def send(
        self,
        title: str,
        message: str,
        *,
        priority: str = PRIORITY_DEFAULT,
        url: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        if not self.enabled:
            log.info("NOTIFY (no channel configured) %s -- %s", title, message)
            return

        jobs = []
        if self.secrets.ntfy_topic:
            jobs.append(self._ntfy(title, message, priority, url, tags))
        if self.secrets.pushover_token and self.secrets.pushover_user:
            jobs.append(self._pushover(title, message, priority, url))

        results = await asyncio.gather(*jobs, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                log.warning("Notification failed: %s", result)

    async def _ntfy(
        self, title: str, message: str, priority: str, url: str | None, tags: list[str] | None
    ) -> None:
        # HTTP headers are latin-1; a venue named "Café ..." must degrade
        # gracefully in the title rather than crash the notification.
        safe_title = title.encode("latin-1", "replace").decode("latin-1")
        headers = {
            "Title": safe_title,
            "Priority": _NTFY_PRIORITY.get(priority, "3"),
        }
        if tags:
            headers["Tags"] = ",".join(tags)
        if url:
            headers["Click"] = url
        if self.secrets.ntfy_token:
            headers["Authorization"] = f"Bearer {self.secrets.ntfy_token}"

        endpoint = f"{self.secrets.ntfy_server.rstrip('/')}/{self.secrets.ntfy_topic}"
        resp = await self._client.post(endpoint, content=message.encode("utf-8"), headers=headers)
        resp.raise_for_status()

    async def _pushover(self, title: str, message: str, priority: str, url: str | None) -> None:
        data = {
            "token": self.secrets.pushover_token,
            "user": self.secrets.pushover_user,
            "title": title,
            "message": message,
            "priority": _PUSHOVER_PRIORITY.get(priority, "0"),
        }
        if url:
            data["url"] = url
            data["url_title"] = "Open in Resy"
        resp = await self._client.post("https://api.pushover.net/1/messages.json", data=data)
        resp.raise_for_status()


def resy_url(slug: str, *, day: str, party_size: int, location: str = "ny") -> str:
    """Deep link straight to the venue on the right date and party size."""
    return f"https://resy.com/cities/{location}/{slug}?date={day}&seats={party_size}"
