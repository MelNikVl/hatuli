"""Dated, immutable analytical articles from a frozen aggregate digest.

The public request path only reads local files. Drafts cannot supply chart
values: both prose tokens and graph rows resolve against the same dated facts.
An old edition remains a dated archive, never a fresh market snapshot.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import tempfile
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

LOCAL_TIMEZONE = ZoneInfo("Asia/Almaty")
KINDS = {"market": "Рынок жилья", "rent": "Аренда"}
SLUG = re.compile(r"(?P<day>\d{4}-\d{2}-\d{2})-(?P<kind>market|rent)(?P<local>-local)?\Z")
FACT_ID = re.compile(r"[a-z][a-z0-9_.]{1,79}\Z")
FACT_TOKEN = re.compile(r"\{fact:([a-z][a-z0-9_.]{1,79})\}")
LABEL_TOKEN = re.compile(r"\{label:([a-z][a-z0-9_.]{1,79})\}")
SOURCE_HOSTS = {"hatuli.ai-groundtruth.com", "stat.gov.kz", "nationalbank.kz", "gov.kz",
                "adilet.zan.kz", "egov.kz", "kdif.kz"}
UNITS = {"объявлений", "₸", "₸/м²", "₸/м²/мес.", "₸/мес.", "₸/сутки", "%", "м²", "сделок", "индекс", "единиц"}
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")
MAX_FILE_BYTES = 1_500_000
MAX_SCAN = 100_000


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


def archive_directory(root=None, *, local=False):
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    return base / "var" / ("analytics-local-articles" if local else "analytics-articles")


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("ISO edition date required")
    return date.fromisoformat(value)


def _date_label(value):
    value = value.astimezone(LOCAL_TIMEZONE) if isinstance(value, datetime) else value
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def _source_url(value):
    if not isinstance(value, str) or len(value) > 600:
        raise ValueError("Invalid source URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in SOURCE_HOSTS
            or parsed.username or parsed.password or parsed.port or parsed.fragment
            or any(ord(c) < 32 for c in value)):
        raise ValueError("Only approved official or Hatuli HTTPS sources are permitted")
    return value


def _plain(value, minimum=1, maximum=2000):
    if (not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum
            or value != value.strip() or re.search(r"[<>\x00-\x08\x0b-\x1f]", value)
            or re.search(r"https?://|www\.|javascript:|data:|\b[\w.+-]+@[\w.-]+\.", value, re.I)
            or re.search(r"(?:\+\d|\b[78]\s*\()\d?[\d\s()-]{8,}", value)):
        raise ValueError("Plain editorial text without HTML, URLs, or contacts required")
    return value


def number_label(value, unit):
    digits = f"{value:,.1f}".rstrip("0").rstrip(".") if not float(value).is_integer() else f"{int(value):,}"
    digits = digits.replace(",", " ").replace(".", ",")
    return f"{digits}{unit}" if unit == "%" else f"{digits} {unit}"


def _place_label(value):
    """Only a bounded place name, never prose or another numeric claim."""
    _plain(value, 2, 120)
    if (re.search(r"[{}%₸$€]|\b(?:тенге|kzt|usd|percent)\b|скидк|доход|рентаб|прибыл|процент|гарантир", value, re.I)
            or re.search(r"\d\s*(?:млн|тыс|м²|м2|мес|дней|год|комнат)\b", value, re.I)):
        raise ValueError("A label token requires a place name without numeric claims")
    districts = {"Есиль", "Нура", "Алматы", "Сарыарка", "Сарайшык", "Байконур"}
    if value not in districts:
        name = next((value[len(prefix):] for prefix in ("ЖК ", "Дом ") if value.startswith(prefix)), "")
        if (not name or not name[0].isalnum() or len(name) > 110
                or any(not (char.isalpha() or char.isdecimal() or char in " .,'’«»\"()/-") for char in name)
                or re.search(r"(?:\d[ -]*){10}", name)):
            raise ValueError("A label token requires an explicit district, complex or house name")
    return value


def validate_digest(raw, *, published_at=None):
    if (not isinstance(raw, dict) or set(raw) != {"schema_version", "generated_at", "scope", "sources", "facts"}
            or raw.get("schema_version") != 1):
        raise ValueError("Invalid aggregate digest schema")
    generated = utc(raw["generated_at"])
    if published_at is not None and not published_at - timedelta(hours=24) <= generated <= published_at:
        raise ValueError("The aggregate digest must be no older than 24 hours at publication")
    _plain(raw["scope"], 30, 800)
    sources = raw["sources"]
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 12:
        raise ValueError("Dated sources required")
    for key, source in sources.items():
        if (not FACT_ID.fullmatch(key) or not isinstance(source, dict)
                or set(source) != {"kind", "label", "url", "as_of", "published_at", "retrieved_at", "reuse_url", "rights_status"}
                or source["kind"] not in {"official_publication", "hatuli_observations"}
                or source["rights_status"] not in {"official_reuse_permitted", "not_cleared", "operator_authorized"}):
            raise ValueError("Invalid source")
        _plain(source["label"], 10, 220)
        _source_url(source["url"])
        observed, released, retrieved = [utc(source[field]) for field in ("as_of", "published_at", "retrieved_at")]
        if not generated - timedelta(days=1830) <= observed <= released <= retrieved <= generated:
            raise ValueError("Invalid source observation time")
        if retrieved < generated - timedelta(hours=24):
            raise ValueError("Sources must have been checked within 24 hours of digest creation")
        _source_url(source["reuse_url"])
        if source["kind"] == "official_publication" and urlsplit(source["url"]).hostname == "hatuli.ai-groundtruth.com":
            raise ValueError("An official publication must link to the official source")
    facts = raw["facts"]
    if not isinstance(facts, dict) or not 4 <= len(facts) <= 500:
        raise ValueError("Aggregate facts required")
    for key, fact in facts.items():
        if (not FACT_ID.fullmatch(key) or not isinstance(fact, dict)
                or set(fact) != {"label", "value", "unit", "n", "as_of", "source_id"}
                or type(fact["value"]) not in (int, float) or not math.isfinite(fact["value"])
                or fact["unit"] not in UNITS or fact["source_id"] not in sources
                or not (-100 if fact["unit"] == "%" else 0) <= fact["value"] <= 1e12):
            raise ValueError("Invalid aggregate fact")
        _plain(fact["label"], 2, 120)
        if utc(fact["as_of"]) != utc(sources[fact["source_id"]]["as_of"]):
            raise ValueError("Fact and source dates must agree")
        official = sources[fact["source_id"]]["kind"] == "official_publication"
        if not (official and fact["n"] is None) and (type(fact["n"]) is not int or not 0 <= fact["n"] <= MAX_SCAN):
            raise ValueError("Official undisclosed sample size may be null; observed sample size must be explicit")
        if not official and fact["unit"] != "объявлений" and fact["n"] < 10:
            raise ValueError("Price distributions require at least ten observations")
        if fact["unit"] == "объявлений" and (not float(fact["value"]).is_integer() or not official and fact["value"] > fact["n"]):
            raise ValueError("Invalid count denominator")
        if fact["unit"] == "%" and fact["value"] > 100:
            raise ValueError("Invalid percentage")
    return raw


def _resolve(text, facts, minimum=1, maximum=2000):
    _plain(text, minimum, maximum)
    literal = LABEL_TOKEN.sub("", FACT_TOKEN.sub("", text))
    # A digit in prose must be backed by a dated aggregate. Graph values use
    # fact IDs, never arbitrary draft data. Dates are rendered by the page.
    if re.search(r"\d|\{|\}", literal):
        raise ValueError("Use dated fact tokens for all numeric claims")
    def substitute(match):
        key = match[1]
        if key not in facts:
            raise ValueError(f"Unknown fact token: {key}")
        fact = facts[key]
        return number_label(fact["value"], fact["unit"])
    def label(match):
        if match[1] not in facts:
            raise ValueError(f"Unknown label fact: {match[1]}")
        return _place_label(facts[match[1]]["label"])
    return LABEL_TOKEN.sub(label, FACT_TOKEN.sub(substitute, text))


def _text_list(value, facts, count_min, count_max, text_min=20, text_max=1200):
    if not isinstance(value, list) or not count_min <= len(value) <= count_max:
        raise ValueError("Invalid editorial list length")
    return [_resolve(item, facts, text_min, text_max) for item in value]


def _normalize_article(raw, digest, edition_day, published):
    required = {"kind", "title", "summary", "lead", "key_points", "sections", "charts", "source_ids", "methodology"}
    if not isinstance(raw, dict) or set(raw) != required or raw.get("kind") not in KINDS:
        raise ValueError("Invalid article schema")
    facts = digest["facts"]
    kind = raw["kind"]
    title = _resolve(raw["title"], facts, 20, 180)
    summary = _resolve(raw["summary"], facts, 80, 400)
    lead = _resolve(raw["lead"], facts, 100, 1000)
    points = _text_list(raw["key_points"], facts, 3, 5, 30, 500)
    methodology = _text_list(raw["methodology"], facts, 2, 6, 30, 900)
    source_ids = raw["source_ids"]
    if (not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 8
            or len(set(source_ids)) != len(source_ids) or any(key not in digest["sources"] for key in source_ids)):
        raise ValueError("Every article needs dated sources")
    charts = []
    serialized = json.dumps(raw, ensure_ascii=False)
    used_facts = set(FACT_TOKEN.findall(serialized)) | set(LABEL_TOKEN.findall(serialized))
    if not isinstance(raw["charts"], list) or not 2 <= len(raw["charts"]) <= 4:
        raise ValueError("Each article needs two to four fact-backed charts")
    chart_ids = set()
    for chart in raw["charts"]:
        if (not isinstance(chart, dict) or set(chart) - {"id", "title", "caption", "axis_label", "fact_ids", "mode"}
                or not {"id", "title", "caption", "axis_label", "fact_ids"} <= set(chart)
                or chart.get("mode", "comparison") not in {"comparison", "timeline"}
                or not isinstance(chart["id"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", chart["id"])
                or chart["id"] in chart_ids or not isinstance(chart["fact_ids"], list)
                or not 2 <= len(chart["fact_ids"]) <= 12 or len(set(chart["fact_ids"])) != len(chart["fact_ids"])
                or any(key not in facts for key in chart["fact_ids"])):
            raise ValueError("Invalid chart fact references")
        chart_ids.add(chart["id"])
        used_facts.update(chart["fact_ids"])
        entries = [facts[key] for key in chart["fact_ids"]]
        if len({entry["unit"] for entry in entries}) != 1:
            raise ValueError("Chart rows must have a common unit")
        dates = [utc(entry["as_of"]) for entry in entries]
        if chart.get("mode", "comparison") == "timeline":
            if len(set(dates)) != len(dates) or dates != sorted(dates):
                raise ValueError("Timeline dates must be unique and strictly chronological")
        elif len(set(dates)) != 1:
            raise ValueError("Comparison rows must share an observation date; use an explicit timeline for history")
        maximum = max(abs(entry["value"]) for entry in entries)
        if maximum <= 0:
            raise ValueError("Empty chart")
        charts.append({"id": chart["id"], "title": _resolve(chart["title"], facts, 10, 180),
                       "caption": _resolve(chart["caption"], facts, 30, 700),
                       "axis_label": _resolve(chart["axis_label"], facts, 3, 180), "mode": chart.get("mode", "comparison"),
                       "unit": entries[0]["unit"], "rows": [
                           {"label": entry["label"], "value": entry["value"], "n": entry["n"],
                            "value_label": number_label(entry["value"], entry["unit"]),
                            "bar_pct": round(100 * abs(entry["value"]) / maximum, 3)} for entry in entries]})
    if (not isinstance(raw["sections"], list) or not 4 <= len(raw["sections"]) <= 7):
        raise ValueError("An article needs four to seven sections")
    sections = []
    used_charts = []
    for section in raw["sections"]:
        if (not isinstance(section, dict) or set(section) - {"heading", "paragraphs", "chart_id"}
                or not {"heading", "paragraphs"} <= set(section)):
            raise ValueError("Invalid section")
        value = {"heading": _resolve(section["heading"], facts, 8, 150),
                 "paragraphs": _text_list(section["paragraphs"], facts, 1, 4, 80, 2000)}
        if "chart_id" in section:
            if section["chart_id"] not in chart_ids:
                raise ValueError("Unknown section chart")
            used_charts.append(section["chart_id"])
            value["chart_id"] = section["chart_id"]
        sections.append(value)
    if set(used_charts) != chart_ids or len(used_charts) != len(chart_ids):
        raise ValueError("Every chart must be placed in exactly one section")
    if any(facts[key]["source_id"] not in source_ids for key in used_facts):
        raise ValueError("An article must cite every used fact source")
    # Rental/sales units are not silently pooled in the same editorial chart.
    if kind == "rent" and not any(key.startswith(("rent_", "rent.")) for key in used_facts):
        raise ValueError("Rental articles require rental observations")
    words = len(re.findall(r"[А-Яа-яЁёA-Za-z]+(?:[-’][А-Яа-яЁёA-Za-z]+)?", " ".join(
        [lead, *points, *(paragraph for section in sections for paragraph in section["paragraphs"])])))
    if not 350 <= words <= 850:
        raise ValueError(f"Article length must be 350–850 words, got {words}")
    slug = f"{edition_day.isoformat()}-{kind}"
    return {"slug": slug, "url": f"/analytics/{slug}", "title": title, "summary": summary,
            "kind": kind, "category_label": KINDS[kind], "edition_date": edition_day.isoformat(),
            "date_label": _date_label(edition_day),
            "published_label": f"{_date_label(published)}, {published.astimezone(LOCAL_TIMEZONE):%H:%M} UTC+5",
            "reading_minutes": max(2, math.ceil(words / 180)), "lead": lead, "key_points": points,
            "sections": sections, "charts": charts, "sources": [
                {"label": digest["sources"][key]["label"], "url": digest["sources"][key]["url"],
                 "as_of_label": f"Период: {_date_label(utc(digest['sources'][key]['as_of']))}. "
                 f"Опубликовано: {_date_label(utc(digest['sources'][key]['published_at']))}. "
                 f"Проверено: {_date_label(utc(digest['sources'][key]['retrieved_at']))}."}
                for key in source_ids], "methodology": methodology}


def normalize_edition(raw, *, now=None):
    if (not isinstance(raw, dict) or set(raw) != {"schema_version", "edition_date", "published_at", "digest", "articles"}
            or raw.get("schema_version") != 1 or not isinstance(raw.get("articles"), list)
            or len(raw["articles"]) != 2):
        raise ValueError("Exactly two articles form one immutable edition")
    day = _day(raw["edition_date"])
    published = utc(raw["published_at"])
    if published.astimezone(LOCAL_TIMEZONE).date() != day or now is not None and published > utc(now):
        raise ValueError("Invalid edition publication date")
    digest = validate_digest(raw["digest"], published_at=published)
    if any(source["rights_status"] != ("official_reuse_permitted" if source["kind"] == "official_publication"
                                        else "operator_authorized") for source in digest["sources"].values()):
        raise ValueError("Source reuse must be cleared before public publication")
    result = [_normalize_article(article, digest, day, published) for article in raw["articles"]]
    if {article["kind"] for article in result} != set(KINDS):
        raise ValueError("The daily pair must contain one market and one rental article")
    return result


def build_edition(draft, digest, *, now=None):
    now = utc(now or datetime.now(timezone.utc))
    if not isinstance(draft, dict) or set(draft) != {"edition_date", "articles"}:
        raise ValueError("Invalid draft pair")
    if _day(draft["edition_date"]) != now.astimezone(LOCAL_TIMEZONE).date():
        raise ValueError("Only today's local edition may be published; no backfills or future dates")
    edition = {"schema_version": 1, "edition_date": draft["edition_date"],
               "published_at": now.isoformat(), "digest": digest, "articles": draft["articles"]}
    normalize_edition(edition, now=now)
    return edition


def normalize_local_edition(edition, *, now=None):
    values = normalize_edition(edition, now=now)
    facts, sources = edition["digest"]["facts"], edition["digest"]["sources"]
    def observed(key):
        return sources[facts[key]["source_id"]]["kind"] == "hatuli_observations"
    for article in edition["articles"]:
        numeric = set(FACT_TOKEN.findall(json.dumps(article, ensure_ascii=False)))
        numeric.update(key for chart in article["charts"] for key in chart["fact_ids"])
        if len([key for key in numeric if observed(key)]) < 4:
            raise ValueError("Each local article requires at least four used numeric Hatuli observations")
        if any(not all(observed(key) for key in chart["fact_ids"]) for chart in article["charts"]):
            raise ValueError("Local research charts require observed local facts")
    return [_local_article(article) for article in values]


def _read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Invalid archive file")
    with path.open(encoding="utf-8") as handle:
        result = json.load(handle)
    if not isinstance(result, dict):
        raise ValueError("An archive file must contain an object")
    return result


def publish_edition(edition, *, root=None, now=None, local=False):
    now = utc(now or datetime.now(timezone.utc))
    normalize = normalize_local_edition if local else normalize_edition
    normalized = normalize(edition, now=now)
    day = _day(edition["edition_date"])
    if day != now.astimezone(LOCAL_TIMEZONE).date():
        raise ValueError("Only today's edition can be published")
    directory = archive_directory(root, local=local)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{day.isoformat()}.json"
    if target.exists():
        current = normalize(_read_json(target), now=now)
        if len(current) == 2:
            return {"status": "already_published", "edition_date": day.isoformat(), "articles": 2}
    encoded = json.dumps(edition, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError("Edition file too large")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".article-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic create-if-absent: concurrent publishers cannot overwrite even
        # a millisecond after the existence check. A failed pair is never seen.
        try:
            os.link(temporary, target)
        except FileExistsError:
            normalize(_read_json(target), now=now)
            return {"status": "already_published", "edition_date": day.isoformat(), "articles": 2}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"status": "published", "edition_date": day.isoformat(), "articles": len(normalized)}


def list_articles(root=None, category=None, now=None):
    if category is not None and category not in KINDS:
        return []
    now = utc(now or datetime.now(timezone.utc))
    result = []
    # Dated archives do not expire. A malformed day is omitted as a whole pair.
    for local in (True, False):
        directory = archive_directory(root, local=local)
        for path in sorted(directory.glob("*.json"), reverse=True):
            try:
                day = _day(path.stem)
                if day > now.astimezone(LOCAL_TIMEZONE).date():
                    continue
                raw = _read_json(path)
                if raw.get("edition_date") != path.stem:
                    continue
                values = (normalize_local_edition if local else normalize_edition)(raw, now=now)
                result.extend(article for article in values if category is None or article["kind"] == category)
            except (ValueError, TypeError, KeyError, OSError, OverflowError):
                continue
    return sorted(result, key=lambda article: (article["edition_date"], article["slug"].endswith("-local")), reverse=True)


def _local_article(article):
    """A separately dated supplement never replaces the original daily pair."""
    return {**article, "slug": article["slug"] + "-local", "url": article["url"] + "-local"}


def load_article(slug, root=None, now=None):
    if not isinstance(slug, str) or not (match := SLUG.fullmatch(slug)):
        return None
    try:
        _day(match["day"])
        local = bool(match["local"])
        raw = _read_json(archive_directory(root, local=local) / f"{match['day']}.json")
        if raw.get("edition_date") != match["day"]:
            return None
        values = (normalize_local_edition if local else normalize_edition)(raw, now=utc(now or datetime.now(timezone.utc)))
        return next((article for article in values if article["slug"] == slug), None)
    except (ValueError, TypeError, KeyError, OSError, OverflowError):
        return None


DISTRICTS = {"есиль": ("esil", "Есиль"), "нура": ("nura", "Нура"), "алматы": ("almaty", "Алматы"),
             "сарайшык": ("saraishyk", "Сарайшык"), "сарыарка": ("saryarka", "Сарыарка"),
             "байконур": ("baikonur", "Байконур")}


def district_key(value):
    value = value.casefold().replace("ё", "е") if isinstance(value, str) else ""
    for alias, canonical in (("алматинск", "алматы"), ("сарыаркинск", "сарыарка"),
                             ("сарайшық", "сарайшык"), ("байқоңыр", "байконур")):
        value = value.replace(alias, canonical)
    return next((pair for key, pair in DISTRICTS.items() if key in value), None)


def build_digest(sales_rows, rental_rows, *, now=None):
    """Pure current cross-section. No trend, demand, occupancy, or deal target."""
    from bot.core.daily_picks import _valid_source, _number
    from bot.core.rental_stage2_index import _valid
    now = utc(now or datetime.now(timezone.utc))
    if len(sales_rows) > MAX_SCAN or len(rental_rows) > MAX_SCAN:
        raise ValueError("Incomplete bounded population")
    sales = []
    identities = set()
    for raw in sorted(sales_rows, key=lambda row: str(row.get("id", ""))):
        row = dict(raw)
        try:
            price, area = _number(row.get("price")), _number(row.get("area"))
            identity = ("property", row["property_id"]) if row.get("property_id") is not None else ("id", row.get("id"))
            district = district_key(row.get("district"))
            if (identity in identities or not _valid_source(row) or row.get("is_active") is not True
                    or row.get("is_duplicate") is True or row.get("dup_needs_review") is True
                    or row.get("duplicate_of") or row.get("property_review_pending") is True
                    or row.get("archived_at") is not None or row.get("market_type") != "secondary" or not district
                    or price is None or area is None or not 5_000_000 <= price <= 600_000_000
                    or not 20 <= area <= 300 or type(row.get("rooms")) is not int or not 1 <= row["rooms"] <= 6
                    or not 14 <= area / row["rooms"] <= 100 or not 150_000 <= price / area <= 3_000_000
                    or not now - timedelta(hours=72) <= utc(row["last_seen"]) <= now):
                continue
            identities.add(identity)
            sales.append({"price": price, "area": area, "rooms": row["rooms"], "district": district})
        except (ValueError, TypeError, KeyError):
            continue
    rentals = {"monthly": [], "daily": []}
    identities = set()
    for raw in sorted(rental_rows, key=lambda row: str(row.get("id", ""))):
        row = dict(raw)
        try:
            if row.get("id") in identities or not _valid(row, now) or not _valid_source(row):
                continue
            period = row["rent_period"]
            price, area = _number(row.get("price")), _number(row.get("area"))
            if price is None or area is None or not 20 <= area <= 300 or not 12 <= area / row["rooms"] <= 100:
                continue
            if period == "monthly" and not 1000 <= price / area <= 50000:
                continue
            if period == "daily":
                episode, proof = utc(row["rental_episode_started_at"]), utc(row["rental_episode_last_seen_at"])
                if not episode <= proof <= utc(row["last_seen"]) or not now - timedelta(hours=72) <= proof <= now:
                    continue
            identities.add(row["id"])
            rentals[period].append({"price": price, "area": area, "rooms": row["rooms"],
                                    "district": district_key(row.get("district"))})
        except (ValueError, TypeError, KeyError):
            continue
    source_id = "hatuli.observed"
    facts = {}
    def fact(key, label, value, unit, n):
        facts[key] = {"label": label, "value": value, "unit": unit, "n": n,
                      "as_of": now.isoformat(), "source_id": source_id}
    def cohort(prefix, values, price_unit):
        total = len(values)
        if not total:
            return
        fact(f"{prefix}.count", "Вторичная продажа" if prefix == "sales" else "Помесячная аренда" if prefix == "rent_monthly" else "Посуточная аренда", total, "объявлений", total)
        if total >= 10:
            fact(f"{prefix}.price_median", "Медиана цены предложения", round(statistics.median(row["price"] for row in values)), price_unit, total)
            if prefix == "sales":
                fact(f"{prefix}.sqm_median", "Медиана цены за м²", round(statistics.median(row["price"] / row["area"] for row in values)), "₸/м²", total)
                fact("sales.under_100m_pct", "Объявления до ста миллионов тенге", round(100 * sum(row["price"] <= 100_000_000 for row in values) / total, 1), "%", total)
            quartiles = statistics.quantiles([row["price"] for row in values], n=4, method="inclusive")
            fact(f"{prefix}.price_p25", "Нижняя четверть", round(quartiles[0]), price_unit, total)
            fact(f"{prefix}.price_p75", "Верхняя четверть", round(quartiles[2]), price_unit, total)
        for rooms in (1, 2, 3, 4):
            group = [row for row in values if row["rooms"] == rooms] if rooms < 4 else [row for row in values if row["rooms"] >= 4]
            label = f"{rooms} комната" if rooms == 1 else f"{rooms} комнаты" if rooms < 4 else "4 комнаты и больше"
            fact(f"{prefix}.rooms_{rooms}.count", label, len(group), "объявлений", total)
            if len(group) >= 10:
                fact(f"{prefix}.rooms_{rooms}.median", label, round(statistics.median(row["price"] for row in group)), price_unit, len(group))
                if prefix == "sales":
                    fact(f"{prefix}.rooms_{rooms}.sqm", label, round(statistics.median(row["price"] / row["area"] for row in group)), "₸/м²", len(group))
        for key, label in DISTRICTS.values():
            group = [row for row in values if row["district"] and row["district"][0] == key]
            fact(f"{prefix}.district_{key}.count", label, len(group), "объявлений", total)
            if len(group) >= 10:
                fact(f"{prefix}.district_{key}.median", label, round(statistics.median(row["price"] for row in group)), price_unit, len(group))
        if prefix != "sales":
            unknown = sum(row["district"] is None for row in values)
            fact(f"{prefix}.district_unknown.count", "Район не установлен", unknown, "объявлений", total)
    cohort("sales", sales, "₸")
    cohort("rent_monthly", rentals["monthly"], "₸/мес.")
    cohort("rent_daily", rentals["daily"], "₸/сутки")
    digest = {"schema_version": 1, "generated_at": now.isoformat(),
              "scope": "Срез свежих наблюдений Hatuli за объявлениями Астаны; вторичная продажа и явно разделённые периоды аренды. Это наблюдаемая выборка, а не полный рынок.",
              "sources": {source_id: {"kind": "hatuli_observations", "label": "Агрегированные наблюдения Hatuli за объявлениями Астаны",
                                        "url": "https://hatuli.ai-groundtruth.com/how-it-works", "as_of": now.isoformat(),
                                        "published_at": now.isoformat(), "retrieved_at": now.isoformat(),
                                        "reuse_url": "https://hatuli.ai-groundtruth.com/how-it-works", "rights_status": "not_cleared"}},
              "facts": facts}
    validate_digest(digest)
    return digest
