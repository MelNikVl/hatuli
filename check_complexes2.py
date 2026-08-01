import psycopg2, os, re

# Read password from .env
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

# Check complexes table structure
cur.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name='complexes' ORDER BY ordinal_position")
cols = cur.fetchall()
print("=== complexes table ===")
for c in cols:
    print(f"  {c[0]:30s} {c[1]:20s} nullable={c[2]}")

# Check how many have missing data
cur.execute("SELECT COUNT(*) FROM complexes")
total = cur.fetchone()[0]
print(f"\nTotal complexes: {total}")

cur.execute("SELECT COUNT(*) FROM complexes WHERE description IS NULL OR description = ''")
print(f"Missing description: {cur.fetchone()[0]}")

cur.execute("SELECT COUNT(*) FROM complexes WHERE class IS NULL OR class = ''")
print(f"Missing class: {cur.fetchone()[0]}")

cur.execute("SELECT COUNT(*) FROM complexes WHERE year IS NULL OR year = 0")
print(f"Missing year: {cur.fetchone()[0]}")

# Check what class values exist
cur.execute("SELECT DISTINCT class FROM complexes WHERE class IS NOT NULL AND class != '' ORDER BY class")
classes = cur.fetchall()
print(f"\nExisting class values: {[c[0] for c in classes]}")

# Check developer_id column exists
has_dev = any(c[0] == 'developer_id' for c in cols)
print(f"\nHas developer_id: {has_dev}")

# Sample some complexes from our developers
if has_dev:
    cur.execute("""
        SELECT c.id, c.name, c.description IS NOT NULL AND c.description != '' as has_desc,
               c.class IS NOT NULL AND c.class != '' as has_class,
               c.year IS NOT NULL AND c.year > 0 as has_year,
               d.name as dev_name
        FROM complexes c 
        JOIN developers d ON c.developer_id = d.id 
        WHERE d.id IN (3, 12, 14, 16, 74, 76, 103)
        ORDER BY c.id
        LIMIT 30
    """)
    print(f"\nComplexes from our developers:")
    for r in cur.fetchall():
        print(f"  ID={r[0]:5d} '{r[1]:30s}' desc={'✅' if r[2] else '❌'} class={'✅' if r[3] else '❌'} year={'✅' if r[4] else '❌'} dev='{r[5]}'")

conn.close()
