#!/usr/bin/env bash
# Пересборка foot-графа OSRM из свежего OSM-экстракта Казахстана
# (Фаза L3 walkability, задача 2026-08-15). Дороги меняются редко —
# запуск руками ~раз в квартал, перед месячным krisha-complex-walkability.
#
#   ./osrm_update.sh
#
# MLD (partition+customize) вместо классического CH (contract): позволяет
# в будущем обновлять веса без полной пересборки графа — тот же алгоритм
# задан и в osrm-foot.service (osrm-routed --algorithm mld).
set -euo pipefail

DATA=/home/nik/osrm
PBF=$DATA/kazakhstan-latest.osm.pbf
IMG=ghcr.io/project-osrm/osrm-backend:latest

cd "$DATA"
echo "== скачиваю kazakhstan-latest.osm.pbf"
curl -sS -L -o "$PBF.new" https://download.geofabrik.de/asia/kazakhstan-latest.osm.pbf
mv "$PBF.new" "$PBF"

echo "== osrm-extract (foot)"
docker run --rm -v "$DATA:/data" "$IMG" osrm-extract -p /opt/foot.lua "$PBF"
echo "== osrm-partition"
docker run --rm -v "$DATA:/data" "$IMG" osrm-partition /data/kazakhstan-latest.osrm
echo "== osrm-customize"
docker run --rm -v "$DATA:/data" "$IMG" osrm-customize /data/kazakhstan-latest.osrm

echo "== перезапуск osrm-foot"
if systemctl list-unit-files osrm-foot.service >/dev/null 2>&1; then
    sudo systemctl restart osrm-foot
else
    docker rm -f osrm-foot 2>/dev/null || true
    docker run -d --name osrm-foot --restart always -p 127.0.0.1:5000:5000 \
        -v "$DATA:/data" "$IMG" osrm-routed --algorithm mld /data/kazakhstan-latest.osrm
fi

echo "== smoke-тест"
sleep 3
curl -sS "http://127.0.0.1:5000/route/v1/foot/71.43,51.13;71.44,51.13" | head -c 200
echo
echo "готово"
