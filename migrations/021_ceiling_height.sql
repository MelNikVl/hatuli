-- Высота потолков (м) с детальной страницы krisha.kz — уже парсилась в
-- apartment_details.py, но выбрасывалась (не было колонки). Заметный фактор
-- цены в Астане (панель ~2.5м vs монолит 3м+) — используется в deal_score.py.
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS ceiling_height NUMERIC(3,2);
