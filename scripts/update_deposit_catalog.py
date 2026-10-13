#!/usr/bin/env python3
"""Refresh public deposit evidence. No DB. Ordinary HTTPS and bounded responses.

Rates update automatically; changed conditions, documents or uncertain parsing
disable the affected program. An owner can accept conditions only after review.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.core.deposit_comparison import CATALOG_PATH, SCHEMA_VERSION, public_deposit_catalog, load_deposit_catalog
from bot.core.deposit_sources import PRODUCT_SOURCES, INDEX_SOURCES, DOCUMENT_SOURCES, parse_source

MAX_BODY = 2_000_000


def discover_programs(url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    host = urlsplit(url).hostname
    result = set()
    for link in soup.find_all("a", href=True):
        target = urljoin(url, link["href"])
        parts = urlsplit(target)
        if (parts.scheme != "https" or parts.hostname != host or parts.netloc != host
                or parts.query or parts.fragment or parts.username or parts.password):
            continue
        path = parts.path.rstrip("/")
        if host == "bankffin.kz" and path.startswith("/ru/deposits/physical/"):
            result.add(target)
        elif host == "bank.forte.kz":
            nearby_text = link.get_text(" ", strip=True).lower()
            for container in (link.parent, link.parent.parent):
                if container is not None:
                    nearby_text += " " + " ".join(heading.get_text(" ", strip=True).lower()
                                                  for heading in container.find_all(["h2", "h3"]))
            if path != "/ru/deposits" and (path in ("/ru/so-snyatiem", "/ru/sberegatelnyi", "/ru/sber_noadd")
                    or any(word in nearby_text for word in ("депозит", "сберегательный вклад"))):
                result.add(target)
        elif host in ("berekebank.kz", "home.kz") and "/deposits/" in path:
            result.add(target)
    return sorted(result)


def build_catalog(previous: dict, fetched: dict, now: datetime, accept_terms: bool = False) -> dict:
    old_offers = {item["id"]: item for item in previous.get("offers", []) if isinstance(item, dict) and "id" in item}
    old_sources = {item["url"]: item for item in previous.get("sources", []) if isinstance(item, dict) and "url" in item}
    sources, offers, events = [], [], []
    document_flags: dict[str, str] = {}
    for url, product_url in DOCUMENT_SOURCES.items():
        response = fetched.get(url, {})
        content = response.get("content", b"")
        old = old_sources.get(url, {})
        if response.get("error") or not content.startswith(b"%PDF"):
            reason = response.get("error", "Документ банка не является PDF")
            document_flags[product_url] = reason
            sources.append({**old, "url": url, "status": "unavailable", "attempted_at": now.isoformat(), "error": reason})
            continue
        digest = hashlib.sha256(content).hexdigest()
        changed = bool(old.get("source_hash") and old["source_hash"] != digest)
        needs_review = changed or old.get("review_required") or old.get("status") == "needs_review"
        if needs_review and not accept_terms:
            document_flags[product_url] = "Изменён официальный документ условий; нужна проверка"
            if changed and old.get("pending_source_hash") != digest:
                events.append({"kind": "document_changed", "url": url, "at": now.isoformat()})
            # Keep the last accepted document and its verification timestamp.
            # Repeated successful downloads do not approve changed conditions.
            sources.append({**old, "url": url, "status": "needs_review", "attempted_at": now.isoformat(),
                            "pending_source_hash": digest, "review_required": True, "changed": changed})
        else:
            sources.append({"url": url, "status": "verified", "checked_at": now.isoformat(),
                            "source_hash": digest, "changed": changed, "review_required": False})
    for url in PRODUCT_SOURCES:
        response = fetched.get(url, {})
        source = {"url": url, "attempted_at": now.isoformat()}
        try:
            if response.get("error"):
                raise ValueError(response["error"])
            parsed = parse_source(url, response.get("text", ""), now)
            if not parsed:
                raise ValueError("Нет подтверждённых программ")
            source.update(status="verified", checked_at=now.isoformat(), source_hash=parsed[0]["source_hash"])
            unresolved = [item for item in old_offers.values()
                          if item.get("source_url") == url and
                          (item.get("status") == "needs_review" or item.get("pending_terms_hash"))]
            source_terms_changed = any(old_offers.get(item["id"]) and
                                       old_offers[item["id"]].get("terms_hash") != item["terms_hash"]
                                       for item in parsed)
            source_needs_review = bool(unresolved) or source_terms_changed
            for candidate in parsed:
                old = old_offers.get(candidate["id"])
                changed = old and old.get("terms_hash") != candidate["terms_hash"]
                needs_review = source_needs_review or changed or (old and old.get("status") == "needs_review")
                reason = document_flags.get(url)
                if (needs_review and not accept_terms) or reason:
                    offer = dict(old or candidate)
                    if old is None:
                        offer["checked_at"] = None
                    offer.update(status="needs_review", attempted_at=now.isoformat(),
                                 pending_terms_hash=candidate["terms_hash"], pending_conditions=candidate["conditions"],
                                 review_reason=reason or (unresolved[0].get("review_reason") if unresolved else None)
                                 or "Изменились условия программы; нужна проверка")
                    offers.append(offer)
                    if changed and old.get("pending_terms_hash") != candidate["terms_hash"]:
                        events.append({"kind": "conditions_changed", "id": candidate["id"], "at": now.isoformat()})
                else:
                    offers.append(candidate)
                    if old and (old.get("nominal_pct"), old.get("effective_pct")) != (candidate["nominal_pct"], candidate["effective_pct"]):
                        events.append({"kind": "rate_changed", "id": candidate["id"], "at": now.isoformat(),
                                       "old_nominal_pct": old.get("nominal_pct"), "old_effective_pct": old.get("effective_pct"),
                                       "new_nominal_pct": candidate["nominal_pct"], "new_effective_pct": candidate["effective_pct"]})
            # Removed offerings must not remain eligible.
            live_ids = {item["id"] for item in parsed}
            for old in old_offers.values():
                if old.get("source_url") == url and old["id"] not in live_ids:
                    offers.append({**old, "status": "unavailable", "review_reason": "Программа исчезла из источника"})
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            source.update(status="unavailable", error=str(exc))
            for old in old_offers.values():
                if old.get("source_url") == url:
                    offers.append({**old, "status": "unavailable", "attempted_at": now.isoformat(), "review_reason": str(exc)})
        sources.append(source)
    for url in INDEX_SOURCES:
        response = fetched.get(url, {})
        old = old_sources.get(url, {})
        if response.get("error"):
            sources.append({**old, "url": url, "status": "unavailable", "attempted_at": now.isoformat(), "error": response["error"]})
            continue
        programs = discover_programs(url, response.get("text", ""))
        if not programs:
            sources.append({**old, "url": url, "status": "unavailable", "attempted_at": now.isoformat(), "error": "Не найдены ссылки программ"})
            continue
        new_programs = sorted(set(programs) - set(old.get("program_urls", []))) if old else []
        removed = sorted(set(old.get("program_urls", [])) - set(programs)) if old else []
        if new_programs or removed:
            events.append({"kind": "programs_changed", "url": url, "new": new_programs, "removed": removed, "at": now.isoformat()})
        sources.append({"url": url, "status": "verified", "checked_at": now.isoformat(), "program_urls": programs,
                        "source_hash": hashlib.sha256(json.dumps(programs).encode()).hexdigest(),
                        "new_programs": new_programs, "removed_programs": removed})
    return {"schema_version": SCHEMA_VERSION, "checked_at": now.isoformat(), "offers": offers, "sources": sources,
            "changes": (previous.get("changes", []) + events)[-300:],
            "monitor": {"method": "official_public_https", "interval_hours": 6,
                        "rate_expiry_hours": 48, "conditions_changes_require_review": True,
                        "scope": "Выбранные розничные программы и ссылки новых программ в каталогах четырёх банков"}}


async def fetch_sources(client: httpx.AsyncClient) -> dict:
    semaphore = asyncio.Semaphore(3)
    async def fetch(url: str):
        async with semaphore:
            try:
                async with client.stream("GET", url) as response:
                    # Do not follow redirects, challenge pages or unknown endpoints.
                    if response.status_code != 200:
                        return url, {"error": f"HTTP {response.status_code}"}
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BODY:
                            raise ValueError("Ответ превышает 2 МБ")
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    content_type = response.headers.get("content-type", "").lower()
                    if url in DOCUMENT_SOURCES:
                        if "pdf" not in content_type or not body.startswith(b"%PDF"):
                            raise ValueError("Вместо PDF получен другой ответ")
                        return url, {"content": body}
                    if "text/html" not in content_type:
                        raise ValueError("Вместо HTML получен другой ответ")
                    return url, {"text": body.decode("utf-8", errors="strict")}
            except (httpx.HTTPError, ValueError, UnicodeError) as exc:
                return url, {"error": type(exc).__name__ + ": " + str(exc)}
    return dict(await asyncio.gather(*(fetch(url) for url in (*PRODUCT_SOURCES, *INDEX_SOURCES, *DOCUMENT_SOURCES))))


@contextmanager
def catalog_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as lock:
        if os.name == "posix":
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def write_catalog(path: Path, catalog: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".deposit-catalog-", suffix=".json", delete=False) as output:
            temporary = Path(output.name)
            json.dump(catalog, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        if os.name == "posix":
            temporary.chmod(0o640)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=CATALOG_PATH)
    parser.add_argument("--accept-terms", action="store_true", help="Accept changed conditions only after reading current bank documents")
    args = parser.parse_args()
    with catalog_lock(args.output):
        previous = load_deposit_catalog(args.output)
        async def collect():
            async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
                return await fetch_sources(client)
        fetched = asyncio.run(collect())
        catalog = build_catalog(previous, fetched, datetime.now(timezone.utc), args.accept_terms)
        write_catalog(args.output, catalog)
    public = public_deposit_catalog(catalog)
    print(json.dumps({"offers": len(catalog["offers"]), "banks": [{"key": bank["key"], "eligible": bank["eligible"],
           "product": bank["product"], "effective_pct": bank["effective_pct"]} for bank in public["banks"]],
           "sources_unavailable": sum(source["status"] != "verified" for source in catalog["sources"])}, ensure_ascii=False))
    return 1 if public["stale"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
