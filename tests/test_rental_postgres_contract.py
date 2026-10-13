"""Real PostgreSQL contracts, restricted to an explicitly disposable QA DB.

Run on Ubuntu as postgres with HATULI_RENTAL_TEST_DATABASE=hatuli_rental_qa_...
The application's pool and production DSN are never initialized or read.
"""
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.mark.skipif(not os.getenv('HATULI_RENTAL_TEST_DATABASE'), reason='requires isolated PostgreSQL QA DB')
def test_atomic_rental_observation_and_daily_metrics(monkeypatch):
    name = os.environ['HATULI_RENTAL_TEST_DATABASE']
    assert name.startswith('hatuli_rental_qa_') and name.replace('_','').isalnum()

    async def run():
        import asyncpg
        from bot.db import pg
        from bot.core.rental_parser import RentalListing, save_rental_listings
        from bot.core.rental_service_metrics import record_rental_cycle
        connection = await asyncpg.connect(database=name,user='postgres',host='/var/run/postgresql')
        try:
            assert await connection.fetchval('SELECT current_database()') == name
            schema = Path(__file__).resolve().parents[1]/'migrations/000_baseline_rentals.sql'
            await connection.execute(schema.read_text(encoding='utf-8'))
            await connection.execute('ALTER TABLE rental_listings ADD COLUMN archive_reason text')
            await connection.execute('CREATE TABLE app_settings (key text PRIMARY KEY,value text,updated_at timestamptz)')
            monkeypatch.setattr(pg,'fetchrow',connection.fetchrow)
            monkeypatch.setattr(pg,'execute',connection.execute)
            listing = RentalListing(id='123456789',url='https://krisha.kz/a/show/123456789',
                title='2-комнатная квартира',price=200000,area=60,rooms=2,floor=3,
                floors_total=10,address='QA',district='Есиль',complex_name=None,
                city='astana',prop_type='apartment',published_at='13 окт.')
            first = await save_rental_listings([listing])
            assert (int(first),first.new,first.updated,first.reactivated)==(1,1,0,0)
            found = await connection.fetchval('SELECT found_at FROM rental_listings')
            await connection.execute("UPDATE rental_listings SET is_active=false,archived_at=now(),archive_reason='confirmed_gone'")
            listing.price=180000
            second = await save_rental_listings([listing])
            assert (second.new,second.updated,second.reactivated)==(0,1,1)
            row = await connection.fetchrow('SELECT * FROM rental_listings')
            assert row['is_active'] and row['archived_at'] is None and row['archive_reason'] is None
            assert row['found_at']==found and row['price']==180000
            history = await connection.fetchrow('SELECT * FROM rental_price_history')
            assert (history['old_price'],history['new_price'])==(200000,180000)
            third = await save_rental_listings([listing])
            assert third.updated==1 and third.reactivated==0
            assert await connection.fetchval('SELECT count(*) FROM rental_price_history')==1
            when = datetime(2026,10,13,23,tzinfo=timezone.utc)
            await record_rental_cycle({'type':'apartment'},{'new':1,'pages':2},now=when)
            await record_rental_cycle({'type':'parking'},{'new':3,'pages':1},now=when)
            import json
            daily = json.loads(await connection.fetchval("SELECT value FROM app_settings WHERE key='RENTAL_METRICS_V1_2026-10-14'"))
            assert daily['new']==4 and daily['pages']==3
        finally:
            await connection.close()
    asyncio.run(run())
