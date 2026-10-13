"""Shared site navigation follows the verified web admin session only."""
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

from bot.core import admin_sessions


class MainNavigation(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.in_main_nav = False
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "nav" and attrs.get("id") == "hatuli-main-nav":
            self.in_main_nav = True
        elif tag == "a" and self.in_main_nav:
            self.links.append(attrs)

    def handle_endtag(self, tag):
        if tag == "nav":
            self.in_main_nav = False


def header_app(template_name, monkeypatch):
    async def no_database_connections(*args, **kwargs):
        raise AssertionError("Header tests must not connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", no_database_connections)
    monkeypatch.setattr("asyncpg.create_pool", no_database_connections)

    async def verified_session(token):
        if token == "v" * 43:
            return {"id": 7, "username": "operator", "csrf_token": "private-csrf"}
        return None

    monkeypatch.setattr(admin_sessions, "load_admin_session", verified_session)
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "bot" / "templates"))
    templates.env.globals["is_admin"] = admin_sessions.is_admin
    app = FastAPI()
    app.add_middleware(admin_sessions.AdminSessionMiddleware)

    @app.get("/{path:path}")
    async def page(request: Request):
        # A subscriber is a logged-in visitor, not an operator. Even a copied
        # operator username or full_access flag must not reveal operator menus.
        site_user = {"username": "operator", "full_access": True} if request.cookies.get("site_session") else None
        return templates.TemplateResponse(request=request, name=template_name,
                                          context={"request": request, "site_user": site_user})

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("template_name", ["buyer_public_base.html", "base_public.html", "base.html"])
async def test_verified_admin_has_first_menu_item_on_every_site_layout(template_name, monkeypatch):
    app = header_app(template_name, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hatuli.test",
                                cookies={admin_sessions.COOKIE_NAME: "v" * 43}) as client:
        for path in ("/", "/compare", "/map", "/admin-panel", "/admin/settings"):
            response = await client.get(path)
            assert response.status_code == 200
            links = MainNavigation(response.text).links
            assert links[0]["href"] == "/admin-panel"
            assert sum(link["href"] == "/admin-panel" for link in links) == 1
            assert (links[0].get("aria-current") == "page") == path.startswith("/admin")
            assert 'href="/admin/logout"' in response.text
            assert "private-csrf" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("template_name", ["buyer_public_base.html", "base_public.html", "base.html"])
async def test_visitors_forged_and_expired_admin_sessions_do_not_reveal_admin_menu(template_name, monkeypatch):
    app = header_app(template_name, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hatuli.test") as client:
        for cookies in ({}, {"site_session": "ordinary-visitor", "admin_auth": "1", "admin_user": "operator"},
                        {admin_sessions.COOKIE_NAME: "expired"}):
            client.cookies.clear()
            client.cookies.update(cookies)
            response = await client.get("/compare")
            assert response.status_code == 200
            links = MainNavigation(response.text).links
            assert all(link["href"] not in ("/admin-panel", "/info") for link in links)
            assert 'href="/admin/logout"' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/admin", "/admin/login", "/admin/logout"])
async def test_legacy_map_and_auth_pages_do_not_mark_admin_panel_current(path, monkeypatch):
    app = header_app("base_public.html", monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hatuli.test",
                                cookies={admin_sessions.COOKIE_NAME: "v" * 43}) as client:
        links = MainNavigation((await client.get(path)).text).links
        assert links[0]["href"] == "/admin-panel"
        assert "aria-current" not in links[0]
