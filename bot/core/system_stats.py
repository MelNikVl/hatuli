"""
Мониторинг сервера и проекта: CPU/память/диск + размер каталога проекта.

Мгновенные значения читаются на каждый запрос страницы (дёшево — psutil
кэширует /proc). История (для графика "по времени") пишется раз в цикл
парсера продаж, тем же паттерном, что floor_stats_history/archived_at —
отдельная таблица-снимок, не нагружаем БД чаще, чем реально нужно для графика.

Размер каталога проекта — тяжёлая операция (обход дерева файлов), поэтому
считается ТОЛЬКО в периодическом снимке, не на каждый запрос страницы.
"""
from __future__ import annotations

import logging
import os

import psutil

logger = logging.getLogger(__name__)

# Файл живёт в bot/core/system_stats.py — три уровня вверх до корня проекта.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_live_stats() -> dict:
    """Мгновенный снимок для живого обновления на странице (без похода в БД)."""
    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_pct": round(cpu, 1),
        "mem_pct": round(mem.percent, 1),
        "mem_used_gb": round(mem.used / 1024**3, 1),
        "mem_total_gb": round(mem.total / 1024**3, 1),
        "disk_pct": round(disk.percent, 1),
        "disk_used_gb": round(disk.used / 1024**3, 1),
        "disk_total_gb": round(disk.total / 1024**3, 1),
    }


def _dir_size_bytes(path: str) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        # venv/.git — не код проекта, но реально место на диске; не исключаем,
        # раз задача — "сколько весь проект занимает места".
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                continue
    return total


async def snapshot_system_stats() -> dict:
    """Пишет снимок CPU/памяти/диска/размера проекта в system_stats_history.
    Вызывается раз в цикл парсера продаж (см. service_apartments.py)."""
    from bot.db.pg import execute

    await execute("""
        CREATE TABLE IF NOT EXISTS system_stats_history (
            id SERIAL PRIMARY KEY,
            at TIMESTAMPTZ DEFAULT now(),
            cpu_pct REAL,
            mem_pct REAL,
            disk_pct REAL,
            project_size_gb REAL
        )
    """)
    live = read_live_stats()
    project_gb = round(_dir_size_bytes(PROJECT_ROOT) / 1024**3, 2)
    await execute(
        "INSERT INTO system_stats_history (cpu_pct, mem_pct, disk_pct, project_size_gb) "
        "VALUES ($1, $2, $3, $4)",
        live["cpu_pct"], live["mem_pct"], live["disk_pct"], project_gb)
    logger.info("system stats snapshot: cpu=%.0f%% mem=%.0f%% disk=%.0f%% project=%.2fGB",
                live["cpu_pct"], live["mem_pct"], live["disk_pct"], project_gb)
    return {**live, "project_size_gb": project_gb}
