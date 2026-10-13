"""Conservative adapters for public retail bank pages, ordinary HTTP only.

No advertised 'up to' rate, business product, floating rate or rollover is used
as a new 12-month fixed-rate deposit. Changes to non-rate terms require review.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

FREEDOM = "https://bankffin.kz/ru/deposits/physical/moneybox"
FREEDOM_CAPITAL = "https://bankffin.kz/ru/deposits/physical/capital"
FREEDOM_STRATEGY = "https://bankffin.kz/ru/deposits/physical/strategy"
FORTE = "https://bank.forte.kz/ru/sber_noadd"
BEREKE = "https://berekebank.kz/ru/personal/deposits/bereke"
HOME_SIMPLE = "https://home.kz/deposits/depozity-v-tenge"
HOME_SAVINGS = "https://home.kz/deposits/depozit-sberegatelnyj"
HOME_PLUS = "https://home.kz/deposits/depozit-sberegatelnyj-plus"
PRODUCT_SOURCES = (FREEDOM, FREEDOM_CAPITAL, FREEDOM_STRATEGY, FORTE, BEREKE, HOME_SIMPLE, HOME_SAVINGS, HOME_PLUS)
INDEX_SOURCES = ("https://bankffin.kz/ru/deposits/physical",
                 "https://bank.forte.kz/ru/deposits", "https://berekebank.kz/ru/personal/deposits",
                 "https://home.kz/deposits")
DOCUMENT_SOURCES = {"https://bankffin.kz/docs/moneybox_condition_ru.pdf": FREEDOM,
                    "https://bankffin.kz/docs/capital_condition_ru.pdf": FREEDOM_CAPITAL,
                    "https://bankffin.kz/docs/strategy_condition_ru.pdf": FREEDOM_STRATEGY}


def _text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _percent(value: str) -> float:
    match = re.fullmatch(r"\s*(\d{1,2}(?:[.,]\d{1,4})?)\s*%\s*", value)
    if not match:
        raise ValueError("Ставка должна быть одним явным процентом")
    result = float(match[1].replace(",", "."))
    if not 0 <= result <= 30:
        raise ValueError("Ставка вне допустимого диапазона")
    return result


def _rows(table) -> list[list[str]]:
    return [[_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"], recursive=False)]
            for row in table.find_all("tr")]


def _row(rows: list[list[str]], prefix: str) -> list[str]:
    found = [row for row in rows if row and row[0].lower().startswith(prefix.lower())]
    if len(found) != 1:
        raise ValueError(f"Неоднозначная строка условий: {prefix}")
    return found[0]


def _terms_hash(rows: list[list[str]]) -> str:
    # Rate rows may update automatically. All other terms and term columns
    # are preserved, including eligibility, fees and early termination text.
    clean = []
    for row in rows:
        if row and any(word in row[0].lower() for word in ("номиналь", "эффективн")):
            clean.append([row[0]] + [re.sub(r"\d+(?:[.,]\d+)?\s*%", "RATE", cell) for cell in row[1:]])
        else:
            clean.append(row)
    return _digest(clean)


def _offer(key: str, name: str, program: str, nominal: float, effective: float,
           minimum: int, maximum: int | None, top_up: bool, withdraw: bool,
           savings: bool, capitalization: str, early: str, url: str,
           checked_at: datetime, terms_hash: str, source_hash: str, term: int = 12,
           warnings: list[str] | None = None) -> dict:
    if effective < nominal or not 0 <= nominal <= effective <= 30:
        raise ValueError("Номинальная ставка/ГЭСВ не согласованы")
    return {"id": program, "bank_key": key, "name": name, "currency": "KZT",
            "term_months": term, "nominal_pct": nominal, "effective_pct": effective,
            "min_amount": minimum, "max_amount": maximum, "capitalization": capitalization,
            "can_top_up": top_up, "can_withdraw": withdraw, "rate_type": "fixed",
            "deposit_type": "savings" if savings else "other", "guarantee_limit": 20_000_000 if savings else 10_000_000,
            "early_termination": early, "source_url": url, "checked_at": checked_at.isoformat(),
            "status": "verified", "terms_hash": terms_hash, "source_hash": source_hash,
            "warnings": warnings or [], "conditions": [
                f"{term} месяцев; капитализация: {capitalization}",
                "Можно пополнять" if top_up else "Без пополнения",
                "Можно снимать частично" if withdraw else "Без частичного снятия",
                early]}


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _forte_rows(soup: BeautifulSoup) -> list[list[str]]:
    tables = []
    # The public Next.js response embeds the exact table supplied to the
    # displayed component. Decode JSON; never evaluate JavaScript.
    for script in soup.find_all("script"):
        match = re.fullmatch(r"\s*self\.__next_f\.push\((.*)\)\s*;?\s*", script.get_text(), re.S)
        if not match:
            continue
        try:
            chunk = json.loads(match[1])
        except (ValueError, TypeError):
            continue
        if not isinstance(chunk, list) or len(chunk) != 2 or not isinstance(chunk[1], str):
            continue
        for line in chunk[1].splitlines():
            try:
                value = json.loads(line.split(":", 1)[1])
            except (ValueError, IndexError):
                continue
            tables.extend(item for item in _walk(value)
                          if item.get("__typename") == "ComponentDynamicComponentsTableCompare")
    eligible = []
    for table in tables:
        rows = [[_text(item.get("title", ""))] + [_text(value.get("value", ""))
                for value in sorted(item.get("values", []), key=lambda item: item.get("columnIndex", 0))]
                for item in table.get("rows", [])]
        if any(row and row[0].startswith("Срок") and "12 месяцев" in row for row in rows):
            eligible.append(rows)
    if len(eligible) != 1:
        raise ValueError("Не найдена однозначная таблица Forte для нового вклада")
    return eligible[0]


def parse_source(url: str, html: str, checked_at: datetime) -> list[dict]:
    if checked_at.tzinfo is None:
        raise ValueError("Время проверки должно содержать часовой пояс")
    soup = BeautifulSoup(html, "html.parser")
    if not soup.find("title") or len(html) < 500:
        raise ValueError("Источник не является страницей банка")
    source_hash = hashlib.sha256(html.encode()).hexdigest()
    if url == FORTE:
        rows = _forte_rows(soup)
        term_column = _row(rows, "Срок").index("12 месяцев")
        nominal = _percent(_row(rows, "Номинальная")[term_column])
        effective = _percent(_row(rows, "Эффективная")[term_column])
        terms = " ".join(" ".join(row) for row in rows)
        if not all(text in terms for text in ("100 000 ₸", "32 календарных", "0,1%", "не предусмотрено")):
            raise ValueError("Условия Forte требуют проверки")
        return [_offer("forte", "Сберегательный без права пополнения", "forte-no-topup-12", nominal, effective,
              100_000, None, False, False, True, "ежемесячная", "Досрочно: 0,1% годовых, возврат не ранее 32 дней",
              url, checked_at, _terms_hash(rows), source_hash,
              warnings=["Максимальную сумму и тариф перевода денег на покупку уточните у банка."])]
    tables = soup.find_all("table")
    if not tables:
        raise ValueError("Таблица условий отсутствует")
    if url in (FREEDOM_CAPITAL, FREEDOM_STRATEGY):
        rows = _rows(tables[0])
        capital = url == FREEDOM_CAPITAL
        minimum = 100_000 if capital else 15_000
        if (_row(rows, "Валюта")[1] != "KZT" or _row(rows, "Минимальная сумма")[1] != f"{minimum:,}".replace(",", " ") + " ₸"
                or _row(rows, "Пополнение")[1] != ("нет" if capital else "есть")
                or _row(rows, "Частичное снятие")[1] != "нет"):
            raise ValueError("Условия сберегательного вклада Freedom изменены")
        rates = []
        for prefix in ("Номинальная ставка", "Годовая эффективная"):
            matches = re.findall(r"(\d+(?:[.,]\d+)?)%\s*12\s*мес\.", _row(rows, prefix)[1])
            if len(matches) != 1:
                raise ValueError("Неоднозначная ставка Freedom на 12 месяцев")
            rates.append(_percent(matches[0] + "%"))
        return [_offer("freedom", "Капитал" if capital else "Стратегия", "freedom-" + ("capital" if capital else "strategy") + "-12",
                rates[0], rates[1], minimum, None, not capital, False, True, "ежемесячная",
                "Досрочно: возврат после 30 дней, проценты по ставке до востребования",
                url, checked_at, _terms_hash(rows), source_hash,
                warnings=["Сумма до 10% капитала банка; индивидуальный максимум и тарифы переводов уточните у банка."])]
    if url == FREEDOM:
        rows = _rows(tables[0])
        if _row(rows, "Валюта")[1] != "KZT" or _row(rows, "Минимальная сумма")[1] != "500 ₸":
            raise ValueError("Условия Копилки изменились")
        nominal_pairs = re.findall(r"(\d+(?:[.,]\d+)?)%\s*(\d+)\s*мес\.", _row(rows, "Номинальная ставка")[1])
        effective_pairs = re.findall(r"(\d+(?:[.,]\d+)?)%\s*(\d+)\s*мес\.", _row(rows, "Годовая эффективная")[1])
        if len(nominal_pairs) != len(effective_pairs) or not nominal_pairs:
            raise ValueError("Неоднозначные ставки Копилки")
        offered = []
        seen: dict[int, int] = {}
        for (nominal, term), (effective, effective_term) in zip(nominal_pairs, effective_pairs):
            if term != effective_term:
                raise ValueError("Несогласованные сроки Копилки")
            term_int = int(term)
            variant = seen.get(term_int, 0)
            seen[term_int] = variant + 1
            if term_int not in (12, 13) or variant > 1:
                continue
            daily = variant == 1
            offered.append(_offer("freedom", "Копилка — " + ("ежедневная" if daily else "ежемесячная"),
              f"freedom-moneybox-{'daily' if daily else 'monthly'}-{term}", _percent(nominal + "%"), _percent(effective + "%"),
              500, None, True, True, False, "ежедневная" if daily else "ежемесячная",
              "Досрочно: вознаграждение за фактический срок; при коротком хранении возможна комиссия до 2%",
              url, checked_at, _terms_hash(rows), source_hash, term_int,
              ["Сумма до 10% капитала банка; индивидуальный максимум и переводы уточните у банка.",
               "ГЭСВ предполагает сохранение процентов, а не выплаты на другой счёт."]))
        if seen.get(12) != 2:
            raise ValueError("Не подтверждены обе схемы выплаты на 12 месяцев")
        return offered
    if url == BEREKE:
        rows = _rows(next((table for table in tables if "Bereke депозит (срочный)" in table.get_text()), tables[0]))
        text = " ".join(" ".join(row) for row in rows)
        offered = []
        for name, end, program, top_up in (("Bereke депозит (срочный)", "Bereke с пополнением (срочный)", "bereke-term-12", False),
                ("Bereke с пополнением (срочный)", "Bereke депозит Табыс", "bereke-topup-12", True)):
            try:
                segment = text.split(name, 1)[1].split(end, 1)[0]
            except IndexError:
                raise ValueError("Раздел Bereke не найден") from None
            rates = re.findall(r"12 месяцев\s*[-–]\s*(\d+(?:[.,]\d+)?)%\s*\(ГЭСВ\*?\s*[-–]\s*(\d+(?:[.,]\d+)?)%\)", segment)
            if len(rates) != 1 or not all(value in segment for value in ("5 000", "10 000 000 000", "7 календарных", "0%")):
                raise ValueError("Неоднозначные условия Bereke")
            nominal, effective = map(lambda value: _percent(value + "%"), rates[0])
            # Rates occur inside a combined row. Mask only explicit percent
            # values; duration, amounts and all early withdrawal terms remain.
            rate_part, separator, terms_part = segment.partition("* Годовая эффективная")
            if not separator:
                raise ValueError("Не отделены ставки от условий Bereke")
            normalized = re.sub(r"\d+(?:[.,]\d+)?%", "RATE", rate_part) + separator + terms_part
            offered.append(_offer("bereke", name, program, nominal, effective, 5_000, 10_000_000_000,
                  top_up, False, False, "ежемесячная", "Досрочно: ставка 0%, возврат не позднее 7 календарных дней",
                  url, checked_at, _digest(normalized), source_hash))
        return offered
    if url in (HOME_SIMPLE, HOME_SAVINGS, HOME_PLUS):
        table = next((table for table in tables if "Номинальная ставка" in _text(table.get_text())), None)
        if table is None:
            raise ValueError("Нет таблицы Home Credit")
        rows = _rows(table)
        savings = url != HOME_SIMPLE
        if savings:
            term_column = _row(rows, "Срок депозита").index("12 месяцев")
        else:
            # This product puts the term row after currency/rate rows.
            term_column = _row(rows, "Срок депозита").index("12 месяцев")
        nominal = _percent(_row(rows, "Номинальная ставка")[term_column])
        effective = _percent(_row(rows, "Годовая эффективная")[term_column])
        minimum = 100_000 if url == HOME_SAVINGS else 1_000
        if _row(rows, "Минимальная сумма")[1] != f"{minimum:,}".replace(",", " ") + " ₸":
            raise ValueError("Минимум Home Credit изменился")
        if _row(rows, "Капитализация")[1] != "ежедневная":
            raise ValueError("Капитализация Home Credit изменена")
        top_up, withdraw = url != HOME_SAVINGS, url == HOME_SIMPLE
        name = {HOME_SIMPLE: "Простой", HOME_SAVINGS: "Сберегательный Хоум", HOME_PLUS: "Сберегательный Хоум+"}[url]
        return [_offer("home_credit", name, "home-" + ("simple" if withdraw else "savings-plus" if top_up else "savings") + "-12",
                nominal, effective, minimum, None, top_up, withdraw, savings, "ежедневная",
                "Досрочно: деньги с вознаграждением в течение 24 часов" if withdraw else "Досрочно: без вознаграждения, возврат через 30 календарных дней",
                url, checked_at, _terms_hash(rows), source_hash)]
    raise ValueError("Неизвестный официальный источник")
