"""Per-catalog LIST backoff, independent of shared detail-page backoff."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json

BASE_SECONDS = 30 * 60
MAX_SECONDS = 6 * 60 * 60


class RentalCatalogCooldown(RuntimeError):
    source_blocked = True
    source_cooldown = True

    def __init__(self, seconds):
        self.cooldown_remaining_seconds = max(0., float(seconds))
        super().__init__(f"Rental catalog paused for {self.cooldown_remaining_seconds:.0f}s")


def read_backoff(raw, *, now=None):
    if not raw:
        return None, 0.
    now = now or datetime.now(timezone.utc)
    data = json.loads(raw)
    if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
            or type(data.get("streak")) is not int or not 0 <= data["streak"] <= 64):
        raise ValueError("Invalid rental catalog backoff")
    until = datetime.fromisoformat(data["blocked_until"])
    if until.tzinfo is None:
        raise ValueError("Rental catalog backoff must have a timezone")
    return data, max(0., (until - now).total_seconds())


def blocked_state(raw, retry_after=None, *, now=None):
    now = now or datetime.now(timezone.utc)
    previous, _ = read_backoff(raw, now=now)
    streak = min(64, (previous or {}).get("streak", 0) + 1)
    until = now + timedelta(seconds=min(BASE_SECONDS * 2 ** min(streak-1, 10), MAX_SECONDS))
    if previous:
        until = max(until, datetime.fromisoformat(previous["blocked_until"]))
    if isinstance(retry_after, str):
        try:
            server_until = (now + timedelta(seconds=int(retry_after)) if retry_after.strip().isdigit()
                            else parsedate_to_datetime(retry_after))
            if server_until.tzinfo is not None:
                until = max(until, server_until)
        except (TypeError, ValueError, OverflowError):
            pass
    # A server deadline is never truncated to our exponential backoff cap.
    return json.dumps({"version":1,"streak":streak,"blocked_until":until.isoformat()},separators=(",",":"))


def require_catalog_ready(settings, key):
    try:
        previous, remaining = read_backoff(settings.get(key,""))
    except (ValueError, TypeError, KeyError):
        raise RentalCatalogCooldown(MAX_SECONDS)
    if remaining:
        raise RentalCatalogCooldown(remaining)
    return previous


async def remember_catalog_block(settings, key, exc):
    if getattr(exc,"source_cooldown",False):
        return
    response = getattr(exc,"response",None)
    status = getattr(response,"status_code",None)
    if status in (403,429,468) or getattr(exc,"source_blocked",False):
        retry_after = response.headers.get("retry-after") if response is not None else None
        await settings.set(key,blocked_state(settings.get(key,""),retry_after))
