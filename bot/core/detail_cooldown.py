"""Private, restart-safe source backoff. Contains no listing URLs or contacts.

Schema v1: {version:1, blocked_until:<aware UTC ISO>, streak:<int>}.
A lock and atomic replacement preserve the longest deadline across workers.
Unreadable/corrupt state fails closed; only an actual successful source request
may reset the streak, and never before an existing deadline has expired.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

MAX_SECONDS = 6 * 60 * 60
BASE_SECONDS = 15 * 60


def state_path() -> Path:
    return Path(os.environ.get("HATULI_DETAIL_COOLDOWN_PATH") or
                Path(__file__).resolve().parents[2] / "var" / "detail-source-cooldown.json")


@contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0); handle.write(b"0"); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, 0
    if not isinstance(data, dict) or type(data.get("version")) is not int or data.get("version") != 1:
        raise ValueError("Invalid detail cooldown state")
    until = datetime.fromisoformat(data["blocked_until"])
    streak = data["streak"]
    if until.tzinfo is None or type(streak) is not int or not 0 <= streak <= 64:
        raise ValueError("Invalid detail cooldown fields")
    return until.astimezone(timezone.utc), streak


def _write(path, until, streak):
    payload = json.dumps({"version": 1, "blocked_until": until.isoformat(), "streak": streak})
    fd, name = tempfile.mkstemp(prefix=".detail-cooldown-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_cooldown(*, now=None):
    now = now or datetime.now(timezone.utc)
    until, streak = _read(state_path())
    return max(0., (until - now).total_seconds()) if until else 0., streak


def record_block(*, now=None, minimum_streak=1):
    now = now or datetime.now(timezone.utc)
    path = state_path()
    with _locked(path):
        until, streak = _read(path)
        streak = min(64, max(streak + 1, int(minimum_streak)))
        delay = min(BASE_SECONDS * 2 ** min(streak - 1, 10), MAX_SECONDS)
        until = max(until or now, now + timedelta(seconds=delay))
        _write(path, until, streak)
    return max(0., (until - now).total_seconds()), streak


def record_success(*, now=None):
    now = now or datetime.now(timezone.utc)
    path = state_path()
    with _locked(path):
        until, _ = _read(path)
        if until is None or until <= now:
            _write(path, now, 0)
