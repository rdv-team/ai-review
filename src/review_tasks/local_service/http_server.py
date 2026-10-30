from __future__ import annotations

import argparse
import json
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .contracts import ApiError, is_loopback_address, structured_error
from .logging_setup import setup_service_logging
from .service import LocalReviewService
from .service_config import (
    apply_effective_config_env,
    load_service_config,
    cli_overrides_from_args,
    resolve_effective_service_config,
)


class LocalReviewHttpServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], service: LocalReviewService) -> None:
        self.service = service
        super().__init__(server_address, LocalReviewHandler)


class LocalReviewHandler(BaseHTTPRequestHandler):
    server: LocalReviewHttpServer

    def do_OPTIONS(self) -> None:
        self._send(204, {})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _dispatch(self, method: str) -> None:
        if not self._trusted_request(method):
            self._send(403, structured_error("forbidden", "Request is not from a trusted loopback client."))
            return
        try:
            status, body = self._route(method)
        except json.JSONDecodeError as exc:
            status, body = 400, structured_error("invalid_json", str(exc))
        self._send(status, body)

    def _route(self, method: str) -> tuple[int, dict[str, Any]]:
        parsed = urlparse(self.path)
        parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
        service = self.server.service
        if method == "GET" and parts == ["health"]:
            return service.health()
        if method == "GET" and parts == ["ready"]:
            return service.ready()
        if method == "POST" and parts == ["admin", "reload"]:
            return service.admin_reload()
        if len(parts) == 2 and parts[0] == "prefix-config":
            if method == "GET":
                return service.get_prefix_config(parts[1])
            if method == "PUT":
                return service.put_prefix_config(parts[1], self._json_body())
        if method == "POST" and parts == ["reviews"]:
            return service.create_review(self._json_body())
        if method == "GET" and len(parts) == 3 and parts[:2] == ["reviews", "by-issue"]:
            return service.get_review_by_issue(parts[2])
        if method == "GET" and len(parts) == 4 and parts[:3] == ["gitlab", "merge-requests", "by-issue"]:
            return service.discover_merge_requests_by_issue(parts[3])
        if len(parts) == 3 and parts[0] == "reviews":
            job_id, action = parts[1], parts[2]
            if method == "GET" and action == "status":
                return service.get_status(job_id)
            if method == "GET" and action == "summary":
                return service.get_summary(job_id)
            if method == "GET" and action == "report":
                return service.get_report(job_id)
            if method == "GET" and action == "reasoning":
                return service.get_reasoning(job_id)
            if method == "GET" and action == "diagnostics":
                return service.diagnostics(job_id)
            if method == "POST" and action == "cancel":
                return service.cancel(job_id)
            if method == "POST" and action == "open-agent-log":
                return service.open_agent_log(job_id)
            if method == "POST" and action == "open-review-bsl":
                return service.open_review_bsl(job_id)
            if method == "POST" and action == "open-changes":
                return service.open_changes(job_id)
            if method == "POST" and action == "open-log":
                return service.open_job_log(job_id, self._json_body())
        return 404, structured_error("not_found", "Endpoint not found.")

    def _json_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return self.server.service.decode_json_body(raw)

    def _trusted_request(self, method: str) -> bool:
        peer = self.client_address[0] if self.client_address else ""
        if not is_loopback_address(peer):
            return False
        host = self.headers.get("Host", "")
        if host and not is_loopback_address(host):
            return False
        if method == "GET" and urlparse(self.path).path in {"/health", "/ready"}:
            return True
        token = self.server.service.token
        if token and self.headers.get("X-Review-Tasks-Token") != token:
            return False
        origin = self.headers.get("Origin")
        if origin and not (
            origin.startswith("chrome-extension://")
            or origin.startswith("http://127.0.0.1")
            or origin.startswith("http://localhost")
        ):
            return False
        return True

    def _send(self, status: int, body: dict[str, Any]) -> None:
        payload = b"" if status == 204 else json.dumps(body, ensure_ascii=False).encode("utf-8")
        origin = self.headers.get("Origin")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if origin and origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Review-Tasks-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        self.end_headers()
        if payload:
            self.wfile.write(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-tasks local-service",
        description="Запустить loopback HTTP service для JIRA local review extension.",
        allow_abbrev=False,
    )
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host; default: 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8765, help="Port; default: 8765.")
    parser.add_argument("--token", default=None, help="Trusted extension token. Also supports REVIEW_TASKS_SERVICE_TOKEN.")
    parser.add_argument("--service-root", default=None, help="Directory for service job index and logs.")
    parser.add_argument("--config", default=None, help="Path to service.json operational config.")
    parser.add_argument("--max-concurrent-reviews", type=int, default=None, help="Max concurrent review jobs.")
    parser.add_argument("--drain-timeout-sec", type=int, default=None, help="Graceful shutdown drain timeout in seconds.")
    parser.add_argument("--log-file", default=None, help="Path to plain-text service event log file.")
    return parser


def _register_reload_signal(service: LocalReviewService) -> None:
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:
        return

    def _handle_reload_signal(_signum: int, _frame: object) -> None:
        try:
            service.reload_config("SIGHUP")
        except ApiError as exc:
            print(f"local-service config reload error: {exc}", flush=True)

    try:
        signal.signal(sighup, _handle_reload_signal)
    except (OSError, ValueError):
        pass


def _register_shutdown_signals(service: LocalReviewService, server: LocalReviewHttpServer) -> None:
    shutdown_lock = threading.Lock()
    shutdown_started = False

    def _handle_shutdown_signal(signum: int, _frame: object) -> None:
        nonlocal shutdown_started
        with shutdown_lock:
            if shutdown_started:
                return
            shutdown_started = True
        service.initiate_shutdown()

        def _shutdown_sequence() -> None:
            service.drain_and_stop_workers()
            server.shutdown()

        threading.Thread(target=_shutdown_sequence, name="service-shutdown", daemon=True).start()

    for signum in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        if signum is None:
            continue
        try:
            signal.signal(signum, _handle_shutdown_signal)
        except (OSError, ValueError):
            continue


def run_local_service(argv: list[str]) -> int:
    if argv and argv[0] == "config":
        from .config_cli import run_local_service_config

        return run_local_service_config(argv[1:])
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        cli_overrides = cli_overrides_from_args(argv, args)
        effective = resolve_effective_service_config(cli=cli_overrides)
    except ApiError as exc:
        print(f"local-service config error: {exc}", flush=True)
        return 1
    if not is_loopback_address(effective.host):
        print("local-service must bind to loopback only.", flush=True)
        return 1
    apply_effective_config_env(effective)
    setup_service_logging(effective.log_file)
    service = LocalReviewService(
        service_root=effective.service_root,
        token=effective.token,
        max_concurrent_reviews=effective.max_concurrent_reviews,
        drain_timeout_sec=effective.drain_timeout_sec,
        cli_overrides=cli_overrides,
        effective_config=effective,
    )
    service._last_file_config = load_service_config(effective.config_path)
    service.startup_reconcile()
    service.start_config_watcher()
    server = LocalReviewHttpServer((effective.host, effective.port), service)
    _register_reload_signal(service)
    _register_shutdown_signals(service, server)
    print(f"review-tasks local-service listening on http://{effective.host}:{effective.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        service.initiate_shutdown()
        service.drain_and_stop_workers()
        server.shutdown()
        return 130
    finally:
        server.server_close()
    return 0
