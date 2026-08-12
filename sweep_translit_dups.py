#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sweep транслит-дублей (задача гейта 2, п.5, после Tandau/Тандау):
группирует ВСЕ complexes по transliterate(norm_name(name)) — если два
РАЗНЫХ complex_id схлопываются в один и тот же ключ после
транслитерации, это кандидат на тот же паттерн, что Tandau/Тандау
(одно имя, два алфавита), не тронутый до сих пор потому что pg_trgm
без транслита их не видит похожими. Только диагностика (как sibling-
sweep для фазы/блока) — ничего не пишет, кандидаты уходят в консоль
для ручного разбора, не в автослияние (в отличие от разовых
merge_*.py под конкретный найденный кейс, тут заранее неизвестно,
тот ли это самый дом или совпадение имени).

Запуск: venv/bin/python sweep_translit_dups.py
"""
import asyncio
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def _haversine_m(lat1, lon1, lat2, lon2):
    import math
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def main():
    from bot.db.pg import init_pool, close_pool, fetch
    from bot.core.entity_resolution import transliterate
    from hype_tracker.homeportal_scan import norm_name

    await init_pool(DATABASE_URL)
    rows = await fetch("""
        SELECT id, name, lat, lon, developer_id FROM complexes
        WHERE COALESCE(is_garbage, FALSE) = FALSE AND COALESCE(is_street, FALSE) = FALSE
    """)
    print(f"complexes к разбору: {len(rows)}")

    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = transliterate(norm_name(r["name"]))
        if not key or len(key) < 3:
            continue
        groups.setdefault(key, []).append(dict(r))

    candidates = {k: v for k, v in groups.items() if len({x["id"] for x in v}) > 1}
    print(f"ключей с >1 complex_id после транслитерации: {len(candidates)}\n")

    same_dev_close_geo, other = [], []
    for key, members in candidates.items():
        ids = [m["id"] for m in members]
        max_dist = None
        if all(m["lat"] is not None and m["lon"] is not None for m in members) and len(members) == 2:
            max_dist = _haversine_m(members[0]["lat"], members[0]["lon"], members[1]["lat"], members[1]["lon"])
        dev_match = len({m["developer_id"] for m in members if m["developer_id"] is not None}) <= 1
        bucket = same_dev_close_geo if (max_dist is not None and max_dist <= 150) else other
        bucket.append((key, members, max_dist, dev_match))

    print(f"=== высокая уверенность (2 id, гео <=150м): {len(same_dev_close_geo)} ===")
    for key, members, dist, dev_match in same_dev_close_geo:
        names = [f"#{m['id']} {m['name']!r}" for m in members]
        print(f"  {key!r}: {names} — {dist:.0f} м, developer_match={dev_match}")

    print(f"\n=== требует ручной проверки (нет гео у обеих сторон, >2 id, или гео >150м): {len(other)} ===")
    for key, members, dist, dev_match in other:
        names = [f"#{m['id']} {m['name']!r}" for m in members]
        dist_s = f"{dist:.0f} м" if dist is not None else "гео недоступно/>2 id"
        print(f"  {key!r}: {names} — {dist_s}")

    print(f"\nИТОГ: кандидатов всего={len(candidates)}, высокая уверенность={len(same_dev_close_geo)}, "
          f"ручная проверка={len(other)}")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
