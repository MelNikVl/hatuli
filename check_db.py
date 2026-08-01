import sqlite3, json

conn = sqlite3.connect('/home/nik/krisha_bot/bot.db')
conn.row_factory = sqlite3.Row

tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("Tables:", [t[0] for t in tables])

# Check for developer-related tables
for table_name in [t[0] for t in tables]:
    if 'developer' in table_name.lower() or 'dev' in table_name.lower():
        cols = [d[1] for d in conn.execute(f'PRAGMA table_info({table_name})').fetchall()]
        rows = conn.execute(f'SELECT * FROM {table_name} LIMIT 5').fetchall()
        print(f"\n=== {table_name} ===")
        print(f"Columns: {cols}")
        for r in rows:
            print(dict(r))

conn.close()
