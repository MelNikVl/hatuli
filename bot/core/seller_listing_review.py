"""Explainable seller copy review using supplied facts, without network or AI.

Photo contents, ownership, documents and asking-price fairness are not verified
by this module. A seller's photo checklist is kept separate from a photo count.
The generated draft never repeats a pasted description or personal contacts.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
import html
import math
import re
from urllib.parse import urlsplit

VERSION = 1
MAX_DESCRIPTION_LENGTH = 12_000
PHOTO_LABELS = {
    "rooms": "Все жилые комнаты",
    "kitchen": "Кухня",
    "bathroom": "Санузел",
    "floorplan": "Планировка",
    "windows": "Вид из окон",
    "entrance": "Подъезд и вход в дом",
}
PHOTO_ACTIONS = {
    "rooms": "Снимите каждую жилую комнату при дневном свете: общий вид и второй ракурс.",
    "kitchen": "Добавьте общий вид кухни, рабочей поверхности и места для обеденного стола.",
    "bathroom": "Покажите санузел целиком и состояние сантехники без обработки, скрывающей дефекты.",
    "floorplan": "Добавьте читаемую планировку с размерами; закройте личные данные на документе.",
    "windows": "Покажите реальный вид из окон, чтобы покупатель понимал окружение квартиры.",
    "entrance": "Добавьте фото входа, подъезда и лифта, если он есть.",
}
PHOTO_CHECK_ACTIONS = {
    "rooms": "Проверьте, что показана каждая жилая комната и по общим кадрам понятны её размеры и состояние.",
    "kitchen": "Проверьте, что на фото видны кухня целиком, рабочая поверхность и место для обеденного стола.",
    "bathroom": "Проверьте, что по фото понятны размеры санузла и состояние сантехники.",
    "floorplan": "Проверьте читаемость размеров на планировке; закройте личные данные на документе.",
    "windows": "Проверьте, что снимок показывает реальный вид именно из окон продаваемой квартиры.",
    "entrance": "Проверьте, что показаны вход и состояние подъезда, а также лифт, если он есть.",
}
FINISH_LABELS = {
    "rough": "черновая отделка", "prefinish": "предчистовая отделка",
    "finished": "чистовая отделка", "renovated": "с ремонтом",
    "furnished": "с мебелью", "designer": "дизайнерский ремонт",
    "needs_repair": "требуется ремонт",
}
FIELD_LABELS = {
    "rooms": "Количество комнат", "area": "Общая площадь", "price": "Цена",
    "address": "Адрес дома", "floor": "Этаж", "floors_total": "Этажность дома",
    "finish_level": "Состояние квартиры", "kitchen_area": "Площадь кухни",
    "ceiling_height": "Высота потолков", "year_built": "Год постройки",
    "utilities_monthly": "Коммунальные расходы", "included_items": "Что входит в цену",
    "viewing_terms": "Условия просмотра", "legal_status": "Информация об ипотеке",
}
FIELD_PROMPTS = {
    "rooms": "Укажите количество комнат; для студии выберите 0.",
    "area": "Укажите общую площадь в м² по документам и отдельно площадь кухни.",
    "price": "Укажите текущую цену в тенге и условия торга, если он возможен.",
    "address": "Укажите улицу, номер дома и корпус, если он есть. Название ЖК дополните адресом.",
    "floor": "Укажите этаж квартиры и общее количество этажей в доме.",
    "floors_total": "Допишите общее количество этажей в доме.",
    "finish_level": "Опишите отделку и нужные работы; укажите известные дефекты.",
    "kitchen_area": "Укажите площадь кухни в м²; для кухни-гостиной объясните объединённую планировку.",
    "ceiling_height": "Добавьте измеренную высоту потолков в метрах.",
    "year_built": "Укажите год постройки или сдачи дома, если он известен.",
    "utilities_monthly": "Укажите сумму коммунальных расходов и месяц; объясните, что входит в сумму.",
    "included_items": "Перечислите мебель и технику, которые остаются покупателю, или напишите, что квартира продаётся без них.",
    "viewing_terms": "Укажите удобное время просмотра и как его согласовать.",
    "legal_status": "Если известно, укажите, находится ли квартира в ипотеке. Документы и обременения проверяются перед сделкой; личные документы в объявление не выкладывайте.",
}
_PRIORITIES = {"high": 0, "medium": 1, "low": 2}
_NUMBER = r"(\d{1,4}(?:[.,]\d{1,2})?)"
_UNIT_AREA = r"(?:м\s*[²2]|кв\.?\s*м(?:етр\w*)?)(?!\w)"
_CONTACT = re.compile(
    r"(?:\+?\d(?:[\s().-]*\d){8,14})(?!\d|\s*(?:₸|тенге\b|тг\b))|(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})"
    r"|(?:https?://[^\s<>]+)|(?<!\w)@[A-Za-z][A-Za-z0-9_]{3,}", re.IGNORECASE,
)


class SellerListingReviewError(ValueError):
    """Invalid public input; a route may translate this into a friendly 400."""


def _text(value, field, limit):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SellerListingReviewError(f"Поле {field} должно быть текстом.")
    if len(value) > limit:
        raise SellerListingReviewError(f"Поле {field}: максимум {limit} символов.")
    value = html.unescape(value)
    value = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()


def _public_text(value):
    value = _CONTACT.sub("", value)
    return re.sub(r"\s+", " ", value).strip(" ,;—-")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    if isinstance(value, str):
        value = value.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
        if not re.fullmatch(r"\d{1,12}(?:\.\d{1,4})?", value):
            return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _source_url(value):
    value = _text(value, "url", 500)
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"https", "http"} and parsed.hostname in {"krisha.kz", "www.krisha.kz"}
                 and parsed.username is None and parsed.password is None and parsed.port in {None, 80, 443}
                 and re.fullmatch(r"/a/show/\d{6,14}/?", parsed.path)
                 and not re.search(r"[\s\\]", value))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise SellerListingReviewError("Вставьте ссылку вида https://krisha.kz/a/show/123456789.")
    return "https://krisha.kz" + parsed.path.rstrip("/")


def _extract_description(text):
    """Extract explicit numeric labels only; ambiguous or negated labels stay empty."""
    lowered = text.lower()
    found = {}
    patterns = {
        "rooms": rf"(?<!\d)(\d{{1,2}})\s*[-–]?\s*(?:комнат\w*|комн\.?)(?!\w)",
        "area": rf"(?:общая\s+площадь|площадь\s+квартиры|площадь)\s*[:=—-]?\s*{_NUMBER}\s*{_UNIT_AREA}",
        "kitchen_area": rf"(?:площадь\s+кухни|кухня)\s*[:=—-]?\s*{_NUMBER}\s*{_UNIT_AREA}",
        "ceiling_height": rf"(?:высота\s+потолков|потолки)\s*[:=—-]?\s*{_NUMBER}\s*м(?:етра)?(?!\w|[²2])",
        "year_built": r"(?:год\s+постройки|дом\s+построен\s+в|дом\s+сдан\s+в)\s*[:=—-]?\s*(\d{4})(?!\d)",
    }
    for key, pattern in patterns.items():
        values = set()
        for match in re.finditer(pattern, lowered):
            # Avoid reading a negated example as an apartment characteristic.
            prefix = lowered[max(0, match.start() - 15):match.start()]
            if re.search(r"(?:не|без|пример|например)\s*$", prefix):
                continue
            value = _number(match.group(1))
            if value is not None:
                values.add(value)
        if values:
            found[key] = values
    # The standard listing headline has no explicit "общая площадь" label,
    # but an immediately following area belongs to the apartment, not kitchen.
    for match in re.finditer(rf"(?<!\d)\d{{1,2}}\s*[-–]?\s*комнат\w*\s+квартир\w*\s*[,;·—-]?\s*{_NUMBER}\s*{_UNIT_AREA}", lowered):
        prefix = lowered[max(0, match.start() - 15):match.start()]
        if not re.search(r"(?:не|пример|например)\s*$", prefix):
            found.setdefault("area", set()).add(_number(match.group(1)))
    money_pattern = (r"(?:цена(?:\s+квартиры)?|стоимость\s+квартиры)\s*[:=—-]?\s*"
                     r"(\d{1,3}(?:[ \u00a0]\d{3}){1,3}(?:[.,]\d{1,2})?|\d{1,12}(?:[.,]\d{1,2})?)\s*"
                     r"(млн\.?|тыс\.?)?\s*(?:₸|тенге|тг\.?)(?!\w)")
    for match in re.finditer(money_pattern, lowered):
        prefix = lowered[max(0, match.start() - 15):match.start()]
        if re.search(r"(?:не|пример|например)\s*$", prefix):
            continue
        amount = _number(match.group(1))
        if amount is not None:
            multiplier = 1_000_000 if (match.group(2) or "").startswith("млн") else 1_000 if match.group(2) else 1
            found.setdefault("price", set()).add(amount * multiplier)
    floors = set()
    totals = set()
    floor_pattern = (r"(?:этаж\s*[:=]?\s*|на\s+)(\d{1,3})\s*(?:/|из)\s*(\d{1,3})(?!\d)"
                     r"|(?<!\d)(\d{1,3})\s*(?:-?й\s+)?этаж(?:е)?\s+из\s+(\d{1,3})(?!\d)")
    for match in re.finditer(floor_pattern, lowered):
        prefix = lowered[max(0, match.start() - 15):match.start()]
        if re.search(r"(?:не|пример|например)\s*$", prefix):
            continue
        floors.add(float(match.group(1) or match.group(3)))
        totals.add(float(match.group(2) or match.group(4)))
    if not floors:
        for match in re.finditer(r"(?:этаж\s*:\s*(\d{1,3})|(?<!\d)(\d{1,3})\s*(?:-?й\s+)?этаж(?:е)?)(?!\w)", lowered):
            prefix = lowered[max(0, match.start() - 15):match.start()]
            if not re.search(r"(?:не|пример|например)\s*$", prefix):
                floors.add(float(match.group(1) or match.group(2)))
    if floors:
        found["floor"] = floors
    if totals:
        found["floors_total"] = totals
    return found


def _item(code, category, priority, title, evidence, action):
    return {"code": code, "category": category, "priority": priority, "title": title,
            "evidence": evidence, "action": action}


def _label(value):
    return f"{value:g}".replace(".", ",")


def _money(value):
    return f"{round(value):,}".replace(",", " ") + " ₸"


def review_seller_listing(payload: Mapping, *, today: date | None = None) -> dict:
    """Review the user's own advertisement; missing input produces a checklist.

    ``recommendations`` contains at most five prioritized actions, with the rest
    in ``additional_recommendations``. All information remains user/source
    supplied. This routine does not fetch ``url`` or read a database.
    """
    if not isinstance(payload, Mapping):
        raise SellerListingReviewError("Передайте текст объявления или его характеристики.")
    today = today or date.today()
    title = _text(payload.get("title"), "title", 200)
    description = _text(payload.get("description"), "description", MAX_DESCRIPTION_LENGTH)
    source_url = _source_url(payload.get("url"))
    text = title + "\n" + description
    extracted = _extract_description(text)
    facts, evidence, recommendations = {}, {}, []
    limits = {
        "rooms": (0, 20, True), "area": (5, 1000, False), "price": (100_000, 10_000_000_000, False),
        "floor": (1, 100, True), "floors_total": (1, 100, True), "kitchen_area": (1, 200, False),
        "ceiling_height": (1.8, 6, False), "year_built": (1800, today.year + 5, True),
        "utilities_monthly": (0, 2_000_000, False),
    }
    for field, (minimum, maximum, integral) in limits.items():
        supplied = payload.get(field)
        explicit = supplied not in (None, "")
        raw = _number(supplied) if explicit else None
        values = extracted.get(field, set())
        invalid = explicit and (raw is None or not minimum <= raw <= maximum or (integral and not raw.is_integer()))
        ambiguous = len(values) > 1
        conflict = raw is not None and bool(values) and any(abs(value - raw) > .01 for value in values)
        if invalid or ambiguous or conflict:
            recommendations.append(_item(
                "VERIFY_" + field.upper(), "facts", "high", f"Проверьте: {FIELD_LABELS[field].lower()}",
                "В характеристиках и тексте указаны разные значения." if conflict else
                "В тексте указано несколько разных значений." if ambiguous else "Указанное значение вне допустимого диапазона или имеет неверный формат.",
                "Сверьте данные с измерениями или документами и исправьте значение во всех частях объявления.",
            ))
            continue
        if raw is None and len(values) == 1:
            candidate = next(iter(values))
            if minimum <= candidate <= maximum and (not integral or candidate.is_integer()):
                raw = candidate
        if raw is not None:
            facts[field] = int(raw) if integral else round(raw, 2)
            evidence[field] = "provided_fact" if explicit else "explicit_text"
    if "floor" in facts and "floors_total" in facts and facts["floor"] > facts["floors_total"]:
        facts.pop("floor")
        facts.pop("floors_total")
        recommendations.append(_item("FLOOR_CONFLICT", "facts", "high", "Исправьте этажность",
            "Этаж квартиры больше указанной этажности дома.", "Сверьте этаж квартиры и этажность дома; укажите оба значения."))
    if "kitchen_area" in facts and "area" in facts and facts["kitchen_area"] > facts["area"]:
        facts.pop("kitchen_area")
        recommendations.append(_item("KITCHEN_AREA_CONFLICT", "facts", "high", "Исправьте площадь кухни",
            "Площадь кухни больше общей площади квартиры.", "Проверьте единицы измерения и площадь по планировке."))
    private_text_found = bool(_CONTACT.search(text))
    for field, maximum in {"address": 180, "complex_name": 180, "included_items": 400,
                           "viewing_terms": 300, "utilities_period": 80}.items():
        value = _text(payload.get(field), field, maximum)
        private_text_found = private_text_found or bool(_CONTACT.search(value))
        value = _public_text(value)
        if value:
            facts[field] = value
            evidence[field] = "provided_fact"
    finish = payload.get("finish_level")
    if finish is not None and finish != "":
        if not isinstance(finish, str) or finish not in FINISH_LABELS:
            recommendations.append(_item("VERIFY_FINISH_LEVEL", "facts", "medium", "Уточните состояние квартиры",
                "Состояние квартиры не распознано.", FIELD_PROMPTS["finish_level"]))
        else:
            facts["finish_level"] = finish
            evidence["finish_level"] = "provided_fact"
    if isinstance(payload.get("is_owner"), bool):
        facts["is_owner"] = payload["is_owner"]
        evidence["is_owner"] = "provided_fact"
    legal_status = payload.get("legal_status")
    if legal_status not in (None, "", "unknown", "mortgage", "no_mortgage"):
        raise SellerListingReviewError("Выберите известный статус ипотеки или «не указано».")
    if legal_status in {"mortgage", "no_mortgage"}:
        facts["legal_status"] = legal_status
        evidence["legal_status"] = "provided_fact"
    photos_count = payload.get("photos_count", payload.get("photo_count"))
    if photos_count not in (None, ""):
        count = _number(photos_count)
        if count is None or not 0 <= count <= 100 or not count.is_integer():
            raise SellerListingReviewError("Количество фотографий должно быть целым числом от 0 до 100.")
        facts["photos_count"] = int(count)
        evidence["photos_count"] = "provided_fact"
    coverage = payload.get("photo_coverage")
    if coverage is None:
        coverage = {}
    if not isinstance(coverage, Mapping) or any(key not in PHOTO_LABELS for key in coverage):
        raise SellerListingReviewError("Проверьте список фотографий: неизвестный пункт.")
    if any(not isinstance(value, str) or value not in {"present", "missing", "unknown"} for value in coverage.values()):
        raise SellerListingReviewError("Для фотографий выберите «есть», «нет» или «не знаю».")
    photo_checklist = []
    for key, label in PHOTO_LABELS.items():
        status = coverage.get(key, "unknown")
        action = (PHOTO_CHECK_ACTIONS[key] if status == "present" else PHOTO_ACTIONS[key] if status == "missing"
                  else f"Проверьте, есть ли фото: {label.lower()}. Если его нет — " + PHOTO_ACTIONS[key][0].lower() + PHOTO_ACTIONS[key][1:])
        photo_checklist.append({"key": key, "label": label, "status": status, "action": action,
                                "evidence": "seller_self_check" if key in coverage else "not_assessed"})
    if facts.get("photos_count") == 0:
        recommendations.append(_item("NO_PHOTOS", "photos", "high", "Добавьте фотографии квартиры",
            "Вы указали 0 фотографий.", "Начните с общего вида самой светлой комнаты и покажите комнаты, кухню, санузел и планировку."))
        if any(item["status"] == "present" for item in photo_checklist):
            recommendations.append(_item("PHOTO_COUNT_CONFLICT", "photos", "high", "Уточните количество фотографий",
                "Указано 0 фотографий, но некоторые виды отмечены как добавленные.", "Сверьте количество снимков и отметки в списке фотографий."))
    missing_photos = [item for item in photo_checklist if item["status"] == "missing"]
    if missing_photos:
        names = ", ".join(item["label"].lower() for item in missing_photos)
        recommendations.append(_item("PHOTO_COVERAGE", "photos", "high", "Дополните фотографии",
            "Вы отметили как отсутствующие: " + names + ".",
            " ".join(item["action"] for item in missing_photos)))
    elif any(item["status"] == "unknown" for item in photo_checklist):
        recommendations.append(_item("PHOTO_SELF_CHECK", "photos", "medium", "Проверьте, что видно на фотографиях",
            "Содержание фотографий не проверено; часть видов ещё не отмечена.",
            "Отметьте, показаны ли все комнаты, кухня, санузел, планировка, вид из окон и подъезд. Само количество снимков этого не подтверждает."))
    if private_text_found:
        recommendations.append(_item("PERSONAL_DATA", "privacy", "high", "Уберите личные данные из описания",
            "В переданном тексте обнаружены контакты, ссылки или длинные личные номера.",
            "Оставьте связь через контактное поле площадки. Закройте ИИН, подписи и номера документов на фото; в подготовленный текст контакты не перенесены."))
    words = re.findall(r"[\w-]+", description, flags=re.U)
    if len(words) < 15:
        recommendations.append(_item("SHORT_DESCRIPTION", "description", "medium", "Добавьте конкретное описание",
            "Описание не заполнено." if not description else "В описании меньше 15 слов.",
            "Кратко опишите состояние, планировку, что остаётся покупателю, расходы и условия просмотра. Укажите факты, которые можно проверить."))
    letters = [letter for letter in text if letter.isalpha()]
    if len(letters) >= 20 and sum(letter.isupper() for letter in letters) / len(letters) > .7:
        recommendations.append(_item("ALL_CAPS", "description", "low", "Сделайте текст удобнее для чтения",
            "Большая часть текста написана заглавными буквами.", "Используйте обычный регистр и короткие абзацы с одним фактом в каждом."))
    if re.search(r"\b(?:лучш\w*|идеальн\w*|уникальн\w*|элитн\w*)\b", text, re.I):
        recommendations.append(_item("VAGUE_CLAIMS", "description", "low", "Подкрепите общие оценки фактами",
            "В тексте есть общие превосходные оценки квартиры.",
            "Вместо «лучшая» или «идеальная» укажите измерения, состав ремонта и особенности планировки, которые действительно есть."))
    missing_fields = []
    for field, label in FIELD_LABELS.items():
        if field in facts:
            continue
        missing_fields.append({"key": field, "label": label, "prompt": FIELD_PROMPTS[field]})
    essential = [field for field in ("rooms", "area", "price", "address") if field not in facts]
    if essential:
        recommendations.append(_item("ESSENTIAL_FACTS", "facts", "high", "Добавьте основные характеристики",
            "В переданных данных не указаны: " + ", ".join(FIELD_LABELS[field].lower() for field in essential) + ".",
            " ".join(FIELD_PROMPTS[field] for field in essential)))
    condition_missing = [field for field in ("floor", "floors_total", "finish_level") if field not in facts]
    if condition_missing:
        recommendations.append(_item("CONDITION_AND_FLOOR", "facts", "medium", "Уточните состояние и этаж",
            "В переданных данных не указаны: " + ", ".join(FIELD_LABELS[field].lower() for field in condition_missing) + ".",
            "Укажите этаж и этажность, состояние ремонта и известные работы, которые потребуются покупателю."))
    dimensions_missing = [field for field in ("kitchen_area", "ceiling_height", "year_built") if field not in facts]
    if dimensions_missing:
        recommendations.append(_item("MEASUREMENTS", "facts", "low", "Дополните параметры квартиры и дома",
            "В переданных данных не указаны: " + ", ".join(FIELD_LABELS[field].lower() for field in dimensions_missing) + ".",
            " ".join(FIELD_PROMPTS[field] for field in dimensions_missing)))
    if "utilities_monthly" not in facts or "utilities_period" not in facts:
        recommendations.append(_item("UTILITIES", "facts", "medium", "Покажите коммунальные расходы",
            "Не указана сумма коммунальных расходов с периодом." if "utilities_monthly" not in facts else "Сумма есть, но не указан месяц или период.",
            FIELD_PROMPTS["utilities_monthly"]))
    if "included_items" not in facts:
        recommendations.append(_item("INCLUDED_ITEMS", "description", "medium", "Напишите, что входит в цену",
            "В переданных характеристиках нет списка остающейся мебели и техники.", FIELD_PROMPTS["included_items"]))
    if "viewing_terms" not in facts:
        recommendations.append(_item("VIEWING_TERMS", "description", "low", "Объясните, как посмотреть квартиру",
            "Условия просмотра не указаны в переданных характеристиках.", FIELD_PROMPTS["viewing_terms"]))
    if "legal_status" not in facts:
        recommendations.append(_item("DOCUMENT_INFORMATION", "facts", "medium", "Уточните информацию для сделки",
            "Статус ипотеки в переданных характеристиках не указан.", FIELD_PROMPTS["legal_status"]))
    recommendations.sort(key=lambda item: _PRIORITIES[item["priority"]])
    generated_title, generated_description = _draft(facts)
    required = ("rooms", "area", "price", "address", "floor", "floors_total", "finish_level")
    notes = [
        "Проверка основана на переданном тексте, характеристиках и ваших отметках; содержание фотографий не анализировалось.",
        "Подготовленный текст использует только указанные характеристики. Проверьте его перед публикацией и дополните неизвестные сведения.",
        "Достоверность документов, отсутствие долгов, рыночная цена и срок продажи этой проверкой не установлены.",
    ]
    if source_url and not description and not title and not facts:
        notes.insert(0, "Ссылка сама по себе не содержит характеристик: вставьте описание или заполните данные квартиры.")
    return {"version": VERSION, "method": "rules", "source_url": source_url, "facts": facts,
            "fact_evidence": {field: source for field, source in evidence.items() if field in facts},
            "recommendations": recommendations[:5], "additional_recommendations": recommendations[5:],
            "recommendation_count": len(recommendations), "photo_checklist": photo_checklist,
            "missing_fields": missing_fields, "suggested_title": generated_title,
            "suggested_description": generated_description, "notes": notes,
            "coverage": {"known": sum(field in facts for field in required), "required": len(required),
                         "photos_content_checked": False}}


def _draft(facts):
    """A fresh compact draft, excluding unknowns and unverified prose claims."""
    if "rooms" in facts:
        subject = "Студия" if facts["rooms"] == 0 else f"{facts['rooms']}-комнатная квартира"
    else:
        subject = "Квартира"
    title = [subject]
    if "area" in facts:
        title.append(_label(facts["area"]) + " м²")
    place = facts.get("address") or facts.get("complex_name")
    if place:
        title.append(place)
    lines = []
    if any(field in facts for field in ("rooms", "area", "address", "complex_name")):
        parts = [subject[0].lower() + subject[1:], *title[1:]]
        lines.append("Продаётся " + ", ".join(parts) + ".")
    if facts.get("complex_name") and facts.get("address"):
        lines.append("ЖК «" + facts["complex_name"] + "».")
    if "floor" in facts:
        floor = str(facts["floor"]) + (" из " + str(facts["floors_total"]) if "floors_total" in facts else "")
        lines.append("Этаж: " + floor + ".")
    elif "floors_total" in facts:
        lines.append("В доме " + str(facts["floors_total"]) + " этажей.")
    if "kitchen_area" in facts:
        lines.append("Площадь кухни: " + _label(facts["kitchen_area"]) + " м².")
    if "ceiling_height" in facts:
        lines.append("Высота потолков: " + _label(facts["ceiling_height"]) + " м.")
    if "finish_level" in facts:
        lines.append("Состояние: " + FINISH_LABELS[facts["finish_level"]] + ".")
    if "year_built" in facts:
        lines.append("Год постройки дома: " + str(facts["year_built"]) + ".")
    if "included_items" in facts:
        lines.append("В цену входит: " + facts["included_items"].rstrip(".") + ".")
    if "utilities_monthly" in facts and "utilities_period" in facts:
        lines.append("Коммунальные расходы за " + facts["utilities_period"] + ": " + _money(facts["utilities_monthly"]) + ".")
    if "price" in facts:
        lines.append("Цена: " + _money(facts["price"]) + ".")
    if facts.get("legal_status") == "mortgage":
        lines.append("Квартира в ипотеке — по информации продавца. Условия сделки требуется уточнить.")
    elif facts.get("legal_status") == "no_mortgage":
        lines.append("Продавец указывает, что квартира не в ипотеке. Документы и обременения проверяются перед сделкой.")
    if "viewing_terms" in facts:
        lines.append("Просмотр: " + facts["viewing_terms"].rstrip(".") + ".")
    return ", ".join(title), "\n".join(lines)
