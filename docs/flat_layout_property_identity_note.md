# Должны ли flat_layout-объявления участвовать в Property Identity?

Задача 2026-08-18, follow-up "Property Identity — calibration
validation", п.3, последний пункт: "read-only проверить, должны ли
flat_layout участвовать в Property Identity как физические квартиры.
Пока не удалять и не отвязывать их." **Ничего не изменено, только
проверка.**

## Что такое flat_layout (напоминание)

krisha.kz `category.id=52 "sell.flat_layout"` — карточка ТИПА квартиры в
новостройке (постит ЖК/застройщик, `"isComplex":true`), НЕ конкретная
физическая квартира с определённым адресом+этажом. Подтверждено прямой
проверкой множества реальных страниц (см. `docs/coverage_gaps_followup.md`
и `docs/floor_backlog_classification.md`).

## Прямая проверка: уже просочились ли flat_layout в property_listings?

```sql
-- уже связанные listing'и с floor IS NULL (единственный способ ДО этой
-- задачи, которым flat_layout мог бы попасть в Property Identity —
-- compute_address_hash() требует floor, так что БЕЗ floor bootstrap
-- в принципе не может создать property)
SELECT count(*) FROM apartment_listings al
JOIN property_listings pl ON pl.listing_id = al.id
WHERE al.floor IS NULL;
-- = 28 (проверено 2026-08-18)

-- грубый прокси (rooms тоже обычно отсутствует у flat_layout, см.
-- находки в coverage_gaps_followup.md) — ни один результат:
SELECT count(*) FROM apartment_listings al
JOIN property_listings pl ON pl.listing_id = al.id
WHERE al.floor IS NOT NULL AND al.rooms IS NULL;
-- = 0
```

## Вывод (обоснованный, не окончательный без ручной проверки всех 28)

**Структурно flat_layout НЕ должны участвовать в Property Identity как
физические квартиры**: `compute_address_hash()` (см. `bot/identity/
property_linker.py`) уже ФАКТИЧЕСКИ блокирует их — bootstrap требует
`floor`, а у flat_layout его никогда нет (см. `docs/floor_backlog_
classification.md`), значит СЕГОДНЯ ни один flat_layout НЕ может
получить `property_id` через штатный путь. 28 listing'ов с `floor IS
NULL`, но УЖЕ имеющих `property_listings` — скорее всего, редкие
legacy-случаи (ручная линковка / старый код до текущих guard'ов), НЕ
flat_layout (нет floor -> не могли пройти `compute_address_hash()`), но
это ПРЕДПОЛОЖЕНИЕ по конструкции кода, не подтверждено построчной
проверкой всех 28 — вне бюджета этого захода.

**Рекомендация** (не выполнена, требует отдельного решения): формально
исключить `is_flat_layout` записи из целевой популяции Property Identity
вообще (не только floor-бэкфилла) — например, отдельный флаг/статус на
`apartment_listings` ("не физическая единица"), чтобы другие части
пайплайна (score_layers, аналитика, будущий merge) тоже могли их
осознанно пропускать, а не полагаться косвенно на "у них всё равно нет
floor". Сейчас `is_flat_layout` вычисляется ТОЛЬКО live (не сохраняется в
БД) в `fetch_apartment_details()` — постоянное решение потребовало бы
либо новой колонки, либо периодического пересчёта. **Не реализовано в
этом PR** — только находка + рекомендация.

Ничего не удалено, ничего не отвязано.
