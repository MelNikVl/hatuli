"""Юридическая защита дольщика в карточке ЖК (задача 2026-08-15, "БВУ/КЖК/
МИО в карточках ЖК") — get_kzk_info(complex_id, developer_id).

**Важно про имя файла**: полный роут `/complex/{complex_id}` НЕ вынесен из
terminal_extras.py целиком (это отдельная большая задача, тут не
запрошена) — вся остальная сборка карточки ЖК по-прежнему живёт инлайн в
`terminal_extras.py::complex_detail()`. Этот модуль узко под одну фичу
(kzk-статус), "роут не знает SQL" применяется только к ней.

Три несостыковки со спекой задачи, найденные проверкой по реальным
данным ДО реализации (см. отчёт в чате 2026-08-15) — исправлены с
подтверждения пользователя:

1. **bank_name не возвращается** — в сыром JSON developers.kz (проверено
   живым запросом) нет поля с названием банка вообще: ключи ровно
   bin/dev/brand/cities/objects/zhk_n/by_city/scheme/flagged/in_reg/zhk/
   phone. kzk_registry физически не может это поле хранить — не
   изобретаем.
2. **warranty_scheme — ОДНО значение на строку**, не три независимых
   булевых флага БВУ/КЖК/МИО — реально в БД только 4 варианта: NULL /
   "Гарантия КЖК" / "Разрешение МИО" / "Участие БВУ". UI показывает
   ОДИН бейдж схемы (или ни одного при конфликте), не комбинацию.
3. **developer_id -> kzk_registry не 1:1**: у одного developer_id может
   быть несколько bin/строк kzk_registry (разные юрлица одного бренда,
   напр. BI Group — 9 разных bin) с РАЗНЫМИ warranty_scheme/
   is_blacklisted. Прямой лукап "один developer_id -> одна строка"
   недостоверен без агрегации.

**Резолюция (3 уровня, от точного к приблизительному)** — подтверждено
пользователем 2026-08-15:

  0. **Точный BIN через `complex_tech_specs.developer_bin`** (ручной ввод
     админом, тот же БИН что уже используется для ссылки на elicense.kz
     в complex_detail.html) — 395 ЖК заполнено, 394 из них РЕАЛЬНО
     совпадают с каким-то `kzk_registry.bin` (проверено JOIN'ом до
     реализации) — самый надёжный источник, на порядок больше покрытия,
     чем оба fuzzy-пути ниже. Отсутствовал в исходной спеке задачи (там
     был только developer_id) — добавлен по факту находки.
  1. **Точный ЖК-матч**: `kzk_registry.zhk_matches` содержит запись с
     этим `complex_id` и confidence >= 0.8 (AUTO_MATCH_THRESHOLD, тот же
     порог что и в kzk_registry_match.py) — конкретное юрлицо для
     конкретного ЖК, важно когда бренд использует разные bin для разных
     проектов.
  2. **Fallback на developer_id**: смотрим ВСЕ строки kzk_registry этого
     застройщика с ПОДТВЕРЖДЁННЫМ матчингом (developer_match_method IN
     ('bin', 'name_fuzzy_auto', 'manual_confirmed') — НЕ 'name_fuzzy_
     review', те матчи ещё не проверены человеком, в проекте уже ловили
     реальные ложные срабатывания вроде "Otau Group" vs "ULYTAU GROUP",
     см. kzk_registry_match.py). Агрегация при нескольких строках:
       - is_blacklisted: "хуже побеждает" — True, если ХОТЯ БЫ одна
         строка blacklisted=True (защита пользователя важнее полноты).
       - warranty_scheme: показываем, только если ВСЕ строки согласны
         (один и тот же scheme, либо все NULL). При конфликте — бейдж
         схемы не показываем вовсе (Unknown ≠ average, docs/verdict_
         strategy.md §3.1) — молчим, а не гадаем какая схема применима к
         именно этому ЖК.
  3. Ничего не найдено с уверенностью -> None. Блок в UI НЕ рендерится
     (а не "🔴 нет защиты" — это было бы ложной уверенностью)."""
from __future__ import annotations

_AUTO_MATCH_THRESHOLD = 0.8  # см. bot/core/entity_resolution.py — тот же порог
_CONFIRMED_METHODS = ("bin", "name_fuzzy_auto", "manual_confirmed")


def _aggregate_kzk_rows(rows: list[dict]) -> dict:
    """"Хуже побеждает" для blacklist, единогласие для scheme — общая
    агрегация для уровней 1 (zhk-матч, теоретически может дать >1 строку)
    и 2 (developer fallback)."""
    is_blacklisted = any(r["is_blacklisted"] for r in rows)
    schemes = {r["warranty_scheme"] for r in rows if r["warranty_scheme"]}
    scheme_conflict = len(schemes) > 1
    warranty_scheme = next(iter(schemes)) if len(schemes) == 1 else None
    snapshot_dates = [r["source_snapshot_date"] for r in rows if r["source_snapshot_date"]]
    return {
        "warranty_scheme": warranty_scheme,
        "scheme_conflict": scheme_conflict,
        "is_blacklisted": is_blacklisted,
        "source_snapshot_date": max(snapshot_dates) if snapshot_dates else None,
        "matched_bins": [r["bin"] for r in rows],
        # has_signal — есть ли вообще что показать: blacklist ИЛИ
        # однозначная схема (включая однозначное "схемы нет вовсе" —
        # это тоже реальная находка, честный "🔴"). При конфликте схемы
        # И отсутствии blacklist — показать нечего, блок молчит.
        "has_signal": is_blacklisted or not scheme_conflict,
    }


async def get_kzk_info(complex_id: int | None, developer_id: int | None) -> dict | None:
    """None, если ничего не нашли с достаточной уверенностью — вызывающая
    сторона (роут/шаблон) в этом случае просто не рендерит блок."""
    from bot.db.pg import fetchrow, fetch

    # Уровень 0 — точный BIN из ручного ввода админом (complex_tech_specs).
    if complex_id is not None:
        ts = await fetchrow(
            "SELECT developer_bin FROM complex_tech_specs WHERE complex_id = $1", complex_id)
        bin_ = (ts["developer_bin"] or "").strip() if ts else ""
        if bin_:
            row = await fetchrow("""
                SELECT bin, warranty_scheme, is_blacklisted, source_snapshot_date
                FROM kzk_registry WHERE bin = $1
            """, bin_)
            if row:
                result = _aggregate_kzk_rows([dict(row)])
                result["match_level"] = "bin_exact"
                return result

    # Уровень 1 — точный ЖК-матч (zhk_matches.complex_id), только
    # confidence >= AUTO_MATCH_THRESHOLD.
    if complex_id is not None:
        rows = await fetch("""
            SELECT k.bin, k.warranty_scheme, k.is_blacklisted, k.source_snapshot_date
            FROM kzk_registry k, jsonb_array_elements(COALESCE(k.zhk_matches, '[]'::jsonb)) AS m
            WHERE (m->>'complex_id')::int = $1
              AND (m->>'confidence')::float >= $2
        """, complex_id, _AUTO_MATCH_THRESHOLD)
        if rows:
            result = _aggregate_kzk_rows([dict(r) for r in rows])
            result["match_level"] = "complex_match"
            return result

    # Уровень 2 — fallback на developer_id, только подтверждённые строки.
    if developer_id is not None:
        rows = await fetch("""
            SELECT bin, warranty_scheme, is_blacklisted, source_snapshot_date
            FROM kzk_registry
            WHERE developer_id = $1 AND developer_match_method = ANY($2::text[])
        """, developer_id, list(_CONFIRMED_METHODS))
        if rows:
            result = _aggregate_kzk_rows([dict(r) for r in rows])
            result["match_level"] = "developer_match"
            return result

    return None
