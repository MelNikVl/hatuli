with open('/home/nik/krisha_bot/complex_updates.sql', 'rb') as f:
    raw = f.read()
try:
    text = raw.decode('utf-8')
    print('UTF-8 OK')
    print(text[:100])
except:
    try:
        text = raw.decode('cp1251')
        print('CP1251')
        print(text[:100])
    except:
        print('Unknown encoding')
        print(raw[:50])
