"""Validate and persist manual buyer requests; never fetch or send anything."""
from __future__ import annotations

import hashlib
import hmac
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg

STATUSES = ('new', 'reviewing', 'done', 'rejected')
CLIENT_HOURLY_LIMIT = 3
CLIENT_DAILY_LIMIT = 10
CONTACT_DAILY_LIMIT = 3


class RequestFieldsError(ValueError):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__('Check the request fields')


class RequestRateLimit(ValueError):
    pass


class RequestConflict(ValueError):
    pass


def fingerprint(secret: str, purpose: str, value: str) -> str:
    return hmac.new(secret.encode(), (purpose + ':' + value).encode(), hashlib.sha256).hexdigest()


def normalize_submission(values: dict) -> dict:
    errors = {}
    clean = {}
    for field, limit in [('listing_url', 512), ('contact', 254), ('comment', 1000), ('website', 254)]:
        value = values.get(field, '')
        if not isinstance(value, str):
            errors[field] = 'Введите текст.'
            continue
        value = value.strip()
        if len(value) > limit or any(ord(c) < 32 and c not in '\n\r\t' for c in value):
            errors[field] = 'Поле слишком длинное или содержит недопустимые символы.'
        clean[field] = value
    if errors:
        raise RequestFieldsError(errors)
    url = clean['listing_url']
    try:
        parsed = urlsplit(url)
        valid_url = (parsed.scheme in ('http', 'https') and parsed.netloc.lower() in ('krisha.kz', 'www.krisha.kz')
                     and not parsed.username and not parsed.password
                     and re.fullmatch(r'/a/show/[0-9]{5,20}/?', parsed.path))
    except ValueError:
        valid_url = False
    if not valid_url:
        errors['listing_url'] = 'Пришлите ссылку вида https://krisha.kz/a/show/12345678.'
    else:
        clean['listing_id'] = parsed.path.strip('/').split('/')[-1]
        clean['listing_url'] = 'https://krisha.kz/a/show/' + clean['listing_id']
    contact = clean['contact']
    if re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{4,31}', contact):
        clean['contact_type'] = 'telegram'
    elif re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", contact):
        local, domain = contact.rsplit('@', 1)
        labels = domain.split('.')
        if (local.startswith('.') or local.endswith('.') or '..' in local or len(labels) < 2
                or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels)):
            errors['contact'] = 'Укажите @username в Telegram или email.'
        else:
            clean['contact_type'] = 'email'
    else:
        errors['contact'] = 'Укажите @username в Telegram или email.'
    if errors:
        raise RequestFieldsError(errors)
    clean['contact_normalized'] = contact.lower()
    return clean


async def save_request(values: dict, *, client_key: str, secret: str, site_user_id: int | None = None) -> dict:
    from bot.db.pg import get_pool

    clean = normalize_submission(values)
    contact_key = fingerprint(secret, 'analysis-contact', clean['contact_type'] + ':' + clean['contact_normalized'])
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            for key in sorted({'analysis-request:client:' + client_key, 'analysis-request:contact:' + contact_key}):
                await conn.fetchval('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))', key)
            existing = await conn.fetchval('''SELECT request_id FROM analysis_requests
                WHERE listing_id=$1 AND contact_key=$2 AND status IN ('new','reviewing')''',
                clean['listing_id'], contact_key)
            if existing:
                return {'reference': str(existing), 'duplicate': True}
            counts = await conn.fetchrow('''SELECT
                count(*) FILTER (WHERE client_key=$1 AND created_at>now()-interval '1 hour') AS client_hour,
                count(*) FILTER (WHERE client_key=$1) AS client_day,
                count(*) FILTER (WHERE contact_key=$2) AS contact_day
                FROM analysis_requests WHERE created_at>now()-interval '1 day'
                  AND (client_key=$1 OR contact_key=$2)''', client_key, contact_key)
            if (counts['client_hour'] >= CLIENT_HOURLY_LIMIT or counts['client_day'] >= CLIENT_DAILY_LIMIT
                    or counts['contact_day'] >= CONTACT_DAILY_LIMIT):
                raise RequestRateLimit('Слишком много заявок. Попробуйте позже.')
            request_id = uuid4()
            await conn.execute('''INSERT INTO analysis_requests
                (request_id,listing_id,listing_url,contact,contact_type,contact_key,client_key,comment,site_user_id)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)''', request_id, clean['listing_id'], clean['listing_url'],
                clean['contact'], clean['contact_type'], contact_key, client_key, clean['comment'], site_user_id)
    return {'reference': str(request_id), 'duplicate': False}


async def list_requests(status: str = 'new', limit: int = 100) -> list[dict]:
    from bot.db.pg import fetch

    if status not in (*STATUSES, 'all'):
        raise RequestFieldsError({'status': 'Неизвестный статус.'})
    rows = await fetch('''SELECT request_id,listing_url,contact,contact_type,comment,site_user_id,
        status,private_note,reviewed_by,version,created_at,updated_at FROM analysis_requests
        WHERE ($1='all' OR status=$1) ORDER BY created_at ASC, request_id LIMIT $2''', status, max(1, min(limit, 200)))
    return [dict(row) for row in rows]


async def update_request(request_id: UUID, status: str, private_note: str, reviewed_by: str, version: int) -> bool:
    from bot.db.pg import get_pool

    if status not in STATUSES or len(private_note) > 2000 or '\x00' in private_note:
        raise RequestFieldsError({'status': 'Проверьте статус и длину заметки.'})
    try:
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                contact_key = await conn.fetchval('SELECT contact_key FROM analysis_requests WHERE request_id=$1', request_id)
                if contact_key is None:
                    return False
                # Status changes can add/remove the active unique key. Serialize
                # them with submission before touching the row to avoid a race
                # or an inverted row-lock/advisory-lock ordering.
                await conn.fetchval('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
                                    'analysis-request:contact:' + contact_key)
                updated = await conn.fetchval('''UPDATE analysis_requests SET status=$2,private_note=$3,reviewed_by=$4,
                    updated_at=now(),version=version+1 WHERE request_id=$1 AND version=$5 RETURNING request_id''',
                    request_id, status, private_note, reviewed_by, version)
                if not updated and await conn.fetchval('SELECT 1 FROM analysis_requests WHERE request_id=$1', request_id):
                    raise RequestConflict('Заявка уже изменена. Обновите страницу перед сохранением.')
    except asyncpg.UniqueViolationError as exc:
        raise RequestConflict('Для этого контакта и объявления уже есть открытая заявка.') from exc
    return bool(updated)
