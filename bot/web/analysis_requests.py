"""HTML and JSON submission plus a verified-admin-only manual review queue."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from bot.core.analysis_requests import (
    STATUSES, RequestConflict, RequestFieldsError, RequestRateLimit,
    fingerprint, list_requests, save_request, update_request,
)

COOKIE_NAME = 'analysis_form'
TOKEN_TTL = 3600
MAX_BODY_BYTES = 8192
EMPTY_VALUES = {'listing_url': '', 'contact': '', 'comment': '', 'website': ''}


def _secret(request: Request) -> str:
    value = getattr(request.app.state, 'analysis_form_secret', '')
    if not value:
        raise RuntimeError('analysis_form_secret must be configured')
    return value


def _token(secret: str, nonce: str, issued_at: int | None = None) -> str:
    payload = f'{issued_at if issued_at is not None else int(time.time())}.{nonce}'
    signature = hmac.new(secret.encode(), ('analysis-public:' + payload).encode(), hashlib.sha256).hexdigest()
    return payload + '.' + signature


def _valid_token(request: Request, token: str) -> bool:
    nonce = request.cookies.get(COOKIE_NAME, '')
    if not re.fullmatch(r'[0-9a-f]{48}', nonce) or not re.fullmatch(r'[0-9]{1,12}\.[0-9a-f]{48}\.[0-9a-f]{64}', token):
        return False
    issued_at = int(token.split('.')[0])
    if not -60 <= time.time() - issued_at <= TOKEN_TTL:
        return False
    return secrets.compare_digest(token, _token(_secret(request), nonce, issued_at))


def _same_origin(request: Request) -> bool:
    value = request.headers.get('origin') or request.headers.get('referer')
    if not value:
        return False
    try:
        source = urlsplit(value)
        expected = urlsplit(str(request.base_url))
        if source.username or source.password:
            return False
        def origin_key(url):
            scheme = url.scheme.lower()
            return scheme, (url.hostname or '').lower(), url.port or (443 if scheme == 'https' else 80)
        return source.scheme.lower() in ('http', 'https') and origin_key(source) == origin_key(expected)
    except ValueError:
        return False


async def _read_fields(request: Request) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise RequestFieldsError({'form': 'Заявка слишком большая.'})
        body.extend(chunk)
    content_type = request.headers.get('content-type', '').split(';')[0].lower()
    try:
        text = body.decode('utf-8')
        if content_type == 'application/json':
            fields = json.loads(text)
            if not isinstance(fields, dict):
                raise ValueError()
        elif content_type == 'application/x-www-form-urlencoded':
            parsed = parse_qs(text, keep_blank_values=True, max_num_fields=12)
            if any(len(values) != 1 for values in parsed.values()):
                raise ValueError()
            fields = {key: values[0] for key, values in parsed.items()}
        else:
            raise ValueError()
        if len(fields) > 12 or any(not isinstance(value, str) for value in fields.values()):
            raise ValueError()
        return fields
    except (ValueError, UnicodeError):
        raise RequestFieldsError({'form': 'Не удалось прочитать форму. Заполните её ещё раз.'}) from None


def make_analysis_requests_router(templates) -> APIRouter:
    router = APIRouter()

    def render_form(request, *, values=None, errors=None, accepted=False, reference=None, status_code=200):
        nonce = request.cookies.get(COOKIE_NAME, '')
        if not re.fullmatch(r'[0-9a-f]{48}', nonce):
            nonce = secrets.token_hex(24)
        response = templates.TemplateResponse('request_analysis.html', {
            'request': request, 'form_token': _token(_secret(request), nonce),
            'values': values or dict(EMPTY_VALUES), 'errors': errors or {},
            'accepted': accepted, 'reference': reference, 'ref': reference,
        }, status_code=status_code)
        response.set_cookie(COOKIE_NAME, nonce, httponly=True, secure=request.url.scheme == 'https',
                            samesite='lax', max_age=TOKEN_TTL)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    async def submit(request: Request, *, json_response: bool):
        fields = {}
        values = dict(EMPTY_VALUES)
        try:
            fields = await _read_fields(request)
            values.update({key: fields[key] for key in EMPTY_VALUES if key in fields})
            if not _same_origin(request) or not _valid_token(request, fields.get('form_token', '')):
                if json_response:
                    return JSONResponse({'error': 'form_expired'}, status_code=403, headers={'Cache-Control': 'no-store'})
                return render_form(request, values=values, errors={'form': 'Обновите форму и отправьте заявку ещё раз.'}, status_code=403)
            # A honeypot submission gets the same acknowledgement but creates
            # no queue entry and does not trigger any external action.
            if values['website'].strip():
                result = {'reference': str(uuid4())}
            else:
                from bot.core.site_auth import get_user_by_session
                user = await get_user_by_session(request.cookies.get('site_session'))
                uid = user['user_id'] if user and not user.get('is_blocked') else None
                result = await save_request(values,
                    client_key=fingerprint(_secret(request), 'analysis-client', request.client.host if request.client else 'unknown'),
                    secret=_secret(request), site_user_id=uid)
            if json_response:
                return JSONResponse({'accepted': True, 'reference': result['reference']}, status_code=202,
                                    headers={'Cache-Control': 'no-store'})
            return render_form(request, accepted=True, reference=result['reference'], status_code=202)
        except RequestFieldsError as exc:
            if json_response:
                return JSONResponse({'errors': exc.errors}, status_code=422, headers={'Cache-Control': 'no-store'})
            return render_form(request, values=values, errors=exc.errors, status_code=422)
        except RequestRateLimit as exc:
            if json_response:
                return JSONResponse({'error': 'rate_limit', 'message': str(exc)}, status_code=429,
                                    headers={'Retry-After': '3600', 'Cache-Control': 'no-store'})
            response = render_form(request, values=values, errors={'form': str(exc)}, status_code=429)
            response.headers['Retry-After'] = '3600'
            return response

    @router.get('/request-analysis')
    async def request_analysis_page(request: Request):
        return render_form(request)

    @router.post('/request-analysis')
    async def request_analysis_submit(request: Request):
        return await submit(request, json_response=False)

    @router.post('/api/analysis-requests')
    async def api_analysis_request(request: Request):
        return await submit(request, json_response=True)

    @router.get('/admin/analysis-requests')
    async def admin_requests_page(request: Request, status: str = 'new'):
        from bot.core.admin_sessions import is_admin, admin_csrf_token
        if not is_admin(request):
            return RedirectResponse('/admin/login', status_code=302)
        try:
            rows = await list_requests(status)
        except RequestFieldsError as exc:
            return JSONResponse({'errors': exc.errors}, status_code=422)
        return templates.TemplateResponse('admin_analysis_requests.html', {
            'request': request, 'requests': rows, 'status': status, 'statuses': STATUSES,
            'csrf_token': admin_csrf_token(request),
        }, headers={'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY'})

    @router.post('/admin/analysis-requests/{request_id}/status')
    async def admin_request_status(request: Request, request_id: UUID):
        from bot.core.admin_sessions import is_admin, admin_username, validate_admin_csrf
        if not is_admin(request):
            return JSONResponse({'error': 'auth'}, status_code=401)
        try:
            fields = await _read_fields(request)
            if not _same_origin(request) or not validate_admin_csrf(request, fields.get('csrf_token', '')):
                return JSONResponse({'error': 'csrf'}, status_code=403)
            try:
                version = int(fields.get('version', ''))
            except ValueError:
                raise RequestFieldsError({'version': 'Обновите страницу.'}) from None
            updated = await update_request(request_id, fields.get('status', ''), fields.get('private_note', ''),
                                           admin_username(request), version)
            if not updated:
                return JSONResponse({'error': 'not_found'}, status_code=404)
        except RequestFieldsError as exc:
            return JSONResponse({'errors': exc.errors}, status_code=422)
        except RequestConflict as exc:
            return JSONResponse({'error': 'conflict', 'message': str(exc)}, status_code=409)
        return RedirectResponse('/admin/analysis-requests', status_code=303)

    return router
