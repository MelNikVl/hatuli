# -*- coding: utf-8 -*-
import psycopg2, re, sys

sys.stdout.reconfigure(encoding='utf-8')

with open('/home/nik/krisha_bot/.env') as f:
    env = f.read()

match = re.search(r'DATABASE_URL=postgresql://([^:]+):([^@]+)@([^/]+)/(.+)', env)
user, password, host, dbname = match.groups()
conn = psycopg2.connect(host=host, dbname=dbname, user=user, password=password)
cur = conn.cursor()

updates = [
    # Beles complexes
    (2029, '\u043a\u043e\u043c\u0444\u043e\u0440\u0442', 2023, None),  # Керей - комфорт
    (2681, '\u043a\u043e\u043c\u0444\u043e\u0440\u0442', 2013, None),  # СОЗАК - комфорт
    (260, '\u043a\u043e\u043c\u0444\u043e\u0440\u0442', None, None),    # айбике - комфорт
    (148, '\u043a\u043e\u043c\u0444\u043e\u0440\u0442', None, '\u0416\u041a Beles City \u2014 \u0441\u043e\u0432\u0440\u0435\u043c\u0435\u043d\u043d\u044b\u0439 \u0436\u0438\u043b\u043e\u0439 \u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0441 \u043a\u043e\u043c\u0444\u043e\u0440\u0442-\u043a\u043b\u0430\u0441\u0441\u0430 \u0432 \u0410\u0441\u0442\u0430\u043d\u0435 \u043e\u0442 \u0437\u0430\u0441\u0442\u0440\u043e\u0439\u0449\u0438\u043a\u0430 Beles'),
    
    # Sensata Group complexes
    (1732, '\u043a\u043e\u043c\u0444\u043e\u0440\u0442', 2025, None),  # Gasyr - комфорт
    (1745, '\u0431\u0438\u0437\u043d\u0435\u0441', None, None),        # Saryn - бизнес
    (2535, '\u0431\u0438\u0437\u043d\u0435\u0441', 2025, None),        # W Towers - бизнес
    (2685, None, 2025, None),                                          # Aqzam - год
    (332, '\u043a\u043e\u043c\u0444\u043e\u0440\u0442', None, None),  # есиль бокейхана - комфорт
    (2946, '\u0431\u0438\u0437\u043d\u0435\u0441', 2027, None),       # Birjan Sal - бизнес
    
    # Galamat Group complexes
    (2023, '\u043a\u043e\u043c\u0444\u043e\u0440\u0442', 2022, None),  # Tole Bi - комфорт
    (2358, '\u044d\u043b\u0438\u0442', 2017, None),                    # Galamat Towers - элит
    (3023, '\u043a\u043e\u043c\u0444\u043e\u0440\u0442', 2025, None),  # Orleu - комфорт
    
    # Sardar complexes
    (2949, '\u043a\u043e\u043c\u0444\u043e\u0440\u0442', 2025, None),  # Sardar Exclusive - комфорт
    (1916, '\u043a\u043e\u043c\u0444\u043e\u0440\u0442', None, None),  # Sardar Uly Dala - комфорт
    
    # Galamat
    (580, '\u044d\u043a\u043e\u043d\u043e\u043c', None, None),         # Orman Park - эконом
]

for cid, hclass, year, notes in updates:
    sets = []
    params = []
    if hclass:
        sets.append("housing_class = %s")
        params.append(hclass)
    if year:
        sets.append("year_built = %s")
        params.append(year)
    if notes:
        sets.append("notes = %s")
        params.append(notes)
    
    if sets:
        params.append(cid)
        sql = f"UPDATE complexes SET {', '.join(sets)} WHERE id = %s"
        try:
            cur.execute(sql, params)
            print(f"OK: ID={cid}")
        except Exception as e:
            print(f"ERR ID={cid}: {str(e)[:80]}")

# Also update the Керей description and year
cur.execute("""
    UPDATE complexes SET year_built = 2023, notes = 'Жилой комплекс Керей от застройщика Beles. 
    Современный ЖК комфорт-класса в Астане. 
    Расположен в Сарайшык районе по ул. Аманжол Болекпаев 19. 
    В продаже 22 квартиры.' 
    WHERE id = 2029 AND (notes IS NULL OR notes = '')
""")

conn.commit()
conn.close()
print("Done! All updates applied.")
