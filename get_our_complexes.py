import psycopg2, re, json, sys

sys.stdout.reconfigure(encoding='utf-8')

with open('/home/nik/krisha_bot/.env') as f:
    env = f.read()

match = re.search(r'DATABASE_URL=postgresql://([^:]+):([^@]+)@([^/]+)/(.+)', env)
user, password, host, dbname = match.groups()
conn = psycopg2.connect(host=host, dbname=dbname, user=user, password=password)
cur = conn.cursor()

cur.execute("""
    SELECT c.id, c.name, 
           c.housing_class IS NOT NULL AND c.housing_class != '' as has_class,
           c.year_built IS NOT NULL AND c.year_built > 0 as has_year,
           c.notes IS NOT NULL AND c.notes != '' as has_notes,
           d.name as dev_name,
           c.listings_count
    FROM complexes c 
    JOIN developers d ON c.developer_id = d.id 
    WHERE d.id IN (3, 12, 14, 16, 74, 76, 103)
    ORDER BY c.listings_count DESC NULLS LAST
""")

result = []
for r in cur.fetchall():
    result.append({
        'id': r[0], 'name': r[1], 'has_class': r[2], 'has_year': r[3],
        'has_notes': r[4], 'dev_name': r[5], 'listings': r[6]
    })

print(json.dumps(result, ensure_ascii=False))
conn.close()
