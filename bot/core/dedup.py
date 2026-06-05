"""
Deduplication engine for real estate listings.

Groups listings where ≥3 canonical fields match OR image hash distance < 10.
Merged duplicates carry a `sources` field listing all origin source names.
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from typing import Any

logger = logging.getLogger(__name__)

# Prefixes/abbreviations to strip from addresses
_ADDRESS_NOISE = re.compile(
    r"\b(ул|улица|пр|проспект|пр-т|пр-кт|пер|переулок|пл|площадь|бул|бульвар"
    r"|тупик|шос|шоссе|наб|набережная|мкр|микрорайон|д|дом|стр|строение"
    r"|корп|корпус|к|эт|этаж|кв|квартира)\b\.?",
    re.IGNORECASE | re.UNICODE,
)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_address(address: str) -> str:
    """Return a lowercased, stripped canonical form of an address string."""
    if not address:
        return ""
    text = unicodedata.normalize("NFKC", address)
    text = _ADDRESS_NOISE.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip().lower()


def _canonical_fields(listing: dict[str, Any]) -> dict[str, Any]:
    """Extract the canonical comparison fields from a listing dict."""
    phone = listing.get("phone") or ""
    phone = re.sub(r"\D", "", phone)
    phone = phone[-10:] if len(phone) >= 10 else phone  # last 10 digits

    price = listing.get("price")
    area = listing.get("area")
    floor = listing.get("floor")
    rooms = listing.get("rooms")
    complex_name = (listing.get("complex_name") or "").lower().strip()
    address = normalize_address(listing.get("address") or "")

    return {
        "phone": phone or None,
        "complex_name": complex_name or None,
        "floor": floor,
        "area": round(float(area), 0) if area is not None else None,
        "price": price,
        "address": address or None,
        "rooms": rooms,
    }


def _fields_match_count(a: dict[str, Any], b: dict[str, Any]) -> int:
    """Count how many canonical fields are non-None and equal between a and b."""
    count = 0
    for key in ("phone", "complex_name", "floor", "area", "price", "address", "rooms"):
        va, vb = a.get(key), b.get(key)
        if va is not None and vb is not None and va == vb:
            count += 1
    return count


def _hash_distance(h1: str | None, h2: str | None) -> int | None:
    """
    Compute hamming distance between two perceptual hash strings.
    Returns None if either hash is missing or hashes have different lengths.
    """
    if not h1 or not h2 or len(h1) != len(h2):
        return None
    return sum(c1 != c2 for c1, c2 in zip(h1, h2))


def compute_image_hash(image_bytes: bytes) -> str | None:
    """
    Compute a perceptual hash for raw image bytes.
    Returns hex string or None on failure.
    Requires `imagehash` and `Pillow` packages.
    """
    try:
        import imagehash
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        return str(imagehash.phash(img))
    except Exception as exc:  # noqa: BLE001
        logger.debug("compute_image_hash failed: %s", exc)
        return None


def _are_duplicates(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """
    Return True when listings a and b should be considered duplicates:
      - ≥3 canonical fields match, OR
      - image hash hamming distance < 10
    """
    fields_a = _canonical_fields(a)
    fields_b = _canonical_fields(b)

    if _fields_match_count(fields_a, fields_b) >= 3:
        return True

    dist = _hash_distance(a.get("photo_hash"), b.get("photo_hash"))
    if dist is not None and dist < 10:
        return True

    return False


def deduplicate(listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Remove duplicate listings, merging sources from duplicates into the winner.

    The first occurrence (by list order) is kept as the canonical listing.
    Duplicates are merged: their `sources` lists are combined and the kept listing
    retains the lowest price if prices differ.

    Args:
        listings: List of listing dicts. Each may contain a `sources` field
                  (list of str) and a `photo_hash` field (hex str).

    Returns:
        Deduplicated list of listing dicts, each with a `sources: list[str]` field.
    """
    groups: list[list[int]] = []          # groups of indices into `listings`
    assigned: list[int] = [-1] * len(listings)  # group index for each listing

    for i, listing in enumerate(listings):
        placed = False
        for g_idx, group in enumerate(groups):
            representative = listings[group[0]]
            if _are_duplicates(representative, listing):
                group.append(i)
                assigned[i] = g_idx
                placed = True
                break
        if not placed:
            assigned[i] = len(groups)
            groups.append([i])

    result: list[dict[str, Any]] = []
    for group in groups:
        if not group:
            continue

        # Pick the listing with the most data (non-None fields) as canonical
        canonical = max(
            (listings[i] for i in group),
            key=lambda l: sum(1 for v in l.values() if v is not None),
        )
        merged = dict(canonical)

        # Collect all sources
        all_sources: list[str] = []
        for i in group:
            src = listings[i].get("sources")
            if isinstance(src, list):
                all_sources.extend(src)
            elif isinstance(src, str) and src:
                all_sources.append(src)

        merged["sources"] = list(dict.fromkeys(all_sources))  # deduplicate preserving order

        result.append(merged)

    return result
