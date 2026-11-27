"""Compact, immutable report versions stored inside the task capsule."""
from __future__ import annotations

import re
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .memory import ReportStateError, _empty_entry, read_existing_report_state
from .report_view import build_report_snapshot
from .review_yaml import expand_review_result_inputs

_VERSION = re.compile(r"(\d{8}T\d{12}Z)-[0-9a-f]{32}")


def _history_root(task_dir: Path) -> Path:
    return task_dir / ".history"


def _history_roots(task_dir: Path) -> list[Path]:
    """Include interrupted resets and the previous layout without changing files."""
    holding = sorted(path for path in task_dir.parent.glob(".*-history-*")
                     if re.fullmatch(re.escape(f".{task_dir.name}-history-") + r"[0-9a-f]{32}", path.name))
    return list(dict.fromkeys([_history_root(task_dir), *holding, task_dir.parent / ".history" / task_dir.name]))


def _merge_history(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    _within(source, source)
    _within(destination, destination)
    for path in source.rglob("*"):
        _within(path, source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        source.rename(destination)
        return
    entries = list(source.iterdir())
    # Check all names before moving anything; a collision never overwrites files.
    if any((destination / entry.name).exists() or (destination / entry.name).is_symlink() for entry in entries):
        raise OSError(f"Конфликт версий истории: {source} и {destination}. Оба каталога сохранены.")
    for entry in entries:
        entry.rename(destination / entry.name)
    source.rmdir()


def restore_report_history(task_dir: Path) -> None:
    """Recover interrupted resets and migrate the old layout under task_lock."""
    task_dir = task_dir.resolve()
    destination = _history_root(task_dir)
    for source in _history_roots(task_dir)[1:]:
        try:
            _merge_history(source, destination)
        except Exception as exc:
            raise OSError(f"Не удалось восстановить историю из {source}: {exc}") from exc


@contextmanager
def preserve_report_history(task_dir: Path) -> Iterator[None]:
    """Temporarily displace history while the caller destructively resets a task."""
    task_dir = task_dir.resolve()
    history = _history_root(task_dir)
    _within(history, history)
    holding = task_dir.parent / f".{task_dir.name}-history-{uuid.uuid4().hex}"
    displaced = history.exists()
    if displaced:
        history.rename(holding)
    try:
        yield
    finally:
        if displaced:
            try:
                _merge_history(holding, history)
            except Exception as exc:
                raise OSError(f"История сохранена в {holding}; не удалось вернуть её в {history}: {exc}") from exc


def _archived_at(version_id: str) -> str:
    match = _VERSION.fullmatch(version_id)
    if match is None:
        raise ValueError("Invalid report version")
    return datetime.strptime(match[1], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc).isoformat()


def _within(path: Path, root: Path) -> Path:
    if not path.resolve().is_relative_to(root):
        raise ReportStateError(404, "report_version_not_found", "Версия отчёта недоступна.")
    return path


def archive_report(task_dir: Path, report_name: str) -> str | None:
    """Publish a complete copy before the caller replaces the locked capsule."""
    task_dir = task_dir.resolve()
    preferred = task_dir / report_name
    reports = ([preferred] if preferred.is_file() else
               sorted(path for path in task_dir.glob("review-report-*.md") if path.is_file())[:1])
    if not reports:
        return None
    info = task_dir / "_review_info"
    paths = reports + (expand_review_result_inputs([info]) if info.is_dir() else [])
    paths += [info / name for name in ("report-state.json", "review_source_links.json", "meta.txt", "commit_selection.json")
              if (info / name).exists()]
    root = _history_root(task_dir)
    _within(root, root)
    version_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex
    pending = root / (".pending-" + version_id)
    try:
        pending.mkdir(parents=True)
        for source in paths:
            _within(source, task_dir)
            destination = pending / source.relative_to(task_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        pending.rename(root / version_id)
    except Exception:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    return version_id


def list_report_versions(task_dir: Path) -> list[dict[str, str]]:
    versions: dict[str, dict[str, str]] = {}
    for root in _history_roots(task_dir):
        _within(root, root)
        if not root.is_dir():
            continue
        for path in root.iterdir():
            try:
                archived_at = _archived_at(path.name)
                _within(path, root)
            except (ValueError, ReportStateError):
                continue
            if path.is_dir():
                if path.name in versions:
                    raise OSError(f"Конфликт версии {path.name} в истории задачи {task_dir}.")
                versions[path.name] = {"versionId": path.name, "archivedAt": archived_at}
    return sorted(versions.values(), key=lambda item: (item["archivedAt"], item["versionId"]), reverse=True)


def read_report_version(task_dir: Path, task: str, version_id: str) -> dict[str, Any]:
    try:
        archived_at = _archived_at(version_id)
    except ValueError as exc:
        raise ReportStateError(400, "invalid_report_version", "Недопустимый идентификатор версии.") from exc
    matches = []
    for root in _history_roots(task_dir):
        _within(root, root)
        candidate = _within(root / version_id, root)
        if candidate.is_dir():
            matches.append(candidate)
    if not matches:
        raise ReportStateError(404, "report_version_not_found", "Версия отчёта не найдена.")
    if len(matches) > 1:
        raise OSError(f"Конфликт версии {version_id}: {', '.join(str(path) for path in matches)}")
    version_dir = matches[0]
    # Validate every material the existing projection may read, including sidecars.
    for path in version_dir.rglob("*"):
        _within(path, version_dir)
    reports = sorted(path for path in version_dir.glob("review-report-*.md") if path.is_file())
    if not reports:
        raise ReportStateError(404, "report_version_not_found", "Архивный отчёт не найден.")
    report = reports[0]
    payload: dict[str, Any] = {
        "ok": True, "issueKey": task, "versionId": version_id, "archivedAt": archived_at,
        "readOnly": True, "reportPath": str(report), "content": report.read_text(encoding="utf-8"),
        "view": None, "reportState": None,
    }
    try:
        snapshot = build_report_snapshot(version_dir, task)
        state = read_existing_report_state(version_dir, snapshot)
        report_id = state["reportId"] if state else None
        payload["view"] = {**snapshot.view, "reportId": report_id}
        payload["reportState"] = {
            "ok": True, "reportId": report_id,
            "findings": {item["id"]: (state["findings"].get(item["id"], _empty_entry())
                                      if state else _empty_entry()) for item in snapshot.view["published"]},
        }
    except Exception as exc:
        payload["viewError"] = {"code": getattr(exc, "code", "report_state_unavailable"), "message": str(exc)}
    return payload
