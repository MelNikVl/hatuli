"""Standalone scan control flow with fake psql/HTTP/sleep and private state."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError

import pytest

from bot.core import detail_cooldown
from hype_tracker import krisha_complex_scan as scan


FIRST = "https://krisha.kz/complex/show/astana/first/"
ALT = "https://krisha.kz/complex/show/nur-sultan/first/"
SECOND = "https://krisha.kz/complex/show/astana/second/"
PLAIN = "https://krisha.kz/complex/show/first/"


def document(name="First", count=240, *, photos=True):
    return ('{"name":"' + name + '","prefix":"ЖК"}'
            + f'<dt>Количество квартир</dt><dd>{count}</dd>'
            + ('<img src="https://krisha-photos.kcdn.online/fixture-full.webp">' if photos else ''))


def http_error(url, code):
    return HTTPError(url, code, "Fixture status", {}, None)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    state = tmp_path / "detail-cooldown.json"
    monkeypatch.setenv("HATULI_DETAIL_COOLDOWN_PATH", str(state))
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_test")
    calls, requests, sleeps, events = [], [], [], []
    answers = {}
    queue = [["1", "First", FIRST, ""], ["2", "Second", SECOND, ""]]

    def psql(sql):
        calls.append(sql)
        events.append(("sql", sql))
        if "SELECT value FROM parse_settings" in sql:
            if "krisha_enabled" in sql:
                return "1"
            if "krisha_delay_sec" in sql:
                return "2"
            if "krisha_batch" in sql:
                return "10"
            raise AssertionError("Unexpected setting")
        if "SELECT c.id, c.name, c.krisha_url" in sql:
            return "\n".join("|".join(row) for row in queue)
        if sql.startswith(("UPDATE", "INSERT")):
            return ""
        raise AssertionError("Unexpected SQL: " + sql)

    def fetch(url):
        requests.append(url)
        events.append(("fetch", url))
        answer = answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def sleep(seconds):
        sleeps.append(seconds)
        events.append(("sleep", seconds))

    async def deny(*args, **kwargs):
        raise AssertionError("Standalone scanner tests must not connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", deny)
    monkeypatch.setattr("asyncpg.create_pool", deny)
    monkeypatch.setattr("bot.db.pg.init_pool", deny)
    monkeypatch.setattr(scan, "psql", psql)
    monkeypatch.setattr(scan, "fetch", fetch)
    monkeypatch.setattr(scan.time, "sleep", sleep)
    return {"state": state, "sql": calls, "requests": requests, "sleeps": sleeps,
            "events": events, "answers": answers, "queue": queue}


def writes(harness):
    return [sql for sql in harness["sql"] if sql.startswith(("UPDATE", "INSERT"))]


def logs(harness, status):
    return [sql for sql in writes(harness) if "INSERT INTO krisha_parse_log" in sql and f"'{status}'" in sql]


def test_404_without_city_fallback_is_logged_and_next_complex_is_processed(harness):
    harness["queue"][0][2] = PLAIN
    harness["answers"].update({PLAIN: http_error(PLAIN, 404), SECOND: document("Second")})
    assert scan.main() == 0
    assert harness["requests"] == [PLAIN, SECOND]
    assert len(logs(harness, "error")) == len(logs(harness, "ok")) == 1
    assert "VALUES (1," in logs(harness, "error")[0]
    assert "VALUES (2," in logs(harness, "ok")[0]
    assert harness["sleeps"] == [2, 2]
    assert not harness["state"].exists()


def test_both_404_variants_do_not_kill_the_batch_and_keep_request_pacing(harness):
    harness["answers"].update({FIRST: http_error(FIRST, 404), ALT: http_error(ALT, 404), SECOND: document("Second")})
    assert scan.main() == 0
    assert harness["requests"] == [FIRST, ALT, SECOND]
    assert harness["sleeps"] == [2, 2, 2]
    assert len(logs(harness, "error")) == len(logs(harness, "ok")) == 1
    events = harness["events"]
    assert events[events.index(("fetch", ALT)) - 1] == ("sleep", 2)
    assert not any("krisha_url =" in sql for sql in writes(harness))


def test_validated_fallback_updates_url_then_runs_unchanged_writers(harness):
    harness["answers"].update({FIRST: http_error(FIRST, 404), ALT: document("First"), SECOND: document("Second")})
    assert scan.main() == 0
    url_writes = [sql for sql in writes(harness) if "krisha_url =" in sql]
    assert len(url_writes) == 1 and ALT in url_writes[0]
    assert len(logs(harness, "ok")) == 2 and not logs(harness, "error")
    assert any("housing_class_test" in sql and "240" in sql for sql in writes(harness))
    assert harness["events"].index(("fetch", ALT)) < harness["events"].index(("sql", url_writes[0]))


@pytest.mark.parametrize("html", ["", "<html>Generic page</html>", '{"name":"Wrong","prefix":"ЖК"}'])
def test_fallback_empty_or_non_subject_content_cannot_rewrite_url_name_or_mark_ok(harness, html):
    harness["answers"].update({FIRST: http_error(FIRST, 404), ALT: html, SECOND: document("Second")})
    assert scan.main() == 0
    assert harness["requests"] == [FIRST, ALT, SECOND]
    assert len(logs(harness, "error")) == len(logs(harness, "ok")) == 1
    assert not any("WHERE id = 1" in sql or "WHERE complex_id = 1" in sql for sql in writes(harness))
    assert not any("UPDATE apartment_listings" in sql for sql in writes(harness))


@pytest.mark.parametrize("status", [403, 429, 468])
@pytest.mark.parametrize("alternate", [False, True])
def test_source_block_stops_batch_and_never_becomes_object_error_or_success(harness, status, alternate):
    harness["answers"].update({FIRST: http_error(FIRST, 404) if alternate else http_error(FIRST, status),
                                ALT: http_error(ALT, status), SECOND: document("Second")})
    assert scan.main() == 0
    assert harness["requests"] == ([FIRST, ALT] if alternate else [FIRST])
    assert writes(harness) == []
    state = json.loads(harness["state"].read_text(encoding="utf-8"))
    assert state["streak"] == 1 and detail_cooldown.read_cooldown()[0] > 0
    assert "krisha" not in harness["state"].read_text(encoding="utf-8")


@pytest.mark.parametrize("html", ['<h1>Подтвердите, что вы не робот</h1>', '<html>captcha</html>', '<div>challenge-platform</div>'])
def test_challenge_200_is_a_source_pause_not_a_successful_fallback(harness, html):
    harness["answers"].update({FIRST: http_error(FIRST, 404), ALT: html, SECOND: document("Second")})
    assert scan.main() == 0
    assert harness["requests"] == [FIRST, ALT]
    assert writes(harness) == []
    assert detail_cooldown.read_cooldown()[0] > 0


def test_normal_captcha_widget_with_real_complex_data_is_not_misclassified(harness):
    harness["answers"].update({FIRST: document() + '<script src="captcha.js"></script>', SECOND: document("Second")})
    assert scan.main() == 0
    assert len(logs(harness, "ok")) == 2
    assert not harness["state"].exists()


def test_existing_shared_pause_skips_source_and_queue_without_changing_deadline(harness):
    detail_cooldown.record_block()
    before = harness["state"].read_bytes(), harness["state"].stat().st_mtime_ns
    assert scan.main() == 0
    assert harness["requests"] == harness["sleeps"] == []
    assert not writes(harness)
    assert not any("SELECT c.id" in sql for sql in harness["sql"])
    assert before == (harness["state"].read_bytes(), harness["state"].stat().st_mtime_ns)


def test_corrupt_shared_pause_fails_closed_without_fetch_or_per_object_exclusion(harness):
    harness["state"].write_text("invalid private state", encoding="utf-8")
    assert scan.main() == 0
    assert harness["requests"] == []
    assert writes(harness) == []
    assert harness["state"].read_text(encoding="utf-8") == "invalid private state"


def test_new_pause_between_items_prevents_next_request(harness):
    harness["answers"].update({FIRST: document(), SECOND: document("Second")})
    real_sleep = scan.time.sleep
    def pause_during_sleep(seconds):
        real_sleep(seconds)
        detail_cooldown.record_block()
    scan.time.sleep = pause_during_sleep
    assert scan.main() == 0
    assert harness["requests"] == [FIRST]
    assert len(logs(harness, "ok")) == 1
    assert not logs(harness, "error")


def test_generic_network_error_is_logged_without_alternate_request_or_cooldown(harness):
    harness["answers"].update({FIRST: TimeoutError("Fixture timeout"), SECOND: document("Second")})
    assert scan.main() == 0
    assert harness["requests"] == [FIRST, SECOND]
    assert len(logs(harness, "error")) == len(logs(harness, "ok")) == 1
    assert not harness["state"].exists()


def test_successful_complex_does_not_reset_shared_streak_or_rewrite_expired_state(harness):
    detail_cooldown.record_block(now=datetime.now(timezone.utc) - timedelta(hours=1))
    previous = harness["state"].read_bytes(), harness["state"].stat().st_mtime_ns
    harness["answers"].update({FIRST: document(), SECOND: document("Second")})
    assert scan.main() == 0
    assert len(logs(harness, "ok")) == 2
    assert previous == (harness["state"].read_bytes(), harness["state"].stat().st_mtime_ns)


def test_cooldown_persistence_failure_still_stops_batch_without_object_exclusion(harness, monkeypatch):
    harness["answers"][FIRST] = http_error(FIRST, 468)
    def cannot_write():
        raise OSError("Fixture read-only filesystem")
    monkeypatch.setattr(scan, "record_block", cannot_write)
    assert scan.main() == 0
    assert harness["requests"] == [FIRST]
    assert writes(harness) == []


def test_concurrent_longer_source_pause_is_never_shortened(harness, monkeypatch):
    recorded = []
    def fetch_with_concurrent_pause(url):
        harness["requests"].append(url)
        detail_cooldown.record_block(minimum_streak=10)
        recorded.append(json.loads(harness["state"].read_text(encoding="utf-8"))["blocked_until"])
        raise http_error(url, 429)
    monkeypatch.setattr(scan, "fetch", fetch_with_concurrent_pause)
    assert scan.main() == 0
    assert harness["requests"] == [FIRST]
    current = json.loads(harness["state"].read_text(encoding="utf-8"))
    assert datetime.fromisoformat(current["blocked_until"]) >= datetime.fromisoformat(recorded[0])
    assert current["streak"] >= 11
    assert writes(harness) == []


def test_module_import_does_not_import_application_db_or_run_psql():
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib.abc,sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('bot.db','asyncpg','psycopg')):
            raise AssertionError('DB import from standalone scanner: '+fullname)
sys.meta_path.insert(0,Deny())
import subprocess,urllib.request
subprocess.run=lambda *a,**k: (_ for _ in ()).throw(AssertionError('psql at import'))
urllib.request.urlopen=lambda *a,**k: (_ for _ in ()).throw(AssertionError('HTTP at import'))
import hype_tracker.krisha_complex_scan
print('pure import')
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=root, text=True,
                            capture_output=True, check=True, timeout=10)
    assert result.stdout.strip() == "pure import"
