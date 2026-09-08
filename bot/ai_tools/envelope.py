"""bot/ai_tools/envelope.py — единый machine-readable контракт ответа для
будущих AI tools (Этап 2 задачи "Hatuli API v1 для AI tools").

Намеренно НЕ переписывает существующие core-модули под этот формат —
только адаптер на API-слое (bot/ai_tools/core.py возвращает `ToolResult`,
эта функция раскладывает его в контракт). Форма зафиксирована постановкой
задачи 1:1:

    {
      "entity_type": "...",
      "entity_id": "...",
      "as_of": "...",
      "data": {},
      "confidence": null,
      "evidence": [],
      "warnings": [],
      "methodology_version": "..."
    }

`data` — ВСЕГДА нетронутый passthrough того, что вернула существующая
core-функция (см. докстринг каждой функции в core.py, откуда именно взят
каждый факт) — обёртка ничего не досчитывает и не переформулирует внутри
data. confidence/evidence/warnings/methodology_version на верхнем уровне —
best-effort ИЗВЛЕЧЕНИЕ уже существующих полей (см. ToolResult docstring):
если у конкретного тула честно нет такого поля — здесь остаётся None/[],
а не выдуманное значение (задача явно: "если поля реально нет — не
выдумывать")."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Внутреннее представление результата одной read-only capability
    (bot/ai_tools/core.py) — до сборки в конечный envelope. Поля —
    буквально те же, что в envelope, кроме entity_type/entity_id (их
    знает вызывающий код: entity_id — это ровно то, что ему передали)."""
    data: dict[str, Any]
    as_of: str | None = None
    confidence: Any = None
    evidence: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    methodology_version: str | None = None


def build_envelope(entity_type: str, entity_id: Any, result: ToolResult) -> dict:
    """Единственная точка сборки envelope — используется и router.py (для
    HTTP-ответа), и напрямую в тестах (без HTTP), и будущим orchestrator-
    агентом, если он будет звать core.py в процессе, а не по HTTP."""
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "as_of": result.as_of,
        "data": result.data,
        "confidence": result.confidence,
        "evidence": result.evidence,
        "warnings": result.warnings,
        "methodology_version": result.methodology_version,
    }
