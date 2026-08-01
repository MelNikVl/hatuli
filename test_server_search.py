"""Test if DuckDuckGo search works from the server"""
from ddgs import DDGS
import sys
sys.stdout.reconfigure(encoding='utf-8')

try:
    results = list(DDGS().text('ЖК Respublika SAT-NS Астана', max_results=3))
    print(f'OK: {len(results)} results')
    for r in results:
        print(f'  - {r.get("title","")[:60]}')
except Exception as e:
    print(f'FAIL: {e}')
