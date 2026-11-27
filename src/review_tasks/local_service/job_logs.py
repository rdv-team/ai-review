"""Canonical registry for service-owned job logs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .problem_details import diagnostic_entry, unique_diagnostics
from .opencode_log import READABLE_LOG_FILENAME
from .storage import safe_tail


@dataclass(frozen=True)
class JobLogSource:
    ref: str
    filename: str
    label: str


JOB_LOG_SOURCES: tuple[JobLogSource, ...] = (
    JobLogSource("init.stderr", "init.stderr.log", "Лог инициализации"),
    JobLogSource("codex.stderr", "codex.stderr.log", "Лог Codex"),
    JobLogSource("codex.final", "codex.final.md", "Финальный ответ Codex"),
    JobLogSource("cursor.stderr", "cursor.stderr.log", "Ошибки Cursor"),
    JobLogSource("cursor.stdout", "cursor.stdout.log", "Вывод Cursor"),
    JobLogSource("opencode.stderr", "opencode.stderr.log", "Ошибки OpenCode"),
    JobLogSource("opencode.stdout", "opencode.stdout.log", "Вывод OpenCode"),
    JobLogSource("opencode.agent", READABLE_LOG_FILENAME, "Лог агента OpenCode"),
    JobLogSource("mcp.registry.stderr", "preflight.mcp-registry.stderr.log", "Проверка MCP registry"),
    JobLogSource("mcp.auth.stderr", "preflight.mcp-auth-env.stderr.log", "Проверка MCP auth environment"),
    JobLogSource("mcp.url.stderr", "preflight.mcp-url-env.stderr.log", "Проверка MCP URL environment"),
)
JOB_LOG_BY_REF = {source.ref: source for source in JOB_LOG_SOURCES}
JOB_LOG_BY_FILENAME = {source.filename.casefold(): source for source in JOB_LOG_SOURCES}


def job_log_path(run_dir: Path, log_ref: str) -> Path | None:
    source = JOB_LOG_BY_REF.get(log_ref)
    if source is None:
        return None
    resolved_run_dir = run_dir.expanduser().resolve()
    path = (resolved_run_dir / source.filename).resolve()
    if path.parent != resolved_run_dir:
        return None
    return path


def build_log_diagnostics(run_dir: Path, refs: Iterable[str]) -> list[dict[str, Any]]:
    entries = []
    for log_ref in refs:
        source = JOB_LOG_BY_REF.get(log_ref)
        path = job_log_path(run_dir, log_ref)
        if source is None or path is None:
            continue
        tail = safe_tail(path)
        if tail is None or not tail.strip():
            continue
        entries.append(
            diagnostic_entry(
                source.ref,
                source.label,
                "log",
                value=tail.replace("\r\n", "\n").replace("\r", "\n"),
                log_ref=source.ref,
            )
        )
    return unique_diagnostics(entries)


def build_detail_diagnostics(
    details: dict[str, Any] | None,
    existing: Iterable[dict[str, Any] | None] = (),
) -> list[dict[str, Any]]:
    """Project legacy exception details into canonical entries without duplicating known logs."""
    entries = unique_diagnostics(list(existing))
    seen = {entry["id"] for entry in entries}
    if not details:
        return entries

    consumed: set[str] = set()
    for key, path_value in details.items():
        if not key.casefold().endswith("path") or not isinstance(path_value, str) or not path_value.strip():
            continue
        source = JOB_LOG_BY_FILENAME.get(Path(path_value).name.casefold())
        base = key[:-4]
        tail_key = next((candidate for candidate in details if candidate.casefold() == f"{base}tail".casefold()), None)
        tail = details.get(tail_key) if tail_key is not None else None
        if source is not None:
            consumed.add(key)
            if tail_key is not None:
                consumed.add(tail_key)
            if source.ref not in seen and isinstance(tail, str) and tail.strip():
                entry = diagnostic_entry(
                    source.ref,
                    source.label,
                    "log",
                    value=tail.replace("\r\n", "\n").replace("\r", "\n"),
                    log_ref=source.ref,
                )
                if entry is not None:
                    entries.append(entry)
                    seen.add(source.ref)
            continue

        entry_id = f"error.{base or 'log'}"
        value = tail if isinstance(tail, str) and tail.strip() else path_value
        entry = diagnostic_entry(entry_id, _detail_label(base or key), "log", value=value)
        if entry is not None and entry_id not in seen:
            entries.append(entry)
            seen.add(entry_id)
        consumed.add(key)
        if tail_key is not None:
            consumed.add(tail_key)

    for key, value in details.items():
        if key in consumed or value is None or (isinstance(value, str) and not value.strip()):
            continue
        normalized_key = key.casefold()
        matching_source = next(
            (
                source
                for source in JOB_LOG_SOURCES
                if normalized_key.endswith("tail")
                and all(part in normalized_key for part in source.ref.casefold().split("."))
            ),
            None,
        )
        if matching_source is not None and matching_source.ref in seen:
            continue
        entry_id = matching_source.ref if matching_source is not None else f"error.{key}"
        kind = "log" if normalized_key.endswith("tail") else "path" if normalized_key.endswith("path") else "data"
        log_ref = matching_source.ref if matching_source is not None else None
        entry = diagnostic_entry(entry_id, matching_source.label if matching_source else _detail_label(key), kind, value=value, log_ref=log_ref)
        if entry is not None and entry_id not in seen:
            entries.append(entry)
            seen.add(entry_id)
    return entries


def _detail_label(key: str) -> str:
    labels = {
        "exitCode": "Код завершения",
        "taskDir": "Каталог задачи",
        "missingVars": "Отсутствующие переменные",
        "registryPath": "MCP registry",
        "stderr": "Вывод ошибки",
        "stdout": "Вывод команды",
    }
    return labels.get(key, key)
