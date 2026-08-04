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

# БАГ (найден при расследовании растущего бэклога фото/потолков/геопривязки,
# 2026-08-04): `git diff --quiet HEAD "$REMOTE/$BRANCH"` сравнивает КОММИТЫ,
# не рабочую копию — но в этом репо часто есть локальные закоммиченные, но
# ещё НЕ запушенные изменения (напр. от "DeepSeek"-агента), из-за которых
# HEAD и remote/branch расходятся ПОСТОЯННО, даже когда git pull реально не
# приносит ничего нового ("Already up to date."). Итог: каждые 5 минут,
# КРУГЛОСУТОЧНО, krisha-apartments/rental/web получали systemctl restart —
# TimeoutStopSec=15 не хватало, чтобы корректно остановиться, процесс
# получал SIGKILL. Для apartments это означало, что длинные последовательные
# циклы (coord_backfill — до 80 объявлений по 8-17с паузой между запросами,
# ~15-20 минут) НИКОГДА не успевали доработать даже наполовину — отсюда
# хронически не растущий/еле ползущий прогресс по photos/ceiling_height,
# который стал заметен как растущий бэклog при всплеске новых объявлений.
#
# Фикс: сравниваем не diff, а реально ли remote УШЁЛ ВПЕРЁД локального HEAD
# (rev-list --count BRANCH..REMOTE/BRANCH) — если 0 (ветки разошлись или
# локальный уже впереди), пуллить нечего, рестарт не нужен. И ещё одна
# защита: рестартуем только если HEAD реально изменился после pull.
BEFORE_SHA=$(sudo -u nik git rev-parse HEAD)
AHEAD=$(sudo -u nik git rev-list --count "$BRANCH".."$REMOTE/$BRANCH")

if [ "$AHEAD" -eq 0 ]; then
    echo "No new commits from remote (local HEAD already has everything, or has unpushed local commits ahead)."
    exit 0
fi

echo "New changes found ($AHEAD commit(s) behind remote). Pulling..."
sudo -u nik git pull "$REMOTE" "$BRANCH" 2>&1 || { echo "git pull FAILED"; exit 1; }

AFTER_SHA=$(sudo -u nik git rev-parse HEAD)
if [ "$BEFORE_SHA" = "$AFTER_SHA" ]; then
    echo "Pull completed but HEAD unchanged, skipping restart."
    exit 0
fi

echo "Restarting services (web, apartments, rental only)..."
systemctl restart krisha-web.service krisha-apartments.service krisha-rental.service

echo "=== Update completed at $(date) ==="
