# Установка платных алертов (Telegram Stars)

## Что в комплекте
- `service_alerts.py` — НОВЫЙ файл: бот подписки + рассылка алертов (положить в корень krisha_bot)
- `service_apartments.py` — ЗАМЕНА существующего: добавлен трекинг истории цен (~14 новых строк перед UPDATE)
- `migrations/001_alerts.sql` — таблица price_history + индексы
- `krisha-alerts.service` — systemd-юнит

## Шаги на сервере

```bash
cd /home/nik/krisha_bot

# 1. Скопировать файлы (service_apartments.py заменяет существующий)
#    предварительно: cp service_apartments.py service_apartments.py.bak

# 2. Миграция БД
psql -U krisha -d krisha_bot -h localhost -f migrations/001_alerts.sql

# 3. В .env добавить (BOT_TOKEN уже должен быть от @BotFather):
#    ADMIN_TELEGRAM_ID=<твой telegram id>
#    ALERT_MIN_SCORE=70
#    ALERT_PRICE_DROP_PCT=3
#    ALERT_INTERVAL_MIN=10
#    SUB_PRICE_STARS=500
#    SUB_DAYS=30
#    FREE_TRIAL_DAYS=3

# 4. Запуск
sudo cp krisha-alerts.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart krisha-apartment-parser.timer   # подхватить новый парсер
sudo systemctl enable --now krisha-alerts
journalctl -u krisha-alerts -f

# 5. Коммит
git add service_alerts.py service_apartments.py migrations/ krisha-alerts.service
git commit -m "Add paid alerts via Telegram Stars + price history tracking"
git push hatuli master
```

## Проверка
1. Напиши боту /start — должен ответить и выдать триал
2. /subscribe — должен прийти инвойс в Stars
3. /stats (только с ADMIN_TELEGRAM_ID) — статистика
4. Подожди цикл парсера — объекты со скором 70+ прилетят алертом

## Важно про Stars
- Оплата в XTR не требует платёжного провайдера и юрлица
- Звёзды копятся на балансе бота, вывод через Fragment (нужен TON-кошелёк), холд 21 день
- Цену в звёздах подбирай: 500 ⭐ ≈ $10 ≈ 5300 ₸
