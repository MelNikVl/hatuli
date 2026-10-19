"""
Дополнение к sheets_sync.py — вкладка "Аренда" из PostgreSQL rental_listings.
Добавить вызов sync_rental_to_sheets(db_path) в scheduler после rebuild_rental_index.
"""
import asyncio
import logging
import gspread

logger = logging.getLogger(__name__)

SHEET_URL = "https://docs.google.com/spreadsheets/d/1KaHKjg70JEfX3kLc7A8ZtNPJwsp108iSA8kIqVfrtdI/edit"
CREDS_PATH = "/home/nik/krisha_bot/google_creds.json"

RENTAL_HEADERS = [
    "ID", "Тип", "Комнат", "Район", "ЖК", "Площадь м²",
    "Цена из объявления ₸", "Единица ставки", "Этаж", "Этажей", "Адрес", "URL", "Замечено в списке",
]

# The existing apartment worksheet is monthly inventory. Preserve other
# property categories without guessing whether their price is per hour/m².
RENTAL_EXPORT_SQL = """
    SELECT id, prop_type, rooms, district, complex_name, area,
           price, floor, floors_total, address, url, last_seen,
           rent_period, rent_period_conflict
    FROM rental_listings
    WHERE city='astana' AND prop_type IN ('apartment','parking','commercial')
      AND (prop_type <> 'apartment' OR (rent_period='monthly' AND NOT coalesce(rent_period_conflict,false)))
      AND is_active IS TRUE AND archived_at IS NULL
      AND NOT coalesce(is_duplicate,false) AND NOT coalesce(dup_needs_review,false)
      AND nullif(btrim(duplicate_of),'') IS NULL
      AND last_seen BETWEEN now()-interval '72 hours' AND now()
    ORDER BY last_seen DESC, id
"""


def rental_sheet_rows(rows):
    data = [RENTAL_HEADERS]
    for r in rows:
        observed = r.get('last_seen')
        data.append([r['id'], r.get('prop_type') or '',
            r['rooms'] if r.get('rooms') is not None else '',
            r.get('district') or '', r.get('complex_name') or '',
            r['area'] if r.get('area') is not None else '',
            r['price'] if r.get('price') is not None else '',
            ({'monthly':'месяц','daily':'сутки'}.get(r.get('rent_period'),'не определена')
             if r.get('prop_type') == 'apartment' and r.get('rent_period_conflict') is not True else 'не определена'),
            r['floor'] if r.get('floor') is not None else '',
            r['floors_total'] if r.get('floors_total') is not None else '',
            r.get('address') or '', r.get('url') or '',
            observed.strftime('%Y-%m-%d %H:%M') if observed else ''])
    return data


async def sync_rental_to_sheets() -> bool:
    logger.info("sync_rental_to_sheets: STARTED")
    """Загружает rental_listings из PostgreSQL в вкладку 'Аренда'."""
    try:
        from bot.db.pg import fetch
        rows = await fetch(RENTAL_EXPORT_SQL)
        data = rental_sheet_rows([dict(row) for row in rows])

        def _upload(d):
            gc = gspread.service_account(filename=CREDS_PATH)
            sh = gc.open_by_url(SHEET_URL)
            try:
                ws = sh.worksheet("Аренда")
            except gspread.exceptions.WorksheetNotFound:
                ws = sh.add_worksheet("Аренда", rows=2500, cols=15)
            ws.clear()
            ws.resize(rows=max(ws.row_count,len(d)), cols=max(ws.col_count,len(RENTAL_HEADERS)))
            ws.update(range_name="A1", values=d)
            ws.format("1:1", {"textFormat": {"bold": True}})

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _upload, data)
        logger.info("rental_to_sheets: uploaded %d rows", len(rows))
        return True

    except Exception:
        logger.exception("sync_rental_to_sheets failed")
        return False
