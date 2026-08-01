import psycopg2, json
from datetime import datetime, timezone

conn = psycopg2.connect("postgresql://krisha:123@localhost/krisha_bot")
cur = conn.cursor()

# First, find the IDs by name
names_to_find = ['Beles', 'Galamat Group', 'Galamat', 'Sardar Construction Group', 'Sensata Group', 'Sensata', 'Монтаж и К 2022']
for name in names_to_find:
    cur.execute("SELECT id, name, founded_year, website, description FROM developers WHERE name ILIKE %s", (f'%{name}%',))
    rows = cur.fetchall()
    for r in rows:
        print(f"  ID={r[0]}: name='{r[1]}', founded={r[2]}, website={r[3]}")

print("\n--- UPDATING ---")

# Update Beles (ID 14 from the admin page)
cur.execute("""
    UPDATE developers 
    SET founded_year = 2020, 
        website = 'https://belesholding.kz',
        description = 'BELES - динамично развивающаяся строительная компания, работающая на рынке недвижимости с 2020 года. 20 сданных объектов, 15 ЖК, 5 социальных объектов, 520 500 м² реализовано.',
        updated_at = %s
    WHERE id = 14
""", (datetime.now(timezone.utc),))
print(f"Beles (ID 14): {cur.rowcount} row(s) updated")

# Update Galamat Group (ID 74)
cur.execute("""
    UPDATE developers 
    SET founded_year = 2006,
        description = 'Одна из ведущих строительных компаний Астаны. За 20 лет построено около 800 000 м², введено 46+ объектов. Города: Астана, Кокшетау, Петропавловск, Степногорск, Бурабай.',
        updated_at = %s
    WHERE id = 74
""", (datetime.now(timezone.utc),))
print(f"Galamat Group (ID 74): {cur.rowcount} row(s) updated")

# Update Galamat (ID 12) - same group
cur.execute("""
    UPDATE developers 
    SET founded_year = 2006,
        description = 'Одна из ведущих строительных компаний Астаны. За 20 лет построено около 800 000 м². Входит в группу Galamat Group.',
        updated_at = %s
    WHERE id = 12
""", (datetime.now(timezone.utc),))
print(f"Galamat (ID 12): {cur.rowcount} row(s) updated")

# Update Sardar Construction Group (ID 76)
cur.execute("""
    UPDATE developers 
    SET founded_year = 1998, 
        website = 'https://sardar-group.kz',
        description = 'Строительная компания. На рынке с 1998 года. Проекты: Sardar Compass, Sardar Ūly Dala, Sardar Riverside. Руководитель: Смагулов Алишер.',
        updated_at = %s
    WHERE id = 76
""", (datetime.now(timezone.utc),))
print(f"Sardar Construction Group (ID 76): {cur.rowcount} row(s) updated")

# Update Sensata Group (ID 16) - already has year, add website
cur.execute("""
    UPDATE developers 
    SET website = 'https://sensata.kz',
        updated_at = %s
    WHERE id = 16 AND website IS NULL
""", (datetime.now(timezone.utc),))
print(f"Sensata Group (ID 16): {cur.rowcount} row(s) updated")

# Update Sensata (ID 3) - add year and website
cur.execute("""
    UPDATE developers 
    SET founded_year = 2012,
        website = 'https://sensata.kz',
        description = 'Группа компаний Sensata Group. Образовалась в 2012 году.',
        updated_at = %s
    WHERE id = 3
""", (datetime.now(timezone.utc),))
print(f"Sensata (ID 3): {cur.rowcount} row(s) updated")

# Update Монтаж и К 2022 (ID 103)
cur.execute("""
    UPDATE developers 
    SET founded_year = 2022,
        description = 'ТОО Монтаж и К 2022. Зарегистрировано 06.02.2022. Адрес: Астана, пр. Улы Дала, 1/1.',
        updated_at = %s
    WHERE id = 103
""", (datetime.now(timezone.utc),))
print(f"Монтаж и К 2022 (ID 103): {cur.rowcount} row(s) updated")

conn.commit()
conn.close()
print("\nDone! All updates committed.")
