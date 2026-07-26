-- Многопользовательская админка: логин по username+password вместо одного
-- общего ADMIN_PASSWORD из .env. Первая запись сеется из ADMIN_PASSWORD
-- при первом заходе (см. bot/core/auth_users.py), дальше пароли меняются
-- через /admin/settings.
CREATE TABLE IF NOT EXISTS admin_users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
