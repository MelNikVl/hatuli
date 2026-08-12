"""
Entity resolution, фаза 1 (ЖК) — см. docs/entity_resolution_plan.md.

Единый постоянный ID объекта — complexes.id (PK, без семантики). Этот
модуль:
  - генерирует человеко-читаемый code (JK-000123) для отображения в UI;
  - скорит уверенность связи "источник -> ЖК" по сигналам (имя точное/
    похожее/гео/застройщик/адрес) и пишет её в complex_source_links (spine)
    ЛИБО в очередь на проверку (complex_source_link_candidates), в
    зависимости от confidence;
  - помнит руками отклонённые пары (complex_source_link_rejections) —
    не предлагает их повторно;
  - конфликт (source_id уже привязан к другому ЖК) не перезаписывает
    молча — уходит в очередь как kind='conflict'.

Ревью 2026-08-13 (см. коммит) добавило: fuzzy-ступень имени (pg_trgm
similarity), сигнал адреса, реальное разделение auto-match/review-queue
по факту хранения (раньше и то, и другое одинаково писалось в спайн),
роутинг конфликтов в очередь вместо тихой перезаписи, память отклонений.

Ревью 2026-08-12 (калибровка на живом homeportal-прогоне, см.
docs/entity_resolution_plan.md) добавило сигнал номера очереди/фазы:
"Дармен 2" и "Дармен 1" — разные корпуса одного застройщика в 150 м
друг от друга, гео+застройщик одни без сигнала фазы протаскивали их в
auto как один ЖК. Извлекаем номер очереди из имени (см. _phase_token) —
у обеих сторон он есть и он РАЗНЫЙ -> потолок confidence 0.79; ОБЕ
стороны и токен РАВНЫЙ -> небольшой бонус.

Тот же день, вторая калибровка (sibling-sweep на живом newbuild-
прогоне): "токен только с одной стороны -> нейтрально" не ловило
доминирующий в реальных данных паттерн — первую очередь почти никогда
не подписывают номером вовсе ("Nur Aspan"/"Nur Aspan 2", НЕ "Nur Aspan
1"/"Nur Aspan 2"). 6 из 7 реальных пар "база+номер" в проде проходили
в auto именно поэтому. Теперь при токене с одной стороны сравниваем
"голую" сторону с базой номерованной (той же строкой без суффикса
фазы) — совпадает -> это неявная первая фаза, сравниваем как обычно
(1 vs N); не совпадает -> цифра, похоже, не про фазу, остаёмся
нейтральны, как раньше.

Фаза 2 (юниты — unit_source_links, тот же spine на уровне
newbuild_units.id) зафиксирована в плане, но НЕ реализуется здесь —
ждём, пока фаза 1 покажет уровень шума автоматического матчинга в проде.
"""
from __future__ import annotations

import math
import re

# Пороги авто-матч / очередь на ручную проверку / не создавать связь
# вовсе — согласовано в плане (docs/entity_resolution_plan.md).
AUTO_MATCH_THRESHOLD = 0.8
REVIEW_QUEUE_THRESHOLD = 0.5

# Веса сигналов. Имя — не бинарно (точное/непохоже), а по шкале схожести:
# 1.0 (точное совпадение) даёт полный _W_NAME_EXACT; в диапазоне
# [FUZZY_NAME_THRESHOLD, 1.0) — fuzzy-ступень, вес линейно уменьшается от
# _W_NAME_EXACT к _W_NAME_FUZZY_MIN (частичное доверие — рядом, но не точно,
# типичный случай ребрендинга/опечатки на одном из сайтов-источников).
_W_NAME_EXACT = 0.6
_W_NAME_FUZZY_MIN = 0.35
FUZZY_NAME_THRESHOLD = 0.55  # ниже — считаем разными именами, сигнал 0
_W_GEO = 0.25
_W_DEVELOPER = 0.15
_W_ADDRESS = 0.15
GEO_MATCH_RADIUS_M = 150.0

# Токен очереди/фазы: расходящийся токен не даёт confidence уйти в auto
# (0.79 < AUTO_MATCH_THRESHOLD), но и не гасит совпадение целиком — есть
# и другие валидные сигналы, решение просто уходит человеку в очередь.
PHASE_MISMATCH_CAP = 0.79
_W_PHASE_BONUS = 0.1


def generate_complex_code(complex_id: int) -> str:
    """JK-000123 — только на отображение, семантики источника/застройщика
    в коде нет (те ребрендятся, транслит плодит дубли)."""
    return f"JK-{complex_id:06d}"


async def ensure_complex_code(complex_id: int) -> str:
    """Идемпотентно проставляет code, если его ещё нет."""
    from bot.db.pg import fetchval, execute
    code = await fetchval("SELECT code FROM complexes WHERE id = $1", complex_id)
    if code:
        return code
    code = generate_complex_code(complex_id)
    await execute("UPDATE complexes SET code = $2 WHERE id = $1", complex_id, code)
    return code


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def name_similarity(name_a: str, name_b: str) -> float:
    """Триграммное сходство имён через pg_trgm (0..1, 1 = идентичны после
    normalize). Считаем в БД (SELECT similarity(...)), не в Python —
    та же функция, что использовалась бы для GIN-индексного поиска
    кандидатов, результат должен совпадать 1-в-1."""
    if not name_a or not name_b:
        return 0.0
    if name_a.strip().lower() == name_b.strip().lower():
        return 1.0
    from bot.db.pg import fetchval
    val = await fetchval("SELECT similarity(lower(trim($1)), lower(trim($2)))", name_a, name_b)
    return float(val or 0.0)


_ADDR_NOISE = [
    "рк,", "г. астана", "г.астана", "астана г.", "астана,", "район ", "р-н ",
    "жилой массив", "ж/м", "мкр", "мкр.", "проспект", "пр.", "улица", "ул.",
    "переулок", "пер.",
]


def _normalize_address(addr: str) -> set[str]:
    s = (addr or "").lower()
    for token in _ADDR_NOISE:
        s = s.replace(token, " ")
    return {t for t in s.replace(",", " ").split() if len(t) > 1}


def address_match(addr_a: str | None, addr_b: str | None) -> bool | None:
    """Грубое, но дешёвое сравнение адресов: после вырезания шумных слов
    (город/район/тип улицы) сравниваем оставшиеся токены (название улицы,
    номер дома) — пересечение >= 50% значимых токенов одной из сторон
    считаем совпадением. None, если сравнивать нечего (адреса пустые) —
    сигнал просто не участвует, не штрафуем."""
    if not addr_a or not addr_b:
        return None
    ta, tb = _normalize_address(addr_a), _normalize_address(addr_b)
    if not ta or not tb:
        return None
    overlap = len(ta & tb)
    return overlap / min(len(ta), len(tb)) >= 0.5


_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
_ROMAN_SYMBOLS = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
                  (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
                  (5, "V"), (4, "IV"), (1, "I")]


def _int_to_roman(n: int) -> str:
    out = []
    for v, sym in _ROMAN_SYMBOLS:
        while n >= v:
            out.append(sym)
            n -= v
    return "".join(out)


def _roman_to_int(token: str) -> int | None:
    """None, если это не валидная римская запись — round-trip через
    _int_to_roman отсекает случайные слова из тех же букв (например
    "MIX": буквы m/i/x все "римские", но это не корректная запись)."""
    s = token.lower()
    if not s or any(ch not in _ROMAN_VALUES for ch in s):
        return None
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN_VALUES[ch]
        total += -v if v < prev else v
        prev = max(prev, v)
    if 0 < total <= 49 and _int_to_roman(total) == s.upper():
        return total
    return None


# "2-я очередь" / "2 я очередь" — цифра перед словом
_PHASE_QUEUE_BEFORE_RE = re.compile(r"(\d{1,3})\s*-?\s*я\s*очеред[ьи]")
# "2 очередь" — цифра перед словом, без "-я"
_PHASE_QUEUE_NUM_RE = re.compile(r"(\d{1,3})\s*очеред[ьи]")
# "очередь 2" / "очередь № 2"
_PHASE_QUEUE_AFTER_RE = re.compile(r"очеред[ьи]\s*[№#]?\s*(\d{1,3})")
_PHASE_TRAILING_JUNK_RE = re.compile(r'["\'\)\]»,.]+$')
# хвостовой номер: "Дармен - 2", "Dastur 2" — только цифра как последний
# токен (не часть слова типа "Ishim C3", там перед цифрой нет пробела/тире)
_PHASE_TRAILING_NUM_RE = re.compile(r"(?:^|[\s\-])(\d{1,3})$")
_PHASE_TRAILING_ROMAN_RE = re.compile(r"(?:^|[\s\-])([ivxlcdmIVXLCDM]{1,7})$")


def _phase_token(name: str) -> tuple[str | None, str]:
    """(номер_очереди/фазы_или_None, база_без_суффикса_фазы). Номер —
    эвристика, не гарантия (иногда цифра в имени часть бренда, не фаза):
    "N-я очередь"/"очередь N" (в любом месте строки, включая внутри
    скобок — работает на СЫРОМ имени, вызывающая сторона не обязана
    заранее вырезать скобки/приставку "ЖК" — как раз наоборот, для этого
    сигнала сырой текст и нужен, иначе "Дармен (2 очередь)" после
    агрессивной нормализации имени превращается в просто "дармен" и
    токен теряется), хвостовой номер, хвостовые римские цифры
    (валидируются round-trip'ом, см. _roman_to_int). "База" — имя с
    вырезанным суффиксом фазы, нужна score_match() для калибровки-2026-
    08-12: сравнивать "голую" сторону ("Nur Aspan") с базой номерованной
    ("Nur Aspan 2" -> "Nur Aspan") — первую очередь почти никогда не
    подписывают номером вовсе, так что сама по себе "нет токена" не
    значит "не про фазу"."""
    if not name:
        return None, ""
    s = name.lower()
    for pat in (_PHASE_QUEUE_BEFORE_RE, _PHASE_QUEUE_NUM_RE, _PHASE_QUEUE_AFTER_RE):
        m = pat.search(s)
        if m:
            base = (s[:m.start()] + s[m.end():]).strip(" -.,")
            return str(int(m.group(1))), base
    tail = _PHASE_TRAILING_JUNK_RE.sub("", s).strip()
    m = _PHASE_TRAILING_NUM_RE.search(tail)
    if m:
        return str(int(m.group(1))), tail[:m.start()].strip(" -.,")
    m = _PHASE_TRAILING_ROMAN_RE.search(tail)
    if m:
        n = _roman_to_int(m.group(1))
        if n is not None:
            return str(n), tail[:m.start()].strip(" -.,")
    return None, tail


async def score_match(
    name_a: str, name_b: str, *,
    existing_lat: float | None = None, existing_lon: float | None = None,
    candidate_lat: float | None = None, candidate_lon: float | None = None,
    developer_match: bool | None = None,
    existing_address: str | None = None, candidate_address: str | None = None,
    name_a_full: str | None = None, name_b_full: str | None = None,
) -> tuple[float, str]:
    """Считает confidence + human-readable match_method по сигналам,
    которые реально удалось проверить (сигнал без данных просто не
    участвует — не штрафуем и не выдумываем). Имя — единственный
    обязательный сигнал (без него нет базы для сравнения вовсе).

    name_a/name_b — то, что реально идёт в pg_trgm similarity (вызывающая
    сторона вправе заранее подчистить их для лучшего fuzzy-сравнения,
    как это делает homeportal_scan.py через norm_name()). name_a_full/
    name_b_full — опционально, СЫРЫЕ имена для извлечения токена
    очереди/фазы (_phase_token) — если не переданы, для этого сигнала
    используются name_a/name_b как есть (ок для источников, которые и
    так передают сырые имена без предварительной чистки, см.
    newbuild_common.ensure_complex)."""
    sim = await name_similarity(name_a, name_b)
    if sim >= 1.0:
        score, parts = _W_NAME_EXACT, ["name_exact"]
    elif sim >= FUZZY_NAME_THRESHOLD:
        # линейная интерполяция веса между порогом (min) и точным (exact)
        t = (sim - FUZZY_NAME_THRESHOLD) / (1.0 - FUZZY_NAME_THRESHOLD)
        score = _W_NAME_FUZZY_MIN + t * (_W_NAME_EXACT - _W_NAME_FUZZY_MIN)
        parts = [f"name_fuzzy({sim:.2f})"]
    else:
        return 0.0, "no_match"

    geo_ok = (existing_lat is not None and existing_lon is not None
              and candidate_lat is not None and candidate_lon is not None)
    if geo_ok:
        dist = _haversine_m(existing_lat, existing_lon, candidate_lat, candidate_lon)
        if dist <= GEO_MATCH_RADIUS_M:
            score += _W_GEO
            parts.append("geo")

    if developer_match:
        score += _W_DEVELOPER
        parts.append("developer")

    addr_ok = address_match(existing_address, candidate_address)
    if addr_ok:
        score += _W_ADDRESS
        parts.append("address")

    score = min(score, 1.0)
    phase_a, base_a = _phase_token(name_a_full or name_a)
    phase_b, base_b = _phase_token(name_b_full or name_b)
    cap = None
    if phase_a is not None and phase_b is not None:
        if phase_a == phase_b:
            score = min(score + _W_PHASE_BONUS, 1.0)
            parts.append(f"phase({phase_a})")
        else:
            cap = PHASE_MISMATCH_CAP
            parts.append(f"phase_mismatch({phase_a}!={phase_b})")
    elif phase_a is not None or phase_b is not None:
        # ровно у одной стороны явный номер. Калибровка 2026-08-12 (живой
        # прогон newbuild): первую очередь почти никогда не подписывают
        # номером вовсе ("Nur Aspan" / "Nur Aspan 2" — не "Nur Aspan 1"),
        # так что "нет токена" — это НЕ то же самое, что "цифра не про
        # фазу". Проверяем: "голая" сторона совпадает с базой номерованной
        # (той же строкой без суффикса фазы)? Если да — это неявная
        # первая фаза, сравниваем как обычно; если нет — цифра, похоже,
        # правда часть бренда, остаёмся нейтральны.
        bare_base, numbered_token, numbered_base = (
            (base_a, phase_b, base_b) if phase_a is None else (base_b, phase_a, base_a))
        base_sim = await name_similarity(bare_base, numbered_base) if bare_base and numbered_base else 0.0
        if base_sim >= 0.8:
            if numbered_token == "1":
                score = min(score + _W_PHASE_BONUS, 1.0)
                parts.append("phase(1~implicit)")
            else:
                cap = PHASE_MISMATCH_CAP
                parts.append(f"phase_mismatch(1~implicit!={numbered_token})")
        # иначе — нейтрально, не трогаем score (цифра, похоже, не про фазу)

    if cap is not None:
        score = min(score, cap)
    return round(score, 2), "+".join(parts)


async def record_source_link(
    complex_id: int, source: str, source_id: str, *,
    confidence: float, method: str, url: str | None = None,
    matched_by: str = "auto",
) -> str:
    """Пишет связь — auto (>= AUTO_MATCH_THRESHOLD) сразу в spine
    (complex_source_links); review (>= REVIEW_QUEUE_THRESHOLD, < auto) —
    в очередь на подтверждение (complex_source_link_candidates), НЕ в
    спайн, пока кто-то не approve; ниже REVIEW_QUEUE_THRESHOLD — не
    создаём ничего. Конфликт (source_id уже привязан к ДРУГОМУ complex_id)
    не перезаписывает существующую связь молча — тоже уходит в очередь
    (kind='conflict'), независимо от confidence нового варианта. Пары,
    которые уже руками отклонили (complex_source_link_rejections), больше
    не предлагаются вовсе.

    Возвращает: 'auto' | 'review' | 'conflict' | 'rejected' | 'skipped'."""
    if confidence < REVIEW_QUEUE_THRESHOLD:
        return "skipped"
    from bot.db.pg import fetchrow, execute

    rejected = await fetchrow(
        "SELECT 1 FROM complex_source_link_rejections WHERE source=$1 AND source_id=$2 AND complex_id=$3",
        source, str(source_id), complex_id)
    if rejected:
        return "rejected"

    existing = await fetchrow(
        "SELECT complex_id FROM complex_source_links WHERE source=$1 AND source_id=$2",
        source, str(source_id))
    if existing and existing["complex_id"] != complex_id:
        await execute("""
            INSERT INTO complex_source_link_candidates
                (complex_id, source, source_id, url, match_method, confidence, kind, conflict_with_complex_id)
            VALUES ($1, $2, $3, $4, $5, $6, 'conflict', $7)
            ON CONFLICT (source, source_id, complex_id) DO UPDATE SET
                confidence = EXCLUDED.confidence, match_method = EXCLUDED.match_method
        """, complex_id, source, str(source_id), url, method, confidence, existing["complex_id"])
        return "conflict"

    if is_auto_match(confidence):
        await execute("""
            INSERT INTO complex_source_links
                (complex_id, source, source_id, url, match_method, confidence, matched_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (source, source_id) DO NOTHING
        """, complex_id, source, str(source_id), url, method, confidence, matched_by)
        return "auto"

    await execute("""
        INSERT INTO complex_source_link_candidates
            (complex_id, source, source_id, url, match_method, confidence, kind)
        VALUES ($1, $2, $3, $4, $5, $6, 'review')
        ON CONFLICT (source, source_id, complex_id) DO UPDATE SET confidence = EXCLUDED.confidence
    """, complex_id, source, str(source_id), url, method, confidence)
    return "review"


async def approve_candidate(candidate_id: int, approved_by: str = "admin") -> None:
    """Подтвердить кандидата (review или conflict) — переносит связь в
    спайн. Для conflict: перезаписывает существующую связь (осознанно,
    не молча — через это действие)."""
    from bot.db.pg import fetchrow, execute
    c = await fetchrow("SELECT * FROM complex_source_link_candidates WHERE id = $1", candidate_id)
    if not c:
        return
    await execute("""
        INSERT INTO complex_source_links
            (complex_id, source, source_id, url, match_method, confidence, matched_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (source, source_id) DO UPDATE SET
            complex_id = EXCLUDED.complex_id, url = EXCLUDED.url,
            match_method = EXCLUDED.match_method, confidence = EXCLUDED.confidence,
            matched_by = EXCLUDED.matched_by
    """, c["complex_id"], c["source"], c["source_id"], c["url"], c["match_method"],
        c["confidence"], approved_by)
    await execute("DELETE FROM complex_source_link_candidates WHERE id = $1", candidate_id)


async def reject_candidate(candidate_id: int, rejected_by: str = "admin") -> None:
    """Отклонить кандидата — запоминает пару (память отклонений), больше
    не предложится тем же parser-прогоном."""
    from bot.db.pg import fetchrow, execute
    c = await fetchrow("SELECT * FROM complex_source_link_candidates WHERE id = $1", candidate_id)
    if not c:
        return
    await execute("""
        INSERT INTO complex_source_link_rejections (source, source_id, complex_id, rejected_by)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (source, source_id, complex_id) DO NOTHING
    """, c["source"], c["source_id"], c["complex_id"], rejected_by)
    await execute("DELETE FROM complex_source_link_candidates WHERE id = $1", candidate_id)


def is_auto_match(confidence: float) -> bool:
    return confidence >= AUTO_MATCH_THRESHOLD


def is_review_queue(confidence: float) -> bool:
    return REVIEW_QUEUE_THRESHOLD <= confidence < AUTO_MATCH_THRESHOLD
