"""
Рыночные данные для скоринга: базовая ставка НБРК, рыночные ставки по
депозитам (KDIF), ставки Отбасы, официальный индекс цен на жильё
(Бюро нацстатистики).

Результаты пишутся в app_settings как СПРАВОЧНЫЕ значения (показываются
на странице Инфо) и НЕ меняют ползунки скоринга автоматически — решение
крутить DEPOSIT_RATE/APPRECIATION_PCT остаётся за человеком, чтобы кривой
парс не сломал модель молча.

Ключи:
  NBRK_BASE_RATE          — базовая ставка НБРК, %
  KDIF_RATES_RAW          — найденные ставки KDIF (сырой список)
  OTBASY_RATES_RAW        — найденные ставки Отбасы (сырой список, эксперимент)
  STAT_HOUSING_ASTANA_RAW — строки из свежего xlsx статистики с «Астана»
  MARKET_DATA_UPDATED_AT  — время последнего успешного прогона

Каждый источник независим: падение одного не мешает другим.
Первые прогоны — в режиме --test, чтобы проверить, что регулярки
попадают в реальную разметку (написаны по текстам страниц, не по
живому HTML).
"""
from __future__ import annotations

import io
import logging
import re

import httpx

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

NBRK_URL = "https://nationalbank.kz/ru"
KDIF_URL = "https://kdif.kz/reward-rates"
OTBASY_URL = "https://otbasybank.kz/ru"
STAT_LIST_URL = ("https://stat.gov.kz/ru/industries/economy/prices/"
                 "spreadsheets/?year=&name=19521&period=&type=")
STAT_BASE = "https://stat.gov.kz"


async def _get(url: str, binary: bool = False):
    async with httpx.AsyncClient(headers=HEADERS, timeout=40.0,
                                 follow_redirects=True) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"{url} -> {resp.status_code}")
    return resp.content if binary else resp.text


# ── 1. НБРК: базовая ставка ────────────────────────────────────────────────

async def fetch_nbrk_base_rate() -> float | None:
    """
    Главная nationalbank.kz содержит заголовок пресс-релиза вида
    «О сохранении базовой ставки на уровне 18,0%» / «О снижении ... до 16,5%».
    """
    html = await _get(NBRK_URL)
    m = re.search(r"базово\w+ ставк\w+[^0-9%]{0,30}?(\d{1,2}[.,]\d)\s*%",
                  html, re.IGNORECASE)
    if not m:
        logger.warning("NBRK: базовая ставка не найдена на главной")
        return None
    return float(m.group(1).replace(",", "."))


# ── 2. KDIF: ставки по депозитам ───────────────────────────────────────────

async def fetch_kdif_rates() -> list[str]:
    """
    Страница kdif.kz/reward-rates содержит таблицу рыночных/предельных
    ставок (с 01.03.2025 по тенге это рыночный ориентир, не потолок).
    Собираем все проценты 3..30% рядом с типами вкладов — сырой список
    для показа на Инфо; выбор «правильной» ставки для DEPOSIT_RATE
    остаётся человеку.
    """
    html = await _get(KDIF_URL)
    text = re.sub(r"<[^>]+>", " ", html)
    found = []
    for m in re.finditer(
            r"((?:несрочн|срочн|сберегательн)\w*[^%<>]{0,80}?(\d{1,2}[.,]\d)\s*%)",
            text, re.IGNORECASE):
        found.append(re.sub(r"\s+", " ", m.group(1)).strip()[:120])
    # плюс все одиночные проценты в диапазоне депозитов — как запасной улов
    if not found:
        for m in re.finditer(r"(\d{1,2}[.,]\d)\s*%", text):
            v = float(m.group(1).replace(",", "."))
            if 5 <= v <= 30:
                found.append(f"{v}%")
    return found[:20]


# ── 3. Отбасы: ставки (эксперимент) ───────────────────────────────────────

async def fetch_otbasy_rates() -> list[str]:
    """
    Ставки Отбасы почти статичны (жилстройсбережения ~5% займ, депозит ~2%
    + госпремия 20%). Пытаемся снять актуальные проценты с главной —
    экспериментально: разметка не проверялась вживую.
    """
    html = await _get(OTBASY_URL)
    text = re.sub(r"<[^>]+>", " ", html)
    found = []
    for m in re.finditer(r"([А-Яа-яA-Za-z][^%<>]{5,60}?(\d{1,2}(?:[.,]\d)?)\s*%)", text):
        v = float(m.group(2).replace(",", "."))
        if 1 <= v <= 30:
            found.append(re.sub(r"\s+", " ", m.group(1)).strip()[:100])
    return found[:15]


# ── 4. stat.gov.kz: индекс цен на жильё ────────────────────────────────────

async def fetch_stat_housing() -> tuple[list[str], str | None]:
    """
    1) Находим на странице серии «Индексы цен и цены на рынке жилья»
       ссылку на самый свежий xlsx.
    2) Скачиваем, парсим (openpyxl), вытаскиваем строки с «Астана».
    Возвращает (строки для показа, url файла). Требует openpyxl:
        venv/bin/pip install openpyxl
    """
    html = await _get(STAT_LIST_URL)
    links = re.findall(r'href="([^"]+\.xlsx?)"', html)
    if not links:
        logger.warning("stat.gov.kz: xlsx-ссылки не найдены на странице серии")
        return [], None
    file_url = links[0] if links[0].startswith("http") else STAT_BASE + links[0]

    try:
        import openpyxl
    except ImportError:
        logger.warning("openpyxl не установлен: venv/bin/pip install openpyxl")
        return [f"[файл найден, но openpyxl не установлен] {file_url}"], file_url

    content = await _get(file_url, binary=True)
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    rows_out: list[str] = []
    for sheet in wb.worksheets:
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None]
            line = " | ".join(cells)
            if "Астана" in line:
                rows_out.append(f"[{sheet.title}] {line[:220]}")
            if len(rows_out) >= 30:
                break
        if len(rows_out) >= 30:
            break
    wb.close()
    return rows_out, file_url


# ── Оркестратор ────────────────────────────────────────────────────────────

async def update_all(write: bool = True) -> dict:
    """Прогоняет все источники. write=False — только показать (--test)."""
    from datetime import datetime, timezone
    import time as _time
    _t0 = _time.monotonic()
    results: dict = {}

    try:
        results["NBRK_BASE_RATE"] = await fetch_nbrk_base_rate()
    except Exception as e:
        logger.warning("NBRK failed: %s", e)
        results["NBRK_BASE_RATE"] = None

    try:
        results["KDIF_RATES_RAW"] = await fetch_kdif_rates()
    except Exception as e:
        logger.warning("KDIF failed: %s", e)
        results["KDIF_RATES_RAW"] = []

    try:
        results["OTBASY_RATES_RAW"] = await fetch_otbasy_rates()
    except Exception as e:
        logger.warning("Otbasy failed: %s", e)
        results["OTBASY_RATES_RAW"] = []

    try:
        stat_rows, stat_url = await fetch_stat_housing()
        results["STAT_HOUSING_ASTANA_RAW"] = stat_rows
        results["STAT_HOUSING_FILE_URL"] = stat_url
    except Exception as e:
        logger.warning("stat.gov.kz failed: %s", e)
        results["STAT_HOUSING_ASTANA_RAW"] = []
        results["STAT_HOUSING_FILE_URL"] = None

    if write:
        from bot.db import settings as app_settings
        if results.get("NBRK_BASE_RATE"):
            await app_settings.set("NBRK_BASE_RATE", str(results["NBRK_BASE_RATE"]))
        if results.get("KDIF_RATES_RAW"):
            await app_settings.set("KDIF_RATES_RAW", " ;; ".join(results["KDIF_RATES_RAW"]))
        if results.get("OTBASY_RATES_RAW"):
            await app_settings.set("OTBASY_RATES_RAW", " ;; ".join(results["OTBASY_RATES_RAW"]))
        if results.get("STAT_HOUSING_ASTANA_RAW"):
            await app_settings.set("STAT_HOUSING_ASTANA_RAW",
                                   "\n".join(results["STAT_HOUSING_ASTANA_RAW"]))
        if results.get("STAT_HOUSING_FILE_URL"):
            await app_settings.set("STAT_HOUSING_FILE_URL", results["STAT_HOUSING_FILE_URL"])
        await app_settings.set("MARKET_DATA_UPDATED_AT",
                               datetime.now(timezone.utc).isoformat())
        # Раньше прогоны market нигде не логировались построчно (только
        # перезаписываемые скаляры в app_settings) — не было истории для
        # графика "что спарсилось со временем" на вкладке /admin/parsers.
        # Пишем в тот же source_runs, что уже используют korter/homsters —
        # переиспользуем поле matched как "сколько источников успешно
        # ответили" (0-4: НБРК/KDIF/Отбасы/stat.gov.kz).
        try:
            from bot.core.site_enrichment import record_run
            ok_sources = sum(1 for k in ("NBRK_BASE_RATE", "KDIF_RATES_RAW", "OTBASY_RATES_RAW", "STAT_HOUSING_ASTANA_RAW")
                             if results.get(k))
            await record_run("market", datetime.now(timezone.utc), _time.monotonic() - _t0,
                             matched=ok_sources, created=0, changed=0)
        except Exception as e:
            logger.warning("market source_runs log failed: %s", e)
    return results
