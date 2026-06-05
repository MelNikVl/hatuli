# Этот код вставить в admin_web.py перед строкой "return app"

    @app.get("/admin/analytics", response_class=HTMLResponse)
    async def analytics_page(
        request: Request,
        district: str = "",
        rooms: str = "",
        min_score: int = 60,
        sort: str = "score_total",
        limit: int = 50,
    ):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)

        from bot.db.pg import fetch as pg_fetch

        conditions = ["score_total IS NOT NULL"]
        params = []
        i = 1

        if district:
            conditions.append(f"district ILIKE '%' || ${i} || '%'")
            params.append(district)
            i += 1
        if rooms:
            try:
                conditions.append(f"rooms = ${i}")
                params.append(int(rooms))
                i += 1
            except ValueError:
                pass
        conditions.append(f"score_total >= ${i}")
        params.append(min_score)
        i += 1

        valid_sorts = {"score_total", "yield_pct", "price", "bargain_discount_pct"}
        sort_col = sort if sort in valid_sorts else "score_total"

        where = " AND ".join(conditions)
        rows = await pg_fetch(
            f"""
            SELECT id, url, title, rooms, district, complex_name, area, floor, floors_total,
                   price, est_rent, yield_pct, payback_years,
                   score_total, score_yield, score_price_market, score_location,
                   score_apt_type, score_floor, score_complex, score_supply,
                   reasons, is_owner, seller_type,
                   bargain_discount_pct, bargain_rec, bargain_target,
                   rent_source, year_built, is_new_build,
                   first_seen, last_seen
            FROM apartment_listings
            WHERE {where}
            ORDER BY {sort_col} DESC NULLS LAST
            LIMIT {min(limit, 200)}
            """,
            *params,
        )

        # Статистика rental_index
        rental_stats = await pg_fetch("""
            SELECT district, rooms, median_price, sample_count, complex_name
            FROM rental_index
            WHERE prop_type = 'apartment'
            ORDER BY sample_count DESC
            LIMIT 30
        """)

        return templates.TemplateResponse(
            "analytics.html",
            {
                "request": request,
                "listings": [dict(r) for r in rows],
                "rental_stats": [dict(r) for r in rental_stats],
                "filters": {
                    "district": district,
                    "rooms": rooms,
                    "min_score": min_score,
                    "sort": sort,
                    "limit": limit,
                },
                "total": len(rows),
            },
        )

    @app.get("/admin/analytics/{listing_id}", response_class=HTMLResponse)
    async def analytics_detail(request: Request, listing_id: str):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)

        from bot.db.pg import fetchrow as pg_fetchrow, fetch as pg_fetch
        from bot.core.bargain import get_comparables, analyze_bargain

        row = await pg_fetchrow(
            "SELECT * FROM apartment_listings WHERE id = $1", listing_id
        )
        if not row:
            return HTMLResponse("<h2>Not found</h2>", status_code=404)

        listing = dict(row)

        # Свежие аналоги
        comps = await get_comparables(
            district=listing.get("district"),
            rooms=listing.get("rooms"),
            area=listing.get("area"),
            current_price=listing.get("price", 0),
            exclude_id=listing_id,
        )
        bargain = analyze_bargain(listing.get("price", 0), comps, listing.get("is_owner"))

        # Аренда рядом
        rental_comps = await pg_fetch("""
            SELECT complex_name, district, rooms, price, area
            FROM rental_listings
            WHERE ($1::text IS NULL OR district ILIKE '%' || $1 || '%')
              AND ($2::int IS NULL OR rooms = $2)
              AND price > 0
            ORDER BY found_at DESC
            LIMIT 10
        """, listing.get("district"), listing.get("rooms"))

        return templates.TemplateResponse(
            "analytics_detail.html",
            {
                "request": request,
                "listing": listing,
                "comps": [dict(r) for r in comps],
                "bargain": bargain,
                "rental_comps": [dict(r) for r in rental_comps],
            },
        )
