"""Local human receipts are the only publication handoff; all I/O is isolated."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from scripts import publish_daily_picks_browser as worker
from tests.test_daily_picks_quality_policy import PHOTO_A, edition_rows


@pytest.fixture(autouse=True)
def deny_live_io(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://placeholder:placeholder@127.0.0.1:1/visual_handoff_test")
    async def deny(*args, **kwargs):
        raise AssertionError("No live browser, source, database or candidate export in handoff tests")
    for name in ("connect", "create_pool"):
        monkeypatch.setattr("asyncpg." + name, deny)
    monkeypatch.setattr("bot.db.pg.init_pool", deny)
    monkeypatch.setattr(worker.publisher, "read_candidates", deny)
    monkeypatch.setattr(worker.publisher, "export_candidate_pool", deny)
    monkeypatch.setattr(worker.publisher, "verify_sources", deny)
    monkeypatch.setattr(worker.publisher, "verify_photos", deny)
    monkeypatch.setattr(worker, "check_sources", deny)
    monkeypatch.setattr(worker, "ordinary_browser", deny)
    monkeypatch.setattr("httpx.AsyncClient.request", deny)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)


def args(path=None, **extra):
    return SimpleNamespace(source_checks=str(path) if path is not None else None,
                           dry_run=False, force=True, quality_policy="discounted_four_factors",
                           budget_seconds=60, check_limit=90, headed=False, profile_path=None,
                           **extra)


def local_receipts(tmp_path, now, count=10):
    rows = edition_rows(count=count, now=now)
    for row in rows:
        row.update(candidate_photo_urls=[PHOTO_A], approved_photo_urls=[PHOTO_A])
    records = [{"id": row["id"], "source_url": row["url"], "status": "active", "sale_astana": True,
                "source_complex_name": "Тестовый ЖК", "checked_at": now.isoformat(),
                "interior_photo_urls": [PHOTO_A],
                "photo_review": {"method": "normal_browser_visual", "photo_type": "interior", "checked_at": now.isoformat()},
                **{field: row[field] for field in ("price", "rooms", "area", "floor", "floors_total", "ceiling_height", "kitchen_area")}}
               for row in rows[:count]]
    path = tmp_path / "private-local-checks.json"
    worker.atomic_private_json(path, {"method": "normal_browser", "items": records})
    return path, rows, records


def configure_strict_publisher(monkeypatch, rows):
    state = {"held": False, "reads": 0, "image_batches": 0}
    @contextmanager
    def lock(path):
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False
    async def read(*unused):
        assert state["held"]
        state["reads"] += 1
        return rows
    async def photos(snapshot, deadline):
        assert state["held"]
        state["image_batches"] += 1
        return snapshot, len(snapshot["items"])
    monkeypatch.setattr(worker.publisher, "publisher_lock", lock)
    monkeypatch.setattr(worker.publisher, "read_candidates", read)
    monkeypatch.setattr(worker.publisher, "verify_photos", photos)
    return state


@pytest.mark.asyncio
async def test_no_handoff_refuses_before_export_browser_or_database(tmp_path):
    with pytest.raises(worker.VisualReceiptHandoffRequired, match="--source-checks PRIVATE_PATH"):
        await worker.pipeline(args(), tmp_path / "never-started", {}, tmp_path / "public.json")
    assert not (tmp_path / "never-started").exists()


def test_no_handoff_cli_explains_required_local_receipts_before_locks_or_profile(tmp_path, monkeypatch, capsys):
    def denied(*unused):
        raise AssertionError("A missing handoff must fail before setup")
    monkeypatch.setattr(worker, "dedicated_profile", denied)
    monkeypatch.setattr(worker, "worker_lock", denied)
    with pytest.raises(SystemExit) as error:
        worker.main([])
    assert error.value.code == 2
    assert "scripts/publish_daily_picks.py --quality-policy discounted_four_factors --source-checks PRIVATE_PATH" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_fresh_local_handoff_publishes_ten_with_real_strict_publisher_without_browser(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    path, rows, records = local_receipts(tmp_path, now)
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    state = configure_strict_publisher(monkeypatch, rows)
    snapshot = tmp_path / "daily-picks.json"
    result = await worker.pipeline(args(path), tmp_path / "run", {}, snapshot)
    data = worker.publisher.load_daily_picks(snapshot)
    assert result["published"] is True and result["count"] == 10 and result["source_gets"] == 0
    assert data["publication_policy"]["name"] == "discounted_four_factors"
    assert all(len(item["reasons"]) == 4 and item["photo_urls"] == [PHOTO_A] for item in data["items"])
    assert state["reads"] == 1 and state["image_batches"] >= 1 and not state["held"]
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert not (tmp_path / "run" / "candidates.json").exists()
    assert not (tmp_path / "run" / "source-checks.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing_file", "invalid_json", "wrong_method", "stale", "future"])
async def test_bad_handoff_is_rejected_before_database_export_or_browser_even_with_force(tmp_path, bad):
    now = datetime.now(timezone.utc)
    path, rows, records = local_receipts(tmp_path, now)
    if bad == "missing_file":
        path = tmp_path / "missing.json"
    elif bad == "invalid_json":
        path.write_text("not json", encoding="utf-8")
    else:
        payload = {"method": "normal_browser", "items": records}
        if bad == "wrong_method":
            payload["method"] = "automatic"
        else:
            records[0]["checked_at"] = (now + (timedelta(hours=-3) if bad == "stale" else timedelta(hours=1))).isoformat()
        path.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = tmp_path / "public.json"
    snapshot.write_bytes(b"previous edition")
    before, mtime = snapshot.read_bytes(), snapshot.stat().st_mtime_ns
    with pytest.raises((ValueError, OSError)):
        await worker.pipeline(args(path), tmp_path / "run", {}, snapshot)
    assert snapshot.read_bytes() == before and snapshot.stat().st_mtime_ns == mtime


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing_visual", "different_visual_timestamp", "changed_price"])
async def test_handoff_still_enforces_visual_and_current_offer_gates_and_retains_previous(tmp_path, monkeypatch, bad):
    now = datetime.now(timezone.utc)
    path, rows, records = local_receipts(tmp_path, now)
    if bad == "missing_visual":
        for record in records:
            record.pop("photo_review")
            record.pop("interior_photo_urls")
    elif bad == "different_visual_timestamp":
        records[0]["photo_review"]["checked_at"] = (now-timedelta(seconds=1)).isoformat()
    else:
        records[0]["price"] += 1_000_000
    path.write_text(json.dumps({"method": "normal_browser", "items": records}), encoding="utf-8")
    configure_strict_publisher(monkeypatch, rows)
    snapshot = tmp_path / "public.json"
    snapshot.write_bytes(b"previous edition")
    before, mtime = snapshot.read_bytes(), snapshot.stat().st_mtime_ns
    with pytest.raises((ValueError, worker.InsufficientConfirmedCardsError)):
        await worker.pipeline(args(path), tmp_path / "run", {}, snapshot)
    assert snapshot.read_bytes() == before and snapshot.stat().st_mtime_ns == mtime


@pytest.mark.asyncio
async def test_handoff_dry_run_uses_strict_calculation_without_publishing(tmp_path, monkeypatch):
    path, rows, records = local_receipts(tmp_path, datetime.now(timezone.utc))
    state = configure_strict_publisher(monkeypatch, rows)
    snapshot = tmp_path / "public.json"
    snapshot.write_bytes(b"previous edition")
    before, mtime = snapshot.read_bytes(), snapshot.stat().st_mtime_ns
    options = args(path)
    options.dry_run = True
    result = await worker.pipeline(options, tmp_path / "run", {}, snapshot)
    assert result["status"] == "dry_run" and result["published"] is False and result["count"] == 10
    assert state["reads"] == 1
    assert snapshot.read_bytes() == before and snapshot.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("flags", [["--headed"], ["--profile-path", "unused"], ["--open-browser"],
                                    ["--source-url", "https://krisha.kz/a/show/1016000000"],
                                    ["--dry-run", "--check-limit", "1"]])
def test_handoff_cannot_silently_ignore_browser_only_flags(flags):
    with pytest.raises(SystemExit) as error:
        worker.main(["--source-checks", "private-local-checks.json", *flags])
    assert error.value.code == 2


def test_handoff_ignores_unrelated_profile_environment_and_keeps_todays_idempotency(tmp_path, monkeypatch):
    @contextmanager
    def lock(path):
        path.mkdir(parents=True, exist_ok=True)
        yield
    def denied(*unused):
        raise AssertionError("A handoff must not use the browser profile or repeat a valid edition")
    monkeypatch.setenv("HATULI_DAILY_PICKS_BROWSER_PROFILE", "/unrelated/user/profile")
    monkeypatch.setattr(worker, "dedicated_profile", denied)
    monkeypatch.setattr(worker, "pipeline", denied)
    monkeypatch.setattr(worker, "worker_lock", lock)
    monkeypatch.setattr(worker, "already_published", lambda *unused: True)
    monkeypatch.setattr(worker.publisher, "snapshot_path", lambda: tmp_path / "public.json")
    assert worker.main(["--source-checks", "private-local-checks.json"]) == 0
    status = json.loads((tmp_path / "daily-picks-browser" / "last-run.json").read_text())
    assert status["status"] == "already_published" and status["published"] is False


@pytest.mark.parametrize("options", [[], ["--source-checks", "private-local-checks.json"], ["--source-checks=private-local-checks.json"],
                                      ["--source-checks", "private-local-checks.json", "--open-browser"],
                                      ["--open-browser", "--source-checks", "private-local-checks.json"]])
def test_shell_wrapper_handoff_or_refusal_never_starts_xvfb(tmp_path, options):
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if not bash:
        pytest.skip("Bash is required for the isolated wrapper regression")
    root = tmp_path / "isolated-project"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / "run_daily_picks_browser.sh"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts" / wrapper.name, wrapper)
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$HANDOFF_ARGS_OUTPUT"\n', encoding="utf-8", newline="\n")
    python.chmod(0o755)
    commands = tmp_path / "commands"
    commands.mkdir()
    for name in ("xvfb-run", "systemctl"):
        path = commands / name
        path.write_text('#!/usr/bin/env bash\nexit 88\n', encoding="utf-8", newline="\n")
        path.chmod(0o755)
    output = tmp_path / "python-args.txt"
    environment = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ.get("PATH", ""),
                   "HANDOFF_ARGS_OUTPUT": output.as_posix()}
    result = subprocess.run([bash, str(wrapper), *options], env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text(encoding="utf-8").splitlines() == ["scripts/publish_daily_picks_browser.py", *options]
