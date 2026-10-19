"""Counter policy and inert synthetic rendering; never call an analytics host."""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
import httpx
import pytest

from bot.core.public_analytics import metrika_context


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "bot/templates"


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_analytics_test")

    async def deny(*args, **kwargs):
        raise AssertionError("Counter policy must not touch PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", deny)
    monkeypatch.setattr("asyncpg.create_pool", deny)
    monkeypatch.setattr("bot.db.pg.init_pool", deny)


def request(path="/", *, method="GET", query="", cookies="", host="hatuli.ai-groundtruth.com"):
    return Request({"type": "http", "method": method, "path": path,
                    "query_string": query.encode(), "headers": [(b"cookie", cookies.encode())],
                    "scheme": "https", "server": (host, 443)})


def templates():
    env = Jinja2Templates(directory=str(TEMPLATES))
    env.env.globals.update(is_admin=lambda request: False, metrika_context=metrika_context)
    return env


def snippet(path="/", *, method="GET", query="", cookies="", **context):
    return templates().env.get_template("_hatuli_metrika.html").render(
        request=request(path, method=method, query=query, cookies=cookies), **context)


@pytest.mark.parametrize("path", ["/", "/example", "/how-it-works", "/guides/check-apartment-before-deposit",
    "/best-apartments", "/best-rentals", "/analytics", "/analytics/2026-10-19-market", "/analytics/2026-10-19-rent-local",
    "/compare", "/complexes", "/complex/45", "/developers", "/developer/8", "/banks", "/banks/freedom",
    "/mortgage-calculator", "/news", "/news/88", "/info", "/rent", "/rental/daily", "/improve-listing"])
def test_reviewed_anonymous_get_paths_have_one_counter_and_one_no_script(path):
    html = snippet(path)
    assert html.count('"https://mc.yandex.ru/metrika/tag.js?id=111603221"') == 1
    assert html.count("https://mc.yandex.ru/watch/111603221") == 1
    assert 'window.ym(111603221,"init"' in html
    assert 'referrerpolicy="no-referrer"' in html


@pytest.mark.parametrize("path", ["/admin", "/admin/login", "/login", "/auth/telegram", "/cabinet", "/favorites",
    "/api/auth/start", "/api/auth/complete", "/api/rent/scenario", "/request-analysis", "/request-analysis/thanks",
    "/compare/calculate", "/compare/strategies/calculate", "/rent/request", "/listing/1007342777",
    "/rental/1007342777", "/buyer/map", "/buyer/map/nearby", "/analytics/private", "/unreviewed-page"])
def test_private_unknown_auth_and_detail_routes_never_render_script_or_pixel(path):
    assert "111603221" not in snippet(path)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_only_get_is_eligible_even_when_post_renders_public_template(method):
    assert metrika_context(request("/compare", method=method)) is None
    assert "111603221" not in snippet("/improve-listing", method=method)


@pytest.mark.parametrize("name", ["site_session", "admin_session", "admin_auth", "admin_user"])
def test_session_cookie_presence_excludes_even_public_article_and_never_reads_value(name):
    assert metrika_context(request("/analytics/2026-10-19-market", cookies=f"{name}=SECRET-SESSION")) is None
    assert "111603221" not in snippet("/best-rentals", cookies=f"{name}=SECRET-SESSION")
    assert "SECRET-SESSION" not in snippet("/best-rentals", cookies=f"{name}=SECRET-SESSION")


def test_non_session_theme_and_counter_cookies_do_not_disable_returning_anonymous_visits():
    assert metrika_context(request("/", cookies="hatuli_theme=dark; _ym_uid=123456; _ym_d=100"))
    assert metrika_context(request("/"), admin=True) is None


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "test", "staging.hatuli.ai-groundtruth.com", "hatuli.ai-groundtruth.com.evil.example"])
def test_local_ci_staging_and_lookalike_hosts_never_enable_production_counter(host):
    assert metrika_context(request("/", host=host)) is None
    assert metrika_context(request("/map", host=host), site_tier="public") is None


def test_map_and_scanner_need_explicit_public_context():
    assert "111603221" not in snippet("/map")
    assert "111603221" not in snippet("/map", site_tier="subscriber")
    assert "111603221" in snippet("/map", site_tier="public")
    assert "111603221" not in snippet("/rental/scanner")
    assert "111603221" not in snippet("/rental/scanner", access_locked=False)
    assert "111603221" in snippet("/rental/scanner", access_locked=True)


@pytest.mark.parametrize("path,query,context", [
    ("/compare", "listing=1007342777&hex=31%3A-2&rooms=3&area=61.0&scope=hex", {}),
    ("/compare", "listing=&hex=0%3A0&rooms=2&area=60&scope=city", {}),
    ("/analytics", "category=rent&topic=banks&page=2", {}),
    ("/map", "year=2027&rooms=2&type=sale", {"site_tier": "public"}),
    ("/rental/scanner", "section=candidates", {"access_locked": True}),
    ("/", "_ym_debug=2", {}),
])
def test_normal_public_filters_are_counted_but_not_sent_in_counter_url(path, query, context):
    result = metrika_context(request(path, query=query), **context)
    assert result == {"url": "https://hatuli.ai-groundtruth.com" + path}


def test_valid_campaign_attribution_survives_without_arbitrary_query_values():
    result = metrika_context(request("/compare", query="rooms=2&utm_source=telegram&utm_campaign=autumn-2026&yclid=123456789&gclid=ALPHAbeta-123"))
    assert result["url"] == "https://hatuli.ai-groundtruth.com/compare?utm_source=telegram&utm_campaign=autumn-2026&yclid=123456789&gclid=ALPHAbeta-123"


def test_unicode_campaign_terms_and_long_click_ids_keep_ad_visits_counted():
    labels = {"utm_campaign": "Астана Қазан 2026", "utm_term": "купить квартиру", "gclid": "A_" * 100,
              "fbclid": "b-" * 100}
    result = metrika_context(request("/", query=urlencode(labels)))
    assert parse_qs(urlsplit(result["url"]).query) == {key: [value] for key, value in labels.items()}


@pytest.mark.parametrize("value", ["user@example.com", "https://private.example", "name?token=secret", "A#secret", "line\nbreak", "x" * 201])
def test_utm_fields_reject_contacts_control_urls_or_excess_length(value):
    assert metrika_context(request("/", query=urlencode({"utm_campaign": value}))) is None


@pytest.mark.parametrize("query", ["token=PRIVATE", "phone=PRIVATE", "contact=PRIVATE", "description=PRIVATE",
    "url=https%3A%2F%2Fkrisha.kz%2Fa%2Fshow%2F123", "rooms=2&token=SECRET", "utm_source=user%40example.com",
    "utm_source=%3Cscript%3E", "_ym_debug=SECRET", "rooms=2&rooms=3", "utm_campaign=A&utm_campaign=B"])
def test_unknown_sensitive_or_ambiguous_query_disables_script_and_pixel(query):
    assert metrika_context(request("/compare", query=query)) is None
    assert "111603221" not in snippet("/compare", query=query)


class BodyPixels(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.in_body = False
        self.pixels = self.counter_scripts = 0
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "body":
            self.in_body = True
        if tag == "img" and "111603221" in attrs.get("src", ""):
            assert self.in_body
            self.pixels += 1

    def handle_endtag(self, tag):
        if tag == "body":
            self.in_body = False


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["buyer_public_base.html", "base_public.html", "base.html"])
async def test_each_base_renders_single_valid_body_counter_and_get_only_http(base):
    env = templates()
    child = env.env.from_string("{% extends '" + base + "' %}{% block content %}<p>Public fixture</p>{% endblock %}")
    application = FastAPI()

    @application.api_route("/compare", methods=["GET", "POST"])
    async def view(req: Request):
        return env.TemplateResponse(request=req, name=child, context={"request": req})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="https://hatuli.ai-groundtruth.com") as client:
        response = await client.get("/compare")
        posted = await client.post("/compare", data={"phone": "PRIVATE-PHONE"})
    assert response.status_code == posted.status_code == 200
    assert BodyPixels(response.text).pixels == 1
    assert response.text.count('"https://mc.yandex.ru/metrika/tag.js?id=111603221"') == 1
    assert "111603221" not in posted.text and "PRIVATE-PHONE" not in posted.text


def synthetic_js(**scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for inert JavaScript unit tests")
    html = snippet("/compare", query="utm_source=test-campaign")
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    process = subprocess.run([node, str(ROOT / "tests/js/metrika_harness.cjs")],
                             input=json.dumps({"script": script, **scenario}), text=True,
                             capture_output=True, check=True, timeout=10)
    return json.loads(process.stdout)


def test_synthetic_bootstrap_masks_before_load_keeps_options_and_never_sends_input_values():
    result = synthetic_js(referrer="https://hatuli.ai-groundtruth.com/admin/settings?token=PRIVATE-REF", repeat=True)
    assert result["loaded"] == ["https://mc.yandex.ru/metrika/tag.js?id=111603221"]
    assert result["masked_before_load"] is True
    assert len(result["calls"]) == 1
    counter, method, options = result["calls"][0]
    assert (counter, method) == (111603221, "init")
    for key in ("ssr", "webvisor", "clickmap", "accurateTrackBounce", "trackLinks"):
        assert options[key] is True
    assert options["ecommerce"] == "dataLayer"
    assert options["referrer"] == ""
    assert options["url"] == "https://hatuli.ai-groundtruth.com/compare?utm_source=test-campaign"
    assert "PRIVATE" not in json.dumps(result["calls"])


@pytest.mark.parametrize("hash", ["#token=PRIVATE", "#tgWebAppData=PRIVATE", "#unknown-payload", "#auth/PRIVATE"])
def test_synthetic_sensitive_fragment_stops_before_third_party_bootstrap(hash):
    result = synthetic_js(hash=hash)
    assert result["loaded"] == result["calls"] == []


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "", "staging.hatuli.ai-groundtruth.com"])
def test_synthetic_client_host_guard_prevents_replayed_html_from_sending_pageviews(host):
    result = synthetic_js(hostname=host if host else "about:blank")
    assert result["loaded"] == result["calls"] == []


def test_synthetic_safe_fragment_external_referrer_and_dom_ready_delay():
    result = synthetic_js(hash="#strategies", referrer="https://example.org/private-account?email=PRIVATE#SECRET", loading=True)
    assert result["before_dom_ready"] == 0
    assert len(result["loaded"]) == 1
    assert result["calls"][0][2]["referrer"] == "https://example.org/"
    internal = synthetic_js(referrer="https://hatuli.ai-groundtruth.com/analytics/2026-10-19-market?contact=PRIVATE#SECRET")
    assert internal["calls"][0][2]["referrer"] == "https://hatuli.ai-groundtruth.com/analytics/2026-10-19-market"


@pytest.mark.parametrize("name,needle", [
    ("_investment_strategies.html", 'hs-section ym-hide-content'),
    ("market_comparison.html", 'class="mc-section ym-hide-content" id="investment"'),
    ("rental_tools.html", 'class="rent-calculator ym-hide-content"'),
    ("rental_tools.html", 'class="rent-request ym-hide-content"'),
    ("seller_review.html", 'class="seller-layout ym-hide-content"'),
    ("rental_scanner.html", 'class="sublease-layout ym-hide-content"'),
])
def test_user_inputs_results_and_contact_status_have_server_rendered_masks(name, needle):
    assert needle in (TEMPLATES / name).read_text(encoding="utf-8")


def test_dynamic_map_detail_overlays_are_masked_before_they_enter_dom():
    source = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    for function, identifier in [("ensureModalRoot", "detail-modal-overlay"), ("ensureRentalDetailRoot", "rental-detail-overlay")]:
        snippet = source.split("function " + function + "()", 1)[1].split("return root;", 2)[1]
        assert "root.id = '" + identifier + "';" in snippet
        assert "root.classList.add('ym-hide-content', 'ym-disable-keys', 'ym-disable-clickmap', 'ym-disable-tracklink');" in snippet
        assert snippet.index("root.classList.add") < snippet.index("document.body.appendChild(root)")
