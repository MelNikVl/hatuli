#!/bin/bash
# ============================================================
# Hatuli — локальный запуск в WSL (Ubuntu) для разработки/теста.
#
# Отличия от прод-сервера:
#   - не нужны systemd-юниты — сервисы запускаются вручную (nohup/tmux)
#   - своя локальная PostgreSQL внутри WSL, отдельная от прод-базы
#   - парсеры Крыши можно держать выключенными (PARSER_ENABLED=0),
#     чтобы не долбить сайт с двух машин одновременно
#
# Запуск (из WSL, НЕ из PowerShell):
#   cd ~/hatuli   # или где лежит клон репозитория внутри WSL
#   bash setup_wsl.sh
# ============================================================
set -e

echo "== 1. Системные пакеты =="
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip \
    postgresql postgresql-contrib

echo "== 2. Запуск PostgreSQL (в WSL сервис не стартует сам после перезагрузки) =="
sudo service postgresql start

echo "== 3. Локальная БД и пользователь =="
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='krisha'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE USER krisha WITH PASSWORD '123';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='krisha_bot'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE DATABASE krisha_bot OWNER krisha;"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE krisha_bot TO krisha;"

echo "== 4. Виртуальное окружение и зависимости =="
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "== 5. Схема (users/listings/rental_index и т.п.) + все миграции по порядку =="
export PGPASSWORD=123
psql -U krisha -d krisha_bot -h localhost -f bot/db/schema.sql
for f in migrations/*.sql; do
  echo "  -> $f"
  psql -U krisha -d krisha_bot -h localhost -f "$f"
done
echo "  (миграция 000 создаёт apartment_listings/complexes/developers —"
echo "   раньше эти таблицы существовали только на проде и нигде не были"
echo "   описаны в репозитории; теперь это исправлено)"

echo "== 6. .env =="
if [ ! -f .env ]; then
  cp env.example .env
  # локальная база вместо прод-пароля-заглушки
  sed -i 's|DATABASE_URL=.*|DATABASE_URL=postgresql://krisha:123@localhost/krisha_bot|' .env
  # парсеры Крыши по умолчанию выключены — не долбим сайт с двух машин
  sed -i 's|PARSER_ENABLED=.*|PARSER_ENABLED=0|' .env
  echo "  .env создан из env.example. ЗАПОЛНИ вручную: ADMIN_PASSWORD и при желании ANTHROPIC_API_KEY/DEEPSEEK_API_KEY"
else
  echo "  .env уже есть — не трогаю"
fi

echo ""
echo "== Готово =="
echo "Запуск веб-терминала:  venv/bin/python service_web.py"
echo "Открыть:               http://localhost:8082/admin"
echo ""
echo "База пустая — если хочешь тестовые данные, самый простой способ:"
echo "  1) сделай дамп с прод-сервера:  pg_dump -U krisha -h 192.168.1.68 krisha_bot > dump.sql"
echo "     (сначала открой доступ postgres к своему IP в pg_hba.conf на сервере,"
echo "      либо просто скопируй dump.sql через файлообменник/флешку)"
echo "  2) залей сюда:  psql -U krisha -d krisha_bot -h localhost -f dump.sql"
