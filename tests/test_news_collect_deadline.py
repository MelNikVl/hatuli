"""Тесты для hype_tracker/news_collect.py — задача 2026-08-17 ("global
deadline"): единый wall-clock бюджет на весь main() вместо суммы
независимых бюджетов по стадиям (RSS-сбор, прямой og:image, 2х
Playwright-обогащение), которая раньше могла превысить внешний ~300с
ssh-таймаут хоста (200+48+70+70=388с в худшем случае).

Всё через monkeypatch — ни реальных сетевых запросов, ни реального
Playwright/браузера, ни реального ожидания (time.monotonic/time.sleep
подменены управляемыми фейками), ни реальной БД (psycopg2.connect
подменён фейковым connection/cursor). "database" здесь — обычные dict,
не psycopg2.extras.RealDictCursor, но код news_collect.py обращается к
строкам результата по ключу (r["url"]) одинаково для обоих."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hype_tracker"))
import news_collect  # noqa: E402


# ── Фейковые часы: time.monotonic()/time.sleep() подменяются одним
# управляемым объектом — sleep() не ждёт по-настоящему, а просто
# продвигает тот же счётчик, что читает monotonic(). Это и есть
# "тесты через fake/monkeypatch monotonic, без реального ожидания". ──
class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _patch_clock(monkeypatch, start=0.0):
    clock = _FakeClock(start)
    monkeypatch.setattr(news_collect.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(news_collect.time, "sleep", clock.sleep)
    return clock


# ── Фейковая БД: execute()/fetchall() маршрутизируются по подстроке
# SQL, insert'ы просто накапливаются в списках для проверки. ──
class _FakeCursor:
    def __init__(self, backfill_rows=None):
        self.backfill_rows = backfill_rows if backfill_rows is not None else []
        self._pending_result = []
        self.query_stats = []
        self.inserted_news = []
        self.updated_summaries = []
        self.deleted_old = False

    def execute(self, sql, params=None):
        s = sql.strip()
        if s.startswith("CREATE TABLE"):
            pass
        elif "SELECT url FROM news WHERE ts" in s:
            self._pending_result = []
        elif "SELECT id, url, image_url FROM news WHERE summary IS NULL" in s:
            self._pending_result = self.backfill_rows
        elif s.startswith("INSERT INTO news_query_stats"):
            self.query_stats.append(params)
        elif s.startswith("INSERT INTO news"):
            self.inserted_news.append(params)
        elif s.startswith("UPDATE news SET summary"):
            self.updated_summaries.append(params)
        elif s.startswith("DELETE FROM news"):
            self.deleted_old = True
        else:
            raise AssertionError(f"unexpected SQL in fake cursor: {sql!r}")

    def fetchall(self):
        return self._pending_result


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = 0
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self._cursor

    def commit(self):
        self.committed += 1

    def close(self):
        self.closed = True


def _patch_db(monkeypatch, backfill_rows=None):
    cursor = _FakeCursor(backfill_rows=backfill_rows)
    fake_conn = _FakeConn(cursor)
    monkeypatch.setattr(news_collect, "conn", lambda: fake_conn)
    return fake_conn, cursor


# ── Фейковый Playwright: goto()/wait_for_timeout() не открывают
# реальный браузер, wait_for_timeout продвигает фейковые часы на
# заданную "стоимость" одного элемента — управляемо и без ожидания. ──
class _FakePage:
    def __init__(self, clock: _FakeClock, per_item_cost: float):
        self.clock = clock
        self.per_item_cost = per_item_cost
        self.visited = []

    def goto(self, url, wait_until=None, timeout=None):
        self.visited.append(url)

    def wait_for_timeout(self, ms):
        self.clock.advance(self.per_item_cost)

    def content(self):
        return '<meta property="og:image" content="https://img.example/x.jpg">'


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    def new_page(self, user_agent=None):
        return self._page

    def close(self):
        pass


class _FakeChromium:
    def __init__(self, page):
        self._page = page

    def launch(self, headless=True):
        return _FakeBrowser(self._page)


class _FakeSyncPlaywrightCtx:
    def __init__(self, page):
        self.chromium = _FakeChromium(page)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_playwright(monkeypatch, clock: _FakeClock, per_item_cost: float = 0.5):
    """Подменяет `from playwright.sync_api import sync_playwright` внутри
    enrich_images_playwright — sys.modules перехватывается ДО импорта,
    реальный playwright-пакет (даже если установлен) не трогается."""
    page = _FakePage(clock, per_item_cost)
    fake_module = types.SimpleNamespace(sync_playwright=lambda: _FakeSyncPlaywrightCtx(page))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)
    return page


# ═══════════════════════════════════════════════════════════════════
# enrich_images_playwright — бюджет стадии сам по себе
# ═══════════════════════════════════════════════════════════════════

def test_enrich_zero_or_negative_budget_short_circuits_without_playwright(monkeypatch):
    """budget<=0 -> 0 и НИКАКОГО импорта playwright (проверяем, что
    функция даже не пытается открыть браузер зря — вызывающий код
    (main()) полагается именно на это, чтобы не тратить время
    запуска браузера при почти исчерпанном бюджете)."""
    def _boom(*a, **kw):
        raise AssertionError("playwright не должен импортироваться при budget<=0")
    monkeypatch.setitem(sys.modules, "playwright.sync_api",
                         types.SimpleNamespace(sync_playwright=_boom))
    assert news_collect.enrich_images_playwright([{"url": "a"}], budget=0) == 0
    assert news_collect.enrich_images_playwright([{"url": "a"}], budget=-5) == 0


def test_enrich_empty_items_short_circuits(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("playwright не должен импортироваться для пустого items")
    monkeypatch.setitem(sys.modules, "playwright.sync_api",
                         types.SimpleNamespace(sync_playwright=_boom))
    assert news_collect.enrich_images_playwright([], budget=100) == 0


def test_enrich_stops_when_budget_exhausted_mid_loop(monkeypatch):
    """10 items по 1с каждый, budget=3.5с -> должно обработать 3-4, не
    все 10 — бюджет проверяется МЕЖДУ элементами (см. докстринг
    функции), без реального ожидания (fake clock)."""
    clock = _patch_clock(monkeypatch)
    _install_fake_playwright(monkeypatch, clock, per_item_cost=1.0)
    items = [{"url": f"https://news.example/{i}"} for i in range(10)]
    done = news_collect.enrich_images_playwright(items, limit=8, budget=3.5)
    processed = sum(1 for it in items if it.get("image"))
    assert 0 < processed < 10
    assert done == processed


def test_enrich_skips_already_imaged_items_once_limit_reached(monkeypatch):
    """`limit` не строгий "не больше N картинок за прогон" — items БЕЗ
    картинки функция обрабатывает независимо от лимита (см. её
    докстринг/условие `done >= limit and it.get("image")` — приоритет
    не терять картинку, не жёсткий cap). Что `limit` реально экономит:
    Playwright-визит для items, у которых картинка УЖЕ есть (типичный
    случай в main() — они уже прошли стадию 2, прямой og:image) — как
    только done >= limit, такие items больше не идут в goto()."""
    clock = _patch_clock(monkeypatch)
    page = _install_fake_playwright(monkeypatch, clock, per_item_cost=0.01)
    items = [{"url": f"https://news.example/new{i}"} for i in range(3)]
    items += [{"url": f"https://news.example/have{i}", "image": "https://already.example/img"}
              for i in range(5)]
    done = news_collect.enrich_images_playwright(items, limit=3, budget=1000.0)
    assert done == 3
    assert not any("have" in v for v in page.visited), (
        "items с уже известной картинкой после достижения лимита не должны "
        "тратить Playwright-визит")


# ═══════════════════════════════════════════════════════════════════
# main() — единый глобальный дедлайн делится между стадиями
# ═══════════════════════════════════════════════════════════════════

def _patch_common(monkeypatch, queries, rss_cost=0.0, backfill_rows=None):
    """Общая обвязка для main()-тестов: фейковая БД, фейковые
    load_queries/get_rss/parse_rss/get_og_image. enrich_images_
    playwright НЕ подменяется здесь намеренно — каждый main()-тест сам
    решает, шпионить за ним (spy) или дать реально пойти в fake
    playwright, в зависимости от того, что проверяет."""
    fake_conn, cursor = _patch_db(monkeypatch, backfill_rows=backfill_rows)
    monkeypatch.setattr(news_collect, "load_queries", lambda: list(queries))

    def fake_get_rss(q):
        if rss_cost:
            news_collect.time.sleep(rss_cost)
        return f"<xml q={q!r}/>"

    monkeypatch.setattr(news_collect, "get_rss", fake_get_rss)
    monkeypatch.setattr(news_collect, "parse_rss", lambda xml: [])
    monkeypatch.setattr(news_collect, "get_og_image", lambda url: None)
    return fake_conn, cursor


def test_main_runs_to_completion_and_commits_even_with_zero_budget(monkeypatch):
    """Дедлайн считается от run_start ВНУТРИ main() (не от какого-то
    внешнего "нулевого" момента) — единственный реалистичный способ
    смоделировать "бюджет исчерпан ДО старта RSS-цикла" это заставить
    сам ПОДГОТОВИТЕЛЬНЫЙ шаг (здесь — load_queries(), в проде тоже не
    мгновенный: SQL по топ-60 ЖК) съесть весь бюджет как побочный
    эффект. Дальше ни один RSS-запрос не должен выполниться, но main()
    всё равно штатно завершает прогон: commit()/close() вызваны, DELETE
    отработал — "процесс штатно завершает уже собранные результаты",
    не аварийный выход."""
    clock = _patch_clock(monkeypatch)

    def _boom(q):
        raise AssertionError("get_rss не должен вызываться при уже исчерпанном бюджете")

    fake_conn, cursor = _patch_db(monkeypatch)

    def _slow_load_queries():
        clock.advance(news_collect.GLOBAL_BUDGET_S + 1)
        return ["q1", "q2", "q3"]

    monkeypatch.setattr(news_collect, "load_queries", _slow_load_queries)
    monkeypatch.setattr(news_collect, "get_rss", _boom)
    monkeypatch.setattr(news_collect, "parse_rss", lambda xml: [])
    monkeypatch.setattr(news_collect, "get_og_image", lambda url: None)

    news_collect.main()

    assert cursor.query_stats == []  # ни одна RSS-стадия не пыталась выполниться
    assert cursor.inserted_news == []
    assert cursor.deleted_old is True  # уборка старья всё равно случилась
    assert fake_conn.closed is True
    assert fake_conn.committed >= 1


def test_main_stops_rss_loop_early_when_budget_runs_out_mid_way(monkeypatch):
    """5 запросов по 60с (по времени get_rss + пауза 2с между) при
    GLOBAL_BUDGET_S=240 -> где-то на 4-м запросе бюджет должен
    закончиться, оставшиеся не выполняются, но то что успело собраться
    — идёт в обработку как обычно (без исключений)."""
    clock = _patch_clock(monkeypatch)
    queries = [f"q{i}" for i in range(5)]
    fake_conn, cursor = _patch_common(monkeypatch, queries, rss_cost=60.0)
    monkeypatch.setattr(news_collect, "enrich_images_playwright", lambda *a, **kw: 0)

    news_collect.main()

    # 240с бюджета / (~60с работы + 2с паузы) за запрос -> не все 5
    # успевают выполниться.
    assert 0 < len(cursor.query_stats) < 5
    assert fake_conn.closed is True


def test_main_never_exceeds_global_budget_summed_across_stages(monkeypatch):
    """Ключевая регрессия задачи: раньше стадии считали НЕЗАВИСИМЫЕ
    бюджеты (сумма 200+48+70+70=388с могла превысить внешний ~300с
    таймаут). Теперь эмулируем RSS-стадию, "потратившую" почти весь
    GLOBAL_BUDGET_S (за счёт rss_cost), и проверяем, что enrich_images_
    playwright ДЛЯ ОБЕИХ стадий (основной список + backfill) либо не
    вызывается вовсе (бюджета не осталось), либо вызывается с бюджетом,
    строго укладывающимся в то, что реально осталось от GLOBAL_BUDGET_S
    — НЕ с фиксированными 70/150, как было раньше."""
    clock = _patch_clock(monkeypatch)
    queries = ["q0"]
    # Одна "RSS-стадия" уже съедает 235 из 240с общего бюджета.
    fake_conn, cursor = _patch_common(monkeypatch, queries, rss_cost=235.0,
                                       backfill_rows=[{"id": 1, "url": "u", "image_url": None}])

    calls = []

    def spy_enrich(items, limit=8, budget=150.0):
        calls.append(budget)
        return 0

    monkeypatch.setattr(news_collect, "enrich_images_playwright", spy_enrich)

    news_collect.main()

    # Ни один зафиксированный вызов не мог получить больше, чем реально
    # оставалось от GLOBAL_BUDGET_S на момент вызова — так как часы
    # управляемые, к моменту первого enrich-вызова прошло ~235с (+2с
    # паузы RSS-цикла), т.е. остаток заведомо < 10с < старых
    # фиксированных 70с.
    for budget in calls:
        assert budget < 10.0, (
            f"stage budget {budget} не может быть больше остатка "
            f"глобального бюджета — регрессия к старой сумме независимых "
            f"бюджетов")


def test_main_skips_playwright_stage_when_remaining_budget_below_minimum(monkeypatch):
    """Остаток бюджета меньше _MIN_PLAYWRIGHT_BUDGET_S -> enrich_images_
    playwright вообще не вызывается для этой стадии (не тратим время на
    запуск браузера ради секунд полезной работы)."""
    clock = _patch_clock(monkeypatch)
    queries = ["q0"]
    fake_conn, cursor = _patch_common(
        monkeypatch, queries,
        rss_cost=news_collect.GLOBAL_BUDGET_S - news_collect._MIN_PLAYWRIGHT_BUDGET_S / 2)

    calls = []
    monkeypatch.setattr(news_collect, "enrich_images_playwright",
                         lambda *a, **kw: calls.append(kw.get("budget", a[2] if len(a) > 2 else None)) or 0)

    news_collect.main()

    assert calls == [], "Playwright-стадия не должна вызываться при остатке < _MIN_PLAYWRIGHT_BUDGET_S"
    assert fake_conn.closed is True


def test_main_inserts_items_collected_before_budget_ran_out(monkeypatch):
    """То, что успело собраться до исчерпания бюджета, реально
    записывается в news (ON CONFLICT DO NOTHING путь через фейковый
    cursor) — "штатно завершает уже собранные результаты", не теряет
    их."""
    clock = _patch_clock(monkeypatch)
    queries = ["q0"]
    fake_conn, cursor = _patch_db(monkeypatch)
    monkeypatch.setattr(news_collect, "load_queries", lambda: list(queries))
    monkeypatch.setattr(news_collect, "get_rss", lambda q: "<xml/>")
    monkeypatch.setattr(news_collect, "parse_rss", lambda xml: [
        {"title": "А" * 25, "url": "https://a.example/1", "source": "Tengrinews", "image": None},
        {"title": "Б" * 25, "url": "https://a.example/2", "source": "Tengrinews", "image": None},
    ])
    monkeypatch.setattr(news_collect, "get_og_image", lambda url: None)
    monkeypatch.setattr(news_collect, "enrich_images_playwright", lambda *a, **kw: 0)

    news_collect.main()

    assert len(cursor.inserted_news) == 2
    urls = {p[2] for p in cursor.inserted_news}
    assert urls == {"https://a.example/1", "https://a.example/2"}
