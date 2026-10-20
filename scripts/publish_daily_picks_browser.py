#!/usr/bin/env python3
"""Publish local human-verified receipts, or run explicit browser diagnostics.

Publication requires --source-checks from a real local browser review, including
fresh interior-photo receipts. That path never launches Chromium or exports
candidates. The legacy server browser can inspect basic facts only in --dry-run
or --open-browser diagnostics; it cannot create visual proof or publish.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import publish_daily_picks as publisher
from bot.core.daily_picks import snapshot_status

MAX_CHECKS = 90
DEFAULT_BUDGET_SECONDS = 30 * 60
VISIT_INTERVAL_SECONDS = 1.5
BLOCKED_STATUSES = {403, 429, 468}
_ID = re.compile(r"[0-9]{5,20}\Z")
_TITLE_ID = re.compile(r"(?:№|объявление\s*(?:№)?)\s*([0-9]{5,20})\b", re.I)
_CHALLENGE = re.compile(
    r"checking your browser|verify (?:that )?you are (?:a )?human|"
    r"(?:подтвердите|докажите)[^.\n]{0,80}(?:человек|не робот)|"
    r"вы\s+не\s+робот|провер(?:ка|ьте)[^.\n]{0,60}(?:браузер|робот)|"
    r"(?:доступ (?:запрещ[её]н|ограничен)|access denied|too many requests)|"
    r"(?:введите|пройдите|решите)[^.\n]{0,60}(?:капч|captcha)", re.I,
)

# Read only rendered DOM, never hidden app state, cookies, storage, scripts or
# internal source APIs. Raw rendered text remains in memory for classification;
# never persist or log it, contacts, descriptions, or addresses.
READ_OFFER_DOM = r"""() => {
  const visible = element => {
    if (!element) return false;
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      element.getClientRects().length > 0;
  };
  const firstVisible = (root, selector) =>
    [...root.querySelectorAll(selector)].find(visible);
  const offer = firstVisible(document, '.offer') || firstVisible(document, 'main');
  const title = offer && firstVisible(offer, 'h1, .offer__title');
  const price = offer && firstVisible(offer, '.offer__price');
  const sourceComplexNames = offer ? [...offer.querySelectorAll(
    '.offer__info-item[data-name="map.complex"] a[href*="/complex/show/"]'
  )].filter(visible).map(element => element.innerText.replace(/\s+/g, ' ').trim()).filter(Boolean) : [];
  const uniqueComplexNames = [...new Set(sourceComplexNames)];
  const authorLabels = [];
  const measurements = {};
  const measurementValues = {kitchen_area: [], ceiling_height: []};
  if (offer) {
    const visibleFactRows = [...offer.querySelectorAll(
      '.offer__info-item, .offer__advert-short-info, .offer__parameters dl, .offer__parameters dt, .offer__seller-type, .owner-label'
    )].filter(visible);
    [...offer.querySelectorAll('*')].filter(element => visible(element) && element.children.length === 0 &&
      /^(?:Автор объявления|Площадь кухни|Высота потолков)\s*[:—–-]?$/i.test(element.innerText.trim()) &&
      !element.closest('.offer__description, .offer__text, .offer__description-text')
    ).forEach(label => {
      let row = label.parentElement;
      for (let depth = 0; row && row !== offer && depth < 3; depth++, row = row.parentElement) {
        if (visible(row) && row.innerText.length <= 500) visibleFactRows.push(row);
      }
    });
    visibleFactRows.forEach(element => {
      let text = element.innerText.replace(/\s+/g, ' ').trim();
      if (element.tagName === 'DT' && /^(?:Автор объявления|Площадь кухни|Высота потолков)\s*[:—–-]?$/i.test(text)) {
        const value = element.nextElementSibling;
        if (value && value.tagName === 'DD' && visible(value)) text += ' ' + value.innerText.replace(/\s+/g, ' ').trim();
      }
      const match = text.match(/^Автор объявления\s*[:—–-]?\s*(Хозяин недвижимости|Риелтор|Агентство недвижимости|Застройщик)(?=\s|$|[.,;:])/i);
      if (match) authorLabels.push(match[1]);
      const kitchen = text.match(/^(?:Площадь\s*[:—–-]?\s*\d+(?:[.,]\d+)?\s*м(?:²|2)\s*,\s*)?Площадь кухни\s*[:—–-]?\s*(\d+(?:[.,]\d+)?)\s*м(?:²|2)(?=\s|$|[.,;:])/i);
      const ceiling = text.match(/^Высота потолков\s*[:—–-]?\s*(\d+(?:[.,]\d+)?)\s*м(?:етра|етров)?(?=\s|$|[.,;:])/i);
      if (kitchen) measurementValues.kitchen_area.push(Number(kitchen[1].replace(',', '.')));
      if (ceiling) measurementValues.ceiling_height.push(Number(ceiling[1].replace(',', '.')));
    });
    Object.entries(measurementValues).forEach(([key, values]) => {
      const unique = [...new Set(values)].filter(Number.isFinite);
      if (unique.length === 1) measurements[key] = unique[0];
    });
  }
  const pledged = offer && [...offer.querySelectorAll('.offer__parameters-mortgaged')]
    .filter(visible).some(element => /^В залоге$/i.test(element.innerText.replace(/\s+/g, ' ').trim()));
  const challenge = [...document.querySelectorAll(
    'iframe[src*="captcha"], iframe[src*="challenge"], .g-recaptcha, .h-captcha, [data-sitekey]'
  )].some(visible);
  return {
    page_url: location.href,
    document_title: document.title,
    identity_urls: [...document.querySelectorAll('link[rel="canonical"], meta[property="og:url"]')]
      .map(element => element.getAttribute(element.tagName === 'LINK' ? 'href' : 'content')),
    main_visible: !!offer,
    author_labels: [...new Set(authorLabels)],
    source_complex_name: uniqueComplexNames.length === 1 ? uniqueComplexNames[0] : null,
    source_complex_name_conflict: uniqueComplexNames.length > 1,
    is_pledged: pledged ? true : null,
    ...measurements,
    title: title ? title.innerText.slice(0, 1000) : '',
    price_text: price ? price.innerText.slice(0, 300) : '',
    main_text: offer ? offer.innerText.slice(0, 100000) : '',
    body_text: document.body ? document.body.innerText.slice(0, 200000) : '',
    captcha_visible: challenge,
    breadcrumbs: [...document.querySelectorAll(
      '[itemtype*="BreadcrumbList"] a, .breadcrumbs a, .bread-crumbs a'
    )].filter(visible).map(element => element.getAttribute('href'))
  };
}"""


class BrowserChallengeError(RuntimeError):
    pass


class BrowserBudgetExceeded(TimeoutError):
    pass


class InsufficientConfirmedCardsError(RuntimeError):
    pass


class VisualReceiptHandoffRequired(ValueError):
    pass


VISUAL_HANDOFF_INSTRUCTION = (
    "Publication requires fresh local human browser visual receipts. Use "
    "venv/bin/python scripts/publish_daily_picks.py --quality-policy discounted_four_factors "
    "--source-checks PRIVATE_PATH, or pass --source-checks PRIVATE_PATH to this wrapper. "
    "Legacy server-browser basic checks cannot approve interior photos."
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def atomic_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def worker_lock(run_root: Path):
    """Separate outer lock includes candidate export and browser verification."""
    import fcntl
    run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (run_root / "worker.lock").open("a") as handle:
        os.chmod(handle.name, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def validate_candidates(payload: dict, limit: int) -> list[dict]:
    if (not isinstance(payload, dict) or payload.get("kind") != "unverified_review_candidates"
            or not isinstance(payload.get("items"), list) or len(payload["items"]) > MAX_CHECKS):
        raise ValueError("Invalid browser candidate export")
    seen = set()
    for item in payload["items"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid candidate")
        lid = item.get("id")
        if (not isinstance(lid, str) or not _ID.fullmatch(lid) or lid in seen
                or item.get("source_url") != f"https://krisha.kz/a/show/{lid}"):
            raise ValueError("Invalid candidate source identity")
        seen.add(lid)
        for field in ("price", "rooms", "area", "floor", "floors_total"):
            value = item.get(field)
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid candidate number")
    return payload["items"][:limit]


def is_challenged(observation: dict) -> bool:
    return observation.get("captcha_visible") is True or bool(_CHALLENGE.search(
        str(observation.get("document_title", "")) + "\n" + str(observation.get("body_text", ""))))


def sale_astana(breadcrumbs: list) -> bool:
    category = city = False
    for value in breadcrumbs:
        if not isinstance(value, str):
            continue
        try:
            parsed = urlsplit(value)
            if parsed.hostname and parsed.hostname not in {"krisha.kz", "www.krisha.kz"}:
                continue
            if parsed.scheme and parsed.scheme != "https":
                continue
            path = parsed.path.rstrip("/")
            if path == "/prodazha/kvartiry/astana":
                return True
            category |= path == "/prodazha/kvartiry"
            city |= path == "/catalog/astana"
        except ValueError:
            continue
    return category and city


def source_record(item: dict, observation: dict, checked_at: datetime) -> dict:
    """Only observed, identified main-offer facts can confirm a candidate."""
    result = {"id": item["id"], "source_url": item["source_url"], "status": "unknown",
              "checked_at": checked_at.isoformat()}
    urls = observation.get("identity_urls")
    title_ids = _TITLE_ID.findall(str(observation.get("document_title", "")))
    if (observation.get("page_url", "").rstrip("/") != item["source_url"]
            or not isinstance(urls, list) or not urls
            or any(not isinstance(url, str) or url.rstrip("/") != item["source_url"] for url in urls)
            or not title_ids or any(lid != item["id"] for lid in title_ids)
            or observation.get("main_visible") is not True):
        return result
    main_text = str(observation.get("main_text", ""))
    if re.search(r"в архиве|может быть неактуальным|объявление (?:не найдено|удалено)", main_text, re.I):
        result["status"] = "archived"
        return result
    title, price_text = str(observation.get("title", "")), str(observation.get("price_text", ""))
    rooms = re.search(r"(\d+)[-\s]*комнат", title, re.I)
    area = re.search(r"(\d+(?:[.,]\d+)?)\s*м(?:²|2)", title, re.I)
    floor = re.search(r"(\d+)\s*/\s*(\d+)\s*этаж", title, re.I)
    prices = re.findall(r"(\d[\d\s\u00a0\u202f]*)\s*(?:₸|тг\b|тенге\b)", price_text, re.I)
    if ("квартир" not in title.casefold() or not rooms or not area or not floor or len(prices) != 1
            or re.search(r"\bот\b|за\s*м(?:²|2)|/\s*м(?:²|2)|в месяц", price_text, re.I)
            or not sale_astana(observation.get("breadcrumbs") or [])):
        return result
    values = {"sale_astana": True, "price": int(re.sub(r"\D", "", prices[0])),
              "rooms": int(rooms[1]), "area": float(area[1].replace(",", ".")),
              "floor": int(floor[1]), "floors_total": int(floor[2])}
    if (values["price"] <= 0 or not 1 <= values["floor"] <= values["floors_total"]
            or values["rooms"] <= 0 or values["area"] <= 0):
        return result
    # Missing DOM observation is unknown, not an explicit absence of a ЖК.
    if ("source_complex_name" not in observation or observation.get("source_complex_name_conflict") is True
            or observation["source_complex_name"] is not None and (
                not isinstance(observation["source_complex_name"], str)
                or not observation["source_complex_name"].strip()
                or len(observation["source_complex_name"]) > 200)):
        return result
    result["source_complex_name"] = observation["source_complex_name"]
    result.update(values)
    result["status"] = "active" if (values["price"] == item["price"]
        and values["rooms"] == item["rooms"] and abs(values["area"] - item["area"]) <= .2
        and values["floor"] == item["floor"] and values["floors_total"] == item["floors_total"]) else "mismatch"
    labels = observation.get("author_labels")
    if isinstance(labels, list) and labels and all(isinstance(label, str) for label in labels):
        roles = {" ".join(label.casefold().split()) for label in labels}
        if roles == {"хозяин недвижимости"}:
            result["is_owner"] = True
        elif len(roles) == 1 and roles <= {"риелтор", "агентство недвижимости", "застройщик"}:
            result["is_owner"] = False
    if observation.get("is_pledged") is True:
        result["is_pledged"] = True
    result.update(publisher.browser_measurements(observation, values["area"]))
    return result


@asynccontextmanager
async def ordinary_browser(profile: Path, *, headless: bool = True):
    from playwright.async_api import async_playwright
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), channel="chromium", headless=headless, chromium_sandbox=True)
        try:
            yield context
        finally:
            await context.close()


async def check_sources(items: list[dict], evidence_path: Path, deadline: float, profile: Path, *, headed: bool = False) -> dict:
    """One serial browser pass; blocked navigation never advances to next URL."""
    evidence = {"schema_version": 1, "method": "normal_browser", "generated_at": utc_now().isoformat(),
                "completed": False, "attempted_count": 0, "items": []}
    atomic_private_json(evidence_path, evidence)
    try:
        async with ordinary_browser(profile, headless=not headed) as context:
            page = await context.new_page()
            for index, item in enumerate(items):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrowserBudgetExceeded()
                if index:
                    await asyncio.sleep(min(VISIT_INTERVAL_SECONDS, remaining))
                if time.monotonic() >= deadline:
                    raise BrowserBudgetExceeded()
                evidence["attempted_count"] += 1
                blocked = []
                def observe_response(response):
                    if (response.status in BLOCKED_STATUSES and response.request.is_navigation_request()
                            and response.frame == page.main_frame):
                        blocked.append(response.status)
                page.on("response", observe_response)
                try:
                    response = await page.goto(item["source_url"], wait_until="domcontentloaded",
                        timeout=max(1, min(25000, int((deadline - time.monotonic()) * 1000))))
                    if blocked or (response is not None and response.status in BLOCKED_STATUSES):
                        raise publisher.SourceBlockedError(blocked[0] if blocked else response.status)
                    # Challenges can arrive as 404/503 as well as 200. They stop
                    # the complete run before any ordinary status handling.
                    observation = await page.evaluate(READ_OFFER_DOM)
                    if is_challenged(observation):
                        raise BrowserChallengeError()
                    if response is not None and response.status in {404, 410}:
                        record = {"id": item["id"], "source_url": item["source_url"], "status": "deleted",
                                  "checked_at": utc_now().isoformat()}
                    elif response is None or response.status != 200:
                        record = {"id": item["id"], "source_url": item["source_url"], "status": "unknown",
                                  "checked_at": utc_now().isoformat()}
                    else:
                        if not observation.get("title") or not observation.get("price_text"):
                            try:
                                await page.locator(".offer__price").first.wait_for(state="visible",
                                    timeout=max(1, min(8000, int((deadline - time.monotonic()) * 1000))))
                            except Exception as error:
                                # A missing offer is unknown, never a source confirmation.
                                if type(error).__name__ != "TimeoutError":
                                    raise
                            observation = await page.evaluate(READ_OFFER_DOM)
                            if is_challenged(observation):
                                raise BrowserChallengeError()
                        if blocked:
                            raise publisher.SourceBlockedError(blocked[0])
                        record = source_record(item, observation, utc_now())
                except Exception as error:
                    if blocked:
                        raise publisher.SourceBlockedError(blocked[0]) from error
                    # Navigation timeout/network failure is unknown; do not retry.
                    if type(error).__name__ not in {"TimeoutError"} and not (
                        type(error).__name__ == "Error" and "net::" in str(error)):
                        raise
                    record = {"id": item["id"], "source_url": item["source_url"], "status": "unknown",
                              "checked_at": utc_now().isoformat()}
                finally:
                    page.remove_listener("response", observe_response)
                evidence["items"].append(record)
                atomic_private_json(evidence_path, evidence)
        evidence["completed"] = True
    except BaseException as error:
        evidence["error_type"] = type(error).__name__
        if isinstance(error, publisher.SourceBlockedError):
            evidence["blocked_http_status"] = error.status
        raise
    finally:
        evidence["finished_at"] = utc_now().isoformat()
        atomic_private_json(evidence_path, evidence)
    return evidence


def already_published(path: Path, now: datetime, quality_policy: str = publisher.DEFAULT_QUALITY_POLICY) -> bool:
    existing = publisher.load_daily_picks(path)
    return bool(existing and publisher.publication_satisfies_policy(existing, quality_policy)
        and existing["selection_date"] == now.astimezone(publisher.LOCAL_TIMEZONE).date().isoformat()
        and not snapshot_status(existing, now)["is_stale"])


async def pipeline(args, run_dir: Path, status: dict, snapshot: Path) -> dict:
    handoff = getattr(args, "source_checks", None)
    if not handoff:
        if not args.dry_run:
            raise VisualReceiptHandoffRequired(VISUAL_HANDOFF_INSTRUCTION)
        # Explicit legacy diagnostics only: no source_record from this browser
        # path is ever passed to the publisher as human visual approval.
        deadline = time.monotonic() + args.budget_seconds
        status["phase"] = "prepare"
        atomic_private_json(run_dir / "status.json", status)
        exported = await publisher.export_candidate_pool(str(run_dir / "candidates.json"), utc_now())
        items = validate_candidates(exported, args.check_limit)
        status.update(phase="check", candidate_count=len(items), input_count=exported["input_count"])
        atomic_private_json(run_dir / "status.json", status)
        if not items:
            raise InsufficientConfirmedCardsError()
        evidence = await check_sources(items, run_dir / "source-checks.json", deadline, args.profile_path, headed=args.headed)
        status.update(evidence_counts(evidence))
        return {**status, "status": "dry_run", "phase": "complete", "published": False}
    evidence_path = Path(handoff)
    # Validate the private file before any database/image work. generate reads
    # it again inside the publication lock and enforces all current gates.
    checks = publisher.load_source_checks(str(evidence_path), utc_now())
    status.update(evidence_counts({"items": list(checks.values())}))
    status["phase"] = "publish"
    atomic_private_json(run_dir / "status.json", status)
    with publisher.publisher_lock(snapshot):
        quality_policy = getattr(args, "quality_policy", publisher.DEFAULT_QUALITY_POLICY)
        if not args.force and not args.dry_run and already_published(snapshot, utc_now(), quality_policy):
            return {**status, "status": "already_published", "phase": "complete", "published": False}
        # A required source-checks argument makes ordinary source GET fallback impossible.
        result = await publisher.generate(SimpleNamespace(skip_photo_check=False, source_checks=str(evidence_path)), utc_now())
        count = len(result["snapshot"]["items"])
        if not publisher.MINIMUM_PICKS <= count <= publisher.MAX_PICKS or quality_policy == "daily_30" and count != publisher.MAX_PICKS:
            status["eligible_count"] = len(result["snapshot"]["items"])
            raise InsufficientConfirmedCardsError()
        result["snapshot"] = publisher.prepare_publication(result["snapshot"], quality_policy)
        if args.dry_run:
            return {**status, "status": "dry_run", "phase": "complete", "published": False,
                    "count": count, "photo_heads": result["photo_heads"], "source_gets": result["source_gets"]}
        publisher.publish_snapshot(result["snapshot"], snapshot)
        # Preserve the publication boundary even if subsequent diagnostics fail.
        status.update(published=True, count=len(result["snapshot"]["items"]),
                      selection_date=result["snapshot"]["selection_date"], quality_policy=quality_policy,
                      publication_policy=result["snapshot"].get("publication_policy"))
        return {**status, "status": "published", "phase": "complete", "published": True,
                "count": len(result["snapshot"]["items"]), "photo_heads": result["photo_heads"],
                "source_gets": result["source_gets"], "selection_date": result["snapshot"]["selection_date"]}


def dedicated_profile(value: str | None, snapshot: Path) -> Path:
    parent = (snapshot.parent / "browser-picks").resolve()
    profile = Path(value).resolve() if value else parent / "profile"
    if not profile.is_relative_to(parent) or profile == parent:
        raise ValueError("A dedicated profile below var/browser-picks is required")
    return profile


def evidence_counts(evidence: dict) -> dict:
    items = evidence.get("items") or []
    return {"source_checks": len(items), "attempted_count": evidence.get("attempted_count", len(items)),
            "confirmed_count": sum(item["status"] == "active" for item in items),
            "mismatch_count": sum(item["status"] == "mismatch" for item in items),
            "unknown_count": sum(item["status"] == "unknown" for item in items)}


def validated_source_url(value: str) -> str:
    match = re.fullmatch(r"https://krisha\.kz/a/show/([0-9]{5,20})", value or "")
    if not match:
        raise ValueError("A canonical Krisha apartment URL is required")
    return value


async def open_manual_browser(args, status_path: Path) -> dict:
    """Owner-operated window: no automatic CAPTCHA action and no publisher."""
    status = {"mode": "manual_browser", "status": "opening", "started_at": utc_now().isoformat(),
              "published": False}
    atomic_private_json(status_path, status)
    deadline = time.monotonic() + args.budget_seconds
    async with ordinary_browser(args.profile_path, headless=False) as context:
        closed = asyncio.Event()
        context.on("close", lambda *_: closed.set())
        page = await context.new_page()
        response = await page.goto(args.source_url, wait_until="domcontentloaded", timeout=25000)
        observation = await page.evaluate(READ_OFFER_DOM)
        blocked_status = response.status if response is not None and response.status in BLOCKED_STATUSES else None
        manual_needed = blocked_status is not None or is_challenged(observation)
        status.update(status="manual_confirmation_required" if manual_needed else "browser_open",
                      opened_at=utc_now().isoformat())
        if blocked_status is not None:
            status["blocked_http_status"] = blocked_status
        atomic_private_json(status_path, status)
        print(json.dumps(status, ensure_ascii=False), flush=True)
        try:
            await asyncio.wait_for(closed.wait(), timeout=max(1, deadline - time.monotonic()))
            status["status"] = "browser_closed"
        except TimeoutError:
            status["status"] = "manual_session_expired"
    status["finished_at"] = utc_now().isoformat()
    atomic_private_json(status_path, status)
    return status


def failed_run(error: Exception, status: dict, run_dir: Path | None, run_root: Path,
               snapshot: Path, *, manual: bool, owns_lock: bool) -> int:
    published = status.get("published") is True
    status.update(status="published_with_diagnostics_error" if published else "failed",
                  published=published, previous_snapshot_retained=not published,
                  error_type=type(error).__name__, finished_at=utc_now().isoformat())
    if published:
        status["diagnostics_error_type"] = type(error).__name__
    if isinstance(error, publisher.SourceBlockedError):
        status["blocked_http_status"] = error.status
    if owns_lock and run_dir is not None:
        try:
            status.update(evidence_counts(json.loads((run_dir / "source-checks.json").read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        atomic_private_json(run_dir / "status.json", status)
        atomic_private_json(run_root / "last-run.json", status)
    elif owns_lock and manual:
        status["mode"] = "manual_browser"
        atomic_private_json(snapshot.parent / "browser-picks" / "manual-status.json", status)
    print(json.dumps(status, ensure_ascii=False), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Explicitly replace today's verified selection")
    parser.add_argument("--dry-run", action="store_true", help="Diagnostics only; with --source-checks validate the strict result without publishing")
    parser.add_argument("--source-checks", metavar="PRIVATE_PATH", help="Fresh human-verified local-browser receipts; no server Chromium or candidate export")
    parser.add_argument("--quality-policy", choices=(publisher.DEFAULT_QUALITY_POLICY, "daily_30"), default=publisher.DEFAULT_QUALITY_POLICY,
                        help="Default: 10-30 discounted four-factor cards; daily_30 additionally requires exactly 30")
    parser.add_argument("--check-limit", type=int, default=MAX_CHECKS, help="1-90 candidates; values below 90 are dry-run only")
    parser.add_argument("--budget-seconds", type=int, default=DEFAULT_BUDGET_SECONDS, help="Total bounded run budget, at most 1800 seconds")
    parser.add_argument("--profile-path", help="Dedicated profile below var/browser-picks (diagnostics only)")
    parser.add_argument("--open-browser", action="store_true", help="Open an owner-operated server desktop window; never publish")
    parser.add_argument("--headed", action="store_true", help="Use ordinary visible server Chromium for the daily job; no automatic mode switch")
    parser.add_argument("--source-url", help="One canonical Krisha URL for --open-browser; never fetched by a crawler")
    args = parser.parse_args(argv)
    if not 1 <= args.check_limit <= MAX_CHECKS or args.check_limit < MAX_CHECKS and not args.dry_run:
        parser.error("--check-limit must be 1-90; values below 90 require --dry-run")
    if not 1 <= args.budget_seconds <= DEFAULT_BUDGET_SECONDS:
        parser.error("--budget-seconds must be between 1 and 1800")
    if args.source_checks and (args.open_browser or args.source_url or args.headed or args.profile_path or args.check_limit != MAX_CHECKS):
        parser.error("--source-checks cannot be combined with browser, profile or check-limit options")
    if not args.source_checks and not args.dry_run and not args.open_browser:
        parser.error(VISUAL_HANDOFF_INSTRUCTION)
    snapshot = publisher.snapshot_path()
    try:
        if not args.source_checks:
            args.profile_path = dedicated_profile(args.profile_path or os.environ.get("HATULI_DAILY_PICKS_BROWSER_PROFILE"), snapshot)
        if args.open_browser:
            args.source_url = validated_source_url(args.source_url)
            if args.force or args.dry_run or args.check_limit != MAX_CHECKS or args.quality_policy != publisher.DEFAULT_QUALITY_POLICY:
                parser.error("--open-browser does not support publication or check-limit flags")
        elif args.source_url:
            parser.error("--source-url is only supported with --open-browser")
    except ValueError as error:
        parser.error(str(error))
    run_root = snapshot.parent / "daily-picks-browser"
    status = {"status": "running", "started_at": utc_now().isoformat(), "published": False, "quality_policy": args.quality_policy}
    run_dir = None
    try:
        with worker_lock(run_root):
            try:
                if args.open_browser:
                    # The deadline includes Chromium startup, which can hang
                    # before launch_persistent_context returns on a desktop.
                    status = asyncio.run(asyncio.wait_for(
                        open_manual_browser(args, snapshot.parent / "browser-picks" / "manual-status.json"),
                        timeout=args.budget_seconds))
                    print(json.dumps(status, ensure_ascii=False))
                    return 0
                if not args.force and not args.dry_run and already_published(snapshot, utc_now(), args.quality_policy):
                    status.update(status="already_published", phase="complete")
                else:
                    run_id = utc_now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
                    run_dir = run_root / run_id
                    run_dir.mkdir(mode=0o700)
                    status["run_id"] = run_id
                    status = asyncio.run(asyncio.wait_for(pipeline(args, run_dir, status, snapshot), timeout=args.budget_seconds))
                status["finished_at"] = utc_now().isoformat()
                if run_dir is not None:
                    atomic_private_json(run_dir / "status.json", status)
                atomic_private_json(run_root / "last-run.json", status)
            except Exception as error:
                return failed_run(error, status, run_dir, run_root, snapshot, manual=args.open_browser, owns_lock=True)
        print(json.dumps(status, ensure_ascii=False))
        return 0
    except Exception as error:
        # Failed/busy lock acquisition cannot overwrite another run's report.
        return failed_run(error, status, None, run_root, snapshot, manual=args.open_browser, owns_lock=False)


if __name__ == "__main__":
    raise SystemExit(main())
