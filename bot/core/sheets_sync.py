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


async def sync_apartments_to_sheets_pg():
    """Загружает apartment_listings из PostgreSQL в вкладку 'Квартиры'."""
    try:
        from bot.db.pg import fetch
        rows = await fetch(
            """
            SELECT id, rooms, district, complex_name, area, price,
                   est_rent, yield_pct, payback_years, score_total,
                   score_yield, score_price_market, score_location,
                   score_apt_type, score_floor, score_complex, score_supply,
                   floor, floors_total, year_built, seller_type, is_owner,
                   rent_source, bargain_target, bargain_discount_pct, bargain_rec,
                   url, first_seen, last_seen
            FROM apartment_listings
            ORDER BY score_total DESC NULLS LAST
            LIMIT 2000
            """
        )
        if not rows:
            return

        headers = [
            "ID", "Комнат", "Район", "ЖК", "Площадь", "Цена ₸",
            "Аренда/мес", "Yield%", "Окупаемость лет", "Score",
            "Y", "PM", "L", "AT", "Этаж", "ЖК скор", "Supply",
            "Этаж", "Всего этажей", "Год постройки", "Продавец", "Хозяин",
            "Источник аренды", "Цель торга ₸", "Торг%", "Рек торга",
            "URL", "Первый раз", "Последний раз"
        ]
        data = [headers]
        for r in rows:
            fs = r["first_seen"].strftime("%Y-%m-%d") if r["first_seen"] else ""
            ls = r["last_seen"].strftime("%Y-%m-%d") if r["last_seen"] else ""
            data.append([
                r["id"], r["rooms"] or "", r["district"] or "",
                r["complex_name"] or "", r["area"] or "", r["price"] or 0,
                r["est_rent"] or 0, r["yield_pct"] or 0, r["payback_years"] or "",
                r["score_total"] or 0,
                r["score_yield"] or 0, r["score_price_market"] or 0,
                r["score_location"] or 0, r["score_apt_type"] or 0,
                r["score_floor"] or 0, r["score_complex"] or 0, r["score_supply"] or 0,
                r["floor"] or "", r["floors_total"] or "", r["year_built"] or "",
                r["seller_type"] or "",
                "хозяин" if r["is_owner"] else ("риелтор" if r["is_owner"] is False else ""),
                r["rent_source"] or "",
                r["bargain_target"] or "", r["bargain_discount_pct"] or "",
                r["bargain_rec"] or "",
                r["url"] or "", fs, ls,
            ])

        def _upload(d):
            gc = gspread.service_account(filename=CREDS_PATH)
            sh = gc.open_by_url(SHEET_URL)
            try:
                ws = sh.worksheet("Квартиры")
            except gspread.exceptions.WorksheetNotFound:
                ws = sh.add_worksheet("Квартиры", rows=2500, cols=30)
            ws.clear()
            ws.update(range_name="A1", values=d)
            ws.format("1:1", {"textFormat": {"bold": True}})

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _upload, data)
        import logging
        logging.getLogger(__name__).info("sheets: apartments uploaded %d rows", len(rows))
    except Exception:
        import logging
        logging.getLogger(__name__).exception("sync_apartments_pg failed")
