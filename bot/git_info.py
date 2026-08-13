"""Короткий git-хэш загруженного кода — задача 2026-08-13 (adaptive
recheck restart-путаница, повторилась с krisha-web.service на этой же
неделе, см. docs/entity_resolution_plan.md): "какой код реально
запущен" должен быть однострочным фактом в логе/app_settings, не
раскопкой journalctl+git log. Общий модуль — service_apartments.py и
service_web.py оба используют, не дублируем реализацию."""
import os
import subprocess


def git_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).strip()
    except Exception:
        return "unknown"
