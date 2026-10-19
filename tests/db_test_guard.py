"""Validate an explicitly configured test database before pool initialization."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, unquote, urlsplit


def require_test_database_url(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    dsn = env.get('HATULI_TEST_DATABASE_URL') or env.get('DATABASE_URL') or ''
    try:
        parsed = urlsplit(dsn)
        database = unquote(parsed.path.removeprefix('/'))
        overrides_database = any(
            key.lower() in ('database', 'dbname') for key, _ in parse_qsl(parsed.query)
        )
        safe = (
            parsed.scheme in ('postgresql', 'postgres')
            and '/' not in database
            and bool(re.search(r'(?:^|[_-])test(?:$|[_-])', database, re.I))
            and not overrides_database
        )
    except ValueError:
        safe = False
    if not safe:
        # Never include a DSN in diagnostics: it can contain credentials.
        raise RuntimeError(
            'Refusing database integration tests: set HATULI_TEST_DATABASE_URL '
            'or DATABASE_URL to a PostgreSQL database with a separate "test" '
            'component in its name (for example hatuli_test).'
        )
    return dsn


async def init_test_pool(environ: Mapping[str, str] | None = None):
    dsn = require_test_database_url(environ)
    from bot.db.pg import init_pool
    return await init_pool(dsn)
