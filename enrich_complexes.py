import psycopg2, re, json, sys, os, urllib.request, urllib.parse, time
sys.stdout.reconfigure(encoding='utf-8')
os.environ['PYTHONIOENCODING'] = 'utf-8'

from ddgs import DDGS

with open('/home/nik/krisha_bot/.env') as f:
    env = f.read()

match = re.search(r'DATABASE_URL=postgresql://([^:]+):([^@]+)@([^/]+)/(.+)', env)
user, password, host, dbname = match.groups()
conn = psycopg2.connect(host=host, dbname=dbname, user=user, password=password)
cur = conn.cursor()

# Get all complexes from our developers
cur.execute("""
    SELECT c.id, c.name, c.year_built, c.housing_class, c.notes, 
           d.name as dev_name, c.korter_url
    FROM complexes c 
    JOIN developers d ON c.developer_id = d.id 
    WHERE d.id IN (3, 12, 14, 16, 74, 76, 103)
    ORDER BY c.listings_count DESC NULLS LAST
""")
complexes = cur.fetchall()
print(f"Found {len(complexes)} complexes from our developers")

def search_complex(query, max_results=3):
    """Search for complex info using DuckDuckGo"""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            return [{
                "title": r.get("title", ""),
                "href": r.get("href", ""),
                "body": r.get("body", "")
            } for r in results]
    except:
        return []

def classify_from_text(text):
    """Try to determine housing class from text"""
    text_lower = text.lower()
    if any(w in text_lower for w in ['элит', 'premium', 'премиум', 'elite', 'бизнес+']):
        return 'элит'
    if any(w in text_lower for w in ['бизнес', 'business']):
        return 'бизнес'
    if any(w in text_lower for w in ['комфорт', 'comfort', 'комфорт+']):
        return 'комфорт'
    if any(w in text_lower for w in ['эконом', 'econom', 'эконом+']):
        return 'эконом'
    return None

def extract_year(text):
    """Try to extract year from text"""
    matches = re.findall(r'(?:сдан|построен|постройки|сдачи|введен|год[ао]?)\s*(?::|‑|—|–)?\s*(?:в\s+)?(?:20\d{2}|19\d{2})', text, re.IGNORECASE)
    if matches:
        years = re.findall(r'20\d{2}|19\d{2}', ' '.join(matches))
        if years:
            return int(years[0])
    # Also just look for standalone years near context words
    matches = re.findall(r'(?:20\d{2}|19\d{2})\s*(?:года|год|г\.|постройки|сдачи)', text, re.IGNORECASE)
    if matches:
        years = re.findall(r'20\d{2}|19\d{2}', ' '.join(matches))
        if years:
            return int(years[0])
    # Just any 4-digit year in range
    years = re.findall(r'(?:^|\s)(20[0-2]\d|19[8-9]\d)(?:$|\s|\.|,|г)', text)
    if years:
        return int(years[0])
    return None

def update_complex(complex_id, name, dev_name, results):
    """Update complex data based on search results"""
    housing_class = None
    year_built = None
    description_parts = []
    
    for r in results:
        body = r.get('body', '') + ' ' + r.get('title', '')
        
        # Try to classify
        c = classify_from_text(body)
        if c and not housing_class:
            housing_class = c
        
        # Try to extract year
        y = extract_year(body)
        if y and not year_built:
            year_built = y
        
        # Collect description
        if r.get('body') and len(r['body']) > 20:
            description_parts.append(r['body'])
    
    # Build description
    description = None
    if description_parts:
        # Take the longest meaningful description
        best = max(description_parts, key=len)
        if len(best) > 30:
            description = best[:500]  # Limit to 500 chars
    
    # Update database
    updates = []
    params = []
    
    if housing_class:
        # Check current value
        cur.execute("SELECT housing_class FROM complexes WHERE id = %s", (complex_id,))
        current = cur.fetchone()[0]
        if not current:
            updates.append("housing_class = %s")
            params.append(housing_class)
    
    if year_built:
        cur.execute("SELECT year_built FROM complexes WHERE id = %s", (complex_id,))
        current = cur.fetchone()[0]
        if not current or current == 0:
            updates.append("year_built = %s")
            params.append(year_built)
    
    if description:
        cur.execute("SELECT notes FROM complexes WHERE id = %s", (complex_id,))
        current = cur.fetchone()[0]
        if not current:
            updates.append("notes = %s")
            params.append(description)
    
    if updates:
        params.append(complex_id)
        sql = f"UPDATE complexes SET {', '.join(updates)} WHERE id = %s"
        cur.execute(sql, params)
        conn.commit()
        return f"✅ Updated: class={'✅' if housing_class else '❌'} year={'✅' if year_built else '❌'} desc={'✅' if description else '❌'}"
    
    return "⏭️ No new data"

# Process each complex
for idx, (cid, cname, cyear, cclass, cnotes, dev_name, korter_url) in enumerate(complexes):
    print(f"\n[{idx+1}/{len(complexes)}] {dev_name} — {cname} (ID={cid})")
    
    # Skip if already has all data
    has_data = (cyear and cyear > 0) and cclass and cnotes
    if has_data:
        print(f"  ⏭️ Already complete")
        continue
    
    # Search DuckDuckGo
    query = f"{cname} {dev_name} Астана жилой комплекс год постройки класс"
    results = search_complex(query)
    
    if not results:
        print(f"  ❌ No search results")
        continue
    
    result = update_complex(cid, cname, dev_name, results)
    print(f"  {result}")
    
    # Show what was found
    for r in results[:2]:
        print(f"    • {r['title'][:60]}")
    
    # Rate limiting
    time.sleep(1)

conn.close()
print("\n🎉 Done!")
