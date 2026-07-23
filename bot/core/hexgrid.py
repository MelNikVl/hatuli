"""
Гексагональная сетка (pointy-top, осевые координаты) для микролокального
анализа цен. Чистый Python, без библиотек: H3 даёт только фиксированные
ступени размера (66м / 174м), а нам нужно настраиваемое ребро (дефолт 50м).

Координаты переводятся в метры локальной равнопромежуточной проекцией
вокруг центра Астаны (на масштабе города искажение пренебрежимо), затем
в осевые координаты гексагона (q, r) с кубическим округлением.

hex_id = "q:r" (строка, зависит от ребра — при смене ребра пересчитывается всё).
"""
from __future__ import annotations

import math

# Центр Астаны — точка отсчёта локальной проекции
_LAT0 = 51.128
_LON0 = 71.430
_M_PER_DEG_LAT = 110_574.0
_M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(_LAT0))


def _to_xy(lat: float, lon: float) -> tuple[float, float]:
    return ((lon - _LON0) * _M_PER_DEG_LON, (lat - _LAT0) * _M_PER_DEG_LAT)


def hex_id(lat: float, lon: float, edge_m: float = 50.0) -> str:
    """Осевые координаты гексагона, содержащего точку."""
    x, y = _to_xy(lat, lon)
    # pointy-top: size = длина ребра
    q = (math.sqrt(3) / 3 * x - 1 / 3 * y) / edge_m
    r = (2 / 3 * y) / edge_m
    # кубическое округление
    s = -q - r
    rq, rr, rs = round(q), round(r), round(s)
    dq, dr, ds = abs(rq - q), abs(rr - r), abs(rs - s)
    if dq > dr and dq > ds:
        rq = -rr - rs
    elif dr > ds:
        rr = -rq - rs
    return f"{int(rq)}:{int(rr)}"


_DIRS = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)]


def neighbors(hid: str) -> list[str]:
    """6 соседей первого кольца."""
    q, r = (int(v) for v in hid.split(":"))
    return [f"{q + dq}:{r + dr}" for dq, dr in _DIRS]


def hex_center(hid: str, edge_m: float = 50.0) -> tuple[float, float]:
    """Центр гексагона (lat, lon) — обратное преобразование к hex_id.
    Порт JS-функции hexCenter из dashboard.html (та же формула/проекция)."""
    q, r = (int(v) for v in hid.split(":"))
    x = edge_m * math.sqrt(3) * (q + r / 2)
    y = edge_m * 1.5 * r
    return (_LAT0 + y / _M_PER_DEG_LAT, _LON0 + x / _M_PER_DEG_LON)


def hex_corners(hid: str, edge_m: float = 50.0) -> list[tuple[float, float]]:
    """6 угловых точек (lat, lon) гексагона — для отрисовки полигоном на карте.
    Порт JS-функции hexCorners из dashboard.html."""
    clat, clon = hex_center(hid, edge_m)
    out = []
    for i in range(6):
        a = math.radians(60 * i - 30)
        out.append((
            clat + (edge_m * math.sin(a)) / _M_PER_DEG_LAT,
            clon + (edge_m * math.cos(a)) / _M_PER_DEG_LON,
        ))
    return out
