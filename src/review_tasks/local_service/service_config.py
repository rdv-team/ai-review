from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .contracts import ApiError
from .gitlab import GitLabInstance, gitlab_instances_to_json, normalize_gitlab_instances
from .storage import default_service_root, write_json

SERVICE_CONFIG_FILENAME = "service.json"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_CONCURRENT_REVIEWS = 2
DEFAULT_DRAIN_TIMEOUT_SEC = 60
DEFAULT_AUTO_RELOAD = True
DEFAULT_CONFIG_WATCH_INTERVAL_SEC = 60
DEFAULT_CURSOR_MODEL = "composer-2.5-fast"
DEFAULT_OPENCODE_CLI_VERSION = "v2"


class ConfigSource(str, Enum):
    CLI = "cli"
    ENV = "env"
    FILE = "service.json"
    DEFAULT = "default"


@dataclass(frozen=True)
class ResolvedValue:
    value: Any
    source: ConfigSource


@dataclass(frozen=True)
class EffectiveServiceConfig:
    host: str
    port: int
    token: str | None
    codex_bin: str | None
    cursor_bin: str | None
    opencode_bin: str | None
    opencode_cli_version: str
    cursor_model: str | None
    log_file: Path
    max_concurrent_reviews: int
    drain_timeout_sec: int
    auto_reload: bool
    config_watch_interval_sec: int
    gitlab_instances: tuple[GitLabInstance, ...]
    service_root: Path
    config_path: Path
    field_sources: dict[str, ConfigSource]

    def resolved_field(self, name: str) -> ResolvedValue:
        value = getattr(self, name)
        source = self.field_sources[name]
        return ResolvedValue(value=value, source=source)


@dataclass(frozen=True)
class CliServiceOverrides:
    host: str | None = None
    port: int | None = None
    token: str | None = None
    service_root: Path | None = None
    config_path: Path | None = None
    max_concurrent_reviews: int | None = None
    drain_timeout_sec: int | None = None
    log_file: str | None = None


def resolve_service_config_path(
    *,
    config_path: str | Path | None = None,
    service_root: Path | None = None,
) -> Path:
    if config_path is not None:
        return Path(config_path).expanduser().resolve()
    root = (service_root or default_service_root()).resolve()
    return root / SERVICE_CONFIG_FILENAME


def load_service_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ApiError(
            "invalid_service_config",
            f"Invalid JSON in service config: {path}",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        raise ApiError(
            "invalid_service_config",
            f"Service config root must be a JSON object: {path}",
            details={"path": str(path)},
        )
    return normalize_service_config(payload)


def save_service_config(path: Path, config: dict[str, Any]) -> None:
    write_json(path, normalize_service_config(config))


def normalize_service_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in config.items():
        if value is None and key != "opencodeCliVersion":
            continue
        normalized[key] = _normalize_config_field(key, value)
    return normalized


def _normalize_config_field(key: str, value: Any) -> Any:
    if key == "opencodeCliVersion":
        if not isinstance(value, str) or value not in {"v1", "v2"}:
            raise ApiError("invalid_service_config", "opencodeCliVersion must be v1 or v2.")
        return value
    if key == "host":
        if not isinstance(value, str) or not value.strip():
            raise ApiError("invalid_service_config", "host must be a non-empty string.")
        return value.strip()
    if key == "port":
        return _normalize_positive_int(value, field="port", minimum=1, maximum=65535)
    if key in {"token", "codexBin", "cursorBin", "opencodeBin", "cursorModel", "logFile"}:
        if not isinstance(value, str) or not value.strip():
            raise ApiError("invalid_service_config", f"{key} must be a non-empty string.")
        if key == "logFile":
            return str(Path(value).expanduser())
        return value.strip()
    if key == "maxConcurrentReviews":
        return _normalize_positive_int(value, field="maxConcurrentReviews", minimum=1)
    if key == "drainTimeoutSec":
        return _normalize_positive_int(value, field="drainTimeoutSec", minimum=0)
    if key == "autoReload":
        if not isinstance(value, bool):
            raise ApiError("invalid_service_config", "autoReload must be boolean.")
        return value
    if key == "configWatchIntervalSec":
        return _normalize_positive_int(value, field="configWatchIntervalSec", minimum=1)
    if key == "gitlabInstances":
        return gitlab_instances_to_json(normalize_gitlab_instances(value))
    raise ApiError("invalid_service_config", f"Unsupported service config field: {key}.")


def _normalize_positive_int(
    value: Any,
    *,
    field: str,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ApiError("invalid_service_config", f"{field} must be an integer.")
    if value < minimum:
        raise ApiError("invalid_service_config", f"{field} must be >= {minimum}.")
    if maximum is not None and value > maximum:
        raise ApiError("invalid_service_config", f"{field} must be <= {maximum}.")
    return value


def _resolve_string_field(
    *,
    name: str,
    cli_value: str | None,
    env_name: str | None,
    file_value: str | None,
    default: str | None = None,
) -> tuple[str | None, ConfigSource]:
    if cli_value is not None:
        return cli_value, ConfigSource.CLI
    if env_name:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            return env_value, ConfigSource.ENV
    if file_value is not None:
        return file_value, ConfigSource.FILE
    if default is not None:
        return default, ConfigSource.DEFAULT
    return None, ConfigSource.DEFAULT


def _resolve_int_field(
    *,
    cli_value: int | None,
    env_name: str | None,
    file_value: int | None,
    default: int,
) -> tuple[int, ConfigSource]:
    if cli_value is not None:
        return cli_value, ConfigSource.CLI
    if env_name:
        env_raw = os.environ.get(env_name, "").strip()
        if env_raw:
            try:
                return int(env_raw), ConfigSource.ENV
            except ValueError as exc:
                raise ApiError(
                    "invalid_service_config",
                    f"{env_name} must be an integer.",
                ) from exc
    if file_value is not None:
        return file_value, ConfigSource.FILE
    return default, ConfigSource.DEFAULT


def _resolve_bool_field(
    *,
    env_name: str | None,
    file_value: bool | None,
    default: bool,
) -> tuple[bool, ConfigSource]:
    if env_name:
        env_raw = os.environ.get(env_name, "").strip().lower()
        if env_raw:
            if env_raw in {"1", "true", "yes", "on"}:
                return True, ConfigSource.ENV
            if env_raw in {"0", "false", "no", "off"}:
                return False, ConfigSource.ENV
            raise ApiError("invalid_service_config", f"{env_name} must be a boolean.")
    if file_value is not None:
        return file_value, ConfigSource.FILE
    return default, ConfigSource.DEFAULT


def default_log_file(service_root: Path) -> Path:
    return service_root / "logs" / "service.log"


def resolve_effective_service_config(
    *,
    cli: CliServiceOverrides | None = None,
) -> EffectiveServiceConfig:
    cli = cli or CliServiceOverrides()
    service_root = (cli.service_root or default_service_root()).resolve()
    config_path = resolve_service_config_path(
        config_path=cli.config_path,
        service_root=service_root,
    )
    file_config = load_service_config(config_path)

    host, host_source = _resolve_string_field(
        name="host",
        cli_value=cli.host,
        env_name=None,
        file_value=file_config.get("host"),
        default=DEFAULT_HOST,
    )
    port, port_source = _resolve_int_field(
        cli_value=cli.port,
        env_name=None,
        file_value=file_config.get("port"),
        default=DEFAULT_PORT,
    )
    token, token_source = _resolve_string_field(
        name="token",
        cli_value=cli.token,
        env_name="REVIEW_TASKS_SERVICE_TOKEN",
        file_value=file_config.get("token"),
    )
    codex_bin, codex_source = _resolve_string_field(
        name="codexBin",
        cli_value=None,
        env_name="REVIEW_TASKS_CODEX_BIN",
        file_value=file_config.get("codexBin"),
    )
    cursor_bin, cursor_source = _resolve_string_field(
        name="cursorBin",
        cli_value=None,
        env_name="REVIEW_TASKS_CURSOR_BIN",
        file_value=file_config.get("cursorBin"),
    )
    opencode_bin, opencode_source = _resolve_string_field(
        name="opencodeBin",
        cli_value=None,
        env_name="REVIEW_TASKS_OPENCODE_BIN",
        file_value=file_config.get("opencodeBin"),
    )
    cursor_model, cursor_model_source = _resolve_string_field(
        name="cursorModel",
        cli_value=None,
        env_name="REVIEW_TASKS_CURSOR_MODEL",
        file_value=file_config.get("cursorModel"),
        default=DEFAULT_CURSOR_MODEL,
    )
    log_file_raw, log_file_source = _resolve_string_field(
        name="logFile",
        cli_value=cli.log_file,
        env_name="REVIEW_TASKS_LOG_FILE",
        file_value=file_config.get("logFile"),
        default=str(default_log_file(service_root)),
    )
    max_concurrent, max_concurrent_source = _resolve_int_field(
        cli_value=cli.max_concurrent_reviews,
        env_name="REVIEW_TASKS_MAX_CONCURRENT_REVIEWS",
        file_value=file_config.get("maxConcurrentReviews"),
        default=DEFAULT_MAX_CONCURRENT_REVIEWS,
    )
    drain_timeout, drain_timeout_source = _resolve_int_field(
        cli_value=cli.drain_timeout_sec,
        env_name="REVIEW_TASKS_DRAIN_TIMEOUT_SEC",
        file_value=file_config.get("drainTimeoutSec"),
        default=DEFAULT_DRAIN_TIMEOUT_SEC,
    )
    auto_reload, auto_reload_source = _resolve_bool_field(
        env_name="REVIEW_TASKS_AUTO_RELOAD",
        file_value=file_config.get("autoReload"),
        default=DEFAULT_AUTO_RELOAD,
    )
    watch_interval, watch_interval_source = _resolve_int_field(
        cli_value=None,
        env_name="REVIEW_TASKS_CONFIG_WATCH_INTERVAL_SEC",
        file_value=file_config.get("configWatchIntervalSec"),
        default=DEFAULT_CONFIG_WATCH_INTERVAL_SEC,
    )
    gitlab_instances = normalize_gitlab_instances(file_config.get("gitlabInstances"))
    gitlab_instances_source = ConfigSource.FILE if "gitlabInstances" in file_config else ConfigSource.DEFAULT
    opencode_cli_version = file_config.get("opencodeCliVersion", DEFAULT_OPENCODE_CLI_VERSION)
    opencode_cli_version_source = ConfigSource.FILE if "opencodeCliVersion" in file_config else ConfigSource.DEFAULT

    return EffectiveServiceConfig(
        host=str(host),
        port=int(port),
        token=token,
        codex_bin=codex_bin,
        cursor_bin=cursor_bin,
        opencode_bin=opencode_bin,
        opencode_cli_version=opencode_cli_version,
        cursor_model=cursor_model,
        log_file=Path(str(log_file_raw)).expanduser().resolve(),
        max_concurrent_reviews=int(max_concurrent),
        drain_timeout_sec=int(drain_timeout),
        auto_reload=bool(auto_reload),
        config_watch_interval_sec=int(watch_interval),
        gitlab_instances=gitlab_instances,
        service_root=service_root,
        config_path=config_path,
        field_sources={
            "host": host_source,
            "port": port_source,
            "token": token_source,
            "codex_bin": codex_source,
            "cursor_bin": cursor_source,
            "opencode_bin": opencode_source,
            "opencode_cli_version": opencode_cli_version_source,
            "cursor_model": cursor_model_source,
            "log_file": log_file_source,
            "max_concurrent_reviews": max_concurrent_source,
            "drain_timeout_sec": drain_timeout_source,
            "auto_reload": auto_reload_source,
            "config_watch_interval_sec": watch_interval_source,
            "gitlab_instances": gitlab_instances_source,
        },
    )


_SERVICE_MANAGED_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("REVIEW_TASKS_CODEX_BIN", "codex_bin"),
    ("REVIEW_TASKS_CURSOR_BIN", "cursor_bin"),
    ("REVIEW_TASKS_OPENCODE_BIN", "opencode_bin"),
    ("REVIEW_TASKS_CURSOR_MODEL", "cursor_model"),
)

_service_managed_env: dict[str, str] = {}


def apply_effective_config_env(config: EffectiveServiceConfig) -> None:
    for env_name, attr in _SERVICE_MANAGED_ENV_VARS:
        value = getattr(config, attr)
        if not value:
            _service_managed_env.pop(env_name, None)
            continue
        text = str(value)
        os.environ[env_name] = text
        _service_managed_env[env_name] = text


def clear_service_managed_env_for_resolve() -> None:
    """Drop env values this process injected so ``service.json`` can win on reload."""
    for env_name in _service_managed_env:
        if os.environ.get(env_name) == _service_managed_env.get(env_name):
            os.environ.pop(env_name, None)


def cli_flag_provided(argv: list[str], flag: str) -> bool:
    return flag in argv or any(arg.startswith(f"{flag}=") for arg in argv)


def cli_overrides_from_args(argv: list[str], args: Any) -> CliServiceOverrides:
    return CliServiceOverrides(
        host=args.host if cli_flag_provided(argv, "--host") else None,
        port=args.port if cli_flag_provided(argv, "--port") else None,
        token=args.token if cli_flag_provided(argv, "--token") else None,
        service_root=Path(args.service_root).expanduser().resolve() if args.service_root else None,
        config_path=Path(args.config).expanduser().resolve() if args.config else None,
        max_concurrent_reviews=args.max_concurrent_reviews
        if cli_flag_provided(argv, "--max-concurrent-reviews")
        else None,
        drain_timeout_sec=args.drain_timeout_sec
        if cli_flag_provided(argv, "--drain-timeout-sec")
        else None,
        log_file=args.log_file if cli_flag_provided(argv, "--log-file") else None,
    )
