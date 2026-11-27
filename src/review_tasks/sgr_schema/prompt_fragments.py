"""Список областей Stage 1 из канонического каталога ``sgr_schema``."""

from __future__ import annotations

from functools import lru_cache

from review_tasks.sgr_schema.enums import FOCUS_AREA_CATALOG


@lru_cache(maxsize=1)
def render_focus_areas_list() -> str:
    """Нумерованный Markdown-список SGR-областей в порядке `FOCUS_AREA_CATALOG`.

    Формат строки: ``N. `<name>` — <description> Источник: <source_hint>.``
    Описание и source_hint берутся из ``FocusAreaMeta`` как есть; backticks
    вокруг path-глобов в ``source_hint`` объявлены прямо в каталоге.
    """
    lines: list[str] = []
    for ordinal, meta in enumerate(FOCUS_AREA_CATALOG, start=1):
        lines.append(
            f"{ordinal}. `{meta.name}` — {meta.description} "
            f"Источник: {meta.source_hint}."
        )
    return "\n".join(lines) + "\n"


__all__ = ["render_focus_areas_list"]
