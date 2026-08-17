"""Тесты для service_apartments.py::_next_cycle_sleep_minutes — задача
2026-08-17 ("интервал apartment parser"): пауза между циклами читается
из app_settings.PARSE_INTERVAL_MIN/MAX (дефолт 30/70, было хардкод
50-80), с валидацией границ и фолбэком на дефолт при некорректной
конфигурации. Random сохранён — тесты фиксируют random.seed(), не
подменяют random.uniform, чтобы не тестировать мимо реальной функции."""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from service_apartments import (
    _next_cycle_sleep_minutes,
    _INTERVAL_ABS_MIN_MINUTES,
    _INTERVAL_ABS_MAX_MINUTES,
    _INTERVAL_FALLBACK_MIN,
    _INTERVAL_FALLBACK_MAX,
)


class _FakeSettings:
    """Минимальная замена bot.db.settings — только get_float, как и
    использует _next_cycle_sleep_minutes."""
    def __init__(self, values: dict[str, float]):
        self._values = values

    def get_float(self, key: str, default: float) -> float:
        return self._values.get(key, default)


def test_default_bounds_used_when_settings_empty():
    s = _FakeSettings({})
    random.seed(1)
    for _ in range(200):
        v = _next_cycle_sleep_minutes(s)
        assert _INTERVAL_FALLBACK_MIN <= v <= _INTERVAL_FALLBACK_MAX


def test_custom_valid_bounds_respected():
    s = _FakeSettings({"PARSE_INTERVAL_MIN": 20.0, "PARSE_INTERVAL_MAX": 40.0})
    random.seed(2)
    for _ in range(200):
        v = _next_cycle_sleep_minutes(s)
        assert 20.0 <= v <= 40.0


def test_min_greater_than_max_falls_back_to_default():
    s = _FakeSettings({"PARSE_INTERVAL_MIN": 100.0, "PARSE_INTERVAL_MAX": 50.0})
    random.seed(3)
    for _ in range(50):
        v = _next_cycle_sleep_minutes(s)
        assert _INTERVAL_FALLBACK_MIN <= v <= _INTERVAL_FALLBACK_MAX


def test_min_equal_max_falls_back_to_default():
    s = _FakeSettings({"PARSE_INTERVAL_MIN": 45.0, "PARSE_INTERVAL_MAX": 45.0})
    random.seed(4)
    for _ in range(50):
        v = _next_cycle_sleep_minutes(s)
        assert _INTERVAL_FALLBACK_MIN <= v <= _INTERVAL_FALLBACK_MAX


def test_absurd_values_clamped_to_absolute_bounds():
    """MIN=-10 (типа "0 пауза"), MAX=100000 (типа "никогда") — оба
    клампятся в [_INTERVAL_ABS_MIN_MINUTES, _INTERVAL_ABS_MAX_MINUTES]
    ПЕРЕД проверкой min<max, не проходят как есть."""
    s = _FakeSettings({"PARSE_INTERVAL_MIN": -10.0, "PARSE_INTERVAL_MAX": 100000.0})
    random.seed(5)
    for _ in range(50):
        v = _next_cycle_sleep_minutes(s)
        assert _INTERVAL_ABS_MIN_MINUTES <= v <= _INTERVAL_ABS_MAX_MINUTES


def test_both_absurdly_high_clamp_to_same_ceiling_falls_back():
    """Оба поля выше потолка -> после клампа оба равны потолку -> min>=max
    -> фолбэк на дефолт (не тихо возвращает "потолок,потолок" — тот же
    принцип "не пытаемся угадать", что и min>max)."""
    s = _FakeSettings({"PARSE_INTERVAL_MIN": 9999.0, "PARSE_INTERVAL_MAX": 9999.0})
    random.seed(6)
    v = _next_cycle_sleep_minutes(s)
    assert _INTERVAL_FALLBACK_MIN <= v <= _INTERVAL_FALLBACK_MAX


def test_randomization_actually_varies():
    """Не константа — за 50 прогонов должно быть больше одного уникального
    значения (сохранена случайность, задача явно требует "сохранить
    random-интервал")."""
    s = _FakeSettings({"PARSE_INTERVAL_MIN": 30.0, "PARSE_INTERVAL_MAX": 70.0})
    random.seed(7)
    values = {round(_next_cycle_sleep_minutes(s), 4) for _ in range(50)}
    assert len(values) > 1
