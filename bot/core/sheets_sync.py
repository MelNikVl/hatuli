import logging
import asyncio
import aiosqlite
import gspread

logger = logging.getLogger(__name__)

SHEET_URL = "https://docs.google.com/spreadsheets/d/1KaHKjg70JEfX3kLc7A8ZtNPJwsp108iSA8kIqVfrtdI/edit"
CREDS_PATH = "google_creds.json"
HEADERS = ["ID", "Тип", "Район", "Площадь", "Цена", "Аренда/мес", "Yield%",
           "Окупаемость", "Score", "Y", "L", "SD", "LQ", "Q",
           "URL", "Первый раз", "Последний раз", "Выяснить"]


async def sync_to_sheets(db_path):
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = [dict(r) for r in await (await db.execute(
                "SELECT * FROM investment_listings ORDER BY score_total DESC"
            )).fetchall()]
        if not rows:
            return
        data = [HEADERS]
        for r in rows:
            data.append([
                r["id"], r.get("prop_type", ""), r.get("district", ""),
                r.get("area", ""), r.get("price", 0), r.get("est_rent", 0),
                r.get("yield_pct", 0), r.get("payback_years", ""),
                r.get("score_total", 0), r.get("score_yield", 0),
                r.get("score_location", 0), r.get("score_supply_demand", 0),
                r.get("score_liquidity", 0), r.get("score_quality", 0),
                r.get("url", ""), r.get("first_seen", "")[:10],
                r.get("last_seen", "")[:10], r.get("investigation_notes", ""),
            ])
        def _upload(d):
            gc = gspread.service_account(filename=CREDS_PATH)
            sh = gc.open_by_url(SHEET_URL)
            ws = sh.sheet1
            ws.clear()
            ws.update(range_name="A1", values=d)
            ws.format("1:1", {"textFormat": {"bold": True}})
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _upload, data)
        logger.info("sheets_sync: uploaded %d rows", len(rows))
    except Exception:
        logger.exception("sheets_sync failed")


async def sync_apartments_to_sheets(db_path):
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = [dict(r) for r in await (await db.execute(
                "SELECT * FROM apartment_listings ORDER BY score_total DESC"
            )).fetchall()]
        if not rows:
            return
        headers = ["ID", "Комнат", "Район", "Площадь", "Цена", "Аренда/мес",
                   "Yield%", "Окупаемость", "Score", "Y", "PM", "L", "AT", "B", "S", "G",
                   "URL", "Первый раз", "Последний раз"]
        data = [headers]
        for r in rows:
            data.append([
                r["id"], r.get("rooms",""), r.get("district",""), r.get("area",""),
                r.get("price",0), r.get("est_rent",0), r.get("yield_pct",0),
                r.get("payback_years",""), r.get("score_total",0),
                r.get("score_yield",0), r.get("score_price_market",0),
                r.get("score_location",0), r.get("score_apt_type",0),
                r.get("score_building",0), r.get("score_supply",0),
                r.get("score_growth",0), r.get("url",""),
                r.get("first_seen","")[:10], r.get("last_seen","")[:10],
            ])
        def _upload(d):
            gc = gspread.service_account(filename=CREDS_PATH)
            sh = gc.open_by_url(SHEET_URL)
            try:
                ws = sh.worksheet("Квартиры")
            except gspread.exceptions.WorksheetNotFound:
                ws = sh.add_worksheet("Квартиры", rows=1000, cols=20)
            ws.clear()
            ws.update(range_name="A1", values=d)
            ws.format("1:1", {"textFormat": {"bold": True}})
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _upload, data)
        logger.info("sheets_sync: apartments uploaded %d rows", len(rows))
    except Exception:
        logger.exception("sync_apartments_to_sheets failed")
