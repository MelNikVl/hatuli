import psycopg2, os, re

with open('/home/nik/krisha_bot/.env') as f:
    env = f.read()

match = re.search(r'DATABASE_URL=postgresql://([^:]+):([^@]+)@([^/]+)/(.+)', env)
if match:
    user, password, host, dbname = match.groups()
    conn = psycopg2.connect(host=host, dbname=dbname, user=user, password=password)
else:
    print("Could not parse DATABASE_URL")
    exit(1)

cur = conn.cursor()

# Check the actual nullable status for key fields
cur.execute('SELECT COUNT(*) FROM complexes')
total = cur.fetchone()[0]

for field, check_type in [('housing_class', 'text'), ('year_built', 'int'), ('notes', 'text'), ('developer_id', 'int')]:
    if check_type == 'text':
        cur.execute(f"SELECT COUNT(*) FROM complexes WHERE {field} IS NULL OR {field} = ''")
    else:
        cur.execute(f"SELECT COUNT(*) FROM complexes WHERE {field} IS NULL OR {field} = 0")
    null_count = cur.fetchone()[0]
    print(f"Missing {field}: {null_count}/{total}")

# Check what housing_class values exist
cur.execute("SELECT DISTINCT housing_class FROM complexes WHERE housing_class IS NOT NULL AND housing_class != '' ORDER BY housing_class")
classes = cur.fetchall()
print(f"\nExisting housing_class values: {[c[0] for c in classes]}")

# Sample complexes from our developers
cur.execute("""
    SELECT c.id, c.name, 
           c.housing_class IS NOT NULL AND c.housing_class != '' as has_class,
           c.year_built IS NOT NULL AND c.year_built > 0 as has_year,
           c.notes IS NOT NULL AND c.notes != '' as has_notes,
           d.name as dev_name
    FROM complexes c 
    JOIN developers d ON c.developer_id = d.id 
    WHERE d.id IN (3, 12, 14, 16, 74, 76, 103)
    ORDER BY c.listings_count DESC NULLS LAST
    LIMIT 30
""")
print(f"\nTop complexes from our developers:")
for r in cur.fetchall():
    print(f"  ID={r[0]:5d} '{r[1]:30s}' class={'✅' if r[2] else '❌'} year={'✅' if r[3] else '❌'} notes={'✅' if r[4] else '❌'} dev='{r[5]}'")

# Check what korter_url data looks like for a complex
cur.execute("SELECT id, name, korter_url, source_info FROM complexes WHERE korter_url IS NOT NULL LIMIT 3")
for r in cur.fetchall():
    print(f"\nKorter example: ID={r[0]} name='{r[1]}' url='{r[2]}'")

conn.close()
