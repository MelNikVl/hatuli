"""Public editorial pages read the published article archive, never a database."""
from copy import deepcopy
import math
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse


PAGE_SIZE = 12
SITE_URL = "https://hatuli.ai-groundtruth.com"


def _load_articles(category=None):
    from bot.core.analytics_articles import list_articles

    return list_articles(category=category)


def _load_article(slug):
    from bot.core.analytics_articles import load_article

    return load_article(slug)


def _page_url(category, page):
    query = {}
    if category:
        query["category"] = category
    if page > 1:
        query["page"] = page
    return "/analytics" + ("?" + urlencode(query) if query else "")


def _display_article(article):
    """Do not let an invalid chart width become CSS or a misleading plot."""
    public = deepcopy(article)
    charts = []
    for chart in public.get("charts", []):
        rows = chart.get("rows", [])
        if not rows:
            continue
        valid = True
        for row in rows:
            value, width, n = row.get("value"), row.get("bar_pct"), row.get("n")
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or isinstance(width, bool) or not isinstance(width, (int, float))
                    or not math.isfinite(width) or not 0 <= width <= 100
                    or (n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0))):
                valid = False
                break
            row["bar_pct"] = round(width, 3)
        if valid:
            chart["signed"] = any(row["value"] < 0 for row in rows)
            chart["has_samples"] = any(row.get("n") is not None for row in rows)
            for row in rows:
                row["negative"] = row["value"] < 0
                row["bar_width"] = round(row["bar_pct"] / (2 if chart["signed"] else 1), 3)
            charts.append(chart)
    public["charts"] = charts
    return public


def make_analytics_articles_router(templates):
    router = APIRouter()
    headers = {"Cache-Control": "no-cache", "Referrer-Policy": "same-origin"}

    @router.get("/analytics", response_class=HTMLResponse)
    async def archive(
        request: Request,
        category: str | None = Query(default=None, pattern="^(market|rent)$"),
        page: int = Query(default=1, ge=1, le=10000),
    ):
        articles = _load_articles(category=category)
        page_count = max(1, (len(articles) + PAGE_SIZE - 1) // PAGE_SIZE)
        if page > page_count:
            raise HTTPException(status_code=404, detail="Страница не найдена")
        start = (page - 1) * PAGE_SIZE
        return templates.TemplateResponse(
            request=request,
            name="analytics_articles.html",
            context={
                "request": request,
                "articles": [_display_article(item) for item in articles[start:start + PAGE_SIZE]],
                "category": category,
                "page": page,
                "page_count": page_count,
                "previous_url": _page_url(category, page - 1) if page > 1 else None,
                "next_url": _page_url(category, page + 1) if page < page_count else None,
            },
            headers=headers,
        )

    @router.get("/analytics/{slug}", response_class=HTMLResponse)
    async def article_detail(request: Request, slug: str):
        article = _load_article(slug)
        if not article:
            raise HTTPException(status_code=404, detail="Статья не найдена")
        article = _display_article(article)
        schema = {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": article["title"],
            "description": article["summary"],
            "datePublished": article["edition_date"],
            "inLanguage": "ru-KZ",
            "author": {"@type": "Organization", "name": "Hatuli", "url": SITE_URL},
            "publisher": {"@type": "Organization", "name": "Hatuli", "url": SITE_URL},
            "mainEntityOfPage": SITE_URL + "/analytics/" + article["slug"],
            "articleSection": article["category_label"],
        }
        return templates.TemplateResponse(
            request=request,
            name="analytics_article.html",
            context={"request": request, "article": article, "article_schema": schema},
            headers=headers,
        )

    return router
