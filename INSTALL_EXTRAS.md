# Установка: автозапуск, кнопки, ползунки, топ-10

## Что в этом обновлении

1. **Автозапуск при старте системы** — веб-терминал поднимается сам после перезагрузки
2. **Кнопка "▶️ Запустить проект"** на `/admin/settings` — стартует/стопает парсеры (rental, apartments, alerts) через systemctl
3. **Кнопка "💰 Монетизация ВКЛ/ВЫКЛ"** — флаг в БД (`MONETIZATION_ENABLED`), пока это переключатель; когда появится публичная часть, она будет проверять `app_settings.get_bool("MONETIZATION_ENABLED")`
4. **Ползунки настроек** на `/admin/settings`: депозит, рост цены (те самые 8%), ипотека, комиссия риелтора, порог алертов, страниц парсинга за цикл
5. **Топ-10** на `/admin/top10` — 10 лучших по скору (без дублей и мусора)
6. **Багфикс**: в `admin_web.py` было два роута `/admin/complex_scores` ПОСЛЕ `return app` — мёртвый код, страница не работала. Исправлено.

## Изменённые/новые файлы

```
NEW  migrations/002_app_settings.sql
NEW  bot/db/settings.py
NEW  terminal_extras.py
NEW  bot/templates/settings.html
NEW  bot/templates/top10.html
MOD  bot/admin_web.py          (подключён роутер + фикс мёртвого кода)
MOD  bot/core/insights.py      (константы → чтение из app_settings)
MOD  bot/templates/base.html   (пункты меню Топ-10 и Настройки)
MOD  service_apartments.py     (читает PARSER_MAX_PAGES из настроек в начале цикла)
```

## Шаги на сервере (192.168.1.73)

```bash
cd ~/krisha_bot
git pull   # после того как запушишь эти файлы

# 1. Миграция БД
psql -U krisha -d krisha_bot -h localhost -f migrations/002_app_settings.sql

# 2. Sudoers — чтобы кнопка "Запустить проект" работала без пароля.
#    ВАЖНО: редактировать только через visudo!
sudo visudo -f /etc/sudoers.d/krisha-web
```

Вставить в файл ровно это (пользователь `nik`, только start/stop конкретных сервисов —
никакого общего доступа к systemctl):

```
nik ALL=(root) NOPASSWD: /usr/bin/systemctl start krisha-rental.service, /usr/bin/systemctl stop krisha-rental.service, /usr/bin/systemctl start krisha-apartments.service, /usr/bin/systemctl stop krisha-apartments.service, /usr/bin/systemctl start krisha-alerts.service, /usr/bin/systemctl stop krisha-alerts.service
```

```bash
# 3. Автозапуск: веб-терминал — всегда, парсеры — только по кнопке
sudo systemctl enable krisha-web postgresql
sudo systemctl disable krisha-rental krisha-apartments krisha-alerts  # чтобы не стартовали сами

# 4. Перезапуск веба
sudo systemctl restart krisha-web

# 5. Проверка
curl -s localhost:8082/admin/login | head -1   # должен вернуть HTML
sudo reboot                                     # финальный тест: после ребута
# http://192.168.1.73:8082/admin должен открыться сам
```

## Как это работает

- Настройки лежат в таблице `app_settings` (key/value). Приоритет: **БД → env → дефолт**.
- Веб-терминал пишет туда при сохранении ползунков.
- Парсер квартир вызывает `app_settings.load()` в начале каждого цикла — новые
  значения подхватываются без рестарта сервисов.
- `insights.py` (депозит/ипотека/рост/комиссия) теперь читает значения на каждом
  вызове — карточки объектов сразу считаются по новым ставкам.

## TODO следом (не в этом патче)

- `service_rental.py` и `service_alerts.py` — тоже добавить `await app_settings.load()`
  в начало цикла (по аналогии с service_apartments.py), чтобы ALERT_THRESHOLD
  брался из настроек.
- Публичная часть: когда появится, оборачивать публичные роуты проверкой
  `if app_settings.get_bool("MONETIZATION_ENABLED") and not user_has_subscription: ...`
