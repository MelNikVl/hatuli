"""Тесты глубокого обхода каталога Крыши (service_apartments.py) — задача
2026-09-28, две части:

1. Ложное "круг завершён". По apartments.log за 28.09: когда падала вся
   пачка страниц разом ("[Errno -3] Temporary failure in name resolution",
   2878 упавших страниц за сутки), analyze_apartments возвращал пустой
   список, и старый код трактовал это ровно как "дошли до конца выдачи":
   засчитывал круг, сбрасывал курсор и обнулял снимок max_deep_page. За
   20 часов так "завершилось" 13 кругов подряд — реального обхода
   каталога не было. Побочно это ломало archive_check, для которого
   DEEP_SWEEP_CIRCLE_STARTED_AT — порог "пропало из каталога".

2. Дедлайн круга (<= 2 суток). Размер батча больше не фиксирован —
   _plan_deep_batch() подбирает его из остатка бюджета круга.

_plan_deep_batch вынесена чистой функцией именно ради этих тестов —
вся арифметика дедлайна проверяется без БД и без сети. Развилку
"сеть легла" / "конец выдачи" гонять через настоящий run_cycle нельзя
(там половина тела — запись в БД и синк в Sheets), поэтому
зафиксировано само решение на тех значениях stats, которые реально
приходят из parse_apartments_for_sale.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from service_apartments import (  # noqa: E402
    _CIRCLE_PLAN_SAFETY,
    _CYCLE_PERIOD_SEED_MIN,
    _parse_iso,
    _plan_deep_batch,
)

# Живые цифры прода на 28.09.2026 (app_settings + parser_cycle_history):
# каталог ~832 страницы, обход стартует с PARSER_MAX_PAGES+1 = 9,
# цикл ~70 мин работы + 30-70 мин паузы = ~120 мин период.
CATALOG_PAGES = 832
FIRST_DEEP_PAGE = 9
PAGES_PER_CIRCLE = CATALOG_PAGES - FIRST_DEEP_PAGE + 1  # 824
REAL_PERIOD_MIN = 120.0
TARGET_HOURS = 48.0


def _simulate_circle(*, base_batch=40, max_batch=160, period_min=REAL_PERIOD_MIN,
                     target_hours=TARGET_HOURS, stall_hours=0.0,
                     stall_at_page=None):
    """Прогоняет круг цикл за циклом так же, как это делает run_cycle:
    на каждом шаге планируем батч от ТЕКУЩЕГО остатка и прошедшего
    времени. Возвращает (часы_на_круг, максимальный_использованный_батч).

    stall_hours/stall_at_page — простой (сеть легла): время идёт, курсор
    стоит. Ровно то, что чинит правка №1, и ровно то, что планировщик
    батча должен потом нагнать."""
    cursor = FIRST_DEEP_PAGE
    elapsed_min = 0.0
    max_used = 0
    stalled = False
    guard = 0
    while cursor <= CATALOG_PAGES:
        guard += 1
        assert guard < 10_000, "круг не сходится — планировщик не двигает курсор"
        if stall_at_page is not None and not stalled and cursor >= stall_at_page:
            stalled = True
            elapsed_min += stall_hours * 60.0  # курсор НЕ двигается
        batch = _plan_deep_batch(
            base_batch=base_batch, max_batch=max_batch,
            pages_left=CATALOG_PAGES - cursor + 1,
            elapsed_min=elapsed_min, period_min=period_min,
            target_hours=target_hours,
        )
        max_used = max(max_used, batch)
        cursor += batch
        elapsed_min += period_min
    return elapsed_min / 60.0, max_used


# ── 1. Планировщик батча: базовое поведение ───────────────────────────

def test_batch_never_below_base():
    """Идём с огромным запасом (круг только начался, бюджет целый) — батч
    не опускается ниже базового: тормозить обход незачем."""
    batch = _plan_deep_batch(
        base_batch=40, max_batch=160, pages_left=10,
        elapsed_min=0.0, period_min=REAL_PERIOD_MIN, target_hours=TARGET_HOURS)
    assert batch == 40


def test_batch_never_above_max():
    batch = _plan_deep_batch(
        base_batch=40, max_batch=160, pages_left=100_000,
        elapsed_min=0.0, period_min=REAL_PERIOD_MIN, target_hours=TARGET_HOURS)
    assert batch == 160


def test_batch_grows_when_behind_schedule():
    """Половина бюджета проедена, а пройдена только четверть каталога —
    батч обязан подняться выше базового."""
    on_track = _plan_deep_batch(
        base_batch=40, max_batch=160, pages_left=PAGES_PER_CIRCLE,
        elapsed_min=0.0, period_min=REAL_PERIOD_MIN, target_hours=TARGET_HOURS)
    behind = _plan_deep_batch(
        base_batch=40, max_batch=160, pages_left=int(PAGES_PER_CIRCLE * 0.75),
        elapsed_min=24 * 60.0, period_min=REAL_PERIOD_MIN, target_hours=TARGET_HOURS)
    assert behind > on_track >= 40


def test_budget_already_spent_uses_max_batch():
    """Бюджет проеден целиком — закрываем круг максимально быстро, а не
    делим на ноль оставшихся циклов."""
    batch = _plan_deep_batch(
        base_batch=40, max_batch=160, pages_left=300,
        elapsed_min=100 * 60.0, period_min=REAL_PERIOD_MIN, target_hours=TARGET_HOURS)
    assert batch == 160


def test_no_pages_left_returns_base():
    """max_deep_page ещё не снят (KRISHA_TOTAL_FOUND пуст) — планировать
    нечего, работаем базовым батчем, а не потолком."""
    assert _plan_deep_batch(
        base_batch=40, max_batch=160, pages_left=0,
        elapsed_min=0.0, period_min=REAL_PERIOD_MIN,
        target_hours=TARGET_HOURS) == 40


def test_deep_sweep_disabled_stays_disabled():
    """DEEP_SWEEP_BATCH=0 — обход выключен админом; планировщик не имеет
    права включить его обратно."""
    assert _plan_deep_batch(
        base_batch=0, max_batch=160, pages_left=PAGES_PER_CIRCLE,
        elapsed_min=0.0, period_min=REAL_PERIOD_MIN, target_hours=TARGET_HOURS) == 0


def test_absurd_period_does_not_divide_by_zero():
    for period in (0.0, -5.0):
        batch = _plan_deep_batch(
            base_batch=40, max_batch=160, pages_left=PAGES_PER_CIRCLE,
            elapsed_min=0.0, period_min=period, target_hours=TARGET_HOURS)
        assert 40 <= batch <= 160


def test_plan_reserves_safety_margin():
    """Планируем на safety-долю бюджета, а не на весь: иначе любой простой
    в конце круга сразу выносит за дедлайн, а поднимать батч уже поздно."""
    assert 0.0 < _CIRCLE_PLAN_SAFETY < 1.0
    hours, _ = _simulate_circle()
    assert hours <= TARGET_HOURS * _CIRCLE_PLAN_SAFETY + REAL_PERIOD_MIN / 60.0


# ── 2. Дедлайн круга: цель задачи — полный обход не дольше 2 суток ─────

def test_healthy_circle_fits_two_days():
    hours, max_used = _simulate_circle()
    assert hours <= TARGET_HOURS, f"круг занял {hours:.1f} ч"
    # На здоровом темпе догон не нужен — потолок не задействован.
    assert max_used < 160


def test_circle_fits_two_days_on_slow_cycles():
    """Худший наблюдённый темп: цикл 85 мин (max по parser_cycle_history
    за 14 дней) + максимальная пауза 70 мин = 155 мин период."""
    hours, _ = _simulate_circle(period_min=155.0)
    assert hours <= TARGET_HOURS, f"круг занял {hours:.1f} ч"


def test_circle_fits_two_days_after_long_outage():
    """12-часовой простой в середине круга (ровно то, что случилось 27-28.09
    с DNS) — планировщик обязан нагнать оставшееся и всё равно уложиться."""
    hours, max_used = _simulate_circle(stall_hours=12.0, stall_at_page=400)
    assert hours <= TARGET_HOURS, f"круг занял {hours:.1f} ч"
    assert max_used > 40, "после простоя батч обязан был вырасти"


def test_circle_fits_two_days_when_outage_hits_at_the_very_end():
    """Худший случай для планировщика: простой под самый конец круга
    (стр. 700 из 832), когда циклов на догон почти не осталось и вытянуть
    может только потолок батча. Держит дедлайн ровно за счёт того, что
    планировали на 85% бюджета, а не на все 100%."""
    hours, max_used = _simulate_circle(stall_hours=12.0, stall_at_page=700)
    assert hours <= TARGET_HOURS, f"круг занял {hours:.1f} ч"
    assert max_used == 160, "догон под конец обязан упереться в потолок"


def test_circle_fits_two_days_after_twenty_hour_outage():
    hours, _ = _simulate_circle(stall_hours=20.0, stall_at_page=400)
    assert hours <= TARGET_HOURS, f"круг занял {hours:.1f} ч"


def test_fixed_batch_would_have_missed_deadline_on_outage():
    """Контрольный: со СТАРЫМ поведением (батч прибит гвоздями к 40) тот же
    простой выносит круг за 2 суток. Тест ловит регресс — если кто-то
    вернёт фиксированный батч, падать будет здесь, а не в проде."""
    hours, _ = _simulate_circle(stall_hours=12.0, stall_at_page=400,
                                base_batch=40, max_batch=40)
    assert hours > TARGET_HOURS


def test_shorter_target_is_respected():
    """Админ выкрутил цель до суток — планировщик обязан подстроиться."""
    hours, _ = _simulate_circle(target_hours=24.0)
    assert hours <= 24.0


# ── 3. Развилка "сеть легла" против "конец выдачи" ────────────────────
#
# Значения stats — ровно те, что заполняет
# apartment_parser.parse_apartments_for_sale (pages_ok/pages_failed/
# reached_end). Здесь зафиксировано САМО РЕШЕНИЕ, которое принимает
# run_cycle: засчитывать круг завершённым или держать курсор на месте.

def _decide(*, pages_ok, pages_failed, past_end, has_results):
    """Та же развилка, что в run_cycle (см. блок «ГЛУБОКИЙ ОБХОД»).
    Возвращает 'stall' | 'advance' | 'circle_done'."""
    if not past_end and pages_ok == 0 and pages_failed:
        return "stall"
    if has_results and not past_end:
        return "advance"
    return "circle_done"


def test_total_network_failure_is_not_circle_completion():
    """Регресс 28.09: 40 страниц из 40 упали по DNS, объявлений ноль.
    Это НЕ конец выдачи — круг засчитывать нельзя."""
    assert _decide(pages_ok=0, pages_failed=40,
                   past_end=False, has_results=False) == "stall"


def test_normal_batch_advances_cursor():
    assert _decide(pages_ok=40, pages_failed=0,
                   past_end=False, has_results=True) == "advance"


def test_partial_failure_still_advances():
    """Часть страниц прочитана — прогресс есть, курсор двигаем (иначе на
    вечной частичной ошибке круг встанет навсегда). Потеря покрытия
    логируется предупреждением в run_cycle."""
    assert _decide(pages_ok=35, pages_failed=5,
                   past_end=False, has_results=True) == "advance"


def test_cursor_past_snapshot_completes_circle_even_if_network_died():
    """Курсор уже за снимком max_deep_page — каталог обойдён, и то, что
    последняя пачка легла на сети, этого не отменяет."""
    assert _decide(pages_ok=0, pages_failed=40,
                   past_end=True, has_results=False) == "circle_done"


def test_empty_page_from_krisha_completes_circle():
    """Крыша отдала страницу без карточек и НИ ОДИН запрос не упал —
    настоящий конец выдачи."""
    assert _decide(pages_ok=0, pages_failed=0,
                   past_end=False, has_results=False) == "circle_done"


def test_empty_page_amid_network_failures_is_a_stall_not_the_end():
    """Пограничный случай, ради которого развилка НЕ смотрит на
    reached_end: 39 страниц упали, единственная доехавшая пришла без
    карточек. Про конец выдачи мы отсюда ничего не знаем — это простой,
    а не круг. Иначе тот же баг 28.09 воспроизводится на флапающем DNS."""
    assert _decide(pages_ok=0, pages_failed=39,
                   past_end=False, has_results=False) == "stall"


# ── 4. Разбор меток времени из app_settings ───────────────────────────

def test_parse_iso_handles_naive_and_aware():
    aware = _parse_iso("2026-09-28T15:09:22.301261+00:00")
    naive = _parse_iso("2026-09-28T15:09:22.301261")
    assert aware is not None and naive is not None
    assert aware.tzinfo is not None and naive.tzinfo is not None
    assert aware == naive  # наивное читаем как UTC


def test_parse_iso_survives_garbage():
    assert _parse_iso("") is None
    assert _parse_iso(None) is None
    assert _parse_iso("не дата") is None


def test_period_seed_is_sane():
    assert 1.0 <= _CYCLE_PERIOD_SEED_MIN <= 360.0
