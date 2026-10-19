"""Public origin for Telegram login buttons; session credentials never enter URLs."""
from __future__ import annotations

import os
from urllib.parse import urlencode, urlsplit


def site_origin() -> str:
    value = os.getenv("HATULI_PUBLIC_URL", "https://hatuli.ai-groundtruth.com").rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("HATULI_PUBLIC_URL must be an HTTPS origin")
    return value


def telegram_return_url(token: str) -> str:
    # Fragments aren't sent to the server, access logs or a Telegram URL preview.
    return site_origin() + "/auth/telegram#" + urlencode({"token": token})


def login_request_origin(request) -> str:
    """Use the configured HTTPS origin behind a TLS-terminating proxy.

    Only the exact configured public authority receives this correction. Never
    trust client-supplied Forwarded/X-Forwarded-* headers or accept arbitrary
    origins merely because a proxy may be present.
    """
    public_origin = site_origin()
    if request.url.netloc == urlsplit(public_origin).netloc:
        return public_origin
    return f"{request.url.scheme}://{request.url.netloc}"
