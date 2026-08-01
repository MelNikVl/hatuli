import psycopg2, re, sys, os

BATCH_FILE = sys.argv[1] if len(sys.argv) > 1 else '/home/nik/krisha_bot/current_batch.sql'

with open('/home/nik/krisha_bot/.env') as f:
    env = f.read()

m = re.search(r'postgresql://(.+):(.+)@(.+)/(.+)', env)
conn = psycopg2.connect(host=m.group(3), dbname=m.group(4), user=m.group(1), password=m.group(2))
cur = conn.cursor()

# Detect encoding
with open(BATCH_FILE, 'rb') as f:
    raw = f.read(4)

if raw[:2] == b'\xff\xfe':
    encoding = 'utf-16'
elif raw[:3] == b'\xef\xbb\xbf':
    encoding = 'utf-8-sig'
else:
    encoding = 'utf-8'

count = 0
with open(BATCH_FILE, encoding=encoding) as f:
    for line in f:
        line = line.strip()
        if line and line.startswith('UPDATE'):
            try:
                cur.execute(line)
                count += 1
            except Exception as e:
                print(f'ERR[{count+1}]: {str(e)[:60]}', file=sys.stderr)

conn.commit()
conn.close()
print(f'Applied {count} statements')
