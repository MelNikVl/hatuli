import psycopg2, re

with open('/home/nik/krisha_bot/.env') as f:
    env = f.read()

m = re.search(r'postgresql://(.+):(.+)@(.+)/(.+)', env)
conn = psycopg2.connect(host=m.group(3), dbname=m.group(4), user=m.group(1), password=m.group(2))
cur = conn.cursor()

with open('/home/nik/krisha_bot/batch_1_utf8.sql', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line and line.startswith('UPDATE'):
            try:
                cur.execute(line)
                print('.', end='', flush=True)
            except Exception as e:
                print('\nERR: ' + str(e)[:70])

conn.commit()
conn.close()
print('\nDone!')
