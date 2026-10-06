"""Exercise real map gestures and selection state on a phone-sized viewport."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from bot.buyer.map_web import router


@pytest.mark.asyncio
async def test_mobile_map_selection_search_undo_and_save():
    import uvicorn
    from playwright.async_api import async_playwright
    app=FastAPI();app.include_router(router)
    app.mount('/static',StaticFiles(directory=Path(__file__).resolve().parents[1]/'static'))
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8098,log_level='warning'))
    task=asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started: break
            await asyncio.sleep(.05)
        async with async_playwright() as p:
            browser=await p.chromium.launch()
            page=await browser.new_page(viewport={'width':390,'height':844},device_scale_factor=1)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            await page.route('https://telegram.org/js/telegram-web-app.js',lambda r:r.fulfill(content_type='text/javascript',body="window.Telegram={WebApp:{initData:'test',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){},close(){}}};"))
            await page.route('**/buyer/map/api/session?*',lambda r:r.fulfill(json={'selection':{'hex_ids':[]},'cells':[],'center':[51.128,71.43],'max_cells':200}))
            await page.route('**/buyer/map/api/search?*',lambda r:r.fulfill(json={'results':[{'label':'ЖК Тест','lat':51.135,'lon':71.44}]}))
            submitted=[]
            async def save(route):
                submitted.append(route.request.post_data_json)
                await route.fulfill(json={'count':len(submitted[-1]['hex_ids'])})
            await page.route('**/buyer/map/api/save',save)
            await page.goto('http://127.0.0.1:8098/buyer/map?nonce=test',wait_until='networkidle')
            await page.wait_for_selector('.leaflet-overlay-pane path')
            await page.mouse.click(195,400)
            assert 'Выбрано: 1 /' in await page.locator('#count').inner_text()
            await page.locator('#undo').click()
            assert 'Выбрано: 0 /' in await page.locator('#count').inner_text()
            await page.mouse.click(195,400)
            await page.mouse.move(180,400);await page.mouse.down();await page.mouse.move(250,450,steps=8);await page.mouse.up()
            await page.wait_for_timeout(400)
            assert 'Выбрано: 1 /' in await page.locator('#count').inner_text()
            await page.locator('#query').fill('Тест');await page.locator('#search button').click()
            await page.locator('#results button').click()
            await page.wait_for_timeout(500)
            assert 'Выбрано: 1 /' in await page.locator('#count').inner_text()
            await page.mouse.click(195,400)
            assert 'Выбрано: 2 /' in await page.locator('#count').inner_text()
            await page.screenshot(path='/tmp/clearly-buyer-map-mobile.png')
            await page.locator('#save').click()
            await page.wait_for_function("document.getElementById('save').textContent.includes('Сохранено')")
            assert submitted[0]['edge_m']==100 and len(submitted[0]['hex_ids'])==2
            assert submitted[0]['nonce']=='test'
            assert not errors
            await browser.close()
    finally:
        server.should_exit=True
        await task
