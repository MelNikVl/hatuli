"""Public rental tools use published evidence and a private local pilot queue."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from bot.core.admin_sessions import is_admin, admin_csrf_token, validate_admin_csrf
from bot.core.market_comparison import load_market_snapshot
from bot.core.rental_tools import (
    SCENARIO_FIELDS, REQUEST_STATUSES, STATUS_LABELS,
    RentalToolsError, RentalRequestRateLimit, RentalRequestConflict,
    calculate_rental_scenario, rental_examples_from_snapshot,
    save_rental_request, list_rental_requests, update_rental_request,
)
from bot.web.analysis_requests import _same_origin

COOKIE_NAME = "rental_form"
TOKEN_TTL = 3600
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "same-origin"}
EMPTY_REQUEST = {"kind": "", "contact": "", "brief": "", "consent": False, "website": ""}


def _secret(request):
    secret = getattr(request.app.state, "analysis_form_secret", "")
    if not secret:
        raise RuntimeError("Rental request secret must be configured")
    return secret


def _token(secret, nonce, issued=None):
    payload = f"{int(time.time()) if issued is None else issued}.{nonce}"
    signature = hmac.new(secret.encode(), ("rental-public:"+payload).encode(), hashlib.sha256).hexdigest()
    return payload+"."+signature


def _valid_token(request, token):
    nonce = request.cookies.get(COOKIE_NAME, "")
    if (not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{48}", nonce)
            or not re.fullmatch(r"[0-9]{1,12}\.[0-9a-f]{48}\.[0-9a-f]{64}", token)):
        return False
    issued = int(token.split(".")[0])
    return -60 <= time.time()-issued <= TOKEN_TTL and secrets.compare_digest(token, _token(_secret(request), nonce, issued))


async def _payload(request):
    body = bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk) > 8192:
            raise RentalToolsError({"form": "Форма слишком большая."})
        body.extend(chunk)
    try:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type == "application/json":
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            fields = json.loads(body.decode("utf-8"), object_pairs_hook=unique)
        elif content_type == "application/x-www-form-urlencoded":
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=16)
            if any(len(values) != 1 for values in parsed.values()):
                raise ValueError()
            fields = {key: values[0] for key, values in parsed.items()}
        else:
            raise ValueError()
        if not isinstance(fields, dict) or len(fields) > 16:
            raise ValueError()
        return fields
    except (ValueError, UnicodeError, RecursionError):
        raise RentalToolsError({"form": "Не удалось прочитать форму."}) from None


def make_rental_tools_router(templates):
    router = APIRouter()

    @router.get("/rent")
    async def rental_workspace(request: Request):
        nonce = request.cookies.get(COOKIE_NAME, "")
        if not re.fullmatch(r"[0-9a-f]{48}", nonce):
            nonce = secrets.token_hex(24)
        token = _token(_secret(request), nonce)
        response = templates.TemplateResponse(request=request, name="rental_tools.html", context={
            "request": request, "scenario_values": dict.fromkeys(SCENARIO_FIELDS, ""),
            "rental_examples": rental_examples_from_snapshot(load_market_snapshot()),
            "form_token": token, "request_form_token": token, "request_values": dict(EMPTY_REQUEST),
        }, headers=HEADERS)
        response.set_cookie(COOKIE_NAME, nonce, httponly=True, secure=request.url.scheme == "https",
                            samesite="lax", max_age=TOKEN_TTL)
        return response

    @router.post("/api/rent/scenario")
    async def rental_scenario(request: Request):
        try:
            result = calculate_rental_scenario(await _payload(request))
        except RentalToolsError as exc:
            return JSONResponse({"errors": exc.errors}, status_code=422, headers=HEADERS)
        return JSONResponse(result, headers=HEADERS)

    @router.post("/rent/request")
    async def rental_request(request: Request):
        try:
            fields = await _payload(request)
            if not _same_origin(request) or not _valid_token(request, fields.get("form_token")):
                return JSONResponse({"error": "form_expired", "message": "Обновите страницу и отправьте заявку ещё раз."}, status_code=403, headers=HEADERS)
            website = fields.get("website", "")
            if isinstance(website, str) and website.strip():
                result = {"reference": str(uuid4())}
            else:
                result = await asyncio.to_thread(save_rental_request, fields,
                    client_identifier=request.client.host if request.client else "unknown", secret=_secret(request))
            return JSONResponse({"accepted": True, "reference": result["reference"]}, status_code=202, headers=HEADERS)
        except RentalToolsError as exc:
            return JSONResponse({"errors": exc.errors}, status_code=422, headers=HEADERS)
        except RentalRequestRateLimit:
            return JSONResponse({"error": "rate_limit", "message": "Слишком много заявок. Попробуйте позже."}, status_code=429, headers={**HEADERS, "Retry-After": "3600"})
        except (sqlite3.Error, OSError, ValueError):
            return JSONResponse({"error": "unavailable", "message": "Не удалось сохранить заявку. Попробуйте позже."}, status_code=503, headers=HEADERS)

    @router.get("/admin/rental-requests")
    async def rental_requests_admin(request: Request):
        if not is_admin(request):
            return RedirectResponse("/admin/login", status_code=302, headers=HEADERS)
        try:
            rows = await asyncio.to_thread(list_rental_requests)
        except (sqlite3.Error, OSError, ValueError):
            return JSONResponse({"error": "unavailable"}, status_code=503, headers=HEADERS)
        return templates.TemplateResponse(request=request, name="admin_rental_requests.html", context={
            "request": request, "requests": rows, "statuses": REQUEST_STATUSES,
            "status_labels": STATUS_LABELS, "csrf_token": admin_csrf_token(request),
        }, headers={**HEADERS, "X-Frame-Options": "DENY"})

    @router.post("/admin/rental-requests/{reference}/status")
    async def rental_request_status(request: Request, reference: UUID):
        if not is_admin(request):
            return JSONResponse({"error": "auth"}, status_code=401, headers=HEADERS)
        try:
            fields = await _payload(request)
            if not _same_origin(request) or not validate_admin_csrf(request, fields.get("csrf_token", "")):
                return JSONResponse({"error": "csrf"}, status_code=403, headers=HEADERS)
            raw_version = fields.get("version")
            if isinstance(raw_version, bool) or not isinstance(raw_version, (str, int)) or not re.fullmatch(r"[0-9]{1,7}", str(raw_version)):
                raise RentalToolsError({"version": "Обновите страницу."})
            changed = await asyncio.to_thread(update_rental_request, str(reference), fields.get("status"), int(raw_version))
            if not changed:
                return JSONResponse({"error": "not_found"}, status_code=404, headers=HEADERS)
        except RentalToolsError as exc:
            return JSONResponse({"errors": exc.errors}, status_code=422, headers=HEADERS)
        except RentalRequestConflict:
            return JSONResponse({"error": "conflict", "message": "Заявка уже изменена. Обновите страницу."}, status_code=409, headers=HEADERS)
        except (sqlite3.Error, OSError, ValueError):
            return JSONResponse({"error": "unavailable"}, status_code=503, headers=HEADERS)
        return RedirectResponse("/admin/rental-requests", status_code=303, headers=HEADERS)

    return router
