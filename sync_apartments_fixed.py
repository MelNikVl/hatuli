#!/usr/bin/env python3
import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect('postgresql://krisha:123@localhost/krisha_bot')
    
    rows = await conn.fetch("""
        SELECT id, title, rooms, district, complex_name, area,
               price, floor, floors_total, address,
               score_total, yield_pct, bargain_discount_pct, first_seen
        FROM apartment_listings
        WHERE first_seen > NOW() - INTERVAL '30 days'
        ORDER BY score_total DESC NULLS LAST
        LIMIT 5000
    """)
    
    print(f"Got {len(rows)} rows from DB")
    
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name('/home/nik/krisha_bot/google_creds.json', scope)
    gc = gspread.authorize(creds)
    
    SHEET_URL = "https://docs.google.com/spreadsheets/d/1KaHKjg70JEfX3kLc7A8ZtNPJwsp108iSA8kIqVfrtdI/edit"
    sh = gc.open_by_url(SHEET_URL)
    
    try:
        ws = sh.worksheet("Квартиры")
    except:
        ws = sh.add_worksheet("Квартиры", rows=5500, cols=20)
    
    ws.clear()
    
    headers = ["ID", "Название", "Комнат", "Район", "ЖК", "Площадь", "Цена", "Этаж", "Этажей", "Адрес", "Скор", "Доходность", "Торг %", "Добавлено"]
    data = [headers]
    
    for r in rows:
        price_per_sqm = int(r["price"] / r["area"]) if r["area"] and r["area"] > 0 else ""
        data.append([
            r["id"],
            r["title"] or "",
            r["rooms"] or "",
            r["district"] or "",
            r["complex_name"] or "",
            r["area"] or "",
            r["price"] or "",
            r["floor"] or "",
            r["floors_total"] or "",
            r["address"] or "",
            r["score_total"] or "",
            r["yield_pct"] or "",
            r["bargain_discount_pct"] or "",
            r["first_seen"].strftime("%Y-%m-%d") if r["first_seen"] else "",
        ])
    
    ws.update(range_name="A1", values=data)
    ws.format("1:1", {"textFormat": {"bold": True}})
    
    print(f"Uploaded {len(rows)} rows to Google Sheets")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
