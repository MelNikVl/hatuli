#!/bin/bash
# Автообновление Hatuli из GitHub. Запускается systemd-таймером hatuli-update.timer.
#
# Отличия от заготовки DeepSeek (под нашу реальность):
# - remote называется `hatuli`, НЕ `origin`
# - git выполняется от имени nik (иначе root поломает права на .git)
# - НЕ рестартуем korter/homsters/market: у них циклы 5-7 дней, рестарт
#   запускал бы полный прогон при каждом обновлении
# - сервис krisha-apartment-parser.timer не существует — рестартуем
#   реальные: web, apartments, rental

PROJECT_DIR="/home/nik/krisha_bot"
REMOTE="hatuli"
BRANCH="master"

cd "$PROJECT_DIR" || exit 1

echo "=== Update check at $(date) ==="

sudo -u nik git fetch "$REMOTE" "$BRANCH" 2>&1

if sudo -u nik git diff --quiet HEAD "$REMOTE/$BRANCH"; then
    echo "No changes."
    exit 0
fi

echo "New changes found. Pulling..."
sudo -u nik git pull "$REMOTE" "$BRANCH" 2>&1 || { echo "git pull FAILED"; exit 1; }

echo "Restarting services (web, apartments, rental only)..."
systemctl restart krisha-web.service krisha-apartments.service krisha-rental.service

echo "=== Update completed at $(date) ==="
