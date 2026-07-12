# Обновление 2: актуальность, зоны, карточки ЖК, инфо-страница

## Что внутри

1. **Фикс 500 на карточке объекта** — теперь при ошибке страница показывает полный
   трейсбек (удобно дебажить), шаблон защищён от None в area/price.
2. **Актуальность объявлений** — новые колонки `is_active`, `archived_at`.
   Каждый цикл парсер проверяет страницы 15 топовых объявлений на "В архиве"
   и помечает проданные. Топ-10 и Аналитика показывают только живые
   (+ не виденные парсером >14 дней скрываются).
3. **Координаты** — детальный парсер вытаскивает lat/lon со страницы krisha.
4. **Зоны приоритета** — `/admin/zones`: рисуешь области мышкой на карте
   (Ботанический парк, Туран, Мангилик...), назначаешь бонус +5..+20.
   Объявления с координатами внутри зоны получают бонус к скору
   (колонки zone_bonus/zone_name, учитывается в сортировке Топ-10).
5. **Карточка ЖК** — `/admin/complex/{id}` (клик по имени ЖК в рейтинге):
   статистика по комнатности (медианы, сколько ушло в архив, среднее время
   до архива = прокси "как быстро продаётся"), все объявления продажи и
   аренды, редактируемые поля ОСИ / УК / чаты жителей / заметки,
   быстрые ссылки на 2GIS/Google Maps/Telegram-поиск.
6. **Инфо-страница** — `/admin/info`: что такое хороший yield, как работает
   парсер, что значит колонка "Продажи", как устроен скоринг и торг.
7. **Косметика** — yield округлён до 1 знака везде (было 19.700000762939453%).

## Установка

```bash
cd ~/krisha_bot
# скопировать файлы из архива поверх (или git pull после пуша)

psql -U krisha -d krisha_bot -h localhost -f migrations/003_active_zones_complexes.sql

sudo systemctl restart krisha-web
sudo systemctl restart krisha-apartments   # если запущен
```

## Изменённые/новые файлы

```
NEW  migrations/003_active_zones_complexes.sql
NEW  bot/core/zones.py
NEW  bot/core/archive_check.py
NEW  bot/templates/zones.html
NEW  bot/templates/info.html
NEW  bot/templates/complex_detail.html
MOD  bot/core/apartment_details.py   (координаты + признак архива)
MOD  service_apartments.py           (зоны, архив-чек в цикле)
MOD  terminal_extras.py              (роуты zones/info/complex, фильтры top10)
MOD  bot/admin_web.py                (фильтр актуальности, трейсбек на 500)
MOD  bot/templates/base.html         (меню: Зоны, Инфо)
MOD  bot/templates/top10.html        (бейдж зоны)
MOD  bot/templates/complexes.html    (ЖК кликабелен, yield округлён)
MOD  bot/templates/analytics.html    (округление)
MOD  bot/templates/analytics_detail.html (защита от None)
```

## Важно про старый бот (из твоих логов)

Трейсбек в дашборде — это `bot/main.py` (СТАРЫЙ монолит) падает с
`SystemExit: 1`, потому что uvicorn не может занять порт 8082 — его уже
держит krisha-web. Что-то до сих пор пытается запускать старый main.py.
Проверь и отключи:

```bash
systemctl list-units --all | grep -i krisha     # ищи старый юнит (krisha-bot?)
crontab -l                                       # и в кроне
ps aux | grep main.py
# найдёшь юнит:
sudo systemctl disable --now <имя-старого-юнита>
```

## Парсер аренды не работал с 07.06!

Дашборд показывает "Парсер аренды: Проблема, последний 07.06" — месяц простоя.
Без свежей аренды rental_index протухает и yield продаж считается по старым
ставкам. Проверь:

```bash
sudo systemctl status krisha-rental --no-pager
tail -50 ~/krisha_bot/rental.log   # или journalctl -u krisha-rental -n 50
```
