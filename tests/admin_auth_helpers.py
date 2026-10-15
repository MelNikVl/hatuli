"""Issue real admin sessions for HTTP tests instead of forging legacy cookies."""
from bot.core.admin_sessions import COOKIE_NAME, create_admin_session
from bot.core.auth_users import hash_password
from bot.db.pg import fetchval

_PASSWORD_HASH = hash_password("isolated-http-test-user")


async def admin_cookies(username: str = "pytest") -> dict[str, str]:
    if "test" not in (await fetchval("SELECT current_database()")):
        raise RuntimeError("HTTP admin fixtures require an isolated test database")
    user_id = await fetchval("""
        INSERT INTO admin_users (username,password_hash) VALUES ($1,$2)
        ON CONFLICT (username) DO NOTHING RETURNING id
    """, username, _PASSWORD_HASH)
    if user_id is None:
        user_id = await fetchval("SELECT id FROM admin_users WHERE username=$1", username)
    return {COOKIE_NAME: await create_admin_session(user_id)}


async def authenticate_admin(client, username: str = "pytest") -> None:
    client.cookies.update(await admin_cookies(username))
