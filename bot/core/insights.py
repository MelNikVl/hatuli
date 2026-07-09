"""
Инвест-инсайты для алертов и карточек объектов.

Всё считается из данных, которые УЖЕ есть в БД — без новых парсеров:
  - Полная доходность = аренда + рост цены (а не только rental yield)
  - Сравнение с банковским депозитом (главный конкурент недвижимости в KZ)
  - Ипотечный платёж (аннуитет) и покрывает ли его аренда
  - Красные флаги из описания: залог, аукцион, обременение
  - Зелёные флаги: торг уместен, срочная продажа (мотивированный продавец)
  - Сводка истории цен: сколько раз снижали и на сколько
  - Владелец vs риелтор (комиссия риелтора ~1-3% — влияет на реальную цену)

Настройки через .env (все с разумными дефолтами):
  DEPOSIT_RATE=14.0        # ставка по депозиту KZT, % годовых (KDIF-максимум меняется — обновляй)
  APPRECIATION_PCT=8.0     # консервативная оценка роста цены кв.м в год, %
  MORTGAGE_RATE=17.0       # рыночная ипотечная ставка, % (льготные 7-20-25/Отбасы ниже)
  MORTGAGE_YEARS=20
  MORTGAGE_DOWN_PCT=20     # первоначальный взнос, %
  REALTOR_FEE_PCT=2.0      # типичная комиссия риелтора при покупке через агента
"""
from __future__ import annotations

import os
import re

DEPOSIT_RATE = float(os.getenv("DEPOSIT_RATE", "14.0"))
APPRECIATION_PCT = float(os.getenv("APPRECIATION_PCT", "8.0"))
MORTGAGE_RATE = float(os.getenv("MORTGAGE_RATE", "17.0"))
MORTGAGE_YEARS = int(os.getenv("MORTGAGE_YEARS", "20"))
MORTGAGE_DOWN_PCT = float(os.getenv("MORTGAGE_DOWN_PCT", "20"))
REALTOR_FEE_PCT = float(os.getenv("REALTOR_FEE_PCT", "2.0"))

# ── Красные / зелёные флаги из текста объявления ─────────────────────────────

_RED_PATTERNS = [
    (r"\bзалог", "⛔ Квартира в залоге — сделка сложнее, нужен юрист"),
    (r"аукцион", "⛔ Аукцион/торги — проверить обременения"),
    (r"обременени", "⛔ Есть обременение — проверить документы"),
    (r"рассрочк[аи] от продавца", "⚠️ Продажа в рассрочку — нестандартная сделка"),
    (r"без документ", "⛔ Проблемы с документами"),
    (r"доля|долев(ая|ой) собствен", "⚠️ Долевая собственность — согласие всех владельцев"),
]

_GREEN_PATTERNS = [
    (r"\bторг\b|торг уместен|возможен торг", "🤝 Продавец открыт к торгу"),
    (r"срочно|в связи с переездом|переезд", "🔥 Срочная продажа — мотивированный продавец, торг агрессивнее"),
    (r"один хозяин|единственный собственник", "✅ Один собственник — чистая история"),
]


def text_flags(description: str | None, title: str | None = None) -> tuple[list[str], list[str]]:
    """Возвращает (red, green) флаги из текста объявления."""
    text = f"{title or ''} {description or ''}".lower()
    red = [msg for pat, msg in _RED_PATTERNS if re.search(pat, text)]
    green = [msg for pat, msg in _GREEN_PATTERNS if re.search(pat, text)]
    return red, green


# ── Финансовые расчёты ────────────────────────────────────────────────────────

def total_return_pct(price: float, monthly_rent: float | None) -> dict:
    """
    Полная годовая доходность = аренда + ожидаемый рост цены.
    Rental yield сам по себе занижает картину (замечание верное:
    квартира обычно дорожает, и это часть дохода).
    """
    rental_yield = (monthly_rent * 12 / price * 100) if (monthly_rent and price) else 0.0
    total = rental_yield + APPRECIATION_PCT
    vs_deposit = total - DEPOSIT_RATE
    return {
        "rental_yield": rental_yield,
        "appreciation": APPRECIATION_PCT,
        "total": total,
        "deposit_rate": DEPOSIT_RATE,
        "vs_deposit": vs_deposit,   # >0 — квартира выгоднее депозита
    }


def mortgage_estimate(price: float) -> dict:
    """Аннуитетный платёж по рыночной ставке."""
    down = price * MORTGAGE_DOWN_PCT / 100
    principal = price - down
    r = MORTGAGE_RATE / 100 / 12
    n = MORTGAGE_YEARS * 12
    if r <= 0:
        monthly = principal / n
    else:
        monthly = principal * r * (1 + r) ** n / ((1 + r) ** n - 1)
    return {
        "down_payment": down,
        "monthly": monthly,
        "rate": MORTGAGE_RATE,
        "years": MORTGAGE_YEARS,
    }


def realtor_note(is_owner, seller_type: str | None, price: float | None) -> str | None:
    """Владелец vs агент: у агента реальная цена выше на комиссию."""
    if is_owner or seller_type == "owner":
        return "✅ От собственника — без комиссии риелтора"
    if seller_type == "agent" and price:
        fee = price * REALTOR_FEE_PCT / 100
        return f"💼 Через риелтора — заложи ещё ~{_fmt(fee)} комиссии ({REALTOR_FEE_PCT:.0f}%)"
    if seller_type == "developer":
        return "🏗 От застройщика"
    return None


def _fmt(p) -> str:
    try:
        return f"{int(round(p)):,} ₸".replace(",", "\u2009")
    except (TypeError, ValueError):
        return "—"


def price_history_note(changes: list[dict]) -> str | None:
    """
    changes: [{old_price, new_price, changed_at}, ...] по одному объявлению,
    отсортировано по времени. Возвращает сводку для карточки.
    """
    if not changes:
        return None
    first_old = changes[0]["old_price"]
    last_new = changes[-1]["new_price"]
    if not first_old or not last_new:
        return None
    diff_pct = (first_old - last_new) / first_old * 100
    n = len(changes)
    if diff_pct > 0:
        return (f"📉 Цена снижалась {n} раз(а), суммарно −{diff_pct:.1f}% "
                f"(с {_fmt(first_old)}) — продавец уступает, дави в торге")
    if diff_pct < 0:
        return f"📈 Цену подняли на +{abs(diff_pct):.1f}% — рынок в этом ЖК греется"
    return None


# ── Сборка блока инсайтов для карточки ────────────────────────────────────────

def build_insights_block(row: dict, price_changes: list[dict] | None = None) -> str:
    """
    row — запись apartment_listings (dict).
    Возвращает готовый HTML-блок строк для телеграм-карточки.
    """
    lines: list[str] = []
    price = row.get("price") or 0
    area = row.get("area")
    est_rent = row.get("est_rent")

    # Цена за м²
    if price and area:
        lines.append(f"📐 {_fmt(price / area)}/м² · {area:.0f} м²")

    # Полная доходность vs депозит
    if price:
        tr = total_return_pct(price, est_rent)
        if tr["rental_yield"] > 0:
            verdict = ("выгоднее депозита" if tr["vs_deposit"] > 0
                       else "депозит доходнее — брать только с дисконтом")
            lines.append(
                f"💹 Аренда {tr['rental_yield']:.1f}% + рост ~{tr['appreciation']:.0f}% "
                f"= <b>{tr['total']:.1f}%/год</b> против депозита {tr['deposit_rate']:.0f}% — {verdict}"
            )

    # Ипотека: платёж и покрывает ли его аренда
    if price:
        m = mortgage_estimate(price)
        cover = ""
        if est_rent:
            ratio = est_rent / m["monthly"] * 100
            cover = f", аренда покрывает {ratio:.0f}% платежа"
        lines.append(
            f"🏦 Ипотека {m['rate']:.0f}%/{m['years']}л: взнос {_fmt(m['down_payment'])}, "
            f"платёж ~{_fmt(m['monthly'])}/мес{cover}"
        )

    # Владелец / риелтор
    rn = realtor_note(row.get("is_owner"), row.get("seller_type"), price)
    if rn:
        lines.append(rn)

    # История цен
    phn = price_history_note(price_changes or [])
    if phn:
        lines.append(phn)

    # Ремонт, если распарсен
    renovation = row.get("renovation")
    if renovation:
        lines.append(f"🔨 Ремонт: {renovation}")

    # Флаги из описания
    red, green = text_flags(row.get("description"), row.get("title"))
    lines.extend(red)
    lines.extend(green)

    return "\n".join(lines)
