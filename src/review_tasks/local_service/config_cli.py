from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .contracts import ApiError
from .service_config import (
    ConfigSource,
    EffectiveServiceConfig,
    cli_flag_provided,
    cli_overrides_from_args,
    load_service_config,
    resolve_effective_service_config,
    resolve_service_config_path,
    save_service_config,
)

_CONFIG_WRITE_FLAGS: tuple[tuple[str, str, str], ...] = (
    ("--host", "host", "host"),
    ("--port", "port", "port"),
    ("--token", "token", "token"),
    ("--codex-bin", "codex_bin", "codexBin"),
    ("--cursor-bin", "cursor_bin", "cursorBin"),
    ("--opencode-bin", "opencode_bin", "opencodeBin"),
    ("--opencode-cli-version", "opencode_cli_version", "opencodeCliVersion"),
    ("--cursor-model", "cursor_model", "cursorModel"),
    ("--log-file", "log_file", "logFile"),
    ("--max-concurrent-reviews", "max_concurrent_reviews", "maxConcurrentReviews"),
    ("--drain-timeout-sec", "drain_timeout_sec", "drainTimeoutSec"),
    ("--config-watch-interval-sec", "config_watch_interval_sec", "configWatchIntervalSec"),
)

_SHOW_FIELDS: tuple[str, ...] = (
    "host",
    "port",
    "token",
    "codex_bin",
    "cursor_bin",
    "opencode_bin",
    "opencode_cli_version",
    "cursor_model",
    "log_file",
    "max_concurrent_reviews",
    "drain_timeout_sec",
    "auto_reload",
    "config_watch_interval_sec",
    "gitlab_instances",
)


def build_config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-tasks local-service config",
        description="Записать или показать operational config (service.json) без запуска сервиса.",
        allow_abbrev=False,
    )
    parser.add_argument("--config", default=None, help="Path to service.json operational config.")
    parser.add_argument("--service-root", default=None, help="Directory for service job index and logs.")
    parser.add_argument("--host", default=None, help="Loopback host.")
    parser.add_argument("--port", type=int, default=None, help="Port.")
    parser.add_argument("--token", default=None, help="Trusted extension token.")
    parser.add_argument("--codex-bin", default=None, help="Path to Codex CLI binary.")
    parser.add_argument("--cursor-bin", default=None, help="Path to Cursor Agent CLI binary.")
    parser.add_argument("--opencode-bin", default=None, help="Path to OpenCode CLI binary.")
    parser.add_argument(
        "--opencode-cli-version", choices=("v1", "v2"), default=None,
        help="OpenCode CLI generation (default: v2); requires service restart.",
    )
    parser.add_argument("--cursor-model", default=None, help="Cursor Agent model name.")
    parser.add_argument("--log-file", default=None, help="Path to plain-text service event log file.")
    parser.add_argument("--max-concurrent-reviews", type=int, default=None, help="Max concurrent review jobs.")
    parser.add_argument(
        "--drain-timeout-sec",
        type=int,
        default=None,
        help="Graceful shutdown drain timeout in seconds.",
    )
    parser.add_argument(
        "--auto-reload",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Auto-reload service.json when it changes on disk.",
    )
    parser.add_argument(
        "--config-watch-interval-sec",
        type=int,
        default=None,
        help="Interval in seconds for service.json watch polling.",
    )
    parser.add_argument(
        "--gitlab-instance",
        nargs=3,
        action="append",
        metavar=("BASE_URL", "API_BASE_URL", "TOKEN_ENV"),
        default=None,
        help="Add GitLab MR discovery instance; can be repeated.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Print effective config and field sources; do not write service.json.",
    )
    return parser


def _auto_reload_provided(argv: list[str]) -> bool:
    return cli_flag_provided(argv, "--auto-reload") or cli_flag_provided(argv, "--no-auto-reload")


def _resolve_config_path(args: argparse.Namespace) -> Path:
    service_root = Path(args.service_root).expanduser().resolve() if args.service_root else None
    config_path = Path(args.config).expanduser().resolve() if args.config else None
    return resolve_service_config_path(config_path=config_path, service_root=service_root)


def _provided_config_updates(argv: list[str], args: argparse.Namespace) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for flag, attr, json_key in _CONFIG_WRITE_FLAGS:
        if cli_flag_provided(argv, flag):
            updates[json_key] = getattr(args, attr)
    if _auto_reload_provided(argv):
        updates["autoReload"] = args.auto_reload
    if args.gitlab_instance is not None:
        updates["gitlabInstances"] = [
            {"baseUrl": base_url, "apiBaseUrl": api_base_url, "tokenEnv": token_env}
            for base_url, api_base_url, token_env in args.gitlab_instance
        ]
    return updates


def _format_config_value(field: str, value: Any) -> str:
    if field == "token" and value:
        return "***"
    if isinstance(value, Path):
        return str(value)
    if value is None:
        return "(not set)"
    if field == "gitlab_instances":
        if isinstance(value, tuple):
            value = [item.to_json() for item in value]
        return json_dumps(value)
    return str(value)


def _print_config_entry(field: str, value: Any, source: ConfigSource | None = None) -> None:
    rendered = _format_config_value(field, value)
    if source is None:
        print(f"{field}: {rendered}", flush=True)
        return
    print(f"{field}: {rendered} ({source.value})", flush=True)


def _print_effective_config(config: EffectiveServiceConfig) -> None:
    print(f"config_path: {config.config_path}", flush=True)
    print(f"service_root: {config.service_root}", flush=True)
    for field in _SHOW_FIELDS:
        _print_config_entry(field, getattr(config, field), config.field_sources[field])


def _print_saved_config(path: Path, config: dict[str, Any]) -> None:
    print(str(path.resolve()), flush=True)
    for json_key, value in sorted(config.items()):
        field = _json_key_to_field(json_key)
        _print_config_entry(field or json_key, value)


def _json_key_to_field(json_key: str) -> str | None:
    for _flag, attr, key in _CONFIG_WRITE_FLAGS:
        if key == json_key:
            return attr
    if json_key == "autoReload":
        return "auto_reload"
    if json_key == "gitlabInstances":
        return "gitlab_instances"
    return None


def run_local_service_config(argv: list[str]) -> int:
    parser = build_config_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        if args.show:
            effective = resolve_effective_service_config(cli=cli_overrides_from_args(argv, args))
            _print_effective_config(effective)
            return 0
        return _write_service_config(argv, args)
    except ApiError as exc:
        print(f"local-service config error: {exc}", flush=True)
        return 1


def _write_service_config(argv: list[str], args: argparse.Namespace) -> int:
    path = _resolve_config_path(args)
    existing = load_service_config(path)
    provided = _provided_config_updates(argv, args)
    if not provided:
        print("local-service config error: no config fields provided; use flags or --show.", flush=True)
        return 1
    merged = {**existing, **provided}
    save_service_config(path, merged)
    _print_saved_config(path, merged)
    return 0


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
