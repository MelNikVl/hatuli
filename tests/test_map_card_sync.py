"""tests/test_map_card_sync.py — regression-тест синхронизации «маркер
карты -> карточка в правой панели» на главной странице (задача 2026-08-21,
Часть 1). Реальный браузер (Playwright) против реально запущенного FastAPI
(uvicorn), тестируем НАСТОЯЩИЕ функции dashboard.html (focusSideCard,
openSidePanel, renderSidePanelIds, selectCard) — не переписанную копию.

Данные — синтетические, инжектятся напрямую в CARD_HTML/sidePanelShownIds
через page.evaluate(), в обход сетевого слоя (/admin/api/map-points) —
тестируем именно DOM-синхронизацию (её и ломало периодически), а не
пайплайн загрузки данных карты (у него своя, отдельная логика/тесты)."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
_PORT = 8099

# Общий JS-сниппет: 30 карточек-заполнителей (панель реально скроллится,
# иначе "центрирование" тривиально верно даже сломанным кодом — весь
# список помещается в один экран) + 2 именованные целевые карточки в
# РАЗНЫХ местах списка.
_SEED_JS = """() => {
    window.CARD_HTML = window.CARD_HTML || {};
    const fillers = [];
    for (let i = 0; i < 30; i++) {
        const id = 'filler' + i;
        CARD_HTML[id] = '<div style="height:110px;">filler ' + i + '</div>';
        fillers.push(id);
    }
    CARD_HTML['TARGET_A'] = '<div style="height:140px;" data-testcard="A">Card A</div>';
    CARD_HTML['TARGET_B'] = '<div style="height:140px;" data-testcard="B">Card B</div>';
    window.sidePanelShownIds = fillers.slice(0, 12).concat(['TARGET_A'])
        .concat(fillers.slice(12, 24)).concat(['TARGET_B']).concat(fillers.slice(24));
    renderSidePanelIds(window.sidePanelShownIds, undefined, 'объявлений');
    return window.sidePanelShownIds.length;
}"""


@pytest_asyncio.fixture
async def live_server():
    import uvicorn
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    from bot.admin_web import create_admin_app
    from bot.db.compat import BotDB
    db = BotDB("/tmp/__test_map_sync_admin.db")
    await db.init()
    app = create_admin_app(db, admin_password="x", bot_version="test")
    config = uvicorn.Config(app=app, host="127.0.0.1", port=_PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{_PORT}"
    server.should_exit = True
    await task
    await close_pool()


@pytest_asyncio.fixture
async def page(live_server):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        pg = await browser.new_page(viewport={"width": 1400, "height": 900})
        await pg.goto(live_server + "/", wait_until="networkidle", timeout=25000)
        # Ждём, пока сам dashboard.html объявит нужные глобальные функции
        # (скрипт большой, но синхронный — networkidle уже гарантирует это).
        await pg.wait_for_function("typeof renderSidePanelIds === 'function'", timeout=10000)
        await pg.evaluate(_SEED_JS)
        yield pg
        await browser.close()


async def _card_metrics(page, card_id):
    return await page.evaluate(
        """(id) => {
            const body = document.getElementById('side-panel-body');
            const el = document.getElementById('card-' + id);
            if (!el || !body) return null;
            const bodyRect = body.getBoundingClientRect();
            const elRect = el.getBoundingClientRect();
            return {
                pageScrollY: window.scrollY,
                bodyScrollTop: body.scrollTop,
                elCenterY: elRect.top + elRect.height / 2,
                bodyCenterY: bodyRect.top + bodyRect.height / 2,
                selected: el.classList.contains('side-card-selected'),
                flashed: el.classList.contains('side-card-flash'),
                inViewportOfBody: elRect.top >= bodyRect.top - 2 && elRect.bottom <= bodyRect.bottom + 2,
            };
        }""",
        card_id,
    )


@pytest.mark.asyncio
async def test_marker_click_selects_and_centers_correct_card(page):
    """Требование 1, 3, 4, 5: точный listing_id, скролл именно панели (не
    страницы), карточка по центру видимой области, явное selected."""
    await page.evaluate("selectPin('TARGET_A'); openSidePanel(['TARGET_A']);")
    await page.wait_for_function(
        "document.getElementById('card-TARGET_A') && "
        "document.getElementById('card-TARGET_A').classList.contains('side-card-selected')",
        timeout=3000,
    )
    m = await _card_metrics(page, "TARGET_A")
    assert m is not None
    assert m["selected"] is True
    assert m["pageScrollY"] == 0, "должна скроллиться только #side-panel-body, не вся страница"
    assert abs(m["elCenterY"] - m["bodyCenterY"]) < 40, "карточка должна быть примерно по центру панели"
    assert m["inViewportOfBody"] is True

    # Другая карточка НЕ должна быть выделена одновременно.
    other = await _card_metrics(page, "TARGET_B")
    assert other["selected"] is False


@pytest.mark.asyncio
async def test_repeat_click_same_marker_recenters(page):
    """Требование 6: повторный клик по тому же маркеру снова центрирует
    (даже если пользователь успел проскроллить панель вручную)."""
    await page.evaluate("selectPin('TARGET_A'); openSidePanel(['TARGET_A']);")
    await page.wait_for_function(
        "document.getElementById('card-TARGET_A').classList.contains('side-card-selected')", timeout=3000)
    # Сбиваем скролл вручную.
    await page.evaluate("document.getElementById('side-panel-body').scrollTop = 0;")
    m_before = await _card_metrics(page, "TARGET_A")
    assert abs(m_before["elCenterY"] - m_before["bodyCenterY"]) > 40, "скролл должен был реально сбиться"

    await page.evaluate("selectPin('TARGET_A'); openSidePanel(['TARGET_A']);")
    await page.wait_for_timeout(150)  # rAF + один кадр layout
    m_after = await _card_metrics(page, "TARGET_A")
    assert abs(m_after["elCenterY"] - m_after["bodyCenterY"]) < 40, "повторный клик обязан снова отцентрировать"


@pytest.mark.asyncio
async def test_selection_survives_list_rerender(page):
    """Требование 7 (после обновления результатов/фильтров): если карточка
    была выбрана, а список пересобрался (карточка удалена и добавлена
    заново — НЕ тот же DOM-узел), .side-card-selected должен появиться
    на НОВОМ узле автоматически, без повторного клика."""
    await page.evaluate("selectPin('TARGET_A'); openSidePanel(['TARGET_A']);")
    await page.wait_for_function(
        "document.getElementById('card-TARGET_A').classList.contains('side-card-selected')", timeout=3000)
    old_node_marker = await page.evaluate(
        "() => { document.getElementById('card-TARGET_A')._testMarker = true; return true; }"
    )
    assert old_node_marker is True

    # Полная замена списка (типичная ситуация "сменили фильтр") — TARGET_A
    # временно ИСКЛЮЧЕНА, затем список пересобирается снова уже с ней.
    await page.evaluate("""() => {
        const withoutA = window.sidePanelShownIds.filter(id => id !== 'TARGET_A');
        renderSidePanelIds(withoutA, undefined, 'объявлений');
        renderSidePanelIds(window.sidePanelShownIds, undefined, 'объявлений');
    }""")
    is_new_node = await page.evaluate(
        "() => document.getElementById('card-TARGET_A')._testMarker !== true"
    )
    assert is_new_node is True, "узел должен был реально пересоздаться (тест иначе ничего не проверяет)"
    m = await _card_metrics(page, "TARGET_A")
    assert m["selected"] is True, "selected-состояние должно вернуться на новый узел без повторного клика"


@pytest.mark.asyncio
async def test_cluster_click_selects_first_child_and_centers(page):
    """Требование 1 (клик внутри кластера) + случай, когда конкретный
    listing_id заранее неизвестен вызывающему коду (см. focusSideCard без
    явного cardId) — id для selected берётся с реально найденного узла."""
    await page.evaluate("openSidePanel(['TARGET_B', 'TARGET_A']);")
    await page.wait_for_function(
        "document.getElementById('card-TARGET_B') && "
        "document.getElementById('card-TARGET_B').classList.contains('side-card-selected')",
        timeout=3000,
    )
    m = await _card_metrics(page, "TARGET_B")
    assert m["selected"] is True
    # Кластер из 2 карточек короче панели целиком — центрировать физически
    # нечем (centerCardInPanel честно клэмпит scrollTop в 0, см. код), тут
    # достаточно проверить видимость, а не точное позиционирование по центру
    # (то отдельно проверено в test_marker_click_selects_and_centers_correct_card
    # на достаточно длинном списке).
    assert m["inViewportOfBody"] is True


@pytest.mark.asyncio
async def test_id_type_mismatch_does_not_duplicate_card(page):
    """Аудит нашёл первопричину: id разных источников не всегда совпадали
    по ТИПУ (число vs строка) — карточка УЖЕ показанная под id-строкой
    "777" считалась "новой" при повторном рендере с id=777 (числом) и
    пересоздавалась лишним узлом рядом со старым. renderSidePanelIds
    теперь нормализует все id к строке на входе, поэтому строковый и
    числовой вариант одного и того же id — гарантированно один узел."""
    await page.evaluate("""() => {
        CARD_HTML['777'] = '<div>numeric-ish id card</div>';
        // Первый рендер — id строкой (как обычно из Object.keys()).
        renderSidePanelIds(['777', 'filler0', 'filler1'], undefined, 'объявлений');
        document.getElementById('card-777')._testMarker = true;
    }""")
    await page.evaluate("""() => {
        // Второй рендер ТОГО ЖЕ набора — 777 теперь ЧИСЛОМ (как мог прийти
        // JSON с сервера, не через String()) — без нормализации типов старый
        // код не находил совпадения в Set().has() (777 !== "777") и считал
        // карточку отсутствующей: удалял старый узел и создавал новый —
        // итоговое число узлов не менялось (1), но DOM-identity терялась
        // (сброс transient-состояния, доп. работа) — именно это и проверяем.
        renderSidePanelIds([777, 'filler0', 'filler1'], undefined, 'объявлений');
    }""")
    same_node = await page.evaluate(
        "document.getElementById('card-777') && document.getElementById('card-777')._testMarker === true"
    )
    assert same_node is True, "id 777 (число) и '777' (строка) должны считаться ОДНОЙ карточкой, без пересоздания узла"


@pytest.mark.asyncio
async def test_map_click_clears_card_selection(page):
    """closeSidePanel()/clearMapSelection() снимают selected и с карточки,
    не только с пина на карте — оба состояния синхронны."""
    await page.evaluate("selectPin('TARGET_A'); openSidePanel(['TARGET_A']);")
    await page.wait_for_function(
        "document.getElementById('card-TARGET_A').classList.contains('side-card-selected')", timeout=3000)
    await page.evaluate("closeSidePanel();")
    selected_after = await page.evaluate(
        "document.getElementById('card-TARGET_A').classList.contains('side-card-selected')"
    )
    assert selected_after is False
