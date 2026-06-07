#!/usr/bin/env python3
"""Запуск только веб-админки без бота"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import asyncio
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

# Простое приложение без зависимостей от бота
app = FastAPI(title="Krisha Analytics")
templates = Jinja2Templates(directory="/home/nik/krisha_bot/bot/templates")

@app.get("/")
async def root():
    return RedirectResponse(url="/admin")

@app.get("/admin")
async def admin_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/admin/complex_scores")
async def complex_scores_page(request: Request):
    import asyncpg
    conn = await asyncpg.connect('postgresql://krisha:123@localhost/krisha_bot')
    rows = await conn.fetch("""
        SELECT complex_name, rooms, round(avg_score,1) as avg_score,
               round(median_price/1000000,1) as price_m,
               round(yield_pct,1) as yield_pct, listings_count
        FROM complex_scores WHERE yield_pct IS NOT NULL ORDER BY yield_pct DESC LIMIT 50
    """)
    await conn.close()
    return templates.TemplateResponse("complex_scores.html", {"request": request, "complexes": [dict(r) for r in rows]})

@app.get("/admin/scoring")
async def scoring_info(request: Request):
    return templates.TemplateResponse("scoring_info.html", {"request": request})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8082)
