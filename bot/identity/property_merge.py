"""bot/identity/property_merge.py — Safe Physical Property Merge (задача
2026-08-20, "Safe Physical Property Merge"). Реализует
docs/property_merge_design.md как код — эта миграция/модуль НЕ изобретает
вторую систему поверх design doc, переносит его §1-§9 в SQL/Python как
есть, с двумя явными, задокументированными расширениями (см. "Расхождения
с design doc" ниже) и одним новым инструментом (frozen manifest workflow,
design doc его не описывал — появился в задаче 2026-08-20 по прямой
аналогии с уже работающим паттерном scripts/build_photo_evidence_batch_
manifest.py + scripts/photo_evidence_scan.py::load_candidate_ids).

## Аудит перед реализацией (задача явно требует, 2026-08-20, реальные данные)

- `properties`/`property_listings`/`property_match_candidates`/`property_
  match_review_log` — схема НЕ изменилась с docs/property_merge_design.md
  (migrations 083/084/086/088), доверять можно.
- `property_merge_log` НЕ существовал (только зарезервированное имя в
  докстринге migrations/086) — migrations/092 создаёт его РОВНО по схеме
  design doc §2, ни одного лишнего/недостающего поля.
- Merge-helper'ов в коде ДО этого PR не было — только read-only
  `scripts/audit_merge_canonical_scoring_dry_run.py` (canonical scoring,
  design doc §1 — эта задача ПЕРЕНОСИТ его формулу сюда как единственную
  production-реализацию; сам скрипт после этого PR импортирует её отсюда,
  не хранит свою копию — см. его новый докстринг).
- Реальные accepted-рёбра на момент аудита (2026-08-20): **129** (было 101
  на 2026-08-18, design doc §0/§11) → **85** компонент связности (было 70),
  **214** properties затронуты. Самый большой компонент — ТЕ ЖЕ 15
  properties, что в design doc §0/§1.1 (`25757, 25980, 33466, 34292,
  40195, 42869, 43555, 43587, 43665, 43780, 43819, 44847, 44998, 47225,
  52263`) — не вырос. Распределение размеров: 1×15, 1×7, 2×6, 2×5, 3×4,
  6×3, 70×2. Ни одного компонента с edge_count >= node_count (значит НИ
  ОДНОГО цикла/дублирующего ребра — каждый компонент дерево).
- **1 accepted-пара с ТЕКУЩИМ hard conflict** (rooms mismatch), которой не
  было на момент решения человека: candidate_id=316 (property 3521↔2534,
  listing 1007930903). `evidence_snapshot` на момент accept (2026-08-17)
  показывал rooms_a=3/rooms_b=3 (совпадали) — СЕЙЧАС apartment_listings
  для 1007930903 показывает rooms=2, floor=24, area=68.0 (было в снимке:
  price_a=39800000; сейчас price=32000000) — данные листинга A ИЗМЕНИЛИСЬ
  ПОСЛЕ решения человека (перескрап поверх отредактированного продавцом
  объявления, либо исправление парсера). Это ИМЕННО тот сценарий, который
  задача просит перехватывать: "старый accepted candidate без отдельного
  явного решения" + "текущие данные дают hard conflict" -> BLOCK, не
  трогая существующее решение. Ниже — прямой источник теста
  `test_stale_evidence_current_hard_conflict_blocks`.
- **63/129 accepted-пар** дают "house number mismatch" по сырому
  `extract_house_number()` — НО почти все (62/63) относятся к ОДНОЙ
  15-property группе с ОДИНАКОВЫМ `complex_id` (design doc §1.1: "0
  конфликтов floor/area/rooms/complex_id" в этой группе) — разные корпуса/
  подъезды одного ЖК с непоследовательной нотацией адреса ("Сыганак 25К1"
  vs "Сыганак 25/1" vs "Сыганак 3"), НЕ разные дома. Слепое использование
  сырого house-number mismatch как hard-block заблокировало бы ПОЧТИ
  ПОЛОВИНУ реальных accepted-решений, включая ту самую группу, которую
  design doc явно проверил и признал безопасной. См. "Расхождение 2" ниже
  — `_severe_address_mismatch()` учитывает `complex_id` и находит РОВНО 1
  геннуинно рискованную пару на всех 129 (candidate_id=51107, "Кобланды
  батыра 7" vs "7н", РАЗНЫЕ complex_id/None).
- 0 severe price conflict (>30%) среди 129 accepted.
- 0 stale relist<->concurrent flip среди 129 accepted (в отличие от
  PENDING кандидатов — там 1 живой пример нашла Property Timeline post-
  merge валидация, candidate_id=243, уже 'rejected' человеком независимо).

## Расхождения с design doc — описаны, не адаптированы молча (задача явно требует)

**Расхождение 1 — выбор canonical: identity_status приоритет.** Задача
2026-08-20 просит: "confirmed/merged перед provisional, затем наиболее
ранний/stable, затем deterministic ID". design doc §1 (многофакторный
scoring, 7 факторов) НЕ включает identity_status вообще. Решение:
identity_status — ТИР 0 ПЕРЕД существующим scoring (не вторая система,
расширение существующей: confirmed выигрывает у provisional
безусловно, ПОТОМ сортировка по уже задокументированному 7-факторному
score, ПОТОМ property_id — design doc уже давал property_id как
финальный tie-break, здесь просто добавлен один более приоритетный
уровень сравнения). 'merged' в этот тир попасть не должен вообще — см.
следующий пункт. На сегодняшних данных это НЕ меняет ни одного
результата (properties.identity_status = 100% 'provisional' сейчас,
0 'confirmed' — тир 0 у всех равен, tier не дискриминирует), но код
готов к появлению 'confirmed' (design doc §11: "остаётся ручным флагом
будущего PR" — ручной флаг МОЖЕТ появиться раньше, чем следующая
калибровка scoring).

**Расхождение 2 (новое, не было в design doc вообще) — resolve-through-
merge-chain.** design doc §3 явно говорит: "property_match_candidates,
ссылающиеся на losing_id как candidate_property_id, НЕ трогаем —
остаются историческим фактом". Это значит: ПОСЛЕ первого реального merge
в проде граф accepted-рёбер может содержать `candidate_property_id`,
указывающий на УЖЕ 'merged' (пустую, безlisting'овую) property — наивный
union-find добавил бы такую property как "member" компонента, хотя её
слушать листингов там больше физически нет (они уже репойнтнуты).
`_resolve_live_canonical()` — новая функция, НЕ описанная в design doc
(там просто не было этой проблемы — документ писался ДО первого merge)
— проходит property_merge_log (rolled_back_at IS NULL) до фиксированной
точки перед построением компонент. На сегодняшних данных no-op (0 строк
в property_merge_log до этого PR), но БЕЗ неё первый же реальный canary
merge сломал бы граф для всех последующих plan()-вызовов.

**Расхождение 3 — severe address/house mismatch блокирует merge, хотя
`bot/identity/property_linker.py` трактует house_number mismatch как
SOFT conflict** (задача 2026-08-17, "D": "house_number mismatch
перестать использовать как безусловный auto-reject — сделать soft
conflict/manual review"). Это НЕ противоречие, а разный уровень строгости
для разных стадий: property_linker.py решает "показать ли пару человеку
на ручной проверке" (soft = не скрывать, но и не авто-отклонять); ЭТОТ
модуль решает "выполнить ли необратимую-ish repoint-транзакцию поверх
УЖЕ принятого человеком решения, если факты с тех пор могли устареть"
— задача 2026-08-20 явно требует здесь строже ("severe address/house
mismatch -> BLOCK" — отдельный обязательный тест). `_severe_address_
mismatch()` — НЕ голый `extract_house_number()`-mismatch (см. аудит
выше — 62/63 таких были бы ложным блоком), а mismatch БЕЗ подтверждения
общим `complex_id` — тот самый сигнал, которым design doc §1.1 УЖЕ
пользовался для признания 15-property группы безопасной ("floor=3/
area=68.0/rooms=3/complex_id=2070 одинаковы на ВСЕХ 15"), формализованный
здесь как правило, а не разовое наблюдение аудита.

## Что явно НЕ реализовано в этом PR (задача, п.11)

Никаких production merges не выполняется этим PR/веткой ни разу — только
engine + tests + отдельный read-only real-data dry-run (см.
scripts/audit_property_merge_dry_run.py). auto-accept кандидатов,
изменение candidate status, SigLIP, seller identity, Location Trends,
ML — вне скоупа, не тронуто.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone

_MERGE_TOOL_VERSION = "property_merge_v1"

# Severe price conflict — ТОТ ЖЕ порог, что property_linker.py's
# _PRICE_SEVERE_DIFF_PCT (не второй порог "на глаз") — импортируется
# лениво внутри функций, где нужен (см. _severe_price_conflict), тот же
# паттерн отложенного импорта, что уже используется в этом файле для
# bot.db.pg (избегаем импорта БД-клиента при простом импорте модуля,
# напр. в тестах, которые тестируют чистые функции без БД).


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _normalize_seller_name(raw: str | None) -> str | None:
    """Тот же нормализатор, что property_linker.py/property_timeline.py —
    локально продублирован по тому же принципу (одна строка, не тянуть
    межпакетную приватную зависимость bot.core<->bot.identity)."""
    if not raw:
        return None
    return re.sub(r"\s+", " ", raw.strip()).lower()


# ── связность: union-find по accepted-рёбрам ────────────────────────────

def build_components(edges: list[dict]) -> dict[int, set[int]]:
    """edges: [{"prop_a": int, "prop_b": int, ...}, ...] — УЖЕ resolve-
    through-merge-chain (см. _resolve_live_canonical, вызывающий код —
    _load_accepted_edges — делает это САМ, эта функция принимает готовые
    live property_id и просто строит компоненты; тот же union-find
    алгоритм, что scripts/audit_merge_canonical_scoring_dry_run.py
    (design doc §9: "тот же алгоритм") — вынесен сюда как единственная
    реализация, скрипт импортирует эту функцию, не держит свою копию."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for e in edges:
        a, b = e["prop_a"], e["prop_b"]
        if a is None or b is None or a == b:
            continue
        find(a); find(b)
        union(a, b)

    groups: dict[int, set[int]] = defaultdict(set)
    all_props = {p for e in edges for p in (e["prop_a"], e["prop_b"]) if p is not None}
    for p in all_props:
        groups[find(p)].add(p)
    return {min(members): members for members in groups.values()}


async def _resolve_live_canonical(property_id: int) -> int:
    """'merged' property_id -> её ЖИВОЙ canonical (следует property_
    merge_log цепочкой, rolled_back_at IS NULL, до фиксированной точки).
    Не 'merged' property_id (типичный случай сегодня — 100% properties
    'provisional') -> возвращает как есть, 0 запросов дальше find'а
    самого себя не требуется по построению цикла (см. docstring модуля,
    'Расхождение 2')."""
    from bot.db.pg import fetchval
    seen = {property_id}
    current = property_id
    while True:
        nxt = await fetchval(
            "SELECT canonical_property_id FROM property_merge_log "
            "WHERE losing_property_id = $1 AND rolled_back_at IS NULL "
            "ORDER BY executed_at DESC LIMIT 1",
            current,
        )
        if nxt is None or nxt in seen:
            return current
        seen.add(nxt)
        current = nxt


async def _load_accepted_edges(property_ids: list[int] | None = None) -> list[dict]:
    """Живые accepted-рёбра, resolve-through-merge-chain применён к ОБЕИМ
    сторонам. property_ids=None -> ВСЕ accepted-рёбра всей базы (для
    полного dry-run по всем компонентам); иначе — только рёбра, ХОТЬ ОДНОЙ
    стороной касающиеся этого набора property_id ДО resolve (после
    resolve могут смещаться на другие id, поэтому фильтр по property_
    listings/candidate_property_id делается ДО, не после)."""
    from bot.db.pg import fetch

    if property_ids is None:
        rows = await fetch("""
            SELECT pmc.candidate_id, pl.property_id AS prop_a, pmc.candidate_property_id AS prop_b,
                   pmc.relationship_type, pmc.matcher_version, pmc.listing_id AS listing_a_id
            FROM property_match_candidates pmc
            JOIN property_listings pl ON pl.listing_id = pmc.listing_id
            WHERE pmc.status = 'accepted'
        """)
    else:
        rows = await fetch("""
            SELECT pmc.candidate_id, pl.property_id AS prop_a, pmc.candidate_property_id AS prop_b,
                   pmc.relationship_type, pmc.matcher_version, pmc.listing_id AS listing_a_id
            FROM property_match_candidates pmc
            JOIN property_listings pl ON pl.listing_id = pmc.listing_id
            WHERE pmc.status = 'accepted'
              AND (pl.property_id = ANY($1::int[]) OR pmc.candidate_property_id = ANY($1::int[]))
        """, property_ids)

    edges: list[dict] = []
    for r in rows:
        prop_a = await _resolve_live_canonical(r["prop_a"]) if r["prop_a"] is not None else None
        prop_b = await _resolve_live_canonical(r["prop_b"]) if r["prop_b"] is not None else None
        if prop_a is None or prop_b is None or prop_a == prop_b:
            continue
        edges.append({
            "candidate_id": r["candidate_id"], "prop_a": prop_a, "prop_b": prop_b,
            "relationship_type": r["relationship_type"], "matcher_version": r["matcher_version"],
            "listing_a_id": r["listing_a_id"],
        })
    return edges


# ── canonical property scoring (design doc §1, 7-факторный, перенесено
#    сюда как единственная реализация — scripts/audit_merge_canonical_
#    scoring_dry_run.py импортирует эту функцию) ─────────────────────────

_CANONICAL_WEIGHTS = {
    "completeness": 0.25, "address_consistency": 0.15, "coords_presence": 0.10,
    "history_duration": 0.15, "listing_count": 0.15, "conflict_absence": 0.10, "freshness": 0.10,
}
assert abs(sum(_CANONICAL_WEIGHTS.values()) - 1.0) < 1e-9

# Расхождение 1 (см. докстринг модуля) — ТИР ПЕРЕД 7-факторным score.
# 'merged' физически не должен доходить сюда (resolve-through-merge-chain
# убирает его до построения компонент) — ранг существует только защитно,
# на случай вызова score_canonical_candidates() напрямую с сырыми facts.
_IDENTITY_STATUS_RANK = {"confirmed": 0, "provisional": 1, "merged": 2}


def score_canonical_candidates(members: set[int], facts: dict[int, dict]) -> list[dict]:
    """Взвешенный скоринг (design doc §1) + identity_status тир
    (Расхождение 1). facts: {property_id: {"identity_status", "complex_id",
    "floor", "area_sqm", "rooms", "first_seen_at", "last_seen_at",
    "listings": [...], "n_conflicts": int}} — см. _load_component_facts.
    Возвращает список отсортированный по убыванию предпочтительности,
    scored[0] — canonical."""
    member_facts = [facts[p] for p in members if p in facts]
    if not member_facts:
        return []

    durations = [(f["last_seen_at"] - f["first_seen_at"]).total_seconds() for f in member_facts]
    max_duration = max(durations) or 1.0
    counts = [len(f["listings"]) for f in member_facts]
    max_count = max(counts) or 1
    last_seens = [f["last_seen_at"] for f in member_facts]
    newest = max(last_seens)
    oldest = min(last_seens)
    freshness_span = (newest - oldest).total_seconds() or 1.0

    scored = []
    for f in member_facts:
        completeness = sum([
            f["complex_id"] is not None, f["floor"] is not None,
            f["area_sqm"] is not None, f["rooms"] is not None,
        ]) / 4.0

        addrs = {(l["address"] or "").strip().lower() for l in f["listings"] if l.get("address")}
        address_consistency = 1.0 if len(addrs) <= 1 else 1.0 / len(addrs)

        coords_presence = 1.0 if any(l.get("lat") is not None and l.get("lon") is not None
                                      for l in f["listings"]) else 0.0

        duration = (f["last_seen_at"] - f["first_seen_at"]).total_seconds()
        history_duration = duration / max_duration if max_duration else 0.0

        listing_count = len(f["listings"]) / max_count if max_count else 0.0

        conflict_absence = 1.0 / (1 + f["n_conflicts"])

        freshness = (f["last_seen_at"] - oldest).total_seconds() / freshness_span if freshness_span else 1.0

        subscores = {
            "completeness": completeness, "address_consistency": address_consistency,
            "coords_presence": coords_presence, "history_duration": history_duration,
            "listing_count": listing_count, "conflict_absence": conflict_absence, "freshness": freshness,
        }
        total = sum(_CANONICAL_WEIGHTS[k] * v for k, v in subscores.items())
        scored.append({
            "property_id": f["property_id"], "score": round(total, 4),
            "identity_status": f.get("identity_status") or "provisional",
            "subscores": {k: round(v, 3) for k, v in subscores.items()},
            "n_listings": len(f["listings"]), "n_conflicts": f["n_conflicts"],
            "complex_id": f["complex_id"], "floor": f["floor"], "area_sqm": f["area_sqm"],
            "rooms": f["rooms"], "first_seen_at": _iso(f["first_seen_at"]), "last_seen_at": _iso(f["last_seen_at"]),
        })

    scored.sort(key=lambda s: (
        _IDENTITY_STATUS_RANK.get(s["identity_status"], 1), -s["score"], s["property_id"],
    ))
    return scored


async def _load_component_facts(members: set[int]) -> dict[int, dict]:
    from bot.db.pg import fetch

    prop_ids = list(members)
    props = await fetch(
        "SELECT property_id, identity_status, complex_id, floor, area_sqm, rooms, first_seen_at, last_seen_at "
        "FROM properties WHERE property_id = ANY($1::int[])", prop_ids)
    listings = await fetch("""
        SELECT pl.property_id, al.id AS listing_id, al.address, al.lat, al.lon, al.is_active,
               al.rooms, al.floor, al.area, al.price, al.seller_name, al.first_seen, al.last_seen, al.archived_at
        FROM property_listings pl JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE pl.property_id = ANY($1::int[])
    """, prop_ids)
    conflicts = await fetch("""
        SELECT pl.property_id, count(*) AS n
        FROM property_match_candidates pmc
        JOIN property_listings pl ON pl.listing_id = pmc.listing_id
        WHERE pmc.conflict_reasons IS NOT NULL AND pl.property_id = ANY($1::int[])
        GROUP BY pl.property_id
        UNION ALL
        SELECT pmc.candidate_property_id AS property_id, count(*) AS n
        FROM property_match_candidates pmc
        WHERE pmc.conflict_reasons IS NOT NULL AND pmc.candidate_property_id = ANY($1::int[])
        GROUP BY pmc.candidate_property_id
    """, prop_ids)

    listings_by_prop: dict[int, list[dict]] = defaultdict(list)
    for l in listings:
        listings_by_prop[l["property_id"]].append(dict(l))
    conflict_n: dict[int, int] = defaultdict(int)
    for c in conflicts:
        conflict_n[c["property_id"]] += c["n"]

    facts = {}
    for p in props:
        p = dict(p)
        pid = p["property_id"]
        facts[pid] = {**p, "listings": listings_by_prop.get(pid, []), "n_conflicts": conflict_n.get(pid, 0)}
    return facts


# ── pre-merge revalidation (задача §4) ───────────────────────────────────

def _representative_listing(listings: list[dict]) -> dict | None:
    """Детерминированный выбор ОДНОГО listing'а на сторону для попарных
    conflict-проверок — самый ранний по first_seen (стабильнее/дольше
    наблюдаемый), None first_seen идёт последним, tie-break listing_id
    ASC. Компонент с N>1 листингом на property (сегодня на реальных
    данных — всегда 1, см. Property Timeline post-merge validation) не
    теряет корректности: остальные листинги этой property всё равно
    видны в facts/manifest, просто НЕ участвуют в paarwise revalidation
    второй раз (уже покрыты, если у них ЕСТЬ собственное accepted ребро
    — revalidation идёт по рёбрам, не по декартову произведению всех
    листингов компоненты)."""
    if not listings:
        return None
    return sorted(listings, key=lambda l: (l.get("first_seen") is None, l.get("first_seen") or datetime.min.replace(tzinfo=timezone.utc), l["listing_id"]))[0]


def _severe_address_mismatch(a: dict, b: dict, complex_id_a: int | None, complex_id_b: int | None) -> bool:
    """См. докстринг модуля, 'Расхождение 3' — house number отличается
    И НЕТ общего complex_id, подтверждающего 'один ЖК, разная нотация
    подъезда/корпуса'. Общий complex_id ПЕРЕВЕШИВАЕТ формальное
    расхождение номера дома (найдено на реальных данных: 62/63 сырых
    house-number mismatch среди accepted — именно этот случай)."""
    from bot.identity.property_linker import extract_house_number
    hn_a, hn_b = extract_house_number(a.get("address")), extract_house_number(b.get("address"))
    if hn_a is None or hn_b is None or hn_a == hn_b:
        return False
    shared_complex = complex_id_a is not None and complex_id_a == complex_id_b
    return not shared_complex


def _severe_price_conflict(a: dict, b: dict) -> bool:
    """ТОТ ЖЕ порог (30%), что property_linker.py's _PRICE_SEVERE_DIFF_PCT
    — не второе число "на глаз". Ленивый импорт (см. модульный докстринг)."""
    from bot.identity.property_linker import _PRICE_SEVERE_DIFF_PCT
    pa, pb = a.get("price"), b.get("price")
    if not pa or not pb:
        return False
    diff = abs(pa - pb) / max(pa, pb)
    return diff > _PRICE_SEVERE_DIFF_PCT


def _rooms_mismatch(a: dict, b: dict) -> bool:
    """rooms mismatch — ТОТ ЖЕ hard-conflict, что property_linker.py's
    _is_hard_conflict(_conflict_reasons(...)) (единственный HARD conflict
    там), переиспользуется напрямую (bot.identity — тот же пакет, не
    межпакетный приватный импорт)."""
    from bot.identity.property_linker import _conflict_reasons, _is_hard_conflict
    return _is_hard_conflict(_conflict_reasons(a, b))


def _revalidate_edges(edges: list[dict], listings_by_property: dict[int, list[dict]],
                       complex_id_by_property: dict[int, int | None]) -> list[dict]:
    """Пусто -> компонент безопасен для merge. Каждая найденная проблема
    блокирует ВЕСЬ компонент целиком (задача, явно: "component не должен
    попасть в apply manifest" — не частичный merge за вычетом плохого
    ребра, см. модульный докстринг верхнего уровня про 'no silent partial
    merges'). concurrent-vs-relist mismatch НЕ входит сюда вообще (задача,
    явно: "concurrent listings допустимы и не являются конфликтом сами по
    себе") — см. _current_relationship_summary ниже, она ТОЛЬКО
    информационная."""
    problems: list[dict] = []
    for e in edges:
        a = _representative_listing(listings_by_property.get(e["prop_a"], []))
        b = _representative_listing(listings_by_property.get(e["prop_b"], []))
        if a is None or b is None:
            continue

        if _rooms_mismatch(a, b):
            problems.append({
                "reason": "rooms_mismatch", "candidate_id": e["candidate_id"],
                "prop_a": e["prop_a"], "prop_b": e["prop_b"],
                "detail": f"rooms {a.get('rooms')} (listing {a['listing_id']}) vs "
                          f"{b.get('rooms')} (listing {b['listing_id']})",
            })

        if _severe_address_mismatch(a, b, complex_id_by_property.get(e["prop_a"]),
                                     complex_id_by_property.get(e["prop_b"])):
            problems.append({
                "reason": "severe_address_mismatch", "candidate_id": e["candidate_id"],
                "prop_a": e["prop_a"], "prop_b": e["prop_b"],
                "detail": f"address {a.get('address')!r} (listing {a['listing_id']}) vs "
                          f"{b.get('address')!r} (listing {b['listing_id']}), no shared complex_id",
            })

        if _severe_price_conflict(a, b):
            problems.append({
                "reason": "severe_price_conflict", "candidate_id": e["candidate_id"],
                "prop_a": e["prop_a"], "prop_b": e["prop_b"],
                "detail": f"price {a.get('price')} (listing {a['listing_id']}) vs "
                          f"{b.get('price')} (listing {b['listing_id']})",
            })
    return problems


def _current_relationship_summary(edges: list[dict], listings_by_property: dict[int, list[dict]]) -> list[dict]:
    """ТОЛЬКО информационная — сравнивает сохранённый relationship_type
    (на момент candidate-генерации) с ПЕРЕСЧИТАННЫМ сейчас (property_
    linker.py::classify_relationship на текущих timestamps). Расхождение
    НЕ блокирует (задача, явно) — но снимок идёт в evidence_snapshot
    manifest'а, чтобы оператор видел его перед --apply."""
    from bot.identity.property_linker import classify_relationship
    out = []
    for e in edges:
        a = _representative_listing(listings_by_property.get(e["prop_a"], []))
        b = _representative_listing(listings_by_property.get(e["prop_b"], []))
        if a is None or b is None:
            continue
        current = classify_relationship(a, b)
        out.append({
            "candidate_id": e["candidate_id"], "stored_relationship_type": e.get("relationship_type"),
            "current_relationship_type": current, "matches": current == e.get("relationship_type"),
        })
    return out


async def _load_photo_evidence_summary(candidate_ids: list[int]) -> list[dict]:
    """ТОЛЬКО чтение уже сохранённых property_candidate_photo_evidence
    строк (задача: "photo evidence, только из уже сохранённых данных") —
    никакого SigLIP/пересчёта здесь и нигде в этом модуле."""
    if not candidate_ids:
        return []
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT candidate_id, exact_shared_count, perceptual_shared_count, ai_similar_count,
               shared_unit_specific_count, shared_common_count, max_similarity, processing_status
        FROM property_candidate_photo_evidence
        WHERE candidate_id = ANY($1::int[])
    """, candidate_ids)
    return [dict(r) for r in rows]


def _seller_observations_summary(listings_by_property: dict[int, list[dict]]) -> dict[str, list[str]]:
    """observed_seller_name ПО КАЖДОЙ property компоненты — evidence, НЕ
    identity truth (задача, явно: "seller observations как evidence, но
    не как identity truth" — не объединяет/не дедуплицирует продавцов,
    просто перечисляет, что реально наблюдалось)."""
    out: dict[str, list[str]] = {}
    for pid, listings in listings_by_property.items():
        names = sorted({l["seller_name"] for l in listings if l.get("seller_name")})
        out[str(pid)] = names
    return out


async def _load_review_log_ids(candidate_ids: list[int]) -> list[int]:
    if not candidate_ids:
        return []
    from bot.db.pg import fetch
    rows = await fetch(
        "SELECT id FROM property_match_review_log WHERE candidate_id = ANY($1::int[]) AND decision = 'accepted'",
        candidate_ids)
    return sorted(r["id"] for r in rows)


# ── component hash (frozen manifest — НЕ live-reselecting, см. модульный
#    докстринг "новый инструмент") ───────────────────────────────────────

def _compute_component_hash(property_ids: list[int], candidate_ids: list[int], facts: dict[int, dict]) -> str:
    """Детерминированный snapshot ВСЕХ фактов, от которых зависит
    revalidation/scoring — sha256(canonical JSON). Пересчитывается заново
    на --apply из ЖИВЫХ property_ids/candidate_ids манифеста (НЕ через
    'дай мне текущий список accepted' — та самая ошибка photo-canary,
    задача явно просит не повторять). Расхождение хэша -> fail closed,
    новый plan обязателен."""
    payload: dict = {"candidate_ids": sorted(candidate_ids), "properties": {}}
    for pid in sorted(property_ids):
        f = facts.get(pid, {})
        listings = sorted((
            {
                "listing_id": l["listing_id"], "rooms": l.get("rooms"), "floor": l.get("floor"),
                "area": l.get("area"), "address": l.get("address"), "price": l.get("price"),
                "first_seen": _iso(l.get("first_seen")), "last_seen": _iso(l.get("last_seen")),
                "archived_at": _iso(l.get("archived_at")),
            }
            for l in f.get("listings", [])
        ), key=lambda x: x["listing_id"])
        payload["properties"][str(pid)] = {
            "identity_status": f.get("identity_status"), "complex_id": f.get("complex_id"),
            "floor": f.get("floor"), "area_sqm": f.get("area_sqm"), "rooms": f.get("rooms"),
            "listings": listings,
        }
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── manifest I/O + validation ────────────────────────────────────────────

_REQUIRED_MANIFEST_KEYS = (
    "created_at", "candidate_ids", "property_ids", "canonical_property_id",
    "component_hash", "evidence_snapshot",
)


def validate_manifest_shape(data: dict) -> None:
    """Строгая проверка формы манифеста ПЕРЕД любым использованием —
    тот же принцип строгости, что scripts/photo_evidence_scan.py's
    load_candidate_ids (не принимать произвольный JSON молча)."""
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    missing = [k for k in _REQUIRED_MANIFEST_KEYS if k not in data]
    if missing:
        raise ValueError(f"manifest missing required keys: {missing}")
    if not isinstance(data["candidate_ids"], list) or not data["candidate_ids"]:
        raise ValueError("manifest.candidate_ids must be a non-empty list")
    if not isinstance(data["property_ids"], list) or len(data["property_ids"]) < 2:
        raise ValueError("manifest.property_ids must contain at least 2 properties")
    if data["canonical_property_id"] not in data["property_ids"]:
        raise ValueError("manifest.canonical_property_id must be one of property_ids")
    if not isinstance(data["component_hash"], str) or not data["component_hash"]:
        raise ValueError("manifest.component_hash must be a non-empty string")


def save_manifest(manifest: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    validate_manifest_shape(data)
    return data


# ── planning (read-only) ────────────────────────────────────────────────

async def _plan_one_component(members: set[int], edges: list[dict]) -> dict:
    facts = await _load_component_facts(members)
    listings_by_property = {pid: f["listings"] for pid, f in facts.items()}
    complex_id_by_property = {pid: f["complex_id"] for pid, f in facts.items()}

    problems = _revalidate_edges(edges, listings_by_property, complex_id_by_property)
    if problems:
        return {"status": "blocked", "members": sorted(members), "canonical_property_id": None,
                "losing_property_ids": [], "manifest": None, "blocked_reasons": problems}

    scored = score_canonical_candidates(members, facts)
    if not scored:
        return {"status": "blocked", "members": sorted(members), "canonical_property_id": None,
                "losing_property_ids": [],
                "blocked_reasons": [{"reason": "no_facts", "detail": "properties row missing for all members"}],
                "manifest": None}

    canonical_id = scored[0]["property_id"]
    losing_ids = [s["property_id"] for s in scored[1:]]

    candidate_ids = sorted({e["candidate_id"] for e in edges})
    review_log_ids = await _load_review_log_ids(candidate_ids)
    moved_listing_ids = {
        str(pid): sorted(l["listing_id"] for l in listings_by_property.get(pid, []))
        for pid in losing_ids
    }
    current_relationship = _current_relationship_summary(edges, listings_by_property)
    photo_evidence = await _load_photo_evidence_summary(candidate_ids)
    seller_observations = _seller_observations_summary(listings_by_property)
    matcher_version = ",".join(sorted({e["matcher_version"] for e in edges if e.get("matcher_version")})) or "unknown"

    component_hash = _compute_component_hash(sorted(members), candidate_ids, facts)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_ids": candidate_ids,
        "property_ids": sorted(members),
        "canonical_property_id": canonical_id,
        "component_hash": component_hash,
        "evidence_snapshot": {
            "scoring": scored,
            "moved_listing_ids": moved_listing_ids,
            "decision_source": {"candidate_ids": candidate_ids, "review_log_ids": review_log_ids},
            "current_relationship": current_relationship,
            "photo_evidence": photo_evidence,
            "seller_observations": seller_observations,
        },
        "matcher_version": matcher_version,
        "merge_tool_version": _MERGE_TOOL_VERSION,
    }
    return {"status": "planned", "members": sorted(members), "canonical_property_id": canonical_id,
            "losing_property_ids": losing_ids, "manifest": manifest, "blocked_reasons": []}


async def plan_property_merge(component_property_ids: set[int] | None = None) -> list[dict]:
    """Read-only planning/dry-run mode (задача: "planning mode по
    умолчанию" — эта функция САМА НИЧЕГО не пишет, только apply_property_
    merge(dry_run=False) пишет). component_property_ids=None -> план по
    ВСЕМ текущим accepted-компонентам (для полного real-data dry-run
    отчёта); конкретный набор -> план ТОЛЬКО по компоненту(ам), их
    содержащим (оператор целится в один компонент). Возвращает список
    результатов (и 'blocked', и 'planned') — вызывающий код сам решает,
    какие 'planned'-манифесты сохранить на диск через save_manifest()."""
    edges = await _load_accepted_edges(list(component_property_ids) if component_property_ids else None)
    components = build_components(edges)
    if component_property_ids is not None:
        components = {k: m for k, m in components.items() if m & component_property_ids}
        if not components:
            return []

    results = []
    for members in components.values():
        comp_edges = [e for e in edges if e["prop_a"] in members and e["prop_b"] in members]
        results.append(await _plan_one_component(members, comp_edges))
    return results


# ── apply (frozen manifest only) ─────────────────────────────────────────

async def _already_fully_merged(losing_ids: list[int], canonical_id: int) -> bool:
    """Идемпотентность repeated apply — ЕСЛИ этот РОВНО манифест уже был
    успешно применён (все losing уже смерджены в ЭТОТ canonical), apply
    возвращает 'already_merged' БЕЗ повторной ре-валидации живых рёбер:
    после успешного merge property_listings.property_id для бывших
    losing-листингов уже указывает на canonical, а resolve-through-merge-
    chain (см. _load_accepted_edges) схлопнет соответствующее ребро в
    self-loop и молча УБЕРЁТ его из живого набора — без этой проверки
    apply() принял бы совершенно ожидаемое 'уже сделано' состояние за
    'состав компонента изменился с момента plan' и ошибочно fail-closed'ил
    бы повторный (безопасный, идемпотентный по своей природе) вызов."""
    if not losing_ids:
        return False
    from bot.db.pg import fetchval
    n = await fetchval(
        "SELECT count(DISTINCT losing_property_id) FROM property_merge_log "
        "WHERE losing_property_id = ANY($1::int[]) AND canonical_property_id = $2 AND rolled_back_at IS NULL",
        losing_ids, canonical_id,
    )
    return n == len(losing_ids)


async def apply_property_merge(manifest: dict, *, actor: str, dry_run: bool = True) -> dict:
    """Единственная точка входа для реального repoint. Принимает ТОЛЬКО
    frozen manifest (из plan_property_merge -> save_manifest), НИКОГДА не
    строит собственный "живой" список accepted кандидатов заново (задача,
    явно: "Не повторять ошибку photo-canary с live-reselecting query").
    dry_run=True (ДЕФОЛТ) — проверяет всё (идемпотентность, состав рёбер,
    component_hash, revalidation) и возвращает, ЧТО БЫ произошло, без
    единой записи. dry_run=False — то же самое + реальный commit при
    успехе всех проверок (см. _execute_merge)."""
    validate_manifest_shape(manifest)
    property_ids = sorted(int(x) for x in manifest["property_ids"])
    canonical_id = int(manifest["canonical_property_id"])
    losing_ids = sorted(set(property_ids) - {canonical_id})
    manifest_candidate_ids = sorted(int(x) for x in manifest["candidate_ids"])

    if await _already_fully_merged(losing_ids, canonical_id):
        return {"status": "already_merged", "canonical_property_id": canonical_id,
                "losing_property_ids": losing_ids, "dry_run": dry_run}

    live_edges = await _load_accepted_edges(property_ids)
    # Рёбра, ОБЕ стороны которых лежат ровно в этом наборе property_ids —
    # те же условия, что _plan_one_component использовал при построении
    # manifest'а (comp_edges), не любые рёбра, КОСНУВШИЕСЯ набора.
    scoped_edges = [e for e in live_edges if e["prop_a"] in property_ids and e["prop_b"] in property_ids]
    live_candidate_ids = sorted({e["candidate_id"] for e in scoped_edges})
    if live_candidate_ids != manifest_candidate_ids:
        return {"status": "blocked_stale", "dry_run": dry_run,
                "reason": "accepted candidate set for this component changed since plan — re-plan required",
                "manifest_candidate_ids": manifest_candidate_ids, "live_candidate_ids": live_candidate_ids}

    facts = await _load_component_facts(set(property_ids))
    live_hash = _compute_component_hash(property_ids, live_candidate_ids, facts)
    if live_hash != manifest["component_hash"]:
        return {"status": "blocked_stale", "dry_run": dry_run,
                "reason": "underlying property/listing facts changed since plan — re-plan required",
                "manifest_hash": manifest["component_hash"], "live_hash": live_hash}

    listings_by_property = {pid: f["listings"] for pid, f in facts.items()}
    complex_id_by_property = {pid: f["complex_id"] for pid, f in facts.items()}
    problems = _revalidate_edges(scoped_edges, listings_by_property, complex_id_by_property)
    if problems:
        return {"status": "blocked_conflict", "dry_run": dry_run,
                "reason": "current data now shows a hard conflict not present (or not checked) at accept time",
                "blocked_reasons": problems}

    if dry_run:
        return {"status": "would_apply", "dry_run": True, "canonical_property_id": canonical_id,
                "losing_property_ids": losing_ids, "manifest": manifest}

    return await _execute_merge(manifest, scoped_edges, actor=actor)


async def _execute_merge(manifest: dict, edges: list[dict], *, actor: str) -> dict:
    """Один connected component -> одна Postgres-транзакция (задача,
    явно). SELECT ... FOR UPDATE на ВСЕХ properties компонента в начале
    транзакции — та же защита от конкурентного incremental job, что
    design doc §6 просит (advisory-lock-уровня паттерн, но на уровне
    строк, см. bot/db/pg.py::_apply_migrations для прецедента этого же
    acquire()+transaction() приёма в проекте)."""
    from bot.db.pg import get_pool

    property_ids = sorted(int(x) for x in manifest["property_ids"])
    canonical_id = int(manifest["canonical_property_id"])
    losing_ids = sorted(set(property_ids) - {canonical_id})
    matcher_version = manifest.get("matcher_version") or "unknown"
    merge_group_key = uuid.uuid4()

    edges_by_property: dict[int, list[int]] = defaultdict(list)
    for e in edges:
        edges_by_property[e["prop_a"]].append(e["candidate_id"])
        edges_by_property[e["prop_b"]].append(e["candidate_id"])

    log_rows: list[dict] = []
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.fetch(
                "SELECT property_id FROM properties WHERE property_id = ANY($1::int[]) "
                "ORDER BY property_id FOR UPDATE",
                property_ids,
            )

            for losing_id in losing_ids:
                moved_rows = await conn.fetch(
                    "SELECT listing_id FROM property_listings WHERE property_id = $1", losing_id)
                moved_listing_ids = sorted(r["listing_id"] for r in moved_rows)
                if not moved_listing_ids:
                    # Идемпотентность на уровне ОДНОЙ losing property — уже
                    # репойнтнута прежним прогоном этого же (или другого)
                    # события, ничего переносить не осталось.
                    continue

                await conn.execute(
                    "UPDATE property_listings SET property_id = $1 WHERE property_id = $2",
                    canonical_id, losing_id,
                )
                await conn.execute(
                    "UPDATE properties SET identity_status = 'merged' "
                    "WHERE property_id = $1 AND identity_status != 'merged'",
                    losing_id,
                )

                candidate_ids_for_losing = sorted(set(edges_by_property.get(losing_id, [])))
                review_rows = await conn.fetch(
                    "SELECT id FROM property_match_review_log "
                    "WHERE candidate_id = ANY($1::int[]) AND decision = 'accepted'",
                    candidate_ids_for_losing,
                )
                decision_source = {
                    "candidate_ids": candidate_ids_for_losing,
                    "review_log_ids": sorted(r["id"] for r in review_rows),
                }

                row = await conn.fetchrow(
                    """
                    INSERT INTO property_merge_log
                        (merge_group_key, canonical_property_id, losing_property_id, moved_listing_ids,
                         decision_source, matcher_version, merge_tool_version, dry_run, executed_by)
                    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, FALSE, $8)
                    RETURNING merge_id
                    """,
                    merge_group_key, canonical_id, losing_id, json.dumps(moved_listing_ids),
                    json.dumps(decision_source, default=str, ensure_ascii=False),
                    matcher_version, _MERGE_TOOL_VERSION, actor,
                )
                log_rows.append({"merge_id": row["merge_id"], "losing_property_id": losing_id,
                                  "moved_listing_ids": moved_listing_ids})

    if not log_rows:
        # Все losing properties этого манифеста уже были репойнтнуты
        # (гонка с другим apply того же плана) — идемпотентный no-op,
        # ни одной новой записи в журнал.
        return {"status": "already_merged", "dry_run": False, "canonical_property_id": canonical_id,
                "losing_property_ids": losing_ids}

    return {
        "status": "merged", "dry_run": False, "merge_group_key": str(merge_group_key),
        "canonical_property_id": canonical_id,
        "losing_property_ids": [r["losing_property_id"] for r in log_rows],
        "log_rows": log_rows,
    }


# ── rollback ──────────────────────────────────────────────────────────

async def rollback_property_merge(merge_group_key: "uuid.UUID | str", *, actor: str, reason: str) -> dict:
    """Откат ОДНОЙ merge-операции по её merge_group_key — ТОЛЬКО по
    журналу (design doc §5): repoint обратно ТОЛЬКО moved_listing_ids
    снимок, НЕ 'все текущие листинги canonical' (canonical мог получить
    ЕЩЁ листинги ПОСЛЕ этого merge-события — их откат не трогает).
    Повторный rollback на уже откаченный merge_group_key — идемпотентный
    no-op (fail-closed в смысле 'ничего лишнего не делает', не ошибка)."""
    from bot.db.pg import get_pool

    if isinstance(merge_group_key, str):
        merge_group_key = uuid.UUID(merge_group_key)

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT merge_id, canonical_property_id, losing_property_id, moved_listing_ids "
                "FROM property_merge_log WHERE merge_group_key = $1 AND rolled_back_at IS NULL "
                "ORDER BY merge_id FOR UPDATE",
                merge_group_key,
            )
            if not rows:
                return {"status": "not_found_or_already_rolled_back", "merge_group_key": str(merge_group_key)}

            restored: list[dict] = []
            for r in rows:
                moved = r["moved_listing_ids"]
                if isinstance(moved, str):
                    moved = json.loads(moved)
                await conn.execute(
                    "UPDATE property_listings SET property_id = $1 "
                    "WHERE listing_id = ANY($2::text[]) AND property_id = $3",
                    r["losing_property_id"], moved, r["canonical_property_id"],
                )
                await conn.execute(
                    "UPDATE properties SET identity_status = 'provisional' WHERE property_id = $1",
                    r["losing_property_id"],
                )
                restored.append({"losing_property_id": r["losing_property_id"], "moved_listing_ids": moved})

            # actor встраивается в rollback_reason (не отдельная колонка —
            # design doc §2 схему не расширяем без нужды, см. модульный
            # докстринг про верность существующей схеме).
            full_reason = f"actor={actor}: {reason}"
            await conn.execute(
                "UPDATE property_merge_log SET rolled_back_at = now(), rollback_reason = $1 "
                "WHERE merge_group_key = $2 AND rolled_back_at IS NULL",
                full_reason, merge_group_key,
            )

    return {"status": "rolled_back", "merge_group_key": str(merge_group_key), "restored": restored}
