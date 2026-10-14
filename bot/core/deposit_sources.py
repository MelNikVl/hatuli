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
                    "https://bankffin.kz/docs/strategy_condition_ru.pdf": FREEDOM_STRATEGY,
                    "https://home.kz/strapi/uploads/Dogovor_bankovskogo_obsluzhivaniya_fizicheskih_licz_v_AO_Home_Credit_Bank_versiya_3_ot_19_03_2020_goda_1ad3c0218d.pdf": HOME_PLUS,
                    "https://home.kz/strapi/uploads/Svod_Tipovyh_Tarifov_96_versiya_s_kazahskim_yazykom_1_251359ff81.pdf": HOME_PLUS,
                    "https://home.kz/strapi/uploads/Uvedomlenie_KFGD_d45821d81d.pdf": HOME_PLUS}


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
    if isinstance(term, bool) or not isinstance(term, int) or not 1 <= term <= 60:
        raise ValueError("Срок вклада вне допустимого диапазона")
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


def _bereke_product_rows(soup: BeautifulSoup, name: str) -> list[str]:
    """Read one explicitly spanned product, independent of its neighbours."""
    matches = []
    for table in soup.find_all("table"):
        for index, row in enumerate(table.find_all("tr")):
            cells = row.find_all(["td", "th"], recursive=False)
            if cells and _text(cells[0].get_text(" ", strip=True)) == name:
                matches.append((table, index, cells))
    if len(matches) != 1:
        raise ValueError("Неоднозначный раздел Bereke")
    table, start, header = matches[0]
    if table.find_parent("table") is not None or table.find("table") is not None:
        raise ValueError("Вложенная таблица Bereke")
    rows = table.find_all("tr")
    span_text = str(header[0].get("rowspan", ""))
    if (len(header) != 2 or not re.fullmatch(r"[0-9]{1,3}", span_text)
            or not 2 <= int(span_text) <= 100 or start + int(span_text) > len(rows)):
        raise ValueError("Не подтверждены границы раздела Bereke")
    stop = start + int(span_text)
    # An earlier merged cell must not overlap this product's first row.
    for index, row in enumerate(rows[:start]):
        for cell in row.find_all(["td", "th"], recursive=False):
            earlier_span = str(cell.get("rowspan", "1"))
            if (not re.fullmatch(r"[0-9]{1,3}", earlier_span)
                    or not 1 <= int(earlier_span) <= 100
                    or index + int(earlier_span) > start):
                raise ValueError("Пересекаются разделы Bereke")
    values = []
    for index, row in enumerate(rows[start:stop]):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) != (2 if index == 0 else 1):
            raise ValueError("Неоднозначные ячейки раздела Bereke")
        for cell_index, cell in enumerate(cells):
            expected_span = span_text if index == cell_index == 0 else "1"
            if (str(cell.get("colspan", "1")) != "1"
                    or str(cell.get("rowspan", "1")) != expected_span):
                raise ValueError("Пересекаются разделы Bereke")
        values.append(_text(cells[-1].get_text(" ", strip=True)))
    return values


def _validate_bereke_terms(values: list[str], top_up: bool) -> None:
    rows = [[value] for value in values[1:]]
    expected = {
        "Валюта": "Валюта теңге",
        "Минимальная сумма": "Минимальная сумма 5 000 теңге",
        "Максимальная сумма": "Максимальная сумма 10 000 000 000 теңге",
        "Дополнительные взносы": ("Дополнительные взносы осуществляется до максимальной суммы вклада"
                                 if top_up else "Дополнительные взносы не предусмотрены"),
        "Частичные изъятия": "Частичные изъятия не предусмотрены",
        "Начисление вознаграждения": (
            "Начисление вознаграждения - ежемесячная капитализация и выплата в конце срока депозита "
            "- ежемесячная выплата на текущий счет"),
    }
    if any(_row(rows, prefix)[0] != value for prefix, value in expected.items()):
        raise ValueError("Условия Bereke требуют проверки")
    terms = re.fullmatch(r"Срок ([0-9]+(?:, [0-9]+)*) месяцев", _row(rows, "Срок")[0])
    if not terms:
        raise ValueError("Неоднозначные сроки Bereke")
    months = [int(value) for value in terms[1].split(", ")]
    if 12 not in months or len(months) != len(set(months)):
        raise ValueError("Неоднозначные сроки Bereke")
    early = re.fullmatch(
        r"Досрочное расторжение предусмотрено\. Досрочный возврат осуществляется не позднее "
        r"([1-7]) календарных дней\. Вознаграждение по депозиту пересчитывается по ставке 0%\.?",
        _row(rows, "Досрочное расторжение")[0],
    )
    if not early:
        raise ValueError("Досрочный возврат Bereke требует проверки")
    # Preserve unknown extra restrictions in the terms hash, rather than
    # silently dropping them or treating adjacent products as this product.
    _row(rows, "Пролонгация")


def parse_source(url: str, html: str, checked_at: datetime) -> list[dict]:
    if checked_at.tzinfo is None:
        raise ValueError("Время проверки должно содержать часовой пояс")
    soup = BeautifulSoup(html, "html.parser")
    if not soup.find("title") or len(html) < 500:
        raise ValueError("Источник не является страницей банка")
    source_hash = hashlib.sha256(html.encode()).hexdigest()
    if url == FORTE:
        rows = _forte_rows(soup)
        term_cells = _row(rows, "Срок")[1:]
        term_matches = [re.fullmatch(r"(\d+)\s+месяц(?:а|ев)?", cell) for cell in term_cells]
        if not all(term_matches):
            raise ValueError("Неоднозначные сроки Forte")
        term_months = [int(match[1]) for match in term_matches]
        if len(set(term_months)) != len(term_months) or 12 not in term_months:
            raise ValueError("Неоднозначные сроки Forte")
        nominal_row = _row(rows, "Номинальная")[1:]
        effective_row = _row(rows, "Эффективная")[1:]
        if len(nominal_row) != len(term_months) or len(effective_row) != len(term_months):
            raise ValueError("Сроки и ставки Forte не согласованы")
        if _row(rows, "Валюта")[-1] != "KZT":
            raise ValueError("Валюта Forte не подтверждена")
        terms = " ".join(" ".join(row) for row in rows)
        if not all(text in terms for text in ("100 000 ₸", "32 календарных", "0,1%", "не предусмотрено")):
            raise ValueError("Условия Forte требуют проверки")
        offered = [_offer("forte", "Сберегательный без права пополнения", f"forte-no-topup-{term}",
              _percent(nominal_row[index]), _percent(effective_row[index]),
              100_000, None, False, False, True, "ежемесячная", "Досрочно: 0,1% годовых, возврат не ранее 32 дней",
              url, checked_at, _terms_hash(rows), source_hash, term,
              warnings=["Максимальную сумму и тариф перевода денег на покупку уточните у банка."])
              for index, term in enumerate(term_months)]
        return sorted(offered, key=lambda item: (item["term_months"] != 12, item["term_months"]))
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
        rate_pairs = []
        for prefix in ("Номинальная ставка", "Годовая эффективная"):
            rate_text = _row(rows, prefix)[1]
            pair_pattern = r"(\d+(?:[.,]\d+)?)%\s*(\d+)\s*мес\."
            matches = re.findall(pair_pattern, rate_text)
            if (not matches or re.sub(pair_pattern, "", rate_text).strip()
                    or sum(term == "12" for _, term in matches) != 1):
                raise ValueError("Неоднозначные ставки Freedom")
            rate_pairs.append(matches)
        if ([term for _, term in rate_pairs[0]] != [term for _, term in rate_pairs[1]]
                or len({int(term) for _, term in rate_pairs[0]}) != len(rate_pairs[0])):
            raise ValueError("Сроки и ставки Freedom не согласованы")
        offered = [_offer("freedom", "Капитал" if capital else "Стратегия",
                "freedom-" + ("capital" if capital else "strategy") + f"-{term}",
                _percent(nominal + "%"), _percent(effective + "%"), minimum, None, not capital, False, True, "ежемесячная",
                "Досрочно: возврат после 30 дней, проценты по ставке до востребования",
                url, checked_at, _terms_hash(rows), source_hash, int(term),
                warnings=["Сумма до 10% капитала банка; индивидуальный максимум и тарифы переводов уточните у банка."])
                for (nominal, term), (effective, _) in zip(*rate_pairs)]
        return sorted(offered, key=lambda item: (item["term_months"] != 12, item["term_months"]))
    if url == FREEDOM:
        rows = _rows(tables[0])
        if _row(rows, "Валюта")[1] != "KZT" or _row(rows, "Минимальная сумма")[1] != "500 ₸":
            raise ValueError("Условия Копилки изменились")
        pair_pattern = r"(\d+(?:[.,]\d+)?)%\s*(\d+)\s*мес\."
        rate_cells = [_row(rows, prefix)[1] for prefix in ("Номинальная ставка", "Годовая эффективная")]
        if any(re.sub(pair_pattern, "", cell).strip() for cell in rate_cells):
            raise ValueError("Неоднозначные ставки Копилки")
        nominal_pairs, effective_pairs = [re.findall(pair_pattern, cell) for cell in rate_cells]
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
        offered = []
        for name, program, top_up in (("Bereke депозит (срочный)", "bereke-term-12", False),
                ("Bereke с пополнением (срочный)", "bereke-topup-12", True)):
            values = _bereke_product_rows(soup, name)
            _validate_bereke_terms(values, top_up)
            if not values[0].startswith("Номинальная ставка вознаграждения "):
                raise ValueError("Не подтверждена строка ставок Bereke")
            rates = re.findall(r"12 месяцев\s*[-–]\s*(\d+(?:[.,]\d+)?)%\s*\(ГЭСВ\*?\s*[-–]\s*(\d+(?:[.,]\d+)?)%\)", values[0])
            if len(rates) != 1:
                raise ValueError("Неоднозначные условия Bereke")
            nominal, effective = map(lambda value: _percent(value + "%"), rates[0])
            # Leading/trailing spaces reproduce the previously accepted
            # product segment and hash; a structural repair is not approval
            # of changed conditions. Only this product's rowspan is included.
            segment = " " + " ".join(values) + " "
            # Rates occur inside a combined row. Mask only explicit percent
            # values; duration, amounts and all early withdrawal terms remain.
            rate_part, separator, terms_part = segment.partition("* Годовая эффективная")
            if not separator:
                raise ValueError("Не отделены ставки от условий Bereke")
            normalized = re.sub(r"\d+(?:[.,]\d+)?%", "RATE", rate_part) + separator + terms_part
            offered.append(_offer("bereke", name, program, nominal, effective, 5_000, 10_000_000_000,
                  top_up, False, False, "ежемесячная", "Досрочно: ставка 0%, возврат не позднее 7 календарных дней",
                  url, checked_at, _digest(normalized), source_hash,
                  warnings=["ГЭСВ предполагает сохранение процентов, а не выплаты на другой счёт."]))
        return offered
    if url in (HOME_SIMPLE, HOME_SAVINGS, HOME_PLUS):
        table = next((table for table in tables if "Номинальная ставка" in _text(table.get_text())), None)
        if table is None:
            raise ValueError("Нет таблицы Home Credit")
        rows = _rows(table)
        savings = url != HOME_SIMPLE
        term_cells = _row(rows, "Срок депозита")[1:]
        simple_columns = None
        if url == HOME_SIMPLE:
            def expanded_simple_row(prefix):
                matching = [row for row in table.find_all("tr")
                            if row.find(["td", "th"]) is not None
                            and _text(row.find(["td", "th"]).get_text(" ", strip=True)).lower().startswith(prefix.lower())]
                if len(matching) != 1:
                    raise ValueError(f"Неоднозначная строка условий: {prefix}")
                values = []
                for cell in matching[0].find_all(["td", "th"], recursive=False)[1:]:
                    span = str(cell.get("colspan", "1"))
                    if (not span.isdigit() or not 1 <= int(span) <= 6
                            or str(cell.get("rowspan", "1")) != "1"):
                        raise ValueError("Неоднозначные колонки Home Credit")
                    values.extend([_text(cell.get_text(" ", strip=True))] * int(span))
                if not 1 <= len(values) <= 6:
                    raise ValueError("Неоднозначные колонки Home Credit")
                return values

            # One explicit 12m colspan covers KZT, USD and EUR. Pair currency,
            # term and rates before selecting KZT; never truncate foreign rates.
            currencies = expanded_simple_row("Валюта депозита")
            term_cells = expanded_simple_row("Срок депозита")
            if (len(currencies) != len(term_cells)
                    or any(value not in ("теңге", "доллар", "евро") for value in currencies)):
                raise ValueError("Валюта и сроки Home Credit не согласованы")
            simple_columns = [index for index, currency in enumerate(currencies) if currency == "теңге"]
            if not simple_columns:
                raise ValueError("Валюта Home Credit не подтверждена")
        # Home+ explicitly separates its six-month VIP product. Keep the
        # original column positions when excluding it: its higher rate is
        # unavailable to an ordinary retail customer and is not a 12m offer.
        public_columns, term_months = [], []
        vip_columns = 0
        for index, cell in enumerate(term_cells):
            if simple_columns is not None and index not in simple_columns:
                continue
            match = re.fullmatch(r"(\d+)\s+месяц(?:а|ев)?", cell)
            if match:
                public_columns.append(index)
                term_months.append(int(match[1]))
            elif url == HOME_PLUS and re.fullmatch(r"для VIP клиентов 6 месяцев", cell):
                vip_columns += 1
            else:
                raise ValueError("Неоднозначные сроки Home Credit")
        if vip_columns > 1:
            raise ValueError("Неоднозначные сроки Home Credit")
        if len(set(term_months)) != len(term_months) or 12 not in term_months:
            raise ValueError("Неоднозначные сроки Home Credit")
        nominal_row = _row(rows, "Номинальная ставка")[1:]
        effective_row = _row(rows, "Годовая эффективная")[1:]
        if url == HOME_SIMPLE:
            nominal_row = expanded_simple_row("Номинальная ставка")
            effective_row = expanded_simple_row("Годовая эффективная")
        if len(nominal_row) != len(term_cells) or len(effective_row) != len(term_cells):
            raise ValueError("Сроки и ставки Home Credit не согласованы")
        minimum = 100_000 if url == HOME_SAVINGS else 1_000
        minimum_cells = _row(rows, "Минимальная сумма")[1:2]
        capitalization_cells = _row(rows, "Капитализация")[1:2]
        if url == HOME_SIMPLE:
            minimum_cells = expanded_simple_row("Минимальная сумма")
            capitalization_cells = expanded_simple_row("Капитализация")
            if len(minimum_cells) != len(term_cells) or len(capitalization_cells) != len(term_cells):
                raise ValueError("Сроки и условия Home Credit не согласованы")
            minimum_cells = [minimum_cells[index] for index in public_columns]
            capitalization_cells = [capitalization_cells[index] for index in public_columns]
        if not minimum_cells or any(value != f"{minimum:,}".replace(",", " ") + " ₸" for value in minimum_cells):
            raise ValueError("Минимум Home Credit изменился")
        if not capitalization_cells or any(value != "ежедневная" for value in capitalization_cells):
            raise ValueError("Капитализация Home Credit изменена")
        top_up, withdraw = url != HOME_SAVINGS, url == HOME_SIMPLE
        name = {HOME_SIMPLE: "Простой", HOME_SAVINGS: "Сберегательный Хоум", HOME_PLUS: "Сберегательный Хоум+"}[url]
        # Shared cells use colspan: Хоум has no maximum at 6/12 months,
        # but a 100 million cap at 24 months. Preserve that distinction.
        max_cells = None
        max_rows = [row for row in table.find_all("tr")
                    if row.find(["td", "th"]) is not None
                    and _text(row.find(["td", "th"]).get_text(" ", strip=True)).startswith("Максимальная сумма")]
        if len(max_rows) > 1:
            raise ValueError("Неоднозначный максимум Home Credit")
        if max_rows:
            max_cells = []
            for cell in max_rows[0].find_all(["td", "th"], recursive=False)[1:]:
                span = str(cell.get("colspan", "1"))
                if not span.isdigit() or not 1 <= int(span) <= 6:
                    raise ValueError("Неоднозначный максимум Home Credit")
                max_cells.extend([_text(cell.get_text(" ", strip=True))] * int(span))
            if len(max_cells) == 1:
                max_cells *= len(term_cells)
            if len(max_cells) != len(term_cells):
                raise ValueError("Не подтверждены максимумы Home Credit по срокам")
        offered = []
        for index, term in zip(public_columns, term_months):
            maximum = None
            if max_cells is not None and max_cells[index] != "без ограничений":
                amount = re.fullmatch(r"([\d ]+)\s*₸", max_cells[index])
                if not amount:
                    raise ValueError("Не подтверждён максимум Home Credit")
                maximum = int(amount[1].replace(" ", ""))
            offered.append(_offer("home_credit", name,
                "home-" + ("simple" if withdraw else "savings-plus" if top_up else "savings") + f"-{term}",
                _percent(nominal_row[index]), _percent(effective_row[index]), minimum, maximum, top_up, withdraw, savings, "ежедневная",
                "Досрочно: деньги с вознаграждением в течение 24 часов" if withdraw else "Досрочно: без вознаграждения, возврат через 30 календарных дней",
                url, checked_at, _terms_hash(rows), source_hash, term))
        return sorted(offered, key=lambda item: (item["term_months"] != 12, item["term_months"]))
    raise ValueError("Неизвестный официальный источник")
