"""Metadata parsing and completion updates for a review task capsule."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .inspection import META_FILE, TaskCapsuleInspection, inspect_task_capsule


META_LINE_RE = re.compile(r"^\s*([^:=]+)\s*[:=]\s*(.*?)\s*$")
INTEGER_RE = re.compile(r"^[+-]?\d+$")
STATUS_RE = re.compile(r"^\s*status\s*[:=]", re.IGNORECASE)
RUN_STATUS_RE = re.compile(r"^\s*last_review_run_status\s*[:=]", re.IGNORECASE)

RunStatus = Literal["review_completed", "repeat_review_skipped"]
RUN_STATUSES: tuple[RunStatus, ...] = ("review_completed", "repeat_review_skipped")


class TaskMetadataError(Exception):
    """Base error for task metadata operations."""


class MetadataNotUtf8Error(TaskMetadataError):
    """Metadata is not valid UTF-8 text."""


class MetadataReadError(TaskMetadataError):
    """Metadata could not be read."""


class MetadataWriteError(TaskMetadataError):
    """Metadata could not be written."""


class InvalidPromptLayoutError(TaskMetadataError):
    """The task capsule does not use the canonical staged prompt layout."""


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    stripped = value.strip()
    if not INTEGER_RE.fullmatch(stripped):
        return None
    return int(stripped)


def _meta_path(task_root: Path) -> Path:
    return task_root / META_FILE


def _read_metadata_text(meta_path: Path) -> str:
    try:
        return meta_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MetadataNotUtf8Error from exc
    except OSError as exc:
        raise MetadataReadError from exc


def _read_meta_values(meta_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read_metadata_text(meta_path).splitlines():
        match = META_LINE_RE.match(line)
        if match is None:
            continue
        key = match.group(1).strip().lower()
        if key:
            values[key] = match.group(2).strip()
    return values


def read_meta_summary(
    task_root: Path,
    *,
    inspection: TaskCapsuleInspection | None = None,
) -> dict[str, object]:
    """Return the compact metadata fields used to start a review."""
    meta_path = _meta_path(task_root)
    values = _read_meta_values(meta_path)
    current = inspection if inspection is not None else inspect_task_capsule(task_root)
    if not current.has_canonical_staged_layout:
        raise InvalidPromptLayoutError
    return {
        "task": values.get("task") or task_root.name,
        "status": values.get("status") or "pending",
        "priority": _parse_int(values.get("priority")),
    }


def is_mcp_enabled(task_root: Path) -> bool:
    """Legacy capsules keep MCP enabled; init records an explicit off choice."""
    values = _read_meta_values(_meta_path(task_root))
    return values.get("mcp_mode") != "off"


def read_input_truncated(task_root: Path) -> bool | None:
    """Read the coverage flag; legacy or invalid values mean unknown coverage."""
    try:
        value = _read_meta_values(_meta_path(task_root)).get("input_truncated")
    except TaskMetadataError:
        return None
    return {"true": True, "false": False}.get(value)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_completed(task_root: Path, *, run_status: RunStatus = "review_completed") -> None:
    """Normalize and append completion metadata for one review run."""
    if run_status not in RUN_STATUSES:
        raise ValueError(f"Unsupported run status: {run_status}")

    meta_path = _meta_path(task_root)
    text = _read_metadata_text(meta_path)
    output: list[str] = []
    status_written = False
    run_status_written = False
    for line in text.splitlines():
        if STATUS_RE.match(line) is not None:
            if not status_written:
                output.append("status: completed")
                status_written = True
            continue
        if RUN_STATUS_RE.match(line) is not None:
            if not run_status_written:
                output.append(f"last_review_run_status: {run_status}")
                run_status_written = True
            continue
        output.append(line)
    if not status_written:
        output.append("status: completed")
    if not run_status_written:
        output.append(f"last_review_run_status: {run_status}")
    output.append(f"review_run_history: {_utc_timestamp()} {run_status}")

    try:
        meta_path.write_bytes(("\n".join(output) + "\n").encode("utf-8"))
    except OSError as exc:
        raise MetadataWriteError from exc
