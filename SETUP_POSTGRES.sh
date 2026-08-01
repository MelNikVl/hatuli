#!/bin/bash
# ============================================================
# Установка и настройка PostgreSQL на Ubuntu (IdeaPad Pro 5)
# Запускать по одной секции
# ============================================================

# 1. Установка PostgreSQL
sudo apt update
sudo apt install -y postgresql postgresql-contrib

# 2. Запуск сервиса
sudo systemctl enable postgresql
sudo systemctl start postgresql

# 3. Создать пользователя и базу данных
sudo -u postgres psql <<SQL
CREATE USER krisha WITH PASSWORD 'your_strong_password_here';
CREATE DATABASE krisha_bot OWNER krisha;
GRANT ALL PRIVILEGES ON DATABASE krisha_bot TO krisha;
SQL

# 4. Применить схему
psql -U krisha -d krisha_bot -h localhost -f bot/db/schema.sql

# 5. Добавить asyncpg в venv
source venv/bin/activate
pip install asyncpg==0.29.0

# 6. Скопировать .env.example → .env и заполнить
cp .env.example .env
# Отредактируй .env: DATABASE_URL, BOT_TOKEN, PARSER_ENABLED=0

# 7. Мигрировать данные из SQLite (если есть)
python migrate_sqlite_to_pg.py

# 8. Запустить бот (парсер выключен, rental loop работает)
nohup python -m bot.main > bot.log 2>&1 &
echo "Bot PID: $!"

# 9. Смотреть логи
tail -f bot.log | grep -E "rental|error|ERROR"

# 10. Проверить rental_index через 30-60 минут
psql -U krisha -d krisha_bot -h localhost -c "
SELECT prop_type, district, complex_name, rooms,
       median_price, sample_count
FROM rental_index
ORDER BY sample_count DESC
LIMIT 20;"
