# Scoring roadmap — решения заказчика и очередь работ (2026-08-14)

Источник: [docs/scoring_audit.md](scoring_audit.md) принят как живой
документ. Этот файл — очередь исполнения по итогам аудита, порядок
**части 1→5**, коммиты логическими группами. Пока таблицы `decisions`
(п.15) не существует — этот файл замещает её для темы «скоринг»: каждая
волна ниже помечена статусом и результатом (что и так требует п.15 —
`title`, `reasoning`, `expected_impact`, `status`, `actual_outcome`).

## Стратегические решения (зафиксированы, не пересматриваются в рамках этой очереди)

- **W_LOC=0 остаётся.** «Хорошая сделка» ≠ «хорошее место» — локация НЕ
  подмешивается в Deal Score. Локационный скор развивается отдельной
  продуктовой осью (см. Часть 3), не как вес внутри `score_total`.
- **CEO-оркестратор не строится.** Только лёгкая дисциплина: `decisions`-
  лог + weekly-мемо (Часть 5, п.15). Отдельной админ-очереди решений нет —
  обсуждение решений идёт в сеансе (чате), не в отдельном UI.

---

## Часть 1. Скоринг, волна 1 (тихие регрессии) — ✅ ЗАВЕРШЕНО 2026-08-14

Гейт применён ко всем трём пунктам: отчёт распределения до/после,
счётчик изменений >5 баллов, топ-20 movers — см. результаты под каждым
пунктом. Методология: `bot/core/deal_score.compute_deal_scores()` —
чистая функция без побочных эффектов, дифф считается на реальных живых
данных (снапшот `apartment_listings`+`complexes` на момент волны), без
записи в БД, до принятия решения о мерже.

### 1. Bargain — единый источник ✅

**Найденный корень бага** (не совпадает с гипотезой из аудита — там
предполагался «отвал вызова», по факту — рассинхрон имени поля):
`bot/core/apartment_parser.py` продолжал вызывать `get_comparables()`/
`analyze_bargain()` (ТЕ ЖЕ функции, что попап) на каждом цикле и клал
результат плоско на объект листинга — `r["bargain_target"]`/
`r["bargain_discount_pct"]`/`r["bargain_rec"]`. `service_apartments.py`
читал `r["score_data"]["bargain"]["target_price"]` — ключ `score_data`
парсер вообще не создаёт (протухший путь от старой `apartment_score_v2`)
— поэтому `bargain` всегда была `{}`, три колонки не писались с
2026-07-25, хотя сам расчёт всё это время был живым и корректным, просто
терялся молча на записи в БД.

**Фикс**: [`service_apartments.py`](../service_apartments.py) —
`extract_bargain(r)` читает плоские ключи (то, что реально кладёт
парсер) вместо мёртвого `score_data.bargain`. Одна точка изменения чинит
и INSERT, и UPDATE путь разом. Колонки `bargain_target/discount_pct/rec`
**оставлены** (не удалены) — у них оказалось 6 живых читателей
(`terminal_extras.py`, `full_sweep.py`, `bot/admin_web.py` — включая
сортировку по `bargain_discount_pct`, `bot/jobs/scheduler.py`,
`bot/core/sheets_sync.py`, `bot/templates/top10.html`), удаление было бы
куда большим blast radius, чем сама задача.

**Гейт-отчёт** (живая выборка 250 активных объявлений с `first_seen` >
2026-07-25, сейчас `bargain_target IS NULL`, посчитаны той же
`get_comparables`/`analyze_bargain`):
- 250/250 (100%) получили `target_price`, 0 — без аналогов вовсе.
- `discount_pct`: 168 в 0-5% (типично), 17 в 5-10%, 16 в 10-20%, 49
  капнуты на 20% (заметно переоценены относительно локальных аналогов —
  это существующее поведение `analyze_bargain()`, формула не менялась).
- `market_status`: hot=199, normal=45, cold=6 (соответствует
  докстрингу «рынок Астаны быстрый»).
- Top-20 по |discount_pct| — без аномалий (капы на 20%, все с
  разумным числом аналогов 3-30, методы `same_complex`/`hex+ring`/
  `city_segment` — ожидаемый разброс методов при разной плотности
  данных по ЖК).

**Регресс-чек**: [`tests/test_bargain_extract.py`](../tests/test_bargain_extract.py)
(3 теста — извлечение из плоских ключей, регрессия старого мёртвого пути,
честный None без аналогов) + [`tests/test_alert_bargain_line.py`](../tests/test_alert_bargain_line.py)
(3 теста — строка «🤝 Торг» появляется когда target посчитан, не
появляется без него, переживает пустой `rec`).

**Живой прогон**: `krisha-apartments.service` перезапущен 2026-08-14
11:50 — первый цикл после фикса подтвердит заполнение колонок для
новых/пересканированных объявлений (проверяется отдельно, см. журнал
запуска в конце файла).

### 2. House-resolution в скоринге ✅

**Фикс**: два места, оба — тот же паттерн `_listing_id_match` (имя ЖК
ИЛИ `resolved_house_id`), что уже используется everywhere в
`terminal_extras.py`:
- [`bot/core/deal_score.py`](../bot/core/deal_score.py)
  `compute_deal_scores()` — новый опциональный параметр
  `complexes_by_id`; quality-компонент берёт `housing_class`/
  `year_built` дома (по `resolved_house_id`), если он известен,
  вместо текстового имени зонтика. Обратная совместимость: без
  `complexes_by_id` (старые вызовы) поведение не меняется.
- `terminal_extras.py` — геоцентроид ЖК/дома (карточка ЖК + `/admin/api/
  complex/{id}/location-score`) — тот же `OR resolved_house_id = $2`.

**Гейт-отчёт** (реальные данные, 29542 активных вторичных листингов):
- **Quality-компонент**: 492 активных объявления имеют `resolved_house_id`,
  из них у **402 (82%)** входы (`housing_class`/`year_built`) реально
  отличаются между домом и зонтиком → это и есть объявления, которые
  раньше молча получали характеристики чужого (агрегатного) ЖК.
- **score_total изменился у 393/29542 (1.33%)**, из них **>5 баллов: 68
  (0.23%)**. Распределение |Δ|: 1-2б=230, 3-5б=95, 6-10б=12, 11-20б=22,
  >20б=34.
- **Top-20 movers** — почти целиком один кластер: дом «Баскару 2»
  (id=299, house под зонтиком «Баскару 2, 3, 4, 5», id=1797). Проверено
  вручную: у зонтика `housing_class='элит', year_built=2005`
  (вероятно, устаревшие/ошибочные агрегатные данные), у дома
  `housing_class=NULL, year_built=2024`. Раньше объявления этого дома
  получали ложную наценку класса «элит» (+25% к ожидаемой цене) и штраф
  года 2005 — после фикса честно используют прокси по перцентилю цены
  (класс не известен) и год 2024. Δ преимущественно отрицательный
  (переоценённый скор корректируется вниз) — объяснимо, не шум.
- **Геоцентроид**: 32 дома под зонтиками, у 9 раньше не было координат
  вовсе (`no_coords`), у 11 центроид заметно сдвигается — 20/32 (62.5%)
  домов затронуты.

**Регресс-чек**: [`tests/test_deal_score_house_resolution.py`](../tests/test_deal_score_house_resolution.py)
(4 теста — приоритет дома над именем, фолбэк без `resolved_house_id`,
обратная совместимость без `complexes_by_id`, честный фолбэк при неизвестном
id) + [`tests/test_house_resolution_geo.py`](../tests/test_house_resolution_geo.py)
(2 HTTP-теста на реальном ASGI-клиенте, сетевой вызов Overpass
застаблен — тест не про него).

### 3. finish_level: тихий инкремент убран ✅

**Было**: `service_apartments.py` правил `score_total` напрямую
(`+= adj`) при определении отделки — эффект почти всегда стирался в
том же цикле следующим `apply_deal_scores()` (полная перезапись
`score_total = deal`), см. `docs/scoring_audit.md` §5.2 — «живой код,
мёртвый эффект».

**Стало**: [`bot/core/deal_score.py`](../bot/core/deal_score.py) —
`finish_level` (7 кодов из `bot/core/listing_intel.detect_finish_level`)
небольшой (`_W_FINISH_IN_QUALITY = 0.12`) субкомпонент внутри quality
(сопоставимо с прокси-классом 0.30, заметно слабее года 0.35 и рейтинга
0.20) — участвует наравне с остальными опциональными сигналами (просто
не участвует, если `finish_level` не определён — честно, не 50 по
умолчанию). Виден в breakdown (`components.quality.text`: `«отделка
«дизайнерский ремонт»»` и т.п.). `service_apartments.py` больше не
трогает `score_total` — только пишет классификацию `finish_level`.
Докстринг `bot/score_layers/__init__.py` поправлен (там же попутно
почищена ссылка на мёртвый `apartment_score_v2` как «базовый скор» —
это правда Deal Score v4).

**Гейт-отчёт** (реальные данные, эффект изолирован — сравнение с той же
БД, где `finish_level` обнулён вручную в копии, чтобы не смешивать с
эффектом п.2 выше):
- 2150 активных объявлений имеют `finish_level`, из них у **745 (35%)**
  `score_total` реально меняется (остальные — раунд-даун до того же
  целого после нормировки весов quality).
- **>5 баллов: 2 из 745** — крайне узкий эффект, соответствует «небольшой
  вес». Распределение |Δ|: 1-2б=690, 3-5б=53, 6-10б=2.
- Top movers — все Δ∈[-5,+6], без выбросов.

**Регресс-чек**: [`tests/test_deal_score_finish_level.py`](../tests/test_deal_score_finish_level.py)
(5 тестов — виден в breakdown при наличии, отсутствует без сигнала,
дизайнерский > черновой по quality, верхняя граница эффекта не
доминирует, неизвестный код не роняет расчёт).

### Общий регресс волны 1

`./venv/bin/pytest tests/ -q` → **247/247 passed** после всех трёх
пунктов (было 213 до начала волны 1 — 17 новых тестов на bargain/house-
resolution/finish_level + ранее незакоммиченные тесты newbuild-предложений
людей из предыдущей задачи). `krisha-web.service` и
`krisha-apartments.service` перезапущены (тот же урок дня, что и
энтити-айди аккордеон раньше — процессы держат старый код в памяти,
Jinja-шаблоны не спасают бэкенд-логику).

---

## Часть 2. Скоринг, волна 2 (гигиена) — ✅ ЗАВЕРШЕНО 2026-08-14

8. **`effective_score`** ([`migrations/062_effective_score.sql`](../migrations/062_effective_score.sql))
   — `GENERATED ALWAYS ... STORED` column вместо 7 неидентичных копий
   формулы в `terminal_extras.py` (было 4+ на момент первого аудита,
   живой grep перед фиксом нашёл ещё одну — итого 7). Гейт: 46959/47002
   (99.9%) активных объявлений — старая формула и новая колонка дают
   одинаковое значение; ровно 43 расхождения — 100% объясняются найденным
   живым багом (`/admin/api/map-points` уже учитывал `primary_score_total`
   для `market_type='primary'`, остальные 6 копий — нет), не побочный
   эффект унификации.
9. **Общее hedonic-ядро** — [`bot/core/hedonic_constants.py`](../bot/core/hedonic_constants.py),
   единственный источник `AREA_BAND_PCT`/`MIN_BLDG`/`MIN_HEX`/`MIN_RING`/
   `W0`/`W1`/`W2` для `bargain.py` и `deal_score.py` (архитектуры разные —
   async live-запрос vs batch-агрегация в памяти — общий КОД аналогов не
   выносился, только пороги/веса, как и просили).
10. **Мёртвый код** → [`docs/archive/`](../docs/archive/README.md):
    `apartment_score.py`, `apartment_score_v2.py.clean` (→ `.txt`),
    `hex_price.py` — все три подтверждены 0 живых импортёров перед
    переносом.
11. **`housing_class_estimate`** — [`migrations/063`](../migrations/063_housing_class_estimate_computed_at.sql)
    (`computed_at`, урок Г3) + [`housing_class_estimate_recompute.py`](../housing_class_estimate_recompute.py)
    (реконструкция формулы — точная формула одноразового прогона
    2026-08-01 нигде не сохранилась, честно об этом сказано в докстринге
    скрипта) + `krisha-housing-class-estimate.timer` (ежемесячно).
    Живой прогон: покрытие 1068/2397 (45%, заморожено) → 1740/2397 (73%),
    1615 свежих оценок + 125 нетронутых (нет `avg_price_m2` для честного
    пересчёта — не гадаем).
12. **Секция confidence** — [`scoring_audit.md` §5.0](scoring_audit.md#50-четыре-понятия-confidence-добавлено-2026-08-14-часть-2-п12),
    явная таблица домен/шкала/потребители на все 4 понятия.

## Часть 3. Локация как продуктовая ось — ⏳ ОЧЕРЕДЬ

9. Таблица `complex_location_scores` (complex_id, score, breakdown
   JSONB, computed_at) — гейт применяется (влияет на то, что видит
   пользователь). Рендер страницы ЖК читает кэш, не live-Overpass;
   пересчёт — раз в месяц + при смене координат ЖК; healthcheck зеркал
   Overpass с алертом при <2 живых (сейчас, по докстрингу
   `bot/score_layers/osm.py`, часто жив только 1 из 4).
   **`computed_at` — обязательное поле с самого первого варианта схемы,
   не постфактум** (урок Г3, `data_collection_audit.md`/`temporal_
   policy.md`: снимок без своего таймстампа пересчёта — тот же класс
   гэпа, что уже был у `complexes.avg_price_m2`, только на новой
   таблице в день её создания).

## Часть 4. Персональные телеграм-алерты — ⏳ ОЧЕРЕДЬ

10. Таблица `user_alert_configs` (user_id, preset, filters JSONB,
    deal-фильтры, frequency, quiet_hours, daily_cap, active) —
    настройка НАДСТРАИВАЕТСЯ над уже существующими сохранёнными
    поисками, не параллельно.
11. Матчер в цикле парсера → `user_alert_queue`, дедуп по (user,
    listing), сборщик дайджестов по frequency/тихим часам.
12. `/alerts` UX (inline-меню) + фидбек-кнопки 👍/👎/«меньше таких»/
    «скрыть ЖК» → `alert_feedback` (будущая калибровка).
13. Пресеты investor/self; дефолт нового подписчика — daily-дайджест
    score ≥ 70.
14. HTTP/ASGI-тесты меню и матчера; отчёт первого дня (конфиги/алерты).

## Часть 5. Лёгкая CEO-дисциплина — ⏳ ОЧЕРЕДЬ

15. Таблица `decisions` (decision_id, created_at, decided_by, category,
    title, reasoning, alternatives, expected_impact, status,
    actual_outcome) + шаблон мемо, раз в неделю по метрикам+рынку.
    Предложения — `status='proposed'`. До появления таблицы этот файл
    (`docs/scoring_roadmap.md`) исполняет её роль для темы «скоринг».

---

## Программа надёжности скоринга → вердикт-стратегия — ✅ ПЛАН ПРИНЯТ 2026-08-14

Формализовано в [`docs/verdict_strategy.md`](verdict_strategy.md) (не
`scoring_reliability_plan.md`, как этот файл ссылался раньше — название
изменено, цель шире «надёжности»: объяснимый вердикт, не просто
провалидированный скор). Волны 1-2 (условие для старта, см. прежнюю
формулировку ниже) обе завершены к моменту принятия плана — Фаза A того
документа начинается сразу, без дополнительного ожидания. Дальше не
дублируется здесь — см. сам документ (парадигма, принципы, Фазы A-D,
freeze-лист, гейт).

---

## Часть 6. Вердикт-стратегия, Фаза A (данные и outcomes) — ✅ ЗАВЕРШЕНО 2026-08-14

Гейт применён по [`verdict_strategy.md` §8](verdict_strategy.md#8-гейт-одно-правило-на-все-фазы)
ко всем пунктам с изменением формулы (п.4/п.6): отчёт распределения
до/после, счётчик >5 баллов, топ-20 movers — каждый со своим срезом
before-снапшота (п.6 снят ПОСЛЕ применения п.4, чтобы изолировать
эффекты, не смешать их в одном диффе). Полные докстринги/обоснования —
в коде и коммитах, здесь — только итог по формату волн 1-2.

1. **`listing_snapshots`** ([`migrations/064`](../migrations/064_listing_snapshots.sql),
   [`listing_snapshot.py`](../listing_snapshot.py)) — daily-снимок
   listing_id/price/views/is_active. Таймер `krisha-listing-snapshot.timer`
   (08:30) установлен и запущен, первый снимок снят вручную в день
   задачи: 47016 объявлений (43755 активных). Бэкфила нет и не может
   быть — накопление строго с 2026-08-14.
2. **`outcome_labels`** ([`migrations/065`](../migrations/065_outcome_labels.sql),
   [`outcome_labels_recompute.py`](../outcome_labels_recompute.py)) —
   `disappeared_within_30d`/`price_reduction_within_30d`/`survives_90d`/
   `time_on_market`/`views_velocity`, бэкфил по всей истории
   `price_history` (с 2026-07-09) + `archived_at` (с 2026-06-05) +
   ongoing-таймер (08:45). Живой бэкфил: 47016 объявлений,
   `disappeared_within_30d` разрешено 16406 (35%, TRUE=1207),
   `price_reduction_within_30d` разрешено 7427 (TRUE=3677), `survives_90d`
   разрешено 3261 (все FALSE — датасет младше 90 дней, TRUE физически
   невозможен на эту дату), TOM известен для 3261 архивных (среднее 25
   дней), `views_velocity` — 0 (views_history/Г2 живёт только с сегодня).
3. **Baseline-замер** ([`baseline_measure.py`](../baseline_measure.py)) —
   заглавный артефакт фазы, снят ДО правок формулы (п.4/6). AUC
   `score_total`=0.8726 на `disappeared_within_30d`, но почти целиком
   тянет компонент `price` (AUC=0.8706) — `quality`=0.5169 (случайность,
   прямое обоснование для п.4), `market`=0.7108, `risk`=0.5235,
   `deal_confidence`=0.2775 (обратно предсказательна, зафиксировано как
   есть). TOM (n=1226, малая выборка): `market` rho=-0.232 (p=2e-14,
   ожидаемо), `risk` rho=+0.2755 (p=8e-20, контринтуитивно, не
   объяснено). `survives_90d` честно пропущена — окно 90 дней не
   разрешилось ни разу на этом датасете.
4. **Убрать price→quality proxy** ([`bot/core/deal_score.py`](../bot/core/deal_score.py)) —
   при неизвестном `housing_class` больше не подставляется перцентиль
   цены/м² как прокси-класс (эмпирически обоснованно п.3: AUC
   quality=0.517). Confidence лишена частичной надбавки (+8) за сам факт
   возможности прокси. Регресс:
   [`tests/test_deal_score_no_quality_proxy.py`](../tests/test_deal_score_no_quality_proxy.py)
   (4 теста). Живой прогон: 22857/23039 без churn, 72.9% (16664)
   изменили `score_total`, >5 баллов — 24.76% (5659, ожидаемо — у 74%
   объявлений класс неизвестен, quality — 20% веса), топ-20 movers
   ±37..51 без аномалий.
5. **Терминология** ([`bot/templates/dashboard.html`](../bot/templates/dashboard.html),
   [`analytics_detail.html`](../bot/templates/analytics_detail.html),
   [`analytics.html`](../bot/templates/analytics.html)) — «уверенность
   NN%» → «полнота данных NN%» во всех живых местах показа
   `deal_confidence` (только лейбл, не API/поле). `location_score`
   confidence — по-прежнему не в скоупе, отложена на Фазу D.
6. **Risk → флаги вердикта** ([`bot/core/deal_score.py`](../bot/core/deal_score.py)) —
   `W_RISK=0` (было 5%), `risk_bits` + 2 новых сигнала (⚠ мало аналогов,
   ⚠ класс ЖК не известен) — отдельный список `flags`, не вес. price/
   quality/market перенормированы (пропорции 40:20:15 сохранены).
   Регресс:
   [`tests/test_deal_score_risk_flags.py`](../tests/test_deal_score_risk_flags.py)
   (9 тестов). Живой прогон (изолирован от п.4): 84.7% (19398) изменили
   `score_total`, распределение |Δ| **целиком** в 1-5 баллов, 0 изменений
   >5 — математически ожидаемо (старый вклад risk был ограничен ±5
   баллами). Живое распределение флагов: класс не известен 75.1%,
   риелтор 35.6%, мало аналогов 13.4%, последний этаж 10.4%, 1й этаж
   4.8%, совсем без флагов 13.2%.
7. **Гигиена** ([`migrations/066`](../migrations/066_deal_confidence_column.sql)) —
   единственный `ALTER TABLE` из `apply_deal_scores()` (гонялся на
   каждый цикл парсера) вынесен в миграцию.

**Общий регресс Фазы A**: `./venv/bin/pytest tests/ -q` → **281/281
passed** (было 271 до начала фазы — 10 новых тестов на quality-proxy/
risk-flags). `krisha-apartments.service` перезапускался трижды в течение
фазы (после п.4, п.6, п.7) — каждый раз проверено git_hash/mtime по
чеклисту [`process.md`](process.md#deploy-чеклист-код-на-диске--код-в-процессе).

**Дальше** — по решению заказчика Фаза B отложена в пользу
внепланового Фазы A.5 (Baseline Hardening, см. ниже) — Фаза A дала
baseline, но методологически недостаточно строгий, чтобы против него
честно мерить будущие модели.

---

## Часть 7. Вердикт-стратегия, Фаза A.5 (Baseline Hardening) — ✅ ЗАВЕРШЕНО 2026-08-14

Не начинать Фазу B до завершения — условие задачи, выполнено (гейт ниже
пройден в тот же день). Гейт применён по
[`verdict_strategy.md` §8](verdict_strategy.md#8-гейт-одно-правило-на-все-фазы),
как и в Части 6 — до/после диффы, счётчики, топ-20 movers там, где
менялась формула.

1. **Аудит аналогов** ([`bot/core/bargain.py`](../bot/core/bargain.py)) —
   живой баг: ни один из 4 SQL-запросов `get_comparables()` не фильтровал
   активность вовсе — архивные (проданные/снятые) объявления тихо
   участвовали в медиане "текущего рынка". Живая проверка: 2736 архивных
   объявлений структурно подходили под типичные фильтры и могли попасть
   в чью-то медиану аналогов. Фикс — `_activity_filter()`, общий на все 4
   запроса, + опциональный `as_of` для будущего backtesting (точечная
   реконструкция "было активно НА ЭТУ ДАТУ", не текущий `is_active`).
   Self-exclusion (`exclude_id`) и `deal_score.py` — проверены, уже были
   корректны до задачи. Регресс:
   [`tests/test_bargain_comparables_activity.py`](../tests/test_bargain_comparables_activity.py)
   (5 тестов на реальной БД).
   *Побочный фикс, найден при подготовке п.2*: `apartment_listings.
   comparables_cnt`/новая `bargain_method` — были 0/47016 заполнены
   ("живой код, мёртвый эффект", тот же класс, что `finish_level` до
   волны 1) — `service_apartments.py` INSERT/UPDATE никогда их не
   включал, хотя `apartment_parser.py` считал каждый цикл. Исправлено,
   [`migrations/067`](../migrations/067_bargain_method_column.sql).
2. **`deal_score_snapshots`** ([`migrations/068`](../migrations/068_deal_score_snapshots.sql),
   [`deal_score_snapshot.py`](../deal_score_snapshot.py)) — append-only
   исторический журнал Deal Score. НЕ пересчитывает формулу задним
   числом — читает текущее уже посчитанное `apply_deal_scores()`
   состояние, пишет `score_version`/`git_commit`. Скоуп —
   `is_active IS NOT FALSE` (архивные уже заморожены).
3. **Таймер** `krisha-deal-score-snapshot.timer` (08:35, после
   listing-snapshot 08:30) — установлен и запущен, первый снимок снят
   вручную: 37735 строк. Регресс:
   [`tests/test_deal_score_snapshot.py`](../tests/test_deal_score_snapshot.py)
   (4 теста).
4. **`outcome_labels` расширены** ([`migrations/069`](../migrations/069_outcome_labels_extend.sql),
   [`outcome_labels_recompute.py`](../outcome_labels_recompute.py)) —
   `clean_disappearance_within_30d` (уточнение `disappeared_within_30d`
   за вычетом вероятного релиста/модерации, см.
   [`verdict_strategy.md` §3.5](verdict_strategy.md#35-disappeared_within_30d--прокси-ликвидности-не-продажи)),
   `relisted_within_60d`/`possibly_relisted` (два порога уверенности,
   принцип auto/review из `entity_resolution.py`), `possibly_moderation_
   removed`, `observation_days`, `censored`, `outcome_notes`.
   Релист-матчинг сначала упал по 30с `command_timeout` (коррелированные
   `EXISTS`, тот же класс бага, что уже был в `deal_score.py` — см.
   комментарий в том файле) — переписан на JOIN+GROUP BY
   ([`migrations/070`](../migrations/070_apt_complex_name_lower_full_idx.sql)
   — недостающий полный индекс), 1.9с вместо таймаута. По ходу
   разработки тестами пойман и исправлен логический баг: relist_match —
   INNER JOIN, при нуле кандидатов не даёт строки, "нет строки" было
   неотличимо от "окно ещё не закрылось" — добавлена явная проверка
   `window_closed`. Живой прогон (47021): `clean_true=0` — честно
   ожидаемо (60-дневное окно релиста и свежий `first_seen` не могут
   одновременно выполниться до ~2026-09-08, `price_history` моложе 60
   дней), `relisted_true=568`. Регресс:
   [`tests/test_outcome_labels_relist.py`](../tests/test_outcome_labels_relist.py)
   (12 тестов, включая прямой регресс на найденный баг).
5. **`baseline_measure.py` переписан** — temporally_safe (снимок ДО
   начала окна исхода из `deal_score_snapshots`, не текущий score против
   давнего исхода), AUC/PR-AUC/lift@10%/precision@10%/калибровка по
   децилям, сравнение `score_total` vs `price_score` vs `bargain_
   discount_pct`, отдельно по `disappeared_within_30d` и `clean_
   disappearance_within_30d`. Два живых бага найдены и исправлены при
   разработке: (а) `observation_days>=30` как фильтр парадоксально
   вырезал именно TRUE-случаи (n_true=0 из 186); (б) `censored=TRUE`
   неверно трактовался как "исход неизвестен" (убирал 7627→1164 строк);
   (в) ~19% связок в `price_score` — `argsort` стабилен, "топ-10%" и
   "дециль 10" выбирали разные подмножества одной связки
   (precision@10%=0.000 против дециля 0.57 для ОДНОГО сигнала) — фикс:
   фиксированная случайная перестановка перед ранжированиями. Живой
   прогон (n=7627): `score_total` AUC=0.8219, `price_score` AUC=0.8613
   (выше score_total), `quality_score` AUC=0.3935 (ниже 0.5 — обратно
   предсказательна, находка после п.4 Часть 6), `market_score`
   AUC=0.7106. `temporally_safe=True`: 0 строк — ожидаемо,
   `deal_score_snapshots` начал копиться в тот же день. Регресс:
   [`tests/test_baseline_measure_metrics.py`](../tests/test_baseline_measure_metrics.py)
   (12 тестов).
6. **`effective_score` проверен** — живая проверка на всей таблице
   (47021 строка) после правок формулы Часть 6 п.4/п.6: 0 расхождений.
   Структурно не может разъехаться (`GENERATED ALWAYS` ссылается на
   `score_total` напрямую, не дублирует формулу) — миграция не
   потребовалась. Постоянная regress-проверка добавлена в
   [`tests/test_effective_score.py`](../tests/test_effective_score.py).

**Общий регресс Фазы A.5**: `./venv/bin/pytest tests/ -q` → **317/317
passed** (было 281 после Части 6 — 44 новых теста, включая 3 живых бага,
пойманных и исправленных самими тестами при разработке, не оставленных
"на потом"). `krisha-apartments.service`/`krisha-web.service`
перезапускались по ходу фазы (после правки `bargain.py` — оба сразу,
после `service_apartments.py`/`apartment_parser.py` — apartments) —
каждый раз проверено git_hash/mtime по чеклисту
[`process.md`](process.md#deploy-чеклист-код-на-диске--код-в-процессе).

**Гейт A.5** (задача заказчика): нет runtime `ALTER TABLE` (только
единственный из Части 6 п.7, уже вынесен; ни один новый код Фазы A.5 не
добавил своего) ✅; нет аналогов из архива ✅ (п.1); есть тесты ✅ (44);
есть снапшот скора ✅ (п.2-3); `baseline_measure.py` имеет временную
защиту ✅ (п.5); отчёт показывает AUC/PR-AUC/lift и сравнение price-only
vs score_total ✅ (п.5); все изменения закоммичены и применены на проде
✅ (миграции 067-070 применены, таймеры активны, сервисы перезапущены).

**Дальше** — Фаза B (comparable engine v2, класс-модель, ER-калибровка,
параллельно разбор `admin_web.py`) — см.
[`verdict_strategy.md` §5](verdict_strategy.md#фаза-b-недели-3-4--comparable-engine-v2--начало-разбора-admin_webpy).

---

## Часть 8. Фаза B — результаты (✅ ЗАВЕРШЕНО 2026-08-14)

Цель Фазы B — поднять AUC `price_score` через более точный пул аналогов
(`comparable_score`). **Гейт по AUC не пройден** — задокументировано
честно, код не откатывается (см. п.2 ниже и
[`verdict_strategy.md` §5, «Анализ потолка price_score»](verdict_strategy.md#анализ-потолка-price_score-фаза-b-2026-08-14)).

1. **`comparable_score` core** ([`bot/core/comparable_score.py`](../bot/core/comparable_score.py))
   — 8-факторный непрерывный скор сопоставимости пары объявлений (0-1):
   `same_building`/`same_complex`/`area`/`floor`/`year_built`/
   `housing_class`/`finish_level`/`distance`, каждый фактор — `None` при
   недостающих данных (Unknown ≠ average), не 0/среднее. `as_of` первого
   класса. Регресс: [`tests/test_comparable_score.py`](../tests/test_comparable_score.py)
   (36 тестов).
2. **Интеграция в `deal_score.py`** — `P_expected` внутри `own_bldg`/
   own-гекс/кольцо теперь weighted median топ-N (веса = `comparable_score`)
   вместо плоской медианы. Live-гейт distribution объясним (33.2%
   объявлений изменили `score_total`, >5 баллов — 3.87%, топ-20 movers
   ±44, без аномалий) — но **обязательный честный `as_of`-backtest на
   тех же 3 `t0` (2026-07-10/12/14) показал AUC `price_score` v2
   практически неотличим от v1** (Δ от −0.0016 до −0.0021, на порядок
   меньше bootstrap CI ~0.01-0.02). Побочная находка при интеграции:
   `compute_deal_scores()` выросло с <1с до ~8с (профилировано
   `cProfile`) — фикс (`@lru_cache` на `_class_key`, отказ от копирования
   `_WEIGHTS`, `MAX_POOL_BEFORE_SCORING=60`) вернул к ~6.5с. Регресс:
   [`tests/test_deal_score_comparable_integration.py`](../tests/test_deal_score_comparable_integration.py)
   (11 тестов).
   **Итог**: качество пула аналогов — не bottleneck AUC, `price_score`
   ≈0.72 похоже на потолок предсказания по одной цене.
3. **Класс-модель ЖК** ([`bot/core/housing_class_model.py`](../bot/core/housing_class_model.py),
   [`housing_class_model_recompute.py`](../housing_class_model_recompute.py),
   [`migrations/071`](../migrations/071_predicted_housing_class.sql)) —
   Gaussian Naive Bayes на ручных лейблах `complexes.housing_class`
   (`log(avg_price_m2)`, `year_built`), применена как `predicted_
   housing_class`/`_probability`/`_source` — `'manual'` не
   перезаписывается, `'predicted'` — модельный вывод, `NULL` — признаков
   не хватило (не гадаем). Holdout accuracy=0.795; редкие классы (элит/
   эконом) — recall=0.0 на holdout из-за 1-2 примеров, честно
   зафиксировано, не скрыто. **Явно НЕ попытка поднять AUC `price_score`
   прямо сейчас** — подготовка для стратификации/Фазы C. Регресс:
   [`tests/test_housing_class_model.py`](../tests/test_housing_class_model.py)
   (9 тестов), [`tests/test_housing_class_model_recompute.py`](../tests/test_housing_class_model_recompute.py)
   (5 тестов).
4. **ER-калибровка** ([`bot/core/er_calibration.py`](../bot/core/er_calibration.py),
   [`er_calibration_report.py`](../er_calibration_report.py)) — по ходу
   выяснилось, что `unit_match_gold_labels` (юнит-уровень,
   `phase2_unit_match.py`, дерево правил без confidence) и
   `AUTO_MATCH_THRESHOLD`/`REVIEW_QUEUE_THRESHOLD` (ЖК-уровень,
   `entity_resolution.py::score_match()`) — два разных механизма; первый
   физически не калибрует второй. ЖК-уровень откалиброван по
   `complex_source_links`/`_candidates`/`_rejections` (confidence реально
   есть) — пороги 0.8/0.5 подтверждены данными как есть, менять не на
   что. Юнит-уровень — честная сводка того, что есть (0 отклонённых
   кандидатов на сегодня, calibrate-ready gold-labels не накоплены).
   Регресс: [`tests/test_er_calibration.py`](../tests/test_er_calibration.py)
   (7 тестов).
5. **Вынос роутов `admin_web.py`/`terminal_extras.py`** (гигиена, не
   связано с AUC) — 3 роута: `/admin/api/listing/{id}` и
   `/admin/api/price-history/{id}` →
   [`bot/core/listing_detail.py`](../bot/core/listing_detail.py)
   (`build_listing_detail`/`build_price_history`, исключения
   `ListingNotFound`/`ListingRestricted` вместо статус-кодов внутри
   роута); геоцентроид ЖК — тот же SQL был буквально задублирован в двух
   роутах (`/admin/complex/{id}` и `/admin/api/complex/{id}/location-score`)
   → одна функция
   [`bot/core/house_resolution.resolve_complex_geo_centroid()`](../bot/core/house_resolution.py).
   Поведение не менялось. Регресс: [`tests/test_listing_detail.py`](../tests/test_listing_detail.py)
   (6 тестов), [`tests/test_house_resolution_geo_centroid.py`](../tests/test_house_resolution_geo_centroid.py)
   (3 теста).

**Общий регресс Фазы B**: полный прогон `./venv/bin/pytest tests/ -q` →
**405/405 passed** после каждого коммита. `krisha-apartments.service`/
`krisha-web.service` перезапускались по ходу фазы, git_hash проверен по
чеклисту [`process.md`](process.md#deploy-чеклист-код-на-диске--код-в-процессе).

**Главный вывод фазы**: `comparable_score` v2 не поднял AUC (Δ в
пределах шума, потолок `price_score` ≈0.72) — bottleneck не в качестве
пула аналогов, а в потолке цены как единственного сигнала. Подробный
разбор и гипотезы для сдвига потолка — [`verdict_strategy.md` §5,
«Анализ потолка price_score»](verdict_strategy.md#анализ-потолка-price_score-фаза-b-2026-08-14).

**Дальше**: Фаза C **НЕ начинается** до накопления `views_history` ≥30
дней (готовность ~2026-09-14, см. `verdict_strategy.md` шапка) — один из
кандидатов на сдвиг потолка (`views_velocity`) физически недоступен
раньше. Следующий заход — продуктовый трек (алерты/локация,
[`verdict_strategy.md` §7](verdict_strategy.md#7-продуктовая-ветка--персональные-алерты-параллельно-отдельный-заход)),
не Фаза C.

---

## Журнал запусков (для регресс-чека бейджа «🤝 Торг» на живых данных)

- 2026-08-14 11:50 — `krisha-apartments.service` перезапущен (git
  fca2cb9 + незакоммиченные правки волны 1). Первый цикл после фикса
  начался 11:50:43. Проверка заполнения `bargain_target` для
  объявлений младше 3 недель — по завершении цикла (см. следующую
  запись в этом файле или прямой запрос к БД).
