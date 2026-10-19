"""Durable evidence of a sale-catalog traversal, independent of ranking.

Only source pages whose accepted rows were saved can advance the cursor.
Raw source IDs include unpriced/rejected cards; they measure enumeration,
not physical apartments, completed sales, or verified property attributes.
The existing app_settings table stores each journal atomically: no DDL.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

STATE_KEY = "DEEP_SWEEP_STATE_V1"
COMPLETED_KEY = "DEEP_SWEEP_COMPLETED_V1"
MAX_PAGE = 20_000
MAX_STATE_BYTES = 8_000_000
MAX_PRESENCE_IDS = 100_000


def utc(value):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return None
    return (result.astimezone(timezone.utc) if isinstance(result, datetime)
            and result.tzinfo is not None else None)


def positive(value):
    return value if type(value) is int and value > 0 else None


def signature(ids):
    return hashlib.sha256("\n".join(sorted(set(ids))).encode()).hexdigest()


@dataclass
class SaleSweep:
    started_at: str
    pages: dict = field(default_factory=dict)
    failures: dict = field(default_factory=dict)
    total_start: int | None = None
    total_latest: int | None = None
    expected_last_page: int | None = None
    terminal_page: int | None = None
    terminal_reason: str | None = None
    terminal_receipt: dict | None = None
    completed_at: str | None = None
    blocked_until: str | None = None
    requests: int = 0
    failed_attempts: int = 0
    recent_unsaved: dict = field(default_factory=dict)
    presence_overflow: bool = False
    # A lost current journal may have contained unsaved visible IDs. Keep
    # archival paused across restarts until a new complete source traversal
    # supplies replacement evidence; unlike overflow this is recoverable.
    archive_hold_reason: str | None = None
    archive_hold_started_at: str | None = None

    @classmethod
    def new(cls, now):
        return cls(now.astimezone(timezone.utc).isoformat())

    @classmethod
    def loads(cls, raw):
        if not raw or len(raw) > MAX_STATE_BYTES:
            return None
        try:
            data = json.loads(raw)
            if data.pop("version", None) != 1:
                return None
            result = cls(**data)
            if utc(result.started_at) is None or not isinstance(result.pages, dict) or not isinstance(result.failures, dict):
                return None
            for name in ("total_start", "total_latest", "expected_last_page", "terminal_page"):
                value = getattr(result, name)
                if value is not None and (type(value) is not int or value < 0):
                    return None
            if result.expected_last_page and result.expected_last_page > MAX_PAGE:
                return None
            if result.terminal_page is not None and result.terminal_page > MAX_PAGE:
                return None
            for number, page in result.pages.items():
                if not number.isdigit() or not 1 <= int(number) <= MAX_PAGE or not isinstance(page, dict):
                    return None
                if utc(page.get("checked_at")) is None:
                    return None
                for key in ("ids", "saved_ids"):
                    values = page.get(key)
                    if not isinstance(values, list) or len(values) > 200 or any(
                            not isinstance(i, str) or not i.isdigit() for i in values):
                        return None
                if not set(page["saved_ids"]) <= set(page["ids"]):
                    return None
                if page.get("signature") != signature(page["ids"]):
                    return None
            if any(not k.isdigit() or not 1 <= int(k) <= MAX_PAGE or not isinstance(v, dict)
                   for k, v in result.failures.items()):
                return None
            if any(type(getattr(result, key)) is not int or getattr(result, key) < 0
                   for key in ("requests", "failed_attempts")):
                return None
            if (not isinstance(result.recent_unsaved, dict) or len(result.recent_unsaved) > MAX_PRESENCE_IDS
                    or type(result.presence_overflow) is not bool or any(
                        not key.isdigit() or utc(value) is None for key, value in result.recent_unsaved.items())):
                return None
            if result.archive_hold_reason not in (None, "current_journal_unreadable"):
                return None
            if ((result.archive_hold_reason is None) != (result.archive_hold_started_at is None)
                    or (result.archive_hold_started_at is not None and utc(result.archive_hold_started_at) is None)
                    or (result.completed_at is not None and result.archive_hold_reason is not None)):
                return None
            if any(value is not None and utc(value) is None
                   for value in (result.completed_at, result.blocked_until)):
                return None
            if result.completed_at and (utc(result.completed_at) < utc(result.started_at) or any(
                    not utc(result.started_at) <= utc(page["checked_at"]) <= utc(result.completed_at)
                    for page in result.pages.values())):
                return None
            if result.terminal_reason is not None and result.terminal_reason not in (
                    "explicit_empty", "explicit_last_page", "observed_catalog_count"):
                return None
            if result.terminal_receipt is not None:
                terminal = result.terminal_receipt
                if not isinstance(terminal, dict) or utc(terminal.get("checked_at")) is None:
                    return None
                if any(not isinstance(terminal.get(key), list) or len(terminal[key]) > 200 or any(
                        not isinstance(i, str) or not i.isdigit() for i in terminal[key])
                       for key in ("ids", "saved_ids")):
                    return None
                if not set(terminal["saved_ids"]) <= set(terminal["ids"]):
                    return None
                if terminal.get("page") != result.terminal_page + (0 if terminal["ids"] else 1):
                    return None
                if result.completed_at and not utc(result.started_at) <= utc(terminal["checked_at"]) <= utc(result.completed_at):
                    return None
            if result.completed_at and not result.closed:
                return None
            return result
        except (ValueError, TypeError, AttributeError):
            return None

    def dumps(self):
        return json.dumps({"version": 1, **vars(self)}, separators=(",", ":"), sort_keys=True)

    @property
    def next_page(self):
        limit = self.terminal_page if self.terminal_page is not None else (self.expected_last_page or MAX_PAGE)
        return next((n for n in range(1, limit + 1) if str(n) not in self.pages), limit + 1)

    @property
    def closed(self):
        return (self.terminal_page is not None and bool(self.terminal_reason) and self.terminal_receipt is not None
                and all(str(n) in self.pages for n in range(1, self.terminal_page + 1))
                and not any(int(n) <= self.terminal_page for n in self.failures))

    @property
    def unique_ids(self):
        return {i for page in [*self.pages.values(), self.terminal_receipt or {"ids": []}] for i in page["ids"]}

    @property
    def saved_ids(self):
        return {i for page in [*self.pages.values(), self.terminal_receipt or {"saved_ids": []}] for i in page["saved_ids"]}

    @property
    def denominator(self):
        return max(self.total_start or 0, self.total_latest or 0)

    @property
    def coverage_pct(self):
        return round(min(100.0, 100 * len(self.unique_ids) / self.denominator), 2) if self.denominator else None

    def account(self, reports, saved_ids, *, now):
        """Acknowledge receipts only after all accepted IDs were persisted.

        Refreshes of already acknowledged head pages do not rewrite the
        circle's evidence. A failed deep page stays in the retry journal.
        """
        saved_ids = set(saved_ids)
        self.recent_unsaved = {i: checked for i, checked in self.recent_unsaved.items()
                               if utc(checked) >= now - timedelta(days=7)}
        known_signatures = {entry["signature"] for entry in self.pages.values()}
        for report in reports:
            number = report["page"]
            if type(number) is not int or not 1 <= number <= MAX_PAGE:
                raise ValueError("Invalid sale page number")
            key = str(number)
            self.requests += 1
            if report.get("blocked"):
                retry_after = report.get("retry_after_seconds")
                seconds = retry_after if isinstance(retry_after, (int, float)) and math.isfinite(retry_after) else 0
                try:
                    self.blocked_until = (now + timedelta(seconds=max(1800, seconds))).isoformat()
                except OverflowError:
                    self.blocked_until = datetime.max.replace(tzinfo=timezone.utc).isoformat()
            total = positive(report.get("total_count"))
            if total:
                if self.total_start is None and number == 1:
                    self.total_start = total
                self.total_latest = total
            expected = positive(report.get("expected_last_page"))
            if expected:
                self.expected_last_page = max(self.expected_last_page or 0, min(expected, MAX_PAGE))
                if self.terminal_page is not None and expected > self.terminal_page:
                    # A retry can span a growing catalog. A later head
                    # observation invalidates the former end boundary.
                    self.terminal_page = self.terminal_reason = None
                    self.terminal_receipt = self.completed_at = None
            accepted = set(report.get("accepted_ids", []))
            ids = report.get("ids", [])
            error = report.get("error")
            # Independent TTL presence survives refreshed page positions,
            # retry replacements and round boundaries. Never fabricate a
            # new price observation by touching listing.last_seen.
            for identifier in ids:
                if identifier in accepted & saved_ids and not error:
                    self.recent_unsaved.pop(identifier, None)
                elif identifier in self.recent_unsaved or len(self.recent_unsaved) < MAX_PRESENCE_IDS:
                    self.recent_unsaved[identifier] = report["checked_at"]
                else:
                    self.presence_overflow = True  # archiving fails closed
            if not error and (not accepted <= set(ids) or (not ids and not report.get("terminal_reason"))):
                error = "invalid_page_receipt"
            if not error and not accepted <= saved_ids:
                error = "incomplete_database_save"
            # Repeated nonterminal pages are not progress through a catalog.
            digest = signature(ids)
            if not error and key not in self.pages and ids and not report.get("terminal_reason") and digest in known_signatures:
                error = "repeated_page_without_terminal_proof"
            if error:
                if key in self.pages:
                    self.failed_attempts += 1
                    self.pages[key]["refresh_failures"] = self.pages[key].get("refresh_failures", 0) + 1
                    self.pages[key]["last_refresh_error"] = str(error)[:120]
                    continue
                previous = self.failures.get(key, {})
                self.failures[key] = {"attempts": previous.get("attempts", 0) + 1,
                                      "last_error": str(error)[:120], "checked_at": report["checked_at"],
                                      "ids": sorted(set(ids)), "missing_saved_ids": sorted(accepted - saved_ids)}
                self.failed_attempts += 1
                continue
            if key not in self.pages:
                self.pages[key] = {"ids": sorted(set(ids)), "saved_ids": sorted(accepted),
                                   "rejected": report.get("rejected", {}), "signature": digest,
                                   "checked_at": report["checked_at"],
                                   "retry_attempts": self.failures.get(key, {}).get("attempts", 0)}
            known_signatures.add(digest)
            self.failures.pop(key, None)
            if report.get("terminal_reason") and (ids or number > 1 or (report.get("total_count") == 0 and not self.unique_ids)):
                terminal = number if ids else number - 1
                # A genuinely empty first page proves an empty catalog, not
                # health of a previous nonempty market snapshot.
                self.terminal_page = terminal
                self.terminal_reason = report["terminal_reason"]
                self.terminal_receipt = {"page": number, "ids": sorted(set(ids)),
                                         "saved_ids": sorted(accepted), "checked_at": report["checked_at"]}
                if terminal == 0 and self.total_start is None:
                    self.total_start = self.total_latest = 0
        if self.closed:
            self.completed_at = now.isoformat()
            self.archive_hold_reason = self.archive_hold_started_at = None


def completed_evidence(settings):
    result = SaleSweep.loads(settings.get(COMPLETED_KEY))
    return result if result and result.completed_at and result.closed else None


def completion_coverage(settings):
    result = completed_evidence(settings)
    return result.coverage_pct if result else None


def recent_unsaved_source_ids(settings, *, cutoff, now):
    """Protect visible unpriced/failed-save IDs without faking price freshness.

    Updating listing.last_seen with an old price would create an invented
    price observation. Instead the disappearance consumer excludes IDs
    actually present in a recent recognized source page.
    """
    seen = set()
    for key in (STATE_KEY, COMPLETED_KEY):
        state = SaleSweep.loads(settings.get(key))
        if not state:
            continue
        seen.update(i for i, checked in state.recent_unsaved.items() if cutoff <= utc(checked) <= now)
        observations = [*state.pages.values(), *([state.terminal_receipt] if state.terminal_receipt else [])]
        for page in observations:
            checked = utc(page.get("checked_at"))
            if checked and cutoff <= checked <= now:
                seen.update(set(page["ids"]) - set(page["saved_ids"]))
        for failure in state.failures.values():
            checked = utc(failure.get("checked_at"))
            if checked and cutoff <= checked <= now:
                seen.update(failure.get("ids", []))
    return sorted(seen)


def acknowledged_prefix(reports, saved_ids, *, start_page):
    """Cursor/EOF for the manual sale sweep; no jump across parse/save errors."""
    saved_ids = set(saved_ids)
    cursor = start_page
    for report in reports:
        if report["page"] != cursor or report.get("error") or not set(report.get("accepted_ids", [])) <= saved_ids:
            return cursor, False
        if report.get("terminal_reason"):
            return cursor + 1, True
        if not report.get("ids"):
            return cursor, False
        cursor += 1
    return cursor, False


def plan_batch(*, base_batch, max_batch, pages_left, elapsed_min, period_min,
               target_hours=24.0, reference_batch=40, page_seconds=5.0, safety=.85):
    """Choose more pages per paced cycle, never more requests per second.

    The measured cycle includes a previous batch. Charge additional page
    time when increasing that batch rather than assuming its period stays
    constant. An infeasible deadline stays infeasible at the bounded cap.
    """
    def number(value, default, lo, hi):
        try:
            value = float(value)
        except (ValueError, TypeError):
            value = default
        return max(lo, min(hi, value)) if math.isfinite(value) else default
    base = int(number(base_batch, 0, 0, 160))
    cap = max(base, int(number(max_batch, 160, 0, 160)))
    period = number(period_min, 120, 1, 360)
    seconds = number(page_seconds, 5, 2, 60)
    reference = number(reference_batch, base, 0, 160)
    overhead = max(0, period - reference * seconds / 60)
    target = number(target_hours, 24, 1, 168)
    elapsed = number(elapsed_min, 0, 0, 1e7)
    remaining = target * 60 * number(safety, .85, .1, 1) - elapsed
    batch = base
    def eta(size):
        return math.ceil(pages_left / size) * (overhead + size * seconds / 60) if size and pages_left > 0 else None
    if base and pages_left > 0:
        batch = next((size for size in range(base, cap + 1) if eta(size) <= remaining), cap)
    eta_min = eta(batch)
    return {"batch": batch, "estimated_remaining_hours": round(eta_min / 60, 2) if eta_min is not None else None,
            "estimated_total_hours": round((elapsed + eta_min) / 60, 2) if eta_min is not None else None,
            "target_hours": target, "deadline_feasible": bool(eta_min is not None and elapsed + eta_min <= target * 60),
            "page_seconds": seconds, "reference_batch": reference,
            "pacing": "sequential_2_to_5_second_delay_plus_response", "market_coverage": "unknown"}
