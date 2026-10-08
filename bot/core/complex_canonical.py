"""Канонический ЖК для аналитики (задача 2026-10-03; миграция 101).

Справочник complexes наполнялся из многих источников (Крыша, Korter, Homsters,
homeportal, текст объявлений), поэтому:
  * один и тот же ЖК бывает заведён несколькими записями («TAMASHA» и
    «Tamasha Город Астана», «GreenLine. Garden» и «Бигвилль GreenLine. Garden»);
  * часть «ЖК» — обрывки текста объявления, обрезанные на 30 символах
    («Sandi Qala 2 Продается 3-Комнат», «Имеется Тёплый Паркинг (Доступн»).
В отчёте застройщику это ложные конкуренты и расщеплённая статистика.

Правила (физически записи не сливаются — только canonical_id/canonical_reason):
  1. name_prefix — имя-обрывок начинается с имени реального ЖК (≥ 4 символов),
     далее идёт служебный текст → однозначный ЖК (самое длинное совпадение).
  2. krisha_slug — записи с одинаковой ссылкой Крыши на ЖК (/complex/show/<город>/<slug>/),
     если имена — варианты одного (или запись — обрывок). Каноническая — не-обрывок,
     затем зонтик/с классом/новостройка, затем больше объявлений, затем меньший id.
     Общий префикс, номер очереди и общий неоднозначный slug не доказывают дубль.
     Дома под зонтиком (parent_complex_id) не сливаются с зонтиком.
  3. junk_unmatched — обрывок без реального ЖК → в аналитике не используется
     (объявление останется без ЖК, а не с ложным).
"""
from __future__ import annotations

import re
from collections import defaultdict

_SLUG_RE = re.compile(r"/complex/show/[^/]+/([^/?#]+)", re.I)
_JUNK_RE = re.compile(
    r"(продается|продаётся|продам|от надёжного|от надежного|от известного|от топового|застр|"
    r"имеется|улиц|просп|подъезд|класса|расположен|с видом|отличается|позволяет|просторн|"
    r"квартир|комнатн|хороший|хорошем|ключи в|на стадии|первая линия|первой линии|экологич|"
    r"многофункцион|с потолками|— это|—|презентабельн|развивающ|левобережь|выезд)", re.I)
_LEADING_JUNK_RE = re.compile(r"^\s*(на|в|с|-|—|–)\s", re.I)
_PREFIX_STRIP_RE = re.compile(r'^(жк|кг|ЖК)\s*["«]?', re.I)


def krisha_slug(url: str | None) -> str | None:
    if not url:
        return None
    m = _SLUG_RE.search(url)
    return m.group(1).lower().strip() if m else None


def norm_name(name: str | None) -> str:
    s = _PREFIX_STRIP_RE.sub("", (name or "").strip())
    s = re.sub(r"[«»\"'“”]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


_CLASS_WORDS = {"эконом", "комфорт", "комфорт+", "бизнес", "бизнес+", "премиум", "элит", "элитный",
                "стандарт", "комфорт класса", "бизнес класса", "премиум класса"}
_BRAND_PREFIX_RE = re.compile(r"^(бигвилль|bigville)\s+", re.I)
_DEVELOPER_SUFFIX_RE = re.compile(
    r"\s+от\s+(?:nak|нак|bi|bi group|би групп|bazis[- ]a|базис[- ]а|"
    r"sensata|sensata group|сенсата|orda invest|орда инвест)$", re.I)
_CITY_SUFFIX_RE = re.compile(r"(?:,\s*|\s+)(?:город|г\.)\s+(?:астана|нур[- ]султан)$", re.I)
_SEPARATE_RELATIONS = {"sibling_phase", "same_umbrella_project", "separate_neighbor_complex"}


def is_junk_name(name: str | None, *, trusted: bool = False) -> bool:
    """Имя — обрывок текста объявления, а не название ЖК.
    trusted — у записи есть класс или она из источника новостроек: такое имя бывает
    «склеено» со служебным текстом при парсинге, но ЖК реальный; обрывком считаем
    только явную фразу в начале («На …», «- …»)."""
    n = (name or "").strip()
    if not n:
        return False
    if _LEADING_JUNK_RE.search(n) or norm_name(n) in _CLASS_WORDS:
        return True
    if trusted:
        return False
    if _JUNK_RE.search(n):
        return True
    # обрезка источником на 30-31 символе при 4+ словах — признак фразы, а не названия
    return len(n) >= 30 and len(n.split()) >= 4


def _core(name: str | None) -> str:
    n = _BRAND_PREFIX_RE.sub("", norm_name(name))
    # Удаляем только полное имя известного застройщика в самом конце.
    # «от NAK 2 очередь» / «от BI Group Garden» содержат идентификатор проекта,
    # а неизвестный хвост «от …» тоже требует review, не автоматического strip.
    # Номера очередей, Garden/Headliner и прочие собственные названия сохраняются.
    while True:
        stripped = _DEVELOPER_SUFFIX_RE.sub("", _CITY_SUFFIX_RE.sub("", n)).strip()
        if stripped == n:
            return n
        n = stripped


def names_related(a: str | None, b: str | None) -> bool:
    """Точные варианты с явными метаданными («Бигвилль X», «X от NAK»).

    Общий префикс/подстрока не доказывает, что это один ЖК: «Нурсая 2» и
    «Нурсая», «Arena Towers» и «Arena» могут быть разными проектами/очередями.
    """
    x, y = _core(a), _core(b)
    return len(x) >= 3 and x == y


def _trusted(c: dict) -> bool:
    return bool(c.get("housing_class") or c.get("is_newbuild"))


def _prefix_match(junk: str, real: dict[str, set[int]]) -> int | None:
    j = norm_name(junk)
    best: set[int] = set()
    best_len = 0
    for rn, ids in real.items():
        if len(rn) < 4 or len(rn) < best_len or not j.startswith(rn):
            continue
        tail = j[len(rn):]
        # «Нурсая-2 Продается» не является обрывком имени «Нурсая».
        # После точного имени допускается только явно служебный текст.
        if tail:
            if tail[0] not in " ,:;—–-":
                continue
            description = tail.lstrip(" ,:;—–-")
            if not (_JUNK_RE.match(description) or _LEADING_JUNK_RE.match(description)):
                continue
        if len(rn) > best_len:
            best, best_len = set(ids), len(rn)
        else:
            best.update(ids)
    return next(iter(best)) if len(best) == 1 else None


def compute_canonical(complexes: list[dict], listing_counts: dict[int, int],
                      reviewed_relations: list[dict] | None = None) -> dict[int, tuple[int | None, str | None]]:
    """Чистая функция. complexes: [{id, name, krisha_url, parent_complex_id, is_umbrella,
    housing_class, is_newbuild}] (уже без мусора/улиц). Возвращает {id: (canonical_id, reason)}
    только для НЕканонических записей; канонические — (None, None).
    reviewed_relations — факты complex_relations; отдельные проекты/очереди
    нельзя свести даже через промежуточную запись. Positive duplicate/renamed
    пока не создают алиасы автоматически: для них нужен отдельный review flow.
    """
    out: dict[int, tuple[int | None, str | None]] = {c["id"]: (None, None) for c in complexes}
    roots = {cid: cid for cid in out}
    component_members = {cid: {cid} for cid in out}
    separate: dict[int, set[int]] = defaultdict(set)
    for relation in reviewed_relations or []:
        if relation["relation_type"] in _SEPARATE_RELATIONS:
            a, b = relation["complex_id_a"], relation["complex_id_b"]
            separate[a].add(b)
            separate[b].add(a)

    def root(cid: int) -> int:
        if roots[cid] != cid:
            roots[cid] = root(roots[cid])
        return roots[cid]

    def assign(cid: int, target: int, reason: str) -> bool:
        source_root, target_root = root(cid), root(target)
        if source_root == target_root:
            return True
        # Проверяем не только предлагаемую пару, но и уже присоединённые алиасы:
        # A→B, B→C также запрещено, если reviewer разделил A и C.
        if any(separate[mid] & component_members[target_root] for mid in component_members[source_root]):
            return False
        roots[source_root] = target_root
        component_members[target_root].update(component_members.pop(source_root))
        out[cid] = (target_root, reason)
        return True

    def junk(c: dict) -> bool:
        return is_junk_name(c["name"], trusted=_trusted(c))

    def rank(c: dict):
        return (junk(c), not c.get("is_umbrella"), not c.get("housing_class"),
                not c.get("is_newbuild"), -listing_counts.get(c["id"], 0), c["id"])

    # 1) Обрывки текста с именем реального ЖК в начале — по имени. Это сильнее ссылки
    #    Крыши у самой записи complexes: krisha_url таким записям проставлялся из одного
    #    объявления и бывает чужим («Arena Towers От Надёжного Застр» → slug Tandau).
    #    У объявлений со своей ссылкой на ЖК работает правило url в complex_binding.
    real: dict[str, set[int]] = defaultdict(set)
    for c in complexes:
        if not junk(c) and norm_name(c["name"]):
            real[norm_name(c["name"])].add(c["id"])
    for c in complexes:
        if junk(c) and not c.get("parent_complex_id"):
            target = _prefix_match(c["name"], real)
            if target and target != c["id"]:
                assign(c["id"], target, "name_prefix")

    # 2) Одинаковая ссылка Крыши — варианты одного имени и оставшиеся обрывки.
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in complexes:
        s_ = krisha_slug(c.get("krisha_url"))
        if s_ and out[c["id"]][0] is None:
            groups[s_].append(c)
    for members in groups.values():
        heads = [m for m in members if not m.get("parent_complex_id")]  # дома под зонтиком — сами по себе
        if len(heads) < 2:
            continue
        families: dict[str, list[dict]] = defaultdict(list)
        for m in heads:
            if not junk(m) and len(_core(m["name"])) >= 3:
                families[_core(m["name"])].append(m)
        # Не выбираем обрывок каноном. В неоднозначном slug объединяем только
        # доказанные варианты внутри каждой семьи, сохраняя разные ЖК/очереди.
        for variants in families.values():
            canon = min(variants, key=rank)
            for m in sorted(variants, key=rank):
                if m["id"] != canon["id"]:
                    assign(m["id"], canon["id"], "krisha_slug")
        if len(families) == 1 and len({root(m["id"]) for m in next(iter(families.values()))}) == 1:
            canon = min(next(iter(families.values())), key=rank)
            for m in heads:
                n = norm_name(m["name"])
                # Только безымянный служебный текст можно уточнить по уникальному
                # slug. «Нурсая 2 Продается» нельзя назначать «Нурсае» по чужой ссылке.
                if junk(m) and (_JUNK_RE.match(n) or _LEADING_JUNK_RE.match(n) or n in _CLASS_WORDS):
                    assign(m["id"], canon["id"], "krisha_slug")

    # Повторяющиеся названия могли стать однозначными после доказанного сведения
    # по slug. Без такого доказательства порядок строк/число объявлений не решают
    # неоднозначность имени.
    resolved_real = {n: {root(cid) for cid in ids} for n, ids in real.items()}
    for c in complexes:
        if junk(c) and not c.get("parent_complex_id") and out[c["id"]][0] is None:
            target = _prefix_match(c["name"], resolved_real)
            if target is not None:
                assign(c["id"], target, "name_prefix")

    # 3) Обрывки, которые не удалось свести, — в аналитике не используются.
    for c in complexes:
        if junk(c) and out[c["id"]][0] is None:
            out[c["id"]] = (None, "junk_unmatched")
    # цепочки (A→B, B→C) сворачиваем к конечному
    for cid, (tgt, reason) in list(out.items()):
        seen = {cid}
        while tgt is not None and out.get(tgt, (None, None))[0] is not None and tgt not in seen:
            seen.add(tgt)
            tgt = out[tgt][0]
        out[cid] = (tgt, reason)
    return out


async def apply_canonical() -> dict:
    from bot.db.pg import execute, fetch
    rows = [dict(r) for r in await fetch("""
        SELECT id, name, krisha_url, parent_complex_id, is_umbrella, housing_class, is_newbuild,
               canonical_id, canonical_reason
          FROM complexes WHERE COALESCE(is_garbage, FALSE) = FALSE AND COALESCE(is_street, FALSE) = FALSE""")]
    counts = {r["cid"]: r["n"] for r in await fetch("""
        SELECT complex_id AS cid, count(*) AS n FROM apartment_listings
         WHERE complex_id IS NOT NULL GROUP BY 1""")}
    relations = [dict(r) for r in await fetch("""
        SELECT complex_id_a, complex_id_b, relation_type FROM complex_relations""")]
    res = compute_canonical(rows, counts, reviewed_relations=relations)
    changed = 0
    stats = {"krisha_slug": 0, "name_prefix": 0, "junk_unmatched": 0}
    for r in rows:
        tgt, reason = res[r["id"]]
        if reason:
            stats[reason] += 1
        if (tgt, reason) != (r["canonical_id"], r["canonical_reason"]):
            await execute("UPDATE complexes SET canonical_id = $2, canonical_reason = $3 WHERE id = $1",
                          r["id"], tgt, reason)
            changed += 1
    stats["changed"] = changed
    return stats
