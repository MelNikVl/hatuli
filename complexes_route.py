# Вставить в admin_web.py перед "return app"

    @app.get("/admin/complexes", response_class=HTMLResponse)
    async def complexes_page(
        request: Request,
        district: str = "",
        sort: str = "listings",
        search: str = "",
    ):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)

        from bot.db.pg import fetch as pg_fetch

        conditions = []
        params = []
        i = 1

        if district:
            conditions.append(f"c.district ILIKE '%' || ${i} || '%'")
            params.append(district)
            i += 1
        if search:
            conditions.append(f"c.name ILIKE '%' || ${i} || '%'")
            params.append(search)
            i += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        sort_map = {
            "listings": "c.listings_count DESC NULLS LAST",
            "yield": "c.avg_yield DESC NULLS LAST",
            "price": "c.avg_price_m2 DESC NULLS LAST",
            "year": "c.year_built DESC NULLS LAST",
        }
        order = sort_map.get(sort, "c.listings_count DESC NULLS LAST")

        rows = await pg_fetch(
            f"""
            SELECT c.*, d.name as developer_name
            FROM complexes c
            LEFT JOIN developers d ON d.id = c.developer_id
            {where}
            ORDER BY {order}
            LIMIT 200
            """,
            *params,
        )

        return templates.TemplateResponse(
            "complexes.html",
            {
                "request": request,
                "complexes": [dict(r) for r in rows],
                "total": len(rows),
                "filters": {"district": district, "sort": sort, "search": search},
            },
        )

    @app.post("/admin/complexes/update")
    async def complexes_update(
        request: Request,
        id: int = Form(...),
        developer_name: str = Form(default=""),
        year_built: str = Form(default=""),
        address: str = Form(default=""),
        has_parking: bool = Form(default=False),
        has_security: bool = Form(default=False),
        has_closed_territory: bool = Form(default=False),
        has_playground: bool = Form(default=False),
        school_distance_m: str = Form(default=""),
        lrt_distance_m: str = Form(default=""),
        notes: str = Form(default=""),
    ):
        if not is_authed(request):
            return RedirectResponse(url="/admin/login", status_code=302)

        from bot.db.pg import execute as pg_exec, fetchrow as pg_get

        # Найти или создать застройщика
        dev_id = None
        if developer_name.strip():
            dev = await pg_get(
                "SELECT id FROM developers WHERE name ILIKE $1 OR $1 = ANY(aliases)",
                developer_name.strip()
            )
            if dev:
                dev_id = dev["id"]
            else:
                dev_id = await pg_exec(
                    "INSERT INTO developers (name) VALUES ($1) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id",
                    developer_name.strip()
                )

        await pg_exec(
            """
            UPDATE complexes SET
                developer_id = COALESCE($2, developer_id),
                year_built = COALESCE(NULLIF($3, '')::integer, year_built),
                address = COALESCE(NULLIF($4, ''), address),
                has_parking = $5,
                has_security = $6,
                has_closed_territory = $7,
                has_playground = $8,
                school_distance_m = COALESCE(NULLIF($9, '')::integer, school_distance_m),
                lrt_distance_m = COALESCE(NULLIF($10, '')::integer, lrt_distance_m),
                notes = NULLIF($11, ''),
                updated_at = NOW()
            WHERE id = $1
            """,
            id, dev_id,
            year_built.strip() or None,
            address.strip() or None,
            has_parking, has_security, has_closed_territory, has_playground,
            school_distance_m.strip() or None,
            lrt_distance_m.strip() or None,
            notes.strip() or None,
        )

        return RedirectResponse(url="/admin/complexes", status_code=302)
