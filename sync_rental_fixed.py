#!/usr/bin/env python3
import asyncio
import asyncpg

async def main():
    # Прямое подключение без пула
    conn = await asyncpg.connect('postgresql://krisha:123@localhost/krisha_bot')
    
    # Запрос
    rows = await conn.fetch("""
        SELECT id, prop_type, rooms, district, complex_name, area,
               price, floor, floors_total, address, found_at
        FROM rental_listings
        WHERE found_at > NOW() - INTERVAL '30 days'
        ORDER BY found_at DESC
        LIMIT 2000
    """)
    
    print(f"Got {len(rows)} rows from DB")
    
    # Импортируем gspread здесь, чтобы не мешал
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_name('/home/nik/krisha_bot/google_creds.json', scope)
    gc = gspread.authorize(creds)
    
    SHEET_URL = "https://docs.google.com/spreadsheets/d/1KaHKjg70JEfX3kLc7A8ZtNPJwsp108iSA8kIqVfrtdI/edit"
    sh = gc.open_by_url(SHEET_URL)
    
    try:
        ws = sh.worksheet("Аренда")
    except:
        ws = sh.add_worksheet("Аренда", rows=2500, cols=15)
    
    # Очищаем и заполняем
    ws.clear()
    
    headers = ["ID", "Тип", "Комнат", "Район", "ЖК", "Площадь м²", "Цена ₸/мес", "₸/м²", "Этаж", "Этажей", "Адрес", "URL", "Найдено"]
    data = [headers]
    
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
            area,
            price,
            price_per_sqm,
            r["floor"] or "",
            r["floors_total"] or "",
            r["address"] or "",
            f"https://krisha.kz/a/show/{r['id']}",
            found_at,
        ])
    
    ws.update(range_name="A1", values=data)
    ws.format("1:1", {"textFormat": {"bold": True}})
    
    print(f"Uploaded {len(rows)} rows to Google Sheets")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
