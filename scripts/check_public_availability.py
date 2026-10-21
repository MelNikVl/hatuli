#!/usr/bin/env python3
"""Read-only availability receipt; no DB, credentials, redirects or retries.

Requires Python 3.10+ and curl. This confirms routes, not publication freshness,
market coverage, indexing, or users/day. Run from outside the origin server.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

PUBLIC_BASE = 'https://hatuli.ai-groundtruth.com'
LOCAL_BASE = 'http://127.0.0.1:8082'
ROUTES = ('/best-apartments', '/best-rentals', '/analytics', '/sitemap.xml')
HEADINGS = {
    '/best-apartments': 'Лучшие квартиры в Астане',
    '/best-rentals': 'Лучшие квартиры для аренды',
    '/analytics': 'Что происходит с жильём в Астане',
}
MAX_BYTES = 1024 * 1024
REQUEST_SECONDS = 8


class PageShape(HTMLParser):
    def __init__(self):
        super().__init__()
        self.section = None
        self.title, self.heading = [], []

    def handle_starttag(self, tag, attrs):
        if tag in ('title', 'h1'):
            self.section = tag

    def handle_endtag(self, tag):
        if tag == self.section:
            self.section = None

    def handle_data(self, data):
        if self.section == 'title':
            self.title.append(data)
        elif self.section == 'h1':
            self.heading.append(data)


def classify(path, status, body):
    """Fail closed on error bodies, login redirects and generic 200 pages."""
    text = body.decode('utf-8', errors='replace')
    plain = re.sub(r'<[^>]*>', ' ', text)
    plain = ' '.join(plain.split())
    if status == 530 and re.search(r'\berror\s*(?:code\s*)?:?\s*1033\b', plain, re.I):
        return 'tunnel_error_1033'
    if status in (403, 429) or (status == 200 and re.search(
            r'<title[^>]*>\s*(?:Just a moment|Attention Required|Access denied)', text, re.I)):
        return 'access_blocked'
    if 300 <= status < 400:
        return 'unexpected_redirect'
    if status != 200:
        return 'http_error'
    if not body or len(body) > MAX_BYTES:
        return 'unexpected_content'
    if path == '/sitemap.xml':
        try:
            root = ET.fromstring(body)
            namespace = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
            locations = [item.text for item in root.findall(f'{namespace}url/{namespace}loc')]
            return 'ok' if root.tag == namespace + 'urlset' and PUBLIC_BASE + '/analytics' in locations else 'unexpected_content'
        except ET.ParseError:
            return 'unexpected_content'
    page = PageShape()
    page.feed(text)
    branded = re.search(r'\bHatuli\b|Хатули', ' '.join(page.title), re.I)
    heading = ' '.join(' '.join(page.heading).split())
    return 'ok' if branded and heading == HEADINGS.get(path) else 'unexpected_content'


def probe(path, *, target='public'):
    if path not in ROUTES or target not in ('public', 'local'):
        raise ValueError('Only fixed Hatuli routes and targets may be probed')
    base = PUBLIC_BASE if target == 'public' else LOCAL_BASE
    started = time.monotonic()
    result = {'path': path, 'http_status': None, 'outcome': 'checker_error'}
    with tempfile.TemporaryDirectory(prefix='hatuli-availability-') as directory:
        body_path = Path(directory) / 'body'
        try:
            completed = subprocess.run([
                'curl', '--disable', '--silent', '--show-error', '--proto', '=https' if target == 'public' else '=http',
                '--max-time', str(REQUEST_SECONDS), '--connect-timeout', '4',
                '--max-filesize', str(MAX_BYTES), '--user-agent', 'HatuliAvailability/1.0',
                '--output', str(body_path), '--write-out', '%{http_code}', base + path,
            ], capture_output=True, timeout=REQUEST_SECONDS + 2, check=False)
            raw_status = completed.stdout.decode('ascii', errors='replace').strip()
            status = int(raw_status) if re.fullmatch(r'[1-5][0-9]{2}', raw_status) else None
            result['http_status'] = status
            if completed.returncode == 0 and status is not None:
                body = b''
                if body_path.exists():
                    with body_path.open('rb') as stream:
                        body = stream.read(MAX_BYTES + 1)
                result['outcome'] = classify(path, status, body)
            else:
                result['outcome'] = {
                    6: 'dns_error', 7: 'connection_error', 28: 'timeout',
                    35: 'tls_error', 60: 'tls_error', 63: 'body_too_large',
                }.get(completed.returncode, 'transport_error')
        except subprocess.TimeoutExpired:
            result['outcome'] = 'timeout'
        except (OSError, ValueError):
            result['outcome'] = 'checker_error'
    result['duration_ms'] = round((time.monotonic() - started) * 1000)
    return result


def report(*, target='public', fetch=None):
    fetch = fetch or probe
    checks = [fetch(path, target=target) for path in ROUTES]
    return {
        'schema_version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
        'target': target, 'scope': 'availability_only',
        'all_routes_confirmed': all(item['outcome'] == 'ok' for item in checks),
        'checks': checks,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('public', 'local'), default='public')
    args = parser.parse_args(argv)
    receipt = report(target=args.target)
    print(json.dumps(receipt, ensure_ascii=False, allow_nan=False))
    return 0 if receipt['all_routes_confirmed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
