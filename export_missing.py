"""Export all complexes missing data from PostgreSQL to JSON"""
import psycopg2, re, json, sys
sys.stdout.reconfigure(encoding='utf-8')

with open('/home/nik/krisha_bot/.env') as f:
    env = f.read()

match = re.search(r'postgresql://(.+):(.+)@(.+)/(.+)', env)
user, password, host, dbname = match.group(1), match.group(2), match.group(3), match.group(4)
conn = psycopg2.connect(host=host, dbname=dbname, user=user, password=password)
cur = conn.cursor()

# All complexes missing housing_class OR year_built OR notes, with developer name
cur.execute("""
    SELECT c.id, c.name, 
           c.housing_class IS NOT NULL AND c.housing_class != '' as has_class,
           c.year_built IS NOT NULL AND c.year_built > 0 as has_year,
           c.notes IS NOT NULL AND c.notes != '' as has_notes,
           COALESCE(d.name, '') as dev_name,
           c.listings_count
    FROM complexes c
    LEFT JOIN developers d ON c.developer_id = d.id
    WHERE (c.housing_class IS NULL OR c.housing_class = ''
           OR c.year_built IS NULL OR c.year_built = 0
           OR c.notes IS NULL OR c.notes = '')
    ORDER BY c.listings_count DESC NULLS LAST, c.id
""")

result = []
for r in cur.fetchall():
    result.append({
        'id': r[0],
        'name': r[1],
        'has_class': bool(r[2]),
        'has_year': bool(r[3]),
        'has_notes': bool(r[4]),
        'dev_name': r[5],
        'listings': r[6]
    })

print(json.dumps(result, ensure_ascii=False))
conn.close()
