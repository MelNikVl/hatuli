"""Public editorial pages read the published article archive, never a database."""
from copy import deepcopy
from datetime import datetime
import math
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from bot.core.analytics_comparisons import TOPICS


PAGE_SIZE = 12
SITE_URL = "https://hatuli.ai-groundtruth.com"


def _load_articles(category=None):
    from bot.core.analytics_articles import list_articles

    return list_articles(category=category)


def _load_article(slug):
    from bot.core.analytics_articles import load_article

    return load_article(slug)


def _page_url(category, page, topic=None):
    query = {}
    if category:
        query["category"] = category
    if topic:
        query["topic"] = topic
    if page > 1:
        query["page"] = page
    return "/analytics" + ("?" + urlencode(query) if query else "")


def _axis_number(value, step):
    decimals = min(3, max(0, -math.floor(math.log10(step))))
    if step < 1 or step % 1:
        decimals = max(1, decimals)
    rendered = f"{value:,.{decimals}f}"
    if decimals:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered.replace(",", " ").replace(".", ",")


def _timeline_plot(rows):
    """Plot reviewed monthly observations without sorting or bridging gaps."""
    months = []
    for row in rows:
        try:
            observed = datetime.fromisoformat(row["as_of"].replace("Z", "+00:00"))
            if observed.tzinfo is None:
                return None
            month = observed.year * 12 + observed.month - 1
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if months and month <= months[-1]:
            return None
        months.append(month)
    if len(months) < 2:
        return None
    values = [row["value"] for row in rows]
    low, high = min(values), max(values)
    padding = max((high - low) * .12, abs(high) * .002, .01)
    span = high - low + 2 * padding
    raw_step = span / 4
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = next(n * magnitude for n in (1, 2, 2.5, 5, 10) if n * magnitude >= raw_step)
    minimum = math.floor((low - padding) / step) * step
    maximum = math.ceil((high + padding) / step) * step
    def y(value):
        return round(8 + 224 * (maximum - value) / (maximum - minimum), 3)
    ticks = []
    for index in range(round((maximum - minimum) / step) + 1):
        value = maximum - index * step
        ticks.append({"label": _axis_number(value, step), "y": y(value), "top": round(y(value) / 2.4, 3)})
    points, segments, current = [], [], []
    month_labels = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
    for index, (month, row) in enumerate(zip(months, rows)):
        x = round(8 + 984 * (month - months[0]) / (months[-1] - months[0]), 3)
        position = {**row, "x": x, "y": y(row["value"]), "left": round(x / 10, 3),
                    "top": round(y(row["value"]) / 2.4, 3), "month_label": month_labels[month % 12],
                    "year_label": str(month // 12) if index in (0, len(months) - 1) or month // 12 != months[index - 1] // 12 else ""}
        points.append(position)
        if index and month != months[index - 1] + 1:
            if len(current) > 1:
                segments.append(" ".join(current))
            current = []
        current.append(f"{x},{position['y']}")
    if len(current) > 1:
        segments.append(" ".join(current))
    return {"points": points, "segments": segments, "ticks": ticks,
            "period_label": f"{points[0]['month_label']} {months[0] // 12} — {points[-1]['month_label']} {months[-1] // 12}",
            "zero_y": y(0) if minimum <= 0 <= maximum else None,
            "scale_label": f"Шкала: {_axis_number(minimum, step)}–{_axis_number(maximum, step)}",
            "has_gaps": any(b != a + 1 for a, b in zip(months, months[1:])),
            "description": "; ".join(f"{row['label']}: {row['value_label']}" for row in rows)}


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
                row["dot_position"] = round(50 + (-1 if row["negative"] else 1) * row["bar_pct"] / 2, 3) if chart["signed"] else row["bar_pct"]
            chart["plot"] = _timeline_plot(rows) if chart.get("mode") == "timeline" else None
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
        topic: str | None = Query(default=None, pattern="^(places|complexes|banks|developers|strategies)$"),
        page: int = Query(default=1, ge=1, le=10000),
    ):
        articles = _load_articles(category=category)
        available = {value["id"] for item in articles for value in item.get("topics", []) if value.get("id") in TOPICS}
        topics = [{"id": key, "label": label, "url": _page_url(category, 1, key)}
                  for key, label in TOPICS.items() if key in available]
        if topic:
            articles = [item for item in articles if any(value.get("id") == topic for value in item.get("topics", []))]
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
                "topic": topic,
                "topic_label": TOPICS.get(topic),
                "topics": topics,
                "all_topics_url": _page_url(category, 1),
                "page": page,
                "page_count": page_count,
                "previous_url": _page_url(category, page - 1, topic) if page > 1 else None,
                "next_url": _page_url(category, page + 1, topic) if page < page_count else None,
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
            "datePublished": article.get("published_at", article["edition_date"]),
            "dateModified": article.get("date_modified", article.get("published_at", article["edition_date"])),
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
