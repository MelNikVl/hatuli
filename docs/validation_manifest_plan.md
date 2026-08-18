# Blind validation manifest — plan (follow-up п.2)

**Ничего не решено, ничего не смерджено, никакие пороги auto-accept не
установлены.** Этот документ — только reproducible manifest + план счёта
precision/recall, как и просила задача явно ("Пока: решения автоматически
не принимать; пары не объединять; пороги auto-accept не устанавливать;
подготовить только reproducible manifest и план подсчёта precision/recall
по стратам").

## Почему нужен ОТДЕЛЬНЫЙ manifest (не 152 уже принятых решения)

Stage 1.1 этой же задачи явно предупредила: 152(+) ручных решения —
"exploratory calibration, НЕ независимая проверка": выборка маленькая,
очередь показывает пары в ПРИОРИТЕТНОМ порядке (сильный сигнал сначала),
и рецензент ВИДЕЛ фотографии одновременно с решением — три источника
смещения одновременно. Blind-manifest ниже исправляет все три: случайная
(не приоритетная) выборка, стратифицированная для repräsentативности, и
(в будущем UI, не реализованном здесь) рецензент увидит карточки БЕЗ
итоговой рекомендации алгоритма.

## Reproducible manifest

`scripts/build_validation_manifest.py --seed 20260818 --target 250` —
**детерминированный**: тот же seed на тех же данных даёт тот же список
`candidate_id` (`ORDER BY md5(candidate_id::text || seed)`). Two-phase:

1. **Гарантированные страты** (малая квота на каждую редкую комбинацию,
   иначе пропадают за пропорциональной выборкой): corroboration>=2 (30),
   photo exact (20), photo perceptual (20), photo semantic (10), photo
   no_match-но-посчитано (15).
2. **Пропорциональное добавление** по `match_method`-бакету
   (exact_hash/dedup_listings/fuzzy_high/fuzzy_medium/fuzzy_low) до
   общего таргета 250, пропорционально доступной pending-популяции.

### Реальный прогон (2026-08-18, seed=20260818, target=250)

```
guaranteed_strata_achieved:
  corroboration_2plus:      requested=30  achieved=0   <- см. ниже, честная находка
  photo_exact:               requested=20  achieved=20
  photo_perceptual:           requested=20  achieved=20
  photo_semantic:              requested=10  achieved=1
  photo_no_match_computed:      requested=15  achieved=15

bucket_populations (pending): exact_hash=13923, dedup_listings=4069,
  fuzzy_high=15398, fuzzy_medium=7027, fuzzy_low=0

actual_count: 250
photo_signal_coverage_achieved: exact=20, perceptual=21, no_match=17, no_evidence_yet=192
corroboration_coverage_achieved: {"1": 250}
```

**Находка**: `corroboration_2plus` дал **0** — не баг. Прямая проверка:
ВСЕ 72 pending-кандидата с ≥2 corroborating methods уже РЕШЕНЫ (54
accepted + 18 rejected, 0 pending) — очередь ручной проверки уже
полностью вычистила эту страту (она приоритетна первой, задача самого
Stage 0/предыдущих PR). **Для manifest это значит**: blind-валидация
"2+ сигнала" сейчас невозможна на PENDING-популяции — придётся либо
валидировать эти 72 УЖЕ решённых (что превращает их обратно в "не
blind", рецензент видел бы уже принятое решение как часть системы), либо
ждать, пока накопятся НОВЫЕ multi-corroboration кандидаты через
incremental job. Зафиксировано честно, не подделано нулями "как будто
что-то нашли".

`fuzzy_low` (score<0.5) — population **0** на всей базе (не только в
выборке) — этот бакет структурно пуст, `_fuzzy_confidence()` никогда не
возвращает <0.5 на текущих данных (см. формулу в `property_linker.py`).

`photo_semantic` — 1 из 10 (не 0!): в базе ЕСТЬ хотя бы 1 кандидат с
`ai_similar_count>0` — видимо, из предыдущего, более раннего "canary"
прогона AI-стадии (упомянут в докстрингах `bot/identity/photo_evidence.py`
как уже выполненный на ~100 парах ДО этой задачи). Основной корпус
semantic-сигнала появится после Stage 1.3 фото-канарейки (см.
`docs/photo_evidence_canary_followup.md`).

`validation_manifest.json` — полный файл (250 строк с frozen-снимком
`match_method`/`match_score`/`evidence`/`conflict_reasons`/photo-полей на
момент генерации) — коммитится в этот PR как есть, для воспроизводимости
и как базовая версия для будущей blind-валидации.

## План подсчёта precision/recall по стратам (когда появятся решения)

Когда (в будущем PR, blind review UI) рецензент вынесет решение по
каждой из 250 пар manifest'а — решение пишется в НОВУЮ append-only
таблицу (тот же паттерн, что `property_match_review_log`, НЕ
существующий журнал — валидационные решения не должны путаться с
операционными "accepted/rejected" очереди review queue, у них другая
цель — измерение качества сигналов, не финальное решение по конкретной
паре для будущего merge).

Для КАЖДОЙ страты (5 гарантированных + 5 match_method-бакетов + все
достигнутые cross-tab комбинации photo_signal/corroboration):

```
precision_страты = TP / (TP + FP)
  TP = рецензент сказал "одна квартира" И алгоритм БЫ порекомендовал accept
       (по тем же правилам, что review_decisions.py::get_next_candidate
       приоритезирует — НЕ по буквальному "recommendation" полю, которого
       manifest не хранит, а по post-hoc сравнению сигналов со stage 1.1
       эмпирической таблицей precision по сигналу)
  FP = рецензент сказал "разные квартиры", алгоритм БЫ рекомендовал accept

recall_страты = TP / (TP + FN)
  FN = рецензент сказал "одна квартира", но эта страта/сигнал СЛАБЫЙ
       (напр. одиночный fuzzy_medium без фото) — модель НЕ считала бы
       это сильным сигналом
```

**Важно**: "рекомендация алгоритма" ЗДЕСЬ — пост-хок сравнение с
эмпирической таблицей Stage 1.1 (`scripts/audit_review_signal_quality.sql`),
НЕ live-порог внутри самой системы (auto-accept threshold задачей явно
запрещён на этом этапе). Итог по каждой страте — таблица (N, TP, FP, FN,
precision, recall, 95% CI Wilson score — N=20-30 на страту достаточно для
грубого CI, не для узкого доверительного интервала, это тоже нужно
явно указать в итоговом отчёте валидации).

## Что НЕ сделано в этом PR (сознательно)

- Blind review UI (отдельная страница/эндпоинт, скрывающая рекомендацию
  до решения) — НЕ реализована, только manifest + план.
- Ни одно решение по 250 парам manifest'а не принято.
- Ни один порог auto-accept не установлен и не откалиброван.
- `corroboration_2plus`-страта требует отдельного решения (ждать новых
  кандидатов ИЛИ валидировать уже решённые с осознанием, что это не
  чистый blind-тест для этой конкретной страты).
