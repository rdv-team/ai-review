from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .contracts import ApiError
from .logging_setup import log_config_reload
from .service_config import (
    ConfigSource,
    EffectiveServiceConfig,
    apply_effective_config_env,
    clear_service_managed_env_for_resolve,
    load_service_config,
    resolve_effective_service_config,
)

if TYPE_CHECKING:
    from .service import LocalReviewService

_HOT_FIELDS = (
    "codex_bin",
    "cursor_bin",
    "opencode_bin",
    "cursor_model",
    "max_concurrent_reviews",
    "drain_timeout_sec",
    "auto_reload",
    "config_watch_interval_sec",
    "gitlab_instances",
)

_RESTART_FIELDS = ("host", "port", "token", "log_file", "opencode_cli_version")


_FILE_JSON_KEYS: tuple[tuple[str, str], ...] = (
    ("host", "host"),
    ("port", "port"),
    ("token", "token"),
    ("codexBin", "codex_bin"),
    ("cursorBin", "cursor_bin"),
    ("opencodeBin", "opencode_bin"),
    ("opencodeCliVersion", "opencode_cli_version"),
    ("cursorModel", "cursor_model"),
    ("logFile", "log_file"),
    ("maxConcurrentReviews", "max_concurrent_reviews"),
    ("drainTimeoutSec", "drain_timeout_sec"),
    ("autoReload", "auto_reload"),
    ("configWatchIntervalSec", "config_watch_interval_sec"),
    ("gitlabInstances", "gitlab_instances"),
)


def reload_operational_config(service: LocalReviewService, *, trigger: str) -> dict[str, Any]:
    """Re-resolve config and apply hot fields without restarting the HTTP listener."""
    previous = service.effective_config
    if previous is None:
        raise ApiError("config_unavailable", "Operational config is not initialized.")
    config_path = previous.config_path
    previous_file = service._last_file_config
    if previous_file is None:
        previous_file = load_service_config(config_path)
    new_file = load_service_config(config_path)
    file_changes = _collect_file_json_changes(previous_file, new_file)

    clear_service_managed_env_for_resolve()
    try:
        effective = resolve_effective_service_config(cli=service.cli_overrides)
    except ApiError as exc:
        raise exc

    changes = _collect_config_changes(previous, effective)
    not_applied = _collect_not_applied_file_changes(previous_file, new_file, previous, effective)
    notices = _restart_required_notices(
        previous, effective, active_opencode_cli_version=service.active_opencode_cli_version,
    )
    apply_effective_config_env(effective)
    service._last_file_config = dict(new_file)

    service.max_concurrent_reviews = max(1, effective.max_concurrent_reviews)
    service.drain_timeout_sec = max(0, effective.drain_timeout_sec)
    service.orchestrator.gitlab_instances = effective.gitlab_instances
    if service._worker_pool is not None:
        service._worker_pool.resize(service.max_concurrent_reviews)

    service.effective_config = effective
    if service._config_watcher is not None:
        service._config_watcher.update_settings(
            effective.auto_reload,
            effective.config_watch_interval_sec,
            effective.config_path,
        )

    applied = {
        field: {
            "value": _format_applied_value(field, getattr(effective, field)),
            "source": effective.field_sources[field].value,
        }
        for field in _HOT_FIELDS
    }
    _log_config_reload_event(
        trigger=trigger,
        changes=changes,
        file_changes=file_changes,
        not_applied=not_applied,
        notices=notices,
    )

    return {
        "ok": True,
        "trigger": trigger,
        "applied": applied,
        "notices": notices,
        "fileChanges": file_changes,
        "notApplied": not_applied,
    }


def _restart_required_notices(
    previous: EffectiveServiceConfig,
    effective: EffectiveServiceConfig,
    *,
    active_opencode_cli_version: str,
) -> list[str]:
    notices: list[str] = []
    for field in _RESTART_FIELDS:
        old_value = active_opencode_cli_version if field == "opencode_cli_version" else getattr(previous, field)
        new_value = getattr(effective, field)
        source = effective.field_sources[field]
        if source == ConfigSource.CLI:
            continue
        if new_value != old_value:
            notices.append(f"{field} requires service restart to take effect")
    return notices


_CONFIG_TRACKED_FIELDS = _HOT_FIELDS + _RESTART_FIELDS


def _format_config_value(field: str, value: Any) -> str:
    if field == "token":
        return "***" if value else "(not set)"
    if value is None:
        return "(not set)"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if field == "gitlab_instances":
        return str([item.to_json() for item in value])
    return str(value)


def _format_json_config_value(json_key: str, value: Any) -> str:
    if json_key == "token":
        return "***" if value else "(not set)"
    if value is None:
        return "(not set)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if json_key == "gitlabInstances" and isinstance(value, list):
        return str(value)
    return str(value)


def _collect_file_json_changes(previous_file: dict[str, Any], new_file: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    for json_key in sorted(set(previous_file) | set(new_file)):
        old_value = previous_file.get(json_key)
        new_value = new_file.get(json_key)
        if old_value == new_value:
            continue
        changes.append(
            f"{json_key}={_format_json_config_value(json_key, old_value)}"
            f"->{_format_json_config_value(json_key, new_value)}",
        )
    return changes


def _collect_not_applied_file_changes(
    previous_file: dict[str, Any],
    new_file: dict[str, Any],
    previous: EffectiveServiceConfig,
    effective: EffectiveServiceConfig,
) -> list[str]:
    notes: list[str] = []
    for json_key, field in _FILE_JSON_KEYS:
        old_file_value = previous_file.get(json_key)
        new_file_value = new_file.get(json_key)
        if old_file_value == new_file_value:
            continue
        old_effective = getattr(previous, field)
        new_effective = getattr(effective, field)
        if old_effective == new_effective:
            source = effective.field_sources[field].value
            notes.append(
                f"{field} service.json {_format_json_config_value(json_key, old_file_value)}"
                f"->{_format_json_config_value(json_key, new_file_value)}"
                f" but effective unchanged (source={source})",
            )
    return notes


def _collect_config_changes(
    previous: EffectiveServiceConfig,
    effective: EffectiveServiceConfig,
) -> list[str]:
    changes: list[str] = []
    for field in _CONFIG_TRACKED_FIELDS:
        old_value = getattr(previous, field)
        new_value = getattr(effective, field)
        if old_value == new_value:
            continue
        changes.append(
            f"{field}={_format_config_value(field, old_value)}->{_format_config_value(field, new_value)}",
        )
    return changes


def _log_config_reload_event(
    *,
    trigger: str,
    changes: list[str],
    file_changes: list[str],
    not_applied: list[str],
    notices: list[str],
) -> None:
    parts = [f"trigger={trigger}"]
    if changes:
        parts.append(f"changes={'; '.join(changes)}")
    if file_changes:
        parts.append(f"file_changes={'; '.join(file_changes)}")
    if not_applied:
        parts.append(f"not_applied={'; '.join(not_applied)}")
    if notices:
        parts.append(f"notices={'; '.join(notices)}")
    if len(parts) == 1:
        parts.append("unchanged")
    log_config_reload(" ".join(parts))


def _format_applied_value(field: str, value: Any) -> Any:
    if field == "token" and value:
        return "***"
    if isinstance(value, Path):
        return str(value)
    if field == "gitlab_instances":
        return [item.to_json() for item in value]
    return value


class ConfigWatcher:
    """Poll ``service.json`` mtime and trigger hot reload when it changes."""

    def __init__(
        self,
        service: LocalReviewService,
        *,
        config_path: Path,
        interval_sec: int,
        enabled: bool,
    ) -> None:
        self._service = service
        self._config_path = config_path
        self._interval_sec = max(1, interval_sec)
        self._enabled = enabled
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_mtime: float | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._last_mtime = _read_mtime(self._config_path)
            self._thread = threading.Thread(target=self._watch_loop, name="config-watcher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)

    def update_settings(self, enabled: bool, interval_sec: int, config_path: Path) -> None:
        with self._lock:
            self._enabled = enabled
            self._interval_sec = max(1, interval_sec)
            self._config_path = config_path
            self._last_mtime = _read_mtime(self._config_path)

    def _watch_loop(self) -> None:
        while not self._stop_event.wait(self._interval_sec):
            with self._lock:
                if not self._enabled:
                    continue
                config_path = self._config_path
                last_mtime = self._last_mtime
            mtime = _read_mtime(config_path)
            if mtime is None or last_mtime == mtime:
                continue
            time.sleep(0.5)
            if self._stop_event.is_set():
                return
            mtime_after = _read_mtime(config_path)
            if mtime_after is None or mtime_after != mtime:
                continue
            with self._lock:
                self._last_mtime = mtime_after
            try:
                self._service.reload_config("auto-watch")
            except ApiError:
                pass


def _read_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None
