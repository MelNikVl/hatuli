#!/usr/bin/env python3
"""Полный текст статей за сутки для LLM-анализа (Playwright + BeautifulSoup).
Запуск: venv/bin/python hype_tracker/article_text.py [--limit 30] [--days 1]
Выводит JSON: [{"id":..,"title":..,"source":..,"text":"...полный текст..."}]
"""
import argparse
import json
import re
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

BASE = Path("/home/nik/krisha_bot")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def extract_text(content: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, "html.parser")
    # убираем мусорные блоки
    for bad in soup.select("script, style, nav, header, footer, aside, form, .comments, .related, .advert, [class*=advert], [class*=banner]"):
        bad.decompose()
    paras = []
    for p in soup.select("article p, .article-content p, .content p, .news-content p, .post-content p, p"):
        t = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
        if len(t) > 60:
            paras.append(t)
    text = " ".join(paras)
    # если абзацев нет — весь текст body
    if len(text) < 200:
        body = soup.find("body")
        if body:
            text = re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip()
    return text[:8000]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--days", type=int, default=1)
    a = ap.parse_args()

    db = psycopg2.connect(load_database_url().rsplit("/", 1)[0] + "/krisha_bot")
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT id, title, source, url FROM news WHERE ts > now() - interval '%s days' "
        "ORDER BY ts DESC LIMIT %s" % (a.days, a.limit))
    rows = cur.fetchall()
    db.close()

    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(user_agent=UA)
        for r in rows:
            try:
                pg.goto(r["url"], wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(2500)
                out.append({
                    "id": r["id"], "title": r["title"], "source": r["source"],
                    "text": extract_text(pg.content()),
                })
            except Exception as e:
                out.append({"id": r["id"], "title": r["title"], "source": r["source"], "text": "", "error": str(e)})
        b.close()
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
