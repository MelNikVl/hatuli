import psycopg2, json

conn = psycopg2.connect("postgresql://krisha:123@localhost/krisha_bot")
cur = conn.cursor()

cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
tables = [r[0] for r in cur.fetchall()]
print("Tables:", tables)

# Check developer-related tables
for table_name in tables:
    if 'developer' in table_name.lower() or 'builder' in table_name.lower():
        cur.execute(f"SELECT column_name, data_type FROM information_schema.columns WHERE table_name='{table_name}'")
        cols = [(r[0], r[1]) for r in cur.fetchall()]
        print(f"\n=== {table_name} ===")
        print(f"Columns: {[c[0] for c in cols]}")
        cur.execute(f"SELECT * FROM {table_name} LIMIT 5")
        for row in cur.fetchall():
            print(row)

conn.close()
