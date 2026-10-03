"""Канонический ЖК для аналитики (задача 2026-10-03; миграция 101).

Справочник complexes наполнялся из многих источников (Крыша, Korter, Homsters,
homeportal, текст объявлений), поэтому:
  * один и тот же ЖК бывает заведён несколькими записями («TAMASHA» и
    «Tamasha Город Астана», «GreenLine. Garden» и «Бигвилль GreenLine. Garden»);
  * часть «ЖК» — обрывки текста объявления, обрезанные на 30 символах
    («Sandi Qala 2 Продается 3-Комнат», «Имеется Тёплый Паркинг (Доступн»).
В отчёте застройщику это ложные конкуренты и расщеплённая статистика.

Правила (физически записи не сливаются — только canonical_id/canonical_reason):
  1. name_prefix — имя-обрывок начинается с имени реального ЖК (≥ 4 символов,
     граница слова) → этот ЖК (самое длинное совпадение).
  2. krisha_slug — записи с одинаковой ссылкой Крыши на ЖК (/complex/show/<город>/<slug>/),
     если имена — варианты одного (или запись — обрывок). Каноническая — не-обрывок,
     затем зонтик/с классом/новостройка, затем больше объявлений, затем меньший id.
     Дома под зонтиком (parent_complex_id внутри группы) не сливаются с зонтиком.
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
    return _BRAND_PREFIX_RE.sub("", norm_name(name))


def names_related(a: str | None, b: str | None) -> bool:
    """Одна запись — вариант имени другой («Бигвилль X» / «X», «X от NAK» / «X»)."""
    x, y = _core(a), _core(b)
    if not x or not y:
        return False
    short, long_ = sorted((x, y), key=len)
    return len(short) >= 3 and (long_ == short or long_.startswith(short) or short in long_.split()
                                or f" {short}" in f" {long_}")


def _trusted(c: dict) -> bool:
    return bool(c.get("housing_class") or c.get("is_newbuild"))


def _prefix_match(junk: str, real: dict[str, int]) -> int | None:
    j = norm_name(junk)
    best, best_len = None, 0
    for rn, rid in real.items():
        if len(rn) >= 4 and len(rn) > best_len and (j == rn or j.startswith(rn + " ") or j.startswith(rn + ",")
                                                    or j.startswith(rn + "-")):
            best, best_len = rid, len(rn)
    return best


def compute_canonical(complexes: list[dict], listing_counts: dict[int, int]) -> dict[int, tuple[int | None, str | None]]:
    """Чистая функция. complexes: [{id, name, krisha_url, parent_complex_id, is_umbrella,
    housing_class, is_newbuild}] (уже без мусора/улиц). Возвращает {id: (canonical_id, reason)}
    только для НЕканонических записей; канонические — (None, None)."""
    by_id = {c["id"]: c for c in complexes}
    out: dict[int, tuple[int | None, str | None]] = {c["id"]: (None, None) for c in complexes}

    def junk(c: dict) -> bool:
        return is_junk_name(c["name"], trusted=_trusted(c))

    def rank(c: dict):
        return (junk(c), not c.get("is_umbrella"), not c.get("housing_class"),
                not c.get("is_newbuild"), -listing_counts.get(c["id"], 0), c["id"])

    # 1) Обрывки текста с именем реального ЖК в начале — по имени. Это сильнее ссылки
    #    Крыши у самой записи complexes: krisha_url таким записям проставлялся из одного
    #    объявления и бывает чужим («Arena Towers От Надёжного Застр» → slug Tandau).
    #    У объявлений со своей ссылкой на ЖК работает правило url в complex_binding.
    real = {norm_name(c["name"]): c["id"] for c in complexes if not junk(c) and norm_name(c["name"])}
    for c in complexes:
        if junk(c):
            target = _prefix_match(c["name"], real)
            if target and target != c["id"]:
                out[c["id"]] = (target, "name_prefix")

    # 2) Одинаковая ссылка Крыши — варианты одного имени и оставшиеся обрывки.
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in complexes:
        s_ = krisha_slug(c.get("krisha_url"))
        if s_ and out[c["id"]][0] is None:
            groups[s_].append(c)
    for members in groups.values():
        ids = {m["id"] for m in members}
        heads = [m for m in members if m.get("parent_complex_id") not in ids]   # дома под зонтиком — сами по себе
        if len(heads) < 2:
            continue
        canon = min(heads, key=rank)
        for m in heads:
            # krisha_url местами проставлен ошибочно — по slug сводим только варианты
            # одного имени или обрывки; разные имена с общим slug не трогаем (такой slug
            # и в привязке объявлений не используется — неоднозначен).
            if m["id"] != canon["id"] and (junk(m) or names_related(m["name"], canon["name"])):
                out[m["id"]] = (canon["id"], "krisha_slug")

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
    _ = by_id
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
    res = compute_canonical(rows, counts)
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
