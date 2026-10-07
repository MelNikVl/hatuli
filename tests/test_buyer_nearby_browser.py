import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from bot.buyer.map_web import router


@pytest.mark.asyncio
async def test_mobile_nearby_markers_cards_and_listing_links():
    import uvicorn
    from playwright.async_api import async_playwright
    app=FastAPI();app.include_router(router)
    app.mount('/static',StaticFiles(directory=Path(__file__).resolve().parents[1]/'static'))
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8097,log_level='warning'))
    task=asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started: break
            await asyncio.sleep(.05)
        async with async_playwright() as p:
            browser=await p.chromium.launch()
            page=await browser.new_page(viewport={'width':390,'height':844})
            await page.route('https://tile.openstreetmap.org/**',lambda r:r.fulfill(content_type='image/svg+xml',body='<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#edf1e9"/></svg>'))
            await page.route('https://telegram.org/js/telegram-web-app.js',lambda r:r.fulfill(content_type='text/javascript',body=''))
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            items=[dict(id=str(123456+i),index=i,price=26000000-i*500000,area=50+i,rooms=2,lat=51.128+i*.003,lon=71.43+i*.005,distance_m=i*450 if i else None,is_active=True,complex_name='<script>bad</script>') for i in range(3)]
            await page.route('**/api/nearby?*',lambda r:r.fulfill(json={'items':items,'missing':0}))
            await page.goto('http://127.0.0.1:8097/buyer/map/nearby?ids=123456,123457,123458',wait_until='networkidle')
            assert await page.locator('.pin').count()==3
            assert await page.locator('.pin.base').count()==1
            assert await page.locator('.card').count()==3
            await page.locator('.card button').nth(1).click()
            assert 'Вариант 1' in await page.locator('.leaflet-popup-content').inner_text()
            assert await page.locator('.leaflet-popup-content script').count()==0
            assert await page.locator('.leaflet-popup-content a').get_attribute('href')=='https://krisha.kz/a/show/123457'
            await page.locator('#fit').click()
            await page.screenshot(path='/tmp/clearly-nearby-map.png')
            assert not errors
            await browser.close()
    finally:
        server.should_exit=True
        await task
