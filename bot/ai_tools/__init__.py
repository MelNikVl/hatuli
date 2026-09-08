"""bot/ai_tools — read-only AI-tools API поверх уже существующей аналитики
Hatuli (задача "Hatuli API v1 для AI tools / HackAlem", фундамент под
будущий agentic-layer, БЕЗ multi-agent системы и БЕЗ LLM в этом PR).

Что это и чем НЕ является
--------------------------
Это ТОНКАЯ read-only оболочка над уже готовыми core-функциями (property
timeline, risk passport, complex market profile, location detail,
bargain/dom-scenario). Ни одна формула здесь не пересчитывается заново —
каждая функция в core.py документирует, какую именно существующую функцию
она вызывает и откуда взят каждый выдаваемый факт. НЕ ML, НЕ агент, НЕ
изменение семантики Property Identity / Complex Identity / Risk Passport,
НЕ production writes (модуль вообще не импортирует execute()/insert-хелперы
БД — только fetch/fetchrow через уже существующие core-функции).

Структура
---------
  core.py          — 6 read-only capabilities (resolve_listing,
                      get_property_history, get_listing_analysis,
                      get_listing_risks, get_complex_market_profile,
                      get_location_analysis), каждая возвращает ToolResult.
  envelope.py       — единый machine-readable контракт (ToolResult ->
                      {entity_type, entity_id, as_of, data, confidence,
                      evidence, warnings, methodology_version}).
  data_quality.py   — read-only collector health snapshot (Этап 3
                      задачи) — без LLM, без autonomous writes, ничего не
                      чинит, только диагностирует.
  tool_registry.py  — декларативный реестр всех capabilities для будущего
                      orchestrator-агента (name/description/input schema/
                      output schema/read_only=true), без OpenAI/Claude SDK.
  router.py         — тонкий FastAPI-роут (`make_ai_tools_router()`),
                      подключается в bot/admin_web.py тем же способом, что
                      и terminal_extras.make_extras_router() — HTTP-обёртка
                      над core.py/data_quality.py, сама ничего не считает.
"""
