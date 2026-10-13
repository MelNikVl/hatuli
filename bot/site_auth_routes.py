"""One-use Telegram return links, independent of the original polling tab."""
from __future__ import annotations

import json
import re

from fastapi import Request
from fastapi.responses import JSONResponse

from bot.core import site_auth

AUTH_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex, nofollow"}
_RETURN_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,100}\Z")


def register_site_auth_routes(app, templates) -> None:
    @app.get("/auth/telegram")
    async def telegram_return(request: Request):
        # GET is deliberately read-only: Telegram link previews must not redeem
        # a credential. The browser receives it in a fragment and submits POST.
        return templates.TemplateResponse(request=request, name="telegram_login.html",
                                          context={"request": request}, headers=AUTH_HEADERS)

    @app.post("/api/auth/complete")
    async def complete_telegram_login(request: Request):
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if (request.headers.get("x-hatuli-login") != "telegram"
                or (origin is not None and origin != expected_origin)
                or request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none")):
            return JSONResponse({"status": "forbidden"}, status_code=403, headers=AUTH_HEADERS)
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            return JSONResponse({"status": "invalid"}, status_code=415, headers=AUTH_HEADERS)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024:
                return JSONResponse({"status": "invalid"}, status_code=413, headers=AUTH_HEADERS)
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            value = None
        token = value.get("token") if isinstance(value, dict) else None
        if not isinstance(token, str) or not _RETURN_TOKEN.fullmatch(token):
            return JSONResponse({"status": "invalid"}, status_code=400, headers=AUTH_HEADERS)
        session_id = await site_auth.consume_login_token(token)
        if not session_id:
            return JSONResponse({"status": "expired"}, status_code=401, headers=AUTH_HEADERS)
        response = JSONResponse({"status": "verified", "redirect": "/map"}, headers=AUTH_HEADERS)
        site_auth.set_site_session_cookie(response, request, session_id)
        return response
