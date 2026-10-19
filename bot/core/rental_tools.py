"""Manual rental scenarios and a private, local pilot-request queue.

No PostgreSQL, source fetching, messaging, tax rates or occupancy forecasts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import hmac
import math
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from bot.core.comparison_rental_estimate import validate_rental_estimate

SCENARIO_FIELDS = ("monthly_rent_a", "monthly_rent_b", "horizon_months", "waiting_days",
                   "monthly_owner_expenses", "oneoff_expenses", "refundable_deposit")
REQUEST_KINDS = {"owner": "Собственник", "agency": "Агентство", "company": "Компания"}
REQUEST_STATUSES = ("new", "contacted", "done", "declined")
STATUS_LABELS = {"new": "Новая", "contacted": "Связались", "done": "Завершена", "declined": "Отклонена"}
LOCAL_TIMEZONE = ZoneInfo("Asia/Almaty")


class RentalToolsError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("Check rental tool fields")


class RentalRequestRateLimit(ValueError):
    pass


class RentalRequestConflict(ValueError):
    pass


def _decimal(value, field, low, high, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise RentalToolsError({field: "Введите число."})
    if isinstance(value, str) and (not value.strip() or len(value) > 40):
        raise RentalToolsError({field: "Введите число."})
    try:
        number = Decimal(str(value).strip().replace(",", "."))
        valid = (number.is_finite() and Decimal(low) <= number <= Decimal(high)
                 and (number == number.to_integral_value() if integer else number == number.quantize(Decimal(".01"))))
    except (InvalidOperation, ValueError, OverflowError):
        valid = False
    if not valid:
        raise RentalToolsError({field: f"Введите {'целое ' if integer else ''}число от {low} до {high}."})
    return number


def _money(value):
    return float(value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def calculate_rental_scenario(payload):
    """Compare two explicit asking-rent scenarios, with 30-day months.

    The same ownership and one-off expenses apply on both sides, including
    during vacancy. A refundable security deposit is never rental income.
    """
    if not isinstance(payload, dict):
        raise RentalToolsError({"form": "Не удалось прочитать расчёт."})
    limits = {"monthly_rent_a": (1, 10_000_000), "monthly_rent_b": (1, 10_000_000),
              "monthly_owner_expenses": (0, 10_000_000), "oneoff_expenses": (0, 100_000_000),
              "refundable_deposit": (0, 100_000_000), "horizon_months": (1, 24), "waiting_days": (0, 365)}
    values, errors = {}, {}
    for field, (low, high) in limits.items():
        try:
            values[field] = _decimal(payload.get(field), field, low, high,
                                     integer=field in ("horizon_months", "waiting_days"))
        except RentalToolsError as exc:
            errors.update(exc.errors)
    if errors:
        raise RentalToolsError(errors)
    horizon = values["horizon_months"]
    days = horizon * 30
    waiting = values["waiting_days"]
    if waiting > days:
        raise RentalToolsError({"waiting_days": "Ожидание не может превышать выбранный горизонт (месяц — 30 дней)."})
    rent_a, rent_b = values["monthly_rent_a"], values["monthly_rent_b"]
    owner, oneoff = values["monthly_owner_expenses"], values["oneoff_expenses"]

    def scenario(rent, wait):
        gross = rent * (days-wait) / 30
        gross_12 = rent * max(Decimal(0), Decimal(360)-wait) / 30
        return {"gross_horizon": _money(gross), "net_horizon": _money(gross-owner*horizon-oneoff),
                "gross_12_months": _money(gross_12), "net_12_months": _money(gross_12-owner*12-oneoff),
                "occupied_days": int(days-wait), "owner_expenses_horizon": _money(owner*horizon),
                "oneoff_expenses": _money(oneoff)}

    first, second = scenario(rent_a, Decimal(0)), scenario(rent_b, waiting)
    difference = rent_b * (days-waiting) / 30 - rent_a*horizon
    # Equal costs cancel. If B is cheaper even without waiting, there is no
    # nonnegative break-even waiting duration within this horizon.
    break_even_numerator = days*(rent_b-rent_a) if rent_b >= rent_a else None
    break_even = break_even_numerator/rent_b if break_even_numerator is not None else None
    return {"assumptions": {field: (int(values[field]) if field in ("horizon_months", "waiting_days")
                                    else _money(values[field])) for field in SCENARIO_FIELDS},
            "scenario_a": first, "scenario_b": second, "difference": _money(difference),
            "forgone_rent": _money(rent_a*waiting/30),
            "break_even_waiting_days": _money(break_even) if break_even is not None else None,
            "max_whole_waiting_days": int(break_even_numerator//rent_b) if break_even_numerator is not None else None,
            "refundable_deposit": _money(values["refundable_deposit"]), "deposit_is_income": False,
            "days_per_month": 30, "manual_scenario": True,
            "notes": ["Суммы и ожидание введены вами: это сценарий, а не прогноз цены или срока сдачи.",
                      "Расходы владельца, включая введённые вами налоги, учитываются и во время ожидания. Это не налоговый расчёт.",
                      "Возвратный обеспечительный депозит не включён в доход.",
                      "Расчёт на 12 месяцев — отдельный сценарий; условный месяц равен 30 дням."]}


def _utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            return None
    return value.astimezone(timezone.utc) if isinstance(value, datetime) and value.tzinfo else None


def rental_examples_from_snapshot(snapshot, *, now=None, limit=40):
    """Only bounded asking-rent aggregates; no source IDs, addresses or contacts."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(snapshot, dict):
        return []
    generated = _utc(snapshot.get("generated_at"))
    if generated is None or not timedelta(0) <= now-generated <= timedelta(hours=26):
        return []
    examples, seen = [], set()
    for offer in snapshot.get("offers", []):
        if not isinstance(offer, dict):
            continue
        if offer.get("market_type") == "primary" and offer.get("build_status") != "commissioned":
            continue
        estimate = validate_rental_estimate(offer.get("rental_estimate"), now)
        cid, rooms, area = offer.get("complex_id"), offer.get("rooms"), offer.get("area")
        name = offer.get("complex_name")
        if (not estimate or type(cid) is not int or cid <= 0 or type(rooms) is not int or not 1 <= rooms <= 6
                or type(area) not in (int, float) or not math.isfinite(area) or not 20 <= area <= 300
                or not isinstance(name, str) or not name.strip()):
            continue
        key = (cid, rooms, round(area, 1), estimate["monthly_low"], estimate["monthly_high"])
        if key in seen:
            continue
        seen.add(key)
        examples.append({"title": f"{rooms}-комнатная · {area:g} м²", "complex_name": name.strip()[:100],
                         "rooms": rooms, "area": area, **estimate})
    examples.sort(key=lambda item: (-item["sample_count"], item["complex_name"], item["rooms"], item["area"]))
    return [{"key": f"rent-{index}", **item} for index, item in enumerate(examples[:max(0, min(int(limit), 40))], 1)]


def inquiry_path():
    return Path(os.environ.get("HATULI_RENTAL_INQUIRIES_PATH") or
                Path(__file__).resolve().parents[2] / "var" / "rental-inquiries.sqlite3")


def _fingerprint(secret, purpose, value):
    return hmac.new(secret.encode(), (purpose+":"+value).encode(), hashlib.sha256).hexdigest()


def normalize_rental_request(values):
    errors, clean = {}, {}
    for field, maximum in (("kind", 20), ("contact", 254), ("brief", 1000), ("website", 254)):
        value = values.get(field, "")
        if not isinstance(value, str) or len(value) > maximum or any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            errors[field] = "Введите текст допустимой длины."
        else:
            clean[field] = value.strip()
    if errors:
        raise RentalToolsError(errors)
    if clean["kind"] not in REQUEST_KINDS:
        errors["kind"] = "Выберите: собственник, агентство или компания."
    if len(clean["brief"]) < 20:
        errors["brief"] = "Опишите задачу хотя бы в 20 символах."
    if values.get("consent") is not True and values.get("consent") not in ("yes", "true", "1"):
        errors["consent"] = "Нужно согласие на использование контакта для ответа на эту заявку."
    contact = clean["contact"]
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", contact):
        clean["contact_type"] = "telegram"
    elif re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", contact):
        local, domain = contact.rsplit("@", 1)
        labels = domain.split(".")
        if local.startswith(".") or local.endswith(".") or ".." in local or len(labels) < 2 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
            errors["contact"] = "Укажите @username в Telegram или email."
        clean["contact_type"] = "email"
    else:
        errors["contact"] = "Укажите @username в Telegram или email."
    if errors:
        raise RentalToolsError(errors)
    return clean


def _connect(path, *, create):
    path = Path(path)
    if path.is_symlink() or "static" in {part.lower() for part in path.resolve().parts}:
        raise ValueError("Private inquiry destination required")
    if not path.exists() and not create:
        return None
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            if path.is_symlink():
                raise ValueError("Private inquiry destination required") from None
    os.chmod(path, 0o600)
    connection = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # DELETE mode avoids separate persistent WAL/SHM files with wider modes.
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("CREATE TABLE IF NOT EXISTS rental_inquiries (reference TEXT PRIMARY KEY, kind TEXT NOT NULL, contact TEXT NOT NULL, contact_type TEXT NOT NULL, brief TEXT NOT NULL, contact_key TEXT NOT NULL, client_key TEXT NOT NULL, request_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', version INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL)")
    return connection


def save_rental_request(values, *, client_identifier, secret, now=None, path=None):
    clean = normalize_rental_request(values)
    if clean["website"]:
        return {"reference": str(uuid4())}
    if not isinstance(secret, str) or not secret:
        raise ValueError("Private inquiry secret required")
    timestamp = int((now or datetime.now(timezone.utc)).timestamp())
    contact_key = _fingerprint(secret, "rental-contact", clean["contact"].lower())
    client_key = _fingerprint(secret, "rental-client", client_identifier)
    request_key = _fingerprint(secret, "rental-request", clean["kind"]+":"+contact_key+":"+clean["brief"])
    connection = _connect(path or inquiry_path(), create=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT reference FROM rental_inquiries WHERE request_key=? AND status IN ('new','contacted')", (request_key,)).fetchone()
        if existing:
            connection.commit()
            return {"reference": existing["reference"]}
        counts = connection.execute("SELECT count(CASE WHEN client_key=? AND created_at>? THEN 1 END) AS hour, count(CASE WHEN client_key=? THEN 1 END) AS day, count(CASE WHEN contact_key=? THEN 1 END) AS contact_day FROM rental_inquiries WHERE created_at>? AND (client_key=? OR contact_key=?)",
                                    (client_key, timestamp-3600, client_key, contact_key, timestamp-86400, client_key, contact_key)).fetchone()
        if counts["hour"] >= 3 or counts["day"] >= 10 or counts["contact_day"] >= 3:
            raise RentalRequestRateLimit("Слишком много заявок. Попробуйте позже.")
        reference = str(uuid4())
        connection.execute("INSERT INTO rental_inquiries(reference,kind,contact,contact_type,brief,contact_key,client_key,request_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (reference, clean["kind"], clean["contact"], clean["contact_type"], clean["brief"], contact_key, client_key, request_key, timestamp))
        connection.commit()
        return {"reference": reference}
    finally:
        connection.close()


def list_rental_requests(*, path=None, limit=100):
    connection = _connect(path or inquiry_path(), create=False)
    if connection is None:
        return []
    try:
        rows = connection.execute("SELECT reference,kind,contact,contact_type,brief,status,version,created_at FROM rental_inquiries ORDER BY created_at DESC,reference LIMIT ?", (max(1, min(int(limit), 200)),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            created = datetime.fromtimestamp(item["created_at"], timezone.utc)
            item.update(created_at=created.isoformat(), created_at_label=created.astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y, %H:%M"), kind_label=REQUEST_KINDS.get(item["kind"], "Заявка"))
            result.append(item)
        return result
    finally:
        connection.close()


def update_rental_request(reference, status, version, *, path=None):
    try:
        reference = str(UUID(str(reference)))
    except (ValueError, TypeError, AttributeError):
        raise RentalToolsError({"reference": "Некорректный номер заявки."}) from None
    if status not in REQUEST_STATUSES or type(version) is not int or not 1 <= version <= 1_000_000:
        raise RentalToolsError({"status": "Проверьте статус и обновите страницу."})
    connection = _connect(path or inquiry_path(), create=False)
    if connection is None:
        return False
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute("UPDATE rental_inquiries SET status=?,version=version+1 WHERE reference=? AND version=?", (status, reference, version)).rowcount
        if not changed and connection.execute("SELECT 1 FROM rental_inquiries WHERE reference=?", (reference,)).fetchone():
            raise RentalRequestConflict("Заявка уже изменена. Обновите страницу.")
        connection.commit()
        return bool(changed)
    finally:
        connection.close()
