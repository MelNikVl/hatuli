# Обновление 6: korter как отдельный сервис (1-4ч), интеграция целостности

## Установка

```bash
cd ~/krisha_bot
# скопировать файлы из архива поверх (korter_import.py заменить, остальное новое)

sudo cp krisha-korter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now krisha-korter

# добавить korter в sudoers-правило для кнопки "Запустить проект"
sudo visudo -f /etc/sudoers.d/krisha-web
```
Дописать в существующую строку (или новую строку) права на korter:
```
nik ALL=(root) NOPASSWD: ..., /usr/bin/systemctl start krisha-korter.service, /usr/bin/systemctl stop krisha-korter.service
```

Проверка:
```bash
sudo systemctl status krisha-korter --no-pager
tail -30 ~/krisha_bot/korter.log
```

## Что изменилось
- `service_korter.py` — постоянный цикл, интервал **случайный 1-4 часа** между
  прогонами (как и другие парсеры — не бьёт ровно по часам, чтобы не выглядеть
  ботом). Первый прогон сразу при старте сервиса.
- `krisha-korter.service` — systemd юнит, тот же паттерн что у остальных
  (Restart=always, KillSignal=SIGKILL).
- Кнопка "Запустить проект" в `/admin/settings` теперь включает и korter.
- Лог korter доступен в веб-логах (`korter.log`).
- **Если увидишь в korter.log 403/429/капчу** — значит частота 1-4ч слишком
  агрессивная для Korter, надо будет увеличить интервал (следующим шагом
  вынесем его в настройку KORTER_INTERVAL_HOURS).

## Проверка целостности проекта (сделана перед отправкой)
- Все `.py` файлы компилируются без ошибок (кроме `complexes_route.py` —
  это старый мёртвый файл-сниппет с комментарием "вставить в admin_web.py",
  никуда не импортируется, не влияет на рантайм; можно удалить или оставить)
- Все 22 jinja-шаблона валидны
- Все ссылки в шаблонах (`href="/admin/..."`) сверены с реально
  объявленными роутами в `admin_web.py`/`terminal_extras.py` — расхождений нет
- Миграции пронумерованы по порядку 001-006, идемпотентны (IF NOT EXISTS)
- `.gitignore` защищает `.env`, `google_creds.json`, `*.log`, `bot.db`
- Секретов, захардкоженных в коде, не найдено (только дефолтный fallback-пароль
  `admin123` в `service_web.py` — используется если `ADMIN_PASSWORD` не задан
  в `.env`; убедись что в проде задан реальный пароль)
- `env.example` содержит только плейсхолдеры, реальных ключей нет

## Про отправку в GitHub
У меня нет доступа с правом записи к твоему приватному репозиторию — пуш
нужно сделать с твоей стороны. С сервера, где лежат актуальные проверенные
файлы:
```bash
cd ~/krisha_bot
git add -A
git commit -m "Веб-терминал: зоны, слои скоринга, korter-обогащение, AI-анализ, торг"
git push
```
Если `git push` спросит логин/токен — GitHub с 2021 года не принимает пароль
аккаунта для push, нужен Personal Access Token (Settings → Developer settings
→ Personal access tokens) вместо пароля.
