import psycopg2

conn = psycopg2.connect("postgresql://krisha:***@localhost/krisha_bot")
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

# Check developer relationship
if has_dev:
    cur.execute("""
        SELECT c.id, c.name, c.developer_id, d.name 
        FROM complexes c 
        LEFT JOIN developers d ON c.developer_id = d.id 
        WHERE c.developer_id IS NOT NULL 
        LIMIT 5
    """)
    for r in cur.fetchall():
        print(f"  complex {r[0]} '{r[1]}' -> developer {r[2]} '{r[3]}'")

conn.close()
