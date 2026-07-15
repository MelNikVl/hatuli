echo "Проверка займёт до ~2.5 минут (по 30 сек таймаут на зеркало)."
for url in \
  "https://overpass-api.de/api/interpreter" \
  "https://overpass.kumi.systems/api/interpreter" \
  "https://overpass.private.coffee/api/interpreter" \
  "https://overpass.openstreetmap.ru/api/interpreter" \
  "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
do
  echo "=== $url ==="
  start=$(date +%s)
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 \
    -d 'data=[out:json];node(51.10,71.40,51.11,71.41)[amenity=school];out 1;' \
    "$url")
  end=$(date +%s)
  echo "HTTP код: $code, время: $((end-start))с"
  echo ""
done
echo "Готово. Ищи зеркало с HTTP кодом 200."
