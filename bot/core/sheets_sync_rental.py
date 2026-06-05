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
    "Цена ₸/мес", "₸/м²", "Этаж", "Этажей", "Адрес", "URL", "Найдено",
]


async def sync_rental_to_sheets():
    logger.info("sync_rental_to_sheets: STARTED")
    """Загружает rental_listings из PostgreSQL в вкладку 'Аренда'."""
    try:
        from bot.db.pg import fetch
        rows = await fetch(
            """
            SELECT id, prop_type, rooms, district, complex_name, area,
                   price, floor, floors_total, address, url, found_at
            FROM rental_listings
            WHERE found_at > NOW() - INTERVAL '30 days'
            ORDER BY found_at DESC
            LIMIT 2000
            """
        )
        if not rows:
            logger.info("rental_to_sheets: no data")
            return

        data = [RENTAL_HEADERS]
        for r in rows:
            area = r["area"] or 0
            price = r["price"] or 0
            price_per_sqm = int(price / area) if area > 0 else ""
            found_at = r["found_at"].strftime("%Y-%m-%d %H:%M") if r["found_at"] else ""
            data.append([
                r["id"],
                r["prop_type"] or "",
                r["rooms"] or "",
                r["district"] or "",
                r["complex_name"] or "",
                area or "",
                price,
                price_per_sqm,
                r["floor"] or "",
                r["floors_total"] or "",
                r["address"] or "",
                f"https://krisha.kz/a/show/{r['id']}",
                found_at,
            ])

        def _upload(d):
            gc = gspread.service_account(filename=CREDS_PATH)
            sh = gc.open_by_url(SHEET_URL)
            try:
                ws = sh.worksheet("Аренда")
            except gspread.exceptions.WorksheetNotFound:
                ws = sh.add_worksheet("Аренда", rows=2500, cols=15)
            ws.clear()
            ws.update(range_name="A1", values=d)
            ws.format("1:1", {"textFormat": {"bold": True}})

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _upload, data)
        logger.info("rental_to_sheets: uploaded %d rows", len(rows))

    except Exception:
        logger.exception("sync_rental_to_sheets failed")
