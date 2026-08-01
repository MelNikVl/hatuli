import psycopg2, re

with open('/home/nik/krisha_bot/.env') as f:
    env = f.read()

match = re.search(r'DATABASE_URL=postgresql://([^:]+):([^@]+)@([^/]+)/(.+)', env)
user, password, host, dbname = match.groups()
conn = psycopg2.connect(host=host, dbname=dbname, user=user, password=password)
cur = conn.cursor()

with open('/home/nik/krisha_bot/complex_updates.sql', encoding='utf-8') as f:
    sql_content = f.read()

lines = sql_content.strip().split('\n')
current = ''
for line in lines:
    current += line + ' '
    if ';' in line:
        stmt = current.strip()
        if stmt:
            try:
                cur.execute(stmt)
                print(f'OK: {stmt[:70]}...')
            except Exception as e:
                print(f'ERR: {str(e)[:100]}')
        current = ''

conn.commit()
conn.close()
print('Done!')
