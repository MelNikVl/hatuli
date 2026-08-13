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

import json
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


# Транслитерация кириллица->латиница (задача гейта 2, п.5 — калибровка
# 2026-08-12 нашла Tandau/Тандау: то же имя в двух алфавитах, pg_trgm
# сравнивает буквы посимвольно и не видит их похожими вовсе (0.0),
# сигнал имени гас бы даже при идентичном произношении). Приближённая,
# не ГОСТ-таблица — практическая транслитерация, как она реально
# встречается в названиях ЖК (рус. + казахские буквы). Латинские буквы
# в исходной строке проходят маппинг без изменений (identity), так что
# применение к уже латинскому имени — no-op, безопасно гонять на обеих
# сторонах без предварительной детекции алфавита.
_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # казахские буквы
    "і": "i", "ә": "a", "ғ": "g", "қ": "q", "ң": "n", "ө": "o", "ұ": "u",
    "ү": "u", "һ": "h",
}


def transliterate(s: str) -> str:
    return "".join(_CYR_TO_LAT.get(ch, ch) for ch in (s or "").lower())


_ADDR_NOISE = [
    "рк,", "г. астана", "г.астана", "астана г.", "астана,", "район ", "р-н ",
    "жилой массив", "ж/м", "мкр", "мкр.", "проспект", "пр.", "улица", "ул.",
    "переулок", "пер.",
    # Найдено 2026-08-12 (unravel_blobs.py, Parkland F): "р." (district,
    # напр. "р. Сарыарка") и "уч."/"участок" (номер участка) — administrative
    # boilerplate, встречается почти в КАЖДОМ адресе этого формата
    # независимо от улицы. Без них две ЗАВЕДОМО разные улицы ("Бейбарыс
    # Сұлтан" vs "С 902") делили 3 общих шумовых токена из 4 значимых
    # у короткой стороны — 75% overlap, ложный match. "р." — только с
    # точкой и пробелом (не "район "/"р-н " — те уже в списке выше, а "р."
    # само по себе не задевает содержательные токены вроде "русановка").
    "р. ", "уч.", "участок",
    # Название района САМО по себе — тоже слишком грубый сигнал (один
    # район покрывает десятки улиц): без него Parkland F всё равно матчился
    # (единственный оставшийся общий токен "сарыарка" при коротком адресе
    # с одной стороны — 1 из 2 значимых токенов, 50%, всё ещё "match").
    # Районы Астаны, оба варианта написания, где встречались.
    "есиль", "есільский", "есильский", "сарыарка", "сарыаркинский",
    "алматы", "алматинский", "байконур", "байқоңыр", "нура", "нұра",
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


# "позиция"/"позиции" — синоним "очереди" у некоторых источников
# (найдено 2026-08-12 на живой паре 'Bagystan (очередь 3/4)' vs
# 'Bagystan (позиция 12)': без этого синонима "позиция 12" не давала
# токен вообще, generic хвостовой фоллбэк ниже по ошибке цеплял
# последнее число В АДРЕСЕ ("уч. 38"), не номер позиции). "N-я очередь" /
# "N я очередь" — цифра перед словом.
_PHASE_QUEUE_MARKER = r"(?:очеред[ьи]|позици[июя])"
_PHASE_QUEUE_BEFORE_RE = re.compile(rf"(\d{{1,3}})\s*-?\s*я\s*{_PHASE_QUEUE_MARKER}")
# "2 очередь" — цифра перед словом, без "-я"
_PHASE_QUEUE_NUM_RE = re.compile(rf"(\d{{1,3}})\s*{_PHASE_QUEUE_MARKER}")
# "очередь 2" / "очередь № 2" / "позиция 12"
_PHASE_QUEUE_AFTER_RE = re.compile(rf"{_PHASE_QUEUE_MARKER}\s*[№#]?\s*(\d{{1,3}})")
_PHASE_TRAILING_JUNK_RE = re.compile(r'["\'\)\]»,.]+$')
# хвостовой номер: "Дармен - 2", "Dastur 2" — только цифра как последний
# токен (не часть слова типа "Ishim C3", там перед цифрой нет пробела/тире)
_PHASE_TRAILING_NUM_RE = re.compile(r"(?:^|[\s\-])(\d{1,3})$")
_PHASE_TRAILING_ROMAN_RE = re.compile(r"(?:^|[\s\-])([ivxlcdmIVXLCDM]{1,7})$")
# Буквенный блок/корпус — та же фаза-по-смыслу, что и номер очереди,
# просто литерой вместо цифры ("Family Nest F" vs "Family Nest" —
# калибровка 2026-08-12, живой sweep нашёл этот кейс). Со словом-маркером
# ("F блок"/"блок F"/"корпус F"/"литера F") регистр буквы не важен —
# маркер сам достаточно однозначен. Без маркера (голая хвостовая буква)
# регистр ВАЖЕН и проверяется отдельно на исходном (не lower-нутом)
# тексте — иначе "Paris" (заканчивается на "s") или любое другое слово
# ловится как "блок s". Это и есть валидация по формату (аналог
# round-trip у римских цифр: не любая хвостовая буква — токен, только
# отдельным словом И заглавная).
_BLOCK_LETTER_BEFORE_RE = re.compile(r"\b([a-zа-яё])\s+(?:блок|корпус)[а-яё]*\b", re.I)
_BLOCK_LETTER_AFTER_RE = re.compile(r"\b(?:блок|корпус|литера)[а-яё]*\s+([a-zа-яё])\b", re.I)
_BLOCK_LETTER_TRAILING_RE = re.compile(r"(?:^|[\s\-])([A-ZА-ЯЁ])$")
# Калибровка 2026-08-12 (docs/entity_resolution_plan.md — политика
# гранулярности): "пятно"/"квартал" — административные/пермитные
# обозначения участка, НЕ маркетинговый блок-идентификатор. Один
# крупный проект может иметь много разных пятен/кварталов и это
# ОДИН комплекс, не повод для расщепления. Перечисление номеров
# ("блоки 1, 2, 3", "пятна 6, 9") — тоже не про конкретный блок, а
# про диапазон, тоже НЕ разграничитель (без этой проверки generic
# хвостовой номер ловил последнее число списка как токен — баг,
# найденный на "AUSTRIA" (блоки 1, 2, 3)" -> ошибочно '3').
_PYATNO_KVARTAL_RE = re.compile(r"\b(пятн[оа]|квартал)[а-яё]*\s*[№#]?\s*(\d|[ivxlcdmIVXLCDM]{1,7}\b)")
_ENUM_LIST_RE = re.compile(r"\d+\s*,\s*\d+")
# "жк"/"мжк"-приставка маркетплейсов + обёрточные кавычки — база нужна
# ИМЕННО для сравнения с "голой" стороной у чужого источника (у той их,
# как правило, уже нет), иначе 'жк "darmen' vs 'darmen' занижает trgm
# similarity шумом, а не реальным несовпадением имени.
_BASE_MARKETPLACE_PREFIX_RE = re.compile(r'^(жк|мжк)[\s"\'\-:]*')


def _clean_base(base: str) -> str:
    b = _BASE_MARKETPLACE_PREFIX_RE.sub("", base)
    return b.strip(' "\'()').strip()


def _phase_token(name: str) -> tuple[str | None, str]:
    """(номер_очереди/фазы_или_None, база_без_суффикса_фазы). Номер —
    эвристика, не гарантия (иногда цифра в имени часть бренда, не фаза):
    "N-я очередь"/"очередь N" (в любом месте строки, включая внутри
    скобок — работает на СЫРОМ имени, вызывающая сторона не обязана
    заранее вырезать скобки/приставку "ЖК" — как раз наоборот, для этого
    сигнала сырой текст и нужен, иначе "Дармен (2 очередь)" после
    агрессивной нормализации имени превращается в просто "дармен" и
    токен теряется), хвостовой номер, хвостовые римские цифры
    (валидируются round-trip'ом, см. _roman_to_int), буквенный блок/
    корпус ("F блок"/"блок F"/"Family Nest F" — тот же класс сигнала,
    только литерой; см. калибровку 2026-08-12 в docs/
    entity_resolution_plan.md). Токен вида "block:f" никогда не
    совпадёт с числовым — сравнение в score_match() просто строковое,
    типы токенов различать не нужно. "База" — имя с вырезанным суффиксом
    фазы, нужна score_match() для калибровки-2026-08-12: сравнивать
    "голую" сторону ("Nur Aspan") с базой номерованной ("Nur Aspan 2" ->
    "Nur Aspan") — первую очередь/блок почти никогда не подписывают
    явно, так что сама по себе "нет токена" не значит "не про фазу"."""
    if not name:
        return None, ""
    s = name.lower()
    if _PYATNO_KVARTAL_RE.search(s) or _ENUM_LIST_RE.search(s):
        # "пятно"/"квартал" — не маркетинговый блок, а пермитный участок
        # (много пятен = один проект); перечисление номеров — про
        # диапазон, не про конкретный блок. Ни то ни другое не
        # разграничитель — намеренно НЕ пытаемся вытащить token из
        # остатка строки (иначе хвостовой fallback ниже словил бы
        # последнее число списка как будто это чистый номер).
        tail = _PHASE_TRAILING_JUNK_RE.sub("", s).strip()
        return None, _clean_base(tail)
    for pat in (_PHASE_QUEUE_BEFORE_RE, _PHASE_QUEUE_NUM_RE, _PHASE_QUEUE_AFTER_RE):
        m = pat.search(s)
        if m:
            base = (s[:m.start()] + s[m.end():]).strip(" -.,")
            return str(int(m.group(1))), _clean_base(base)
    for pat in (_BLOCK_LETTER_BEFORE_RE, _BLOCK_LETTER_AFTER_RE):
        m = pat.search(s)
        if m:
            base = (s[:m.start()] + s[m.end():]).strip(" -.,")
            return f"block:{m.group(1).lower()}", _clean_base(base)
    tail = _PHASE_TRAILING_JUNK_RE.sub("", s).strip()
    m = _PHASE_TRAILING_NUM_RE.search(tail)
    if m:
        return str(int(m.group(1))), _clean_base(tail[:m.start()].strip(" -.,"))
    m = _PHASE_TRAILING_ROMAN_RE.search(tail)
    if m:
        n = _roman_to_int(m.group(1))
        if n is not None:
            return str(n), _clean_base(tail[:m.start()].strip(" -.,"))
    # хвостовая ОДИНОЧНАЯ буква — без маркера ("блок"/"корпус") регистр
    # решает: заглавная в исходном (не lower-нутом) тексте похожа на
    # обозначение блока ("Family Nest F"), строчная — почти наверняка
    # обычный конец слова, не трогаем вовсе (нет надёжного способа
    # отличить "и" как последнее слово смыслового имени от буквы-блока).
    raw_tail = _PHASE_TRAILING_JUNK_RE.sub("", name).strip()
    m = _BLOCK_LETTER_TRAILING_RE.search(raw_tail)
    if m:
        # m.start() считан по raw_tail (регистр сохранён для проверки
        # выше), но длина/позиции совпадают с tail (уже lower-нутым) —
        # регистронезависимая свёртка не меняет длину строки здесь.
        return f"block:{m.group(1).lower()}", _clean_base(tail[:m.start()].strip(" -.,"))
    return None, _clean_base(tail)


# Продуктовый токен ("Highvill-пенальти", задача гейта 2, п.5 — открытая
# находка калибровки 2026-08-12: "Highvill Ishim D" vs "Highvill Gold
# Ishim" — 0.84 auto от name_fuzzy+geo+developer, без единого сигнала
# фазы/блока, потому что "Gold" не токен фазы вообще, а маркетинговая
# линейка продукта у ОДНОГО застройщика на ОДНОЙ площадке; подтверждено
# 2026-08-12 вторым живым случаем — "Comfort Tandau" (id=2315) при
# слиянии транслит-дубля Tandau/Тандау (id=2217/2563) оказался НЕ тем же
# домом (несколько км в стороне), 'Comfort' — та же категория токена.
#
# В отличие от _phase_token: тут НЕТ implicit-дефолта ни для какого
# случая — продуктовая линейка не имеет "неявной первой линейки" (в
# отличие от "неявной первой очереди/фазы" у номеров), токен на ОДНОЙ
# стороне при отсутствии на другой — это уже потенциальный признак
# другого продукта у того же проекта/района, не нейтрален. Симметрично с
# кодом фазы: совпадают -> небольшой бонус; не совпадают (в т.ч. один
# None) -> потолок confidence, решение уходит человеку.
_PRODUCT_TOKENS = (
    "gold", "premium", "comfort", "lux", "luxe", "deluxe", "business",
    "smart", "standard", "plus", "elite", "vip", "prime", "grand",
)
_PRODUCT_TOKEN_RE = re.compile(
    r"\b(" + "|".join(_PRODUCT_TOKENS) + r")\b", re.I)


def _product_token(name: str) -> str | None:
    m = _PRODUCT_TOKEN_RE.search((name or "").lower())
    return m.group(1).lower() if m else None


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
    translit_note = ""
    if sim < FUZZY_NAME_THRESHOLD:
        # Транслит — только когда прямое сравнение уже не дотягивает до
        # fuzzy-порога (дешёвая подстраховка, не лишний DB round-trip на
        # каждой паре): Tandau/Тандау калибровка 2026-08-12. transliterate()
        # — no-op на уже латинском тексте, гонять на обеих сторонах
        # безопасно без предварительной детекции алфавита.
        t_sim = await name_similarity(transliterate(name_a), transliterate(name_b))
        if t_sim > sim:
            sim, translit_note = t_sim, "_translit"
    if sim >= 1.0:
        score, parts = _W_NAME_EXACT, [f"name_exact{translit_note}"]
    elif sim >= FUZZY_NAME_THRESHOLD:
        # линейная интерполяция веса между порогом (min) и точным (exact)
        t = (sim - FUZZY_NAME_THRESHOLD) / (1.0 - FUZZY_NAME_THRESHOLD)
        score = _W_NAME_FUZZY_MIN + t * (_W_NAME_EXACT - _W_NAME_FUZZY_MIN)
        parts = [f"name_fuzzy({sim:.2f}){translit_note}"]
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
        # ровно у одной стороны явный токен. Калибровка 2026-08-12 (живой
        # прогон newbuild): первую очередь почти никогда не подписывают
        # номером вовсе ("Nur Aspan" / "Nur Aspan 2" — не "Nur Aspan 1"),
        # так что "нет токена" — это НЕ то же самое, что "токен не про
        # фазу". Проверяем: "голая" сторона совпадает с базой номерованной
        # (той же строкой без суффикса фазы)? Если нет — токен, похоже,
        # правда часть бренда, остаёмся нейтральны. Если да — дальше по
        # типу токена: у ЧИСЛА есть осмысленный implicit-default ("1" —
        # первая фаза); у БУКВЕННОГО блока такого дефолта нет (голая
        # сторона не может "означать" конкретную букву без подтверждения,
        # см. Family Nest — калибровка 2026-08-12, sweep) — только cap.
        bare_base, numbered_token, numbered_base = (
            (base_a, phase_b, base_b) if phase_a is None else (base_b, phase_a, base_a))
        base_sim = await name_similarity(bare_base, numbered_base) if bare_base and numbered_base else 0.0
        if base_sim >= 0.8:
            is_letter = numbered_token.startswith("block:")
            implicit_label = "?~implicit" if is_letter else "1~implicit"
            if not is_letter and numbered_token == "1":
                score = min(score + _W_PHASE_BONUS, 1.0)
                parts.append(f"phase({implicit_label})")
            else:
                cap = PHASE_MISMATCH_CAP
                parts.append(f"phase_mismatch({implicit_label}!={numbered_token})")
        # иначе — нейтрально, не трогаем score (цифра, похоже, не про фазу)

    # Продуктовый токен ("Highvill-пенальти", калибровка 2026-08-12: Gold/
    # Comfort/... — маркетинговая линейка продукта, не фаза/блок) — см.
    # _product_token(). В отличие от фазы, implicit-ветки НЕТ вовсе: токен
    # на одной стороне при отсутствии на другой — тоже потолок, не
    # нейтрально (нет "неявной базовой линейки продукта").
    product_a = _product_token(name_a_full or name_a)
    product_b = _product_token(name_b_full or name_b)
    if product_a is not None or product_b is not None:
        if product_a == product_b:
            score = min(score + _W_PHASE_BONUS, 1.0)
            parts.append(f"product({product_a})")
        else:
            cap = PHASE_MISMATCH_CAP if cap is None else min(cap, PHASE_MISMATCH_CAP)
            parts.append(f"product_mismatch({product_a}!={product_b})")

    if cap is not None:
        score = min(score, cap)
    return round(score, 2), "+".join(parts)


async def record_source_link(
    complex_id: int, source: str, source_id: str, *,
    confidence: float, method: str, url: str | None = None,
    matched_by: str = "auto", evidence: dict | None = None,
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

    Предохранитель 2026-08-12 (см. docs/entity_resolution_plan.md —
    21 запись bazis/orda_invest, найденная живой калибровкой): если
    (source, source_id) уже привязан к ЭТОМУ ЖЕ complex_id — решение уже
    принято и лежит в spine, пере-скор этим прогоном ниже auto (типично
    из-за источника, который не отдаёт гео/адрес — сигналов меньше, а
    имя то же самое) не повод класть дубль-кандидата в очередь. Раньше
    это создавало вечный шум: 21 review-запись, каждая — под тем же
    complex_id, что уже в spine на confidence 1.0, approve только
    понизил бы доверие. На ДРУГОЙ complex_id — по-прежнему conflict.

    evidence — задача "очередь кандидатов" 2026-08-13 (docs/entity_
    resolution_plan.md): численные дельты сигналов (geo_m, same_developer,
    address_match, name_sim), не только их имена в match_method — для
    review-UI ("evidence инлайн"). Опционально — старые вызовы без него
    продолжают работать, просто без деталей на дашборде.

    Возвращает: 'auto' | 'review' | 'conflict' | 'already_linked' |
    'rejected' | 'skipped'."""
    if confidence < REVIEW_QUEUE_THRESHOLD:
        return "skipped"
    from bot.db.pg import fetchrow, execute
    evidence_json = json.dumps(evidence) if evidence is not None else None

    rejected = await fetchrow(
        "SELECT 1 FROM complex_source_link_rejections WHERE source=$1 AND source_id=$2 AND complex_id=$3",
        source, str(source_id), complex_id)
    if rejected:
        return "rejected"

    existing = await fetchrow(
        "SELECT complex_id FROM complex_source_links WHERE source=$1 AND source_id=$2",
        source, str(source_id))
    if existing and existing["complex_id"] == complex_id:
        return "already_linked"
    if existing and existing["complex_id"] != complex_id:
        await execute("""
            INSERT INTO complex_source_link_candidates
                (complex_id, source, source_id, url, match_method, confidence, kind, conflict_with_complex_id, evidence)
            VALUES ($1, $2, $3, $4, $5, $6, 'conflict', $7, $8)
            ON CONFLICT (source, source_id, complex_id) DO UPDATE SET
                confidence = EXCLUDED.confidence, match_method = EXCLUDED.match_method, evidence = EXCLUDED.evidence
        """, complex_id, source, str(source_id), url, method, confidence, existing["complex_id"], evidence_json)
        return "conflict"

    if is_auto_match(confidence):
        await execute("""
            INSERT INTO complex_source_links
                (complex_id, source, source_id, url, match_method, confidence, matched_by, evidence)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (source, source_id) DO NOTHING
        """, complex_id, source, str(source_id), url, method, confidence, matched_by, evidence_json)
        return "auto"

    await execute("""
        INSERT INTO complex_source_link_candidates
            (complex_id, source, source_id, url, match_method, confidence, kind, evidence)
        VALUES ($1, $2, $3, $4, $5, $6, 'review', $7)
        ON CONFLICT (source, source_id, complex_id) DO UPDATE SET
            confidence = EXCLUDED.confidence, evidence = EXCLUDED.evidence
    """, complex_id, source, str(source_id), url, method, confidence, evidence_json)
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


async def complex_data_score(cid: int) -> int:
    """links*2 + listings + enrichment (тех.специфика/материалы/отзывы/
    фото/описание/конструктив) — "кто выживает при слиянии дублей"
    (задача гейта 2, п.5 — транслит-мердж). Вынесено сюда из
    merge_translit_dups.py, чтобы UI-approve (ниже) считал канон тем
    же правилом, что и массовый скрипт — не второй, рассинхронизирующийся
    реализацией."""
    from bot.db.pg import fetchval
    name = await fetchval("SELECT name FROM complexes WHERE id = $1", cid)
    links = await fetchval("SELECT count(*) FROM complex_source_links WHERE complex_id = $1", cid)
    listings = await fetchval(
        "SELECT count(*) FROM apartment_listings WHERE lower(trim(complex_name)) = lower(trim($1))", name)
    tech = await fetchval("SELECT count(*) FROM complex_tech_specs WHERE complex_id = $1", cid)
    materials = await fetchval("SELECT count(*) FROM complex_materials WHERE complex_id = $1", cid)
    reviews = await fetchval("SELECT count(*) FROM complex_reviews WHERE complex_id = $1", cid)
    constructive = await fetchval("""
        SELECT (SELECT count(*) FROM complex_windows WHERE complex_id=$1)
             + (SELECT count(*) FROM complex_doors WHERE complex_id=$1)
             + (SELECT count(*) FROM complex_walls WHERE complex_id=$1)
             + (SELECT count(*) FROM complex_concrete_rebar WHERE complex_id=$1)
    """, cid)
    has_desc_photo = await fetchval(
        "SELECT (description IS NOT NULL)::int + (photo_url IS NOT NULL)::int FROM complexes WHERE id = $1", cid)
    return int(links) * 2 + int(listings) + int(tech) + int(materials) + int(reviews) + int(constructive) + int(has_desc_photo)


async def merge_complex_pair(dup_id: int, canon_id: int, *, method: str, matched_by: str) -> dict[str, int]:
    """Слияние дубля в канон — единая реализация, переиспользуется и
    massового скрипта (merge_translit_dups.py), и UI-approve
    (/admin/api/entity-ids/duplicates/{id}/approve, terminal_extras.py).
    Переносит apartment_listings (по complex_name), complex_source_links
    (skip, если (source, source_id) уже занят другим complex_id — не
    затираем молча), complex_favorites (PK user_id+complex_id — DELETE
    дубль-строки, если канон уже в избранном у того же user_id, иначе
    UPDATE). Дубль -> is_garbage=TRUE, provenance на обеих сторонах
    (merged_into / merged_from, накопительно)."""
    import json
    from datetime import datetime, timezone
    from bot.db.pg import fetchval, fetch, execute

    names = {r["id"]: r["name"] for r in
             await fetch("SELECT id, name FROM complexes WHERE id IN ($1, $2)", dup_id, canon_id)}
    dup_name, canon_name = names[dup_id], names[canon_id]

    n_listings = await fetchval(
        "SELECT count(*) FROM apartment_listings WHERE lower(trim(complex_name)) = lower(trim($1))", dup_name)
    await execute(
        "UPDATE apartment_listings SET complex_name = $2 WHERE lower(trim(complex_name)) = lower(trim($1))",
        dup_name, canon_name)

    links = await fetch("SELECT source, source_id FROM complex_source_links WHERE complex_id = $1", dup_id)
    moved_links = 0
    for l in links:
        exists = await fetchval(
            "SELECT 1 FROM complex_source_links WHERE source=$1 AND source_id=$2 AND complex_id != $3",
            l["source"], l["source_id"], dup_id)
        if exists:
            continue
        await execute("UPDATE complex_source_links SET complex_id = $2 WHERE source=$1 AND source_id=$3",
                      l["source"], canon_id, l["source_id"])
        moved_links += 1

    fav_users = await fetch("SELECT user_id FROM complex_favorites WHERE complex_id = $1", dup_id)
    moved_favs = 0
    for f in fav_users:
        exists = await fetchval(
            "SELECT 1 FROM complex_favorites WHERE user_id=$1 AND complex_id=$2", f["user_id"], canon_id)
        if exists:
            await execute("DELETE FROM complex_favorites WHERE user_id=$1 AND complex_id=$2", f["user_id"], dup_id)
        else:
            await execute("UPDATE complex_favorites SET complex_id=$2 WHERE user_id=$1 AND complex_id=$3",
                          f["user_id"], canon_id, dup_id)
            moved_favs += 1

    now_iso = datetime.now(timezone.utc).isoformat()
    await execute("""
        UPDATE complexes SET is_garbage = TRUE,
            provenance = COALESCE(provenance, '{}'::jsonb) || $2::jsonb
        WHERE id = $1
    """, dup_id, json.dumps({"merged_into": canon_id, "method": method, "matched_by": matched_by, "merged_at": now_iso}))
    await execute("""
        UPDATE complexes SET
            provenance = COALESCE(provenance, '{}'::jsonb) ||
                jsonb_build_object('merged_from',
                    COALESCE(provenance->'merged_from', '[]'::jsonb) || to_jsonb($2::int))
        WHERE id = $1
    """, canon_id, dup_id)

    return {"listings_moved": n_listings, "links_moved": moved_links, "favorites_moved": moved_favs}


async def approve_duplicate_candidate(candidate_id: int, approved_by: str = "admin") -> dict | None:
    """Подтвердить кандидата на дубль (complex_duplicate_candidates) —
    считает канон тем же правилом, что massовый скрипт
    (complex_data_score, больше данных выживает), реально мерджит
    (merge_complex_pair), помечает кандидата status='merged'."""
    from bot.db.pg import fetchrow, execute
    c = await fetchrow("SELECT * FROM complex_duplicate_candidates WHERE id = $1", candidate_id)
    if not c:
        return None
    score_a = await complex_data_score(c["complex_id_a"])
    score_b = await complex_data_score(c["complex_id_b"])
    canon_id, dup_id = (c["complex_id_a"], c["complex_id_b"]) if score_a >= score_b else (c["complex_id_b"], c["complex_id_a"])
    result = await merge_complex_pair(dup_id, canon_id, method=c["method"], matched_by=approved_by)
    await execute("""
        UPDATE complex_duplicate_candidates SET status = 'merged', resolved_at = now(), resolved_by = $2
        WHERE id = $1
    """, candidate_id, approved_by)
    return {"canon_id": canon_id, "dup_id": dup_id, **result}


async def reject_duplicate_candidate(candidate_id: int, rejected_by: str = "admin") -> None:
    from bot.db.pg import execute
    await execute("""
        UPDATE complex_duplicate_candidates SET status = 'rejected', resolved_at = now(), resolved_by = $2
        WHERE id = $1
    """, candidate_id, rejected_by)


def is_auto_match(confidence: float) -> bool:
    return confidence >= AUTO_MATCH_THRESHOLD


def is_review_queue(confidence: float) -> bool:
    return REVIEW_QUEUE_THRESHOLD <= confidence < AUTO_MATCH_THRESHOLD


# ─────────────────────────────────────────────────────────────────────
# Фаза 2 (юниты) — review-UX для unit_duplicate_candidates (задача
# 2026-08-13, "review-driven режим" по прямому решению заказчика).
# Тот же паттерн, что approve/reject_duplicate_candidate выше (complex-
# уровень), плюс gold-label журнал (migrations/051) — каждое решение
# человека, ВКЛЮЧАЯ skip, пишется отдельной append-only строкой с
# evidence-снимком на момент решения, для будущей перекалибровки
# unit-matching (не операционное состояние очереди — то остаётся в
# unit_duplicate_candidates.status).
async def _write_unit_gold_label(c, decision: str, decided_by: str) -> None:
    from bot.db.pg import execute
    # c["evidence"] — jsonb-колонка без codec'а в bot/db/pg.py, asyncpg
    # отдаёт её текстом (JSON-строка), поэтому явный ::jsonb на вход.
    await execute("""
        INSERT INTO unit_match_gold_labels
            (candidate_id, unit_id, listing_id, complex_id, reason, decision, evidence, decided_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
    """, c["id"], c["unit_id"], c["listing_id"], c["complex_id"], c["reason"], decision, c["evidence"], decided_by)


async def approve_unit_candidate(candidate_id: int, approved_by: str = "admin") -> dict | None:
    """Подтвердить кандидата на дубль юнита — переносит связь в
    unit_source_links (spine, тот же принцип, что complex_source_links:
    apartment_listings НЕ гарбейджится, живёт своей жизнью), помечает
    кандидата status='merged', пишет gold label."""
    from bot.db.pg import fetchrow, execute
    c = await fetchrow("SELECT * FROM unit_duplicate_candidates WHERE id = $1", candidate_id)
    if not c:
        return None
    await execute("""
        INSERT INTO unit_source_links (unit_id, source, source_id, match_method, confidence, evidence, matched_by)
        VALUES ($1, 'krisha', $2, 'manual_review', 1.0, $3::jsonb, $4)
        ON CONFLICT (source, source_id) DO NOTHING
    """, c["unit_id"], c["listing_id"], c["evidence"], approved_by)
    await execute("""
        UPDATE unit_duplicate_candidates SET status = 'merged', resolved_at = now(), resolved_by = $2
        WHERE id = $1
    """, candidate_id, approved_by)
    await _write_unit_gold_label(c, "approve", approved_by)
    return {"unit_id": c["unit_id"], "listing_id": c["listing_id"]}


async def reject_unit_candidate(candidate_id: int, rejected_by: str = "admin") -> dict | None:
    from bot.db.pg import fetchrow, execute
    c = await fetchrow("SELECT * FROM unit_duplicate_candidates WHERE id = $1", candidate_id)
    if not c:
        return None
    await execute("""
        UPDATE unit_duplicate_candidates SET status = 'rejected', resolved_at = now(), resolved_by = $2
        WHERE id = $1
    """, candidate_id, rejected_by)
    await _write_unit_gold_label(c, "reject", rejected_by)
    return {"unit_id": c["unit_id"], "listing_id": c["listing_id"]}


async def skip_unit_candidate(candidate_id: int, skipped_by: str = "admin") -> dict | None:
    """Skip — "посмотрел, не уверен, вернусь позже": статус кандидата
    НЕ трогаем (остаётся 'review', всплывёт в очереди снова), только
    gold label decision='skip' — само по себе сигнал для калибровки
    ("человек не смог решить по этому evidence")."""
    from bot.db.pg import fetchrow
    c = await fetchrow("SELECT * FROM unit_duplicate_candidates WHERE id = $1", candidate_id)
    if not c:
        return None
    await _write_unit_gold_label(c, "skip", skipped_by)
    return {"unit_id": c["unit_id"], "listing_id": c["listing_id"]}


# ─────────────────────────────────────────────────────────────────────
# "Расшивка" (unravel) как review-очередь — задача 2026-08-13, зеркало
# unravel_blobs.py (тот работает на homeportal_objects), но источник
# сигнала другой: apartment_listings (Крыша), геокластеризация ВНУТРИ
# одного complex_id + явный маркер очереди/блока в title/description
# отдельных объявлений (не в имени ЖК — то для unravel_blobs.py/
# _phase_token). migrations/053_split_candidates.sql,
# split_detect.py — сам детектор.
SPLIT_AUTO_GATE_MIN_DECIDED = 10
SPLIT_AUTO_GATE_MIN_PRECISION = 0.95


async def flag_split_candidate(complex_id: int, comment: str, flagged_by: str) -> int | None:
    """Ручная пометка "на расшивку" — кнопка на /admin/entity-ids и в
    карточке ЖК (комментарий человека — сам сигнал, evidence пустой:
    решателю искать причину руками на карточке, не то, что нашёл бы
    детектор). None, если уже есть неразрешённая пометка на этот ЖК
    (не плодим дубли в очереди)."""
    from bot.db.pg import fetchval
    exists = await fetchval(
        "SELECT 1 FROM split_candidates WHERE complex_id=$1 AND status='review'", complex_id)
    if exists:
        return None
    return await fetchval("""
        INSERT INTO split_candidates (complex_id, reason, comment, evidence, matched_by)
        VALUES ($1, 'manual', $2, '{}'::jsonb, $3) RETURNING id
    """, complex_id, comment, flagged_by)


async def _execute_split_cluster(parent_id: int, name: str, cluster: dict, matched_by: str) -> dict:
    """Один дочерний кластер -> один complex_id. apartment_listings
    привязаны к complexes ИМЕНЕМ (lower/trim), не FK — в отличие от
    unravel_blobs.py (переносит complex_source_links руками), тут для
    большинства кейсов ничего переносить не нужно: как только строка
    complexes с нужным именем существует, уже существующие объявления
    сами приджойнятся по имени на следующий рендер. Если такая строка
    УЖЕ существует (живой кейс: Rio De Janeiro 3, #4008 — создана
    вручную до этой системы) — не дублируем, только доливаем
    provenance (split_from/matched_by='unravel', тот же формат, что
    unravel_blobs.py, чтобы оба пути расшивки были неотличимы по
    provenance)."""
    from datetime import datetime, timezone
    from bot.db.pg import fetchval, fetchrow, execute
    existing = await fetchrow("SELECT id FROM complexes WHERE lower(trim(name))=lower(trim($1))", name)
    now_iso = datetime.now(timezone.utc).isoformat()
    prov = {"split_from": parent_id, "matched_by": "unravel", "split_at": now_iso, "method": "split_detect"}
    if existing:
        child_id = existing["id"]
        await execute(
            "UPDATE complexes SET provenance = COALESCE(provenance,'{}'::jsonb) || $2::jsonb WHERE id=$1",
            child_id, json.dumps(prov))
        action = "provenance_backfilled"
    else:
        child_id = await fetchval("""
            INSERT INTO complexes (name, lat, lon, address, provenance)
            VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING id
        """, name, cluster.get("lat"), cluster.get("lon"), cluster.get("address"), json.dumps(prov))
        action = "created"
    await execute(
        "UPDATE complexes SET provenance = COALESCE(provenance,'{}'::jsonb) || "
        "jsonb_build_object('split_children', COALESCE(provenance->'split_children','[]'::jsonb) || to_jsonb($2::int)) "
        "WHERE id=$1", parent_id, child_id)
    return {"child_id": child_id, "name": name, "action": action}


async def approve_split_candidate(candidate_id: int, approved_by: str) -> dict | None:
    """Исполняет "unravel playbook" по evidence-кластерам (см.
    split_detect.py::build_evidence — только НЕ-паспортные кластеры с
    suggested_name несут действие). Для ручных пометок (reason='manual',
    evidence без кластеров) — реального разнесения тут нет, только
    подтверждение статуса: решатель уже посмотрел карточку и согласен,
    что это split, но evidence искать/структурировать руками (кнопка
    сигнализирует подозрение, не считает кластеры)."""
    from bot.db.pg import fetchrow, execute
    c = await fetchrow("SELECT * FROM split_candidates WHERE id=$1", candidate_id)
    if not c:
        return None
    ev = c["evidence"]
    ev = json.loads(ev) if isinstance(ev, str) and ev else (ev or {})
    created = []
    for cluster in ev.get("clusters", []):
        name = cluster.get("suggested_name")
        if not name:
            continue
        created.append(await _execute_split_cluster(c["complex_id"], name, cluster, approved_by))
    await execute(
        "UPDATE split_candidates SET status='approved', resolved_at=now(), resolved_by=$2 WHERE id=$1",
        candidate_id, approved_by)
    return {"complex_id": c["complex_id"], "children": created}


async def reject_split_candidate(candidate_id: int, rejected_by: str) -> None:
    from bot.db.pg import execute
    await execute(
        "UPDATE split_candidates SET status='rejected', resolved_at=now(), resolved_by=$2 WHERE id=$1",
        candidate_id, rejected_by)


async def split_auto_execution_allowed(reason: str) -> bool:
    """Гейт авто-исполнения явного правила (токен+адрес+тот же
    застройщик) — решение заказчика 2026-08-13: доверять детектору без
    человека можно только после 10 решённых вручную (approve+reject) с
    точностью approved/decided >=95% на ТОЙ ЖЕ reason-корзине. 0 решений
    сейчас -> всегда False, весь трафик идёт в review — режим накопления
    gold labels, не авто-исполнение."""
    from bot.db.pg import fetchrow
    row = await fetchrow("""
        SELECT count(*) FILTER (WHERE status='approved') AS approved,
               count(*) FILTER (WHERE status IN ('approved','rejected')) AS decided
        FROM split_candidates WHERE reason=$1
    """, reason)
    decided = row["decided"] if row else 0
    if decided < SPLIT_AUTO_GATE_MIN_DECIDED:
        return False
    approved = row["approved"] if row else 0
    return (approved / decided) >= SPLIT_AUTO_GATE_MIN_PRECISION
