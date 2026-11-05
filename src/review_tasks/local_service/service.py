from __future__ import annotations

import json
import os
import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from review_tasks.review_init import prefix_config
from review_tasks.review_init.comparison import (
    COMPARISON_LAUNCHER, ComparisonLaunchError, launch_comparison,
)
from review_tasks.review_result.review_yaml import expand_review_result_inputs, read_review_yaml_documents
from review_tasks.review_result import FeedbackError, ReviewReportFeature

from .contracts import (
    ApiError,
    DETAIL_TEXT,
    DONE_DETAIL_STATUSES,
    api_error_response,
    normalize_issue_key,
    normalize_prefix_payload,
    is_active_status,
    normalize_review_request,
    now_iso,
    package_version,
    prefix_entry_from_api,
    prefix_entry_to_api,
    status_to_popup,
    structured_error,
)
from .config_reload import ConfigWatcher, reload_operational_config
from .gitlab import (
    GitLabApiClient,
    GitLabProjectKey,
    list_git_remotes,
    parse_gitlab_mr_url as parse_gitlab_mr_url_ref,
    resolve_git_remote,
    sanitize_git_remote_url,
    title_contains_issue_key,
    token_lookup,
)
from .logging_setup import log_error
from .job_logs import JOB_LOG_SOURCES, build_log_diagnostics, job_log_path
from .orchestrator import CommandBackend, JobOrchestrator
from .service_config import (
    SERVICE_CONFIG_FILENAME,
    CliServiceOverrides,
    EffectiveServiceConfig,
    resolve_effective_service_config,
)
from .orchestrator import extract_cursor_token_usage, extract_opencode_token_usage, extract_token_usage
from .process_liveness import check_review_process_liveness
from .scheduler import ReviewWorkerPool
from .storage import JobStore, read_json, safe_tail
from .text_file_opener import open_text_file

_AGENT_ACTIVE_DETAIL_STATUSES = frozenset({"review_starting", "review_running"})
_AGENT_STDERR_BY_IDE = {
    "codex": "codex.stderr.log",
    "cursor": "cursor.stderr.log",
    "opencode": "opencode.stderr.log",
}
_CANCELLED_REPEAT_DETAIL_TEXT = "Повторный запуск отменен, показан последний завершенный отчет"


class LocalReviewService:
    def __init__(
        self,
        *,
        service_root: Path | None = None,
        token: str | None = None,
        backend: CommandBackend | None = None,
        start_worker: bool = True,
        poll_interval: float = 2.0,
        max_concurrent_reviews: int = 2,
        drain_timeout_sec: int = 60,
        cli_overrides: CliServiceOverrides | None = None,
        effective_config: EffectiveServiceConfig | None = None,
        gitlab_client: GitLabApiClient | None = None,
        file_opener: Callable[[Path], None] | None = None,
        changes_runner: Callable[[Path], None] | None = None,
        feedback_root: Path | None = None,
    ) -> None:
        if effective_config is None and service_root is not None:
            candidate_config = Path(service_root).expanduser().resolve() / SERVICE_CONFIG_FILENAME
            if candidate_config.exists():
                config_overrides = CliServiceOverrides(
                    service_root=Path(service_root).expanduser().resolve(),
                    config_path=candidate_config,
                )
                effective_config = resolve_effective_service_config(cli=config_overrides)
                cli_overrides = cli_overrides or config_overrides
        self.store = JobStore(service_root)
        self.report_feature = ReviewReportFeature(feedback_root)
        self.cli_overrides = cli_overrides or CliServiceOverrides()
        self.effective_config = effective_config
        self.token = token if token is not None else os.environ.get("REVIEW_TASKS_SERVICE_TOKEN")
        self.start_worker = start_worker
        self.max_concurrent_reviews = max(1, max_concurrent_reviews)
        self.drain_timeout_sec = max(0, drain_timeout_sec)
        self.gitlab_client = gitlab_client or GitLabApiClient()
        self.file_opener = file_opener or open_text_file
        self.changes_runner = changes_runner or launch_comparison
        gitlab_instances = effective_config.gitlab_instances if effective_config is not None else ()
        self.orchestrator = JobOrchestrator(
            self.store,
            backend=backend,
            poll_interval=poll_interval,
            gitlab_instances=gitlab_instances,
        )
        self._state_lock = threading.RLock()
        self._reconcile_done = False
        self._shutting_down = False
        self._worker_pool: ReviewWorkerPool | None = None
        self._config_watcher: ConfigWatcher | None = None
        self._last_file_config: dict[str, Any] | None = None
        if start_worker:
            self._worker_pool = ReviewWorkerPool(
                self.orchestrator,
                self.max_concurrent_reviews,
                on_job_skipped=self._on_job_skipped,
                should_run=self._should_run_job,
            )
            self._worker_pool.start()

    def is_reconcile_done(self) -> bool:
        with self._state_lock:
            return self._reconcile_done

    def is_shutting_down(self) -> bool:
        with self._state_lock:
            return self._shutting_down

    def startup_reconcile(self) -> None:
        try:
            self._startup_reconcile()
        finally:
            with self._state_lock:
                self._reconcile_done = True

    def initiate_shutdown(self) -> None:
        with self._state_lock:
            self._shutting_down = True
        if self._worker_pool is not None:
            self._worker_pool.stop_accepting()

    def drain_and_stop_workers(self) -> None:
        if self._config_watcher is not None:
            self._config_watcher.stop()
        self._drain_in_flight_jobs(self.drain_timeout_sec)
        if self._worker_pool is not None:
            self._worker_pool.stop(wait_timeout_sec=min(5.0, float(self.drain_timeout_sec) + 1.0))

    def start_config_watcher(self) -> None:
        if self.effective_config is None:
            return
        self._config_watcher = ConfigWatcher(
            self,
            config_path=self.effective_config.config_path,
            interval_sec=self.effective_config.config_watch_interval_sec,
            enabled=self.effective_config.auto_reload,
        )
        self._config_watcher.start()

    def reload_config(self, trigger: str) -> dict[str, Any]:
        if self.effective_config is None:
            raise ApiError("config_unavailable", "Operational config is not initialized.")
        return reload_operational_config(self, trigger=trigger)

    def admin_reload(self) -> tuple[int, dict[str, Any]]:
        try:
            payload = self.reload_config("admin-reload")
        except ApiError as exc:
            return exc.status, api_error_response(exc)
        return 200, payload

    def health(self) -> tuple[int, dict[str, Any]]:
        return 200, {
            "ok": True,
            "service": "review-tasks-local-review",
            "version": package_version(),
            "available": True,
            "tokenRequired": bool(self.token),
            "shuttingDown": self.is_shutting_down(),
        }

    def ready(self) -> tuple[int, dict[str, Any]]:
        if not self.is_reconcile_done() or self.is_shutting_down():
            return 503, {
                "ready": False,
                "reconcileDone": self.is_reconcile_done(),
                "shuttingDown": self.is_shutting_down(),
            }
        return 200, {"ready": True}

    def get_prefix_config(self, prefix: str) -> tuple[int, dict[str, Any]]:
        normalized = prefix_config.extract_prefix(prefix)
        config = prefix_config.load_prefix_config(prefix_config.get_config_path())
        entry = config.get(normalized)
        if not isinstance(entry, dict):
            return 404, structured_error(
                "config_missing",
                f"Prefix config is missing for {normalized}.",
                details={"prefix": normalized},
            )
        return 200, {"ok": True, "config": prefix_entry_to_api(normalized, entry)}

    def put_prefix_config(self, prefix: str, payload: Any) -> tuple[int, dict[str, Any]]:
        try:
            normalized = prefix_config.extract_prefix(prefix)
            api_config = normalize_prefix_payload(payload)
        except ApiError as exc:
            return exc.status, api_error_response(exc)
        config = prefix_config.load_prefix_config(prefix_config.get_config_path())
        new_entry = prefix_entry_from_api(api_config)
        existing = config.get(normalized)
        if isinstance(existing, dict):
            config[normalized] = {**existing, **new_entry}
        else:
            config[normalized] = new_entry
        try:
            prefix_config.save_prefix_config(prefix_config.get_config_path(), config)
        except OSError as exc:
            return 500, structured_error("config_save_failed", str(exc))
        return 200, {"ok": True, "config": prefix_entry_to_api(normalized, config[normalized])}

    def create_review(self, payload: Any) -> tuple[int, dict[str, Any]]:
        if self.is_shutting_down():
            return 503, structured_error(
                "service_shutting_down",
                "Service is shutting down and does not accept new reviews.",
            )
        try:
            request = normalize_review_request(payload)
            self._validate_selected_mr_request(request)
        except ApiError as exc:
            return exc.status, api_error_response(exc)
        active = self.store.find_active_by_issue(request["issueKey"])
        if active is not None:
            popup_status = status_to_popup(active)
            popup_status["detailStatus"] = "already_running"
            popup_status["detailText"] = "Ревью уже выполняется"
            popup_status["availableActions"] = ["cancel", "refresh"]
            popup_status["error"] = {
                "code": "already_running",
                "message": "An active review job already exists for this issue.",
                "jobId": active.get("jobId"),
            }
            return 409, {"ok": False, "jobId": active.get("jobId"), "status": popup_status}
        status = self.store.create_job(request)
        if self.start_worker and self._worker_pool is not None:
            self._worker_pool.enqueue(str(status["jobId"]))
        return 202, {"ok": True, "jobId": status["jobId"], "status": status_to_popup(status)}

    def discover_merge_requests_by_issue(self, issue_key: str) -> tuple[int, dict[str, Any]]:
        try:
            normalized = normalize_issue_key(issue_key)
        except ApiError as exc:
            return exc.status, api_error_response(exc)
        instances = self._gitlab_instances()
        if not instances:
            return 200, self._discovery_error(normalized, "gitlab_instances_not_configured")

        prefix = prefix_config.extract_prefix(normalized)
        config = prefix_config.load_prefix_config(prefix_config.get_config_path())
        entry = config.get(prefix)
        if not isinstance(entry, dict):
            return 200, self._discovery_error(normalized, "prefix_config_missing", {"prefix": prefix})

        project_scope, diagnostics = self._discover_gitlab_project_scope(entry, instances)
        if not project_scope:
            return 200, self._discovery_error(
                normalized,
                "no_gitlab_remotes",
                {},
                diagnostics=diagnostics,
            )

        used_ids = {project.instance_id for project in project_scope}
        tokens, missing = token_lookup(instances, used_ids)
        if missing:
            return 200, self._discovery_error(
                normalized,
                "gitlab_token_missing",
                {"instances": missing},
                diagnostics=diagnostics,
            )

        candidates_by_key: dict[tuple[str, str, int], dict[str, Any]] = {}
        warnings: list[dict[str, Any]] = []
        successful_projects = 0
        instance_by_id = {instance.id: instance for instance in instances}
        for project in project_scope:
            instance = instance_by_id.get(project.instance_id)
            token = tokens.get(project.instance_id)
            if instance is None or not token:
                continue
            result = self.gitlab_client.fetch_project_merge_requests(
                instance=instance,
                token=token,
                project_path=project.project_path,
                issue_key=normalized,
            )
            if result.ok:
                successful_projects += 1
                for item in result.items:
                    candidate = self._candidate_from_gitlab_item(instance.id, project.project_path, normalized, item)
                    if candidate is not None:
                        key = (candidate["gitlabInstanceId"], candidate["gitlabProjectPath"], candidate["mrId"])
                        candidates_by_key[key] = candidate
            elif result.warning:
                warnings.append(result.warning)

        if successful_projects == 0 and warnings:
            return 200, self._discovery_error(
                normalized,
                "all_projects_failed",
                {},
                warnings=self._sorted_warnings(warnings),
                diagnostics=diagnostics,
            )

        candidates = sorted(
            candidates_by_key.values(),
            key=lambda item: (item["gitlabInstanceId"], item["gitlabProjectPath"], item["mrId"]),
        )
        return 200, {
            "ok": True,
            "issueKey": normalized,
            "state": "ok" if candidates else "empty",
            "candidates": candidates,
            "warnings": self._sorted_warnings(warnings),
        }

    def get_review_by_issue(self, issue_key: str) -> tuple[int, dict[str, Any]]:
        try:
            normalized = normalize_issue_key(issue_key)
        except ApiError as exc:
            return exc.status, api_error_response(exc)
        status = self.store.find_latest_by_issue(normalized)
        status = self._project_issue_status(status)
        return 200, {"ok": True, "status": status_to_popup(self._with_changes_action(status), issue_key=normalized)}

    def get_status(self, job_id: str) -> tuple[int, dict[str, Any]]:
        status = self.store.read_status(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        status = self._enrich_completed_status(status)
        return 200, {"ok": True, "status": status_to_popup(self._with_changes_action(status))}

    def get_summary(self, job_id: str) -> tuple[int, dict[str, Any]]:
        status = self.store.read_status(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        summary = status.get("summary")
        if summary is None:
            run_dir = Path(str(status.get("runDir") or ""))
            summary_path = run_dir / "review_summary.json"
            if summary_path.exists():
                summary = read_json(summary_path)
        if summary is None:
            return 404, structured_error("summary_unavailable", "Machine-readable summary is unavailable.")
        if "warnings" not in summary:
            summary["warnings"] = status.get("warnings") if isinstance(status.get("warnings"), list) else []
        return 200, {"ok": True, "summary": summary}

    def get_report(self, job_id: str) -> tuple[int, dict[str, Any]]:
        status = self.store.read_status(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        report_path = status.get("reportPath")
        if not isinstance(report_path, str) or not Path(report_path).exists():
            return 404, structured_error("report_unavailable", "Report is unavailable.")
        content = Path(report_path).read_text(encoding="utf-8")
        payload: dict[str, Any] = {"ok": True, "reportPath": report_path, "content": content}
        issue_key = status.get("issueKey")
        if isinstance(issue_key, str) and issue_key.strip():
            payload["issueKey"] = issue_key.strip()
        try:
            task_dir, task = self._feedback_context(status)
            payload["view"] = self.report_feature.get_report(task_dir, task)
        except FeedbackError as exc:
            payload["view"] = None
            payload["viewError"] = {"code": exc.code, "message": str(exc)}
        return 200, payload

    @staticmethod
    def _feedback_context(status: dict[str, Any]) -> tuple[Path, str]:
        task_dir = status.get("taskDir")
        task = status.get("issueKey")
        if not isinstance(task_dir, str) or not task_dir.strip() or not isinstance(task, str) or not task.strip():
            raise FeedbackError(409, "feedback_unavailable", "Каталог или номер задачи недоступен.")
        return Path(task_dir).resolve(), task.strip()

    def get_feedback(self, job_id: str) -> tuple[int, dict[str, Any]]:
        status = self.store.read_status(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        try:
            task_dir, task = self._feedback_context(status)
            return 200, self.report_feature.get_feedback(task_dir, task)
        except FeedbackError as exc:
            return exc.status, exc.response()

    def put_feedback(self, job_id: str, payload: Any) -> tuple[int, dict[str, Any]]:
        status = self.store.read_status(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        try:
            task_dir, task = self._feedback_context(status)
            return 200, self.report_feature.put_feedback(task_dir, task, payload)
        except FeedbackError as exc:
            return exc.status, exc.response()

    def get_reasoning(self, job_id: str) -> tuple[int, dict[str, Any]]:
        status = self.store.read_status(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        reasoning_path = status.get("reasoningPath")
        if not isinstance(reasoning_path, str) or not Path(reasoning_path).exists():
            return 404, structured_error("reasoning_unavailable", "Reasoning YAML is unavailable.")
        content = Path(reasoning_path).read_text(encoding="utf-8")
        payload: dict[str, Any] = {"ok": True, "reasoningPath": reasoning_path, "content": content}
        issue_key = status.get("issueKey")
        if isinstance(issue_key, str) and issue_key.strip():
            payload["issueKey"] = issue_key.strip()
        return 200, payload

    def open_agent_log(self, job_id: str) -> tuple[int, dict[str, Any]]:
        status = self.store.read_status(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        available_actions = status.get("availableActions")
        if (
            status.get("detailStatus") not in {"review_running", "done"}
            or not isinstance(available_actions, list)
            or "open_agent_log" not in available_actions
        ):
            return 409, structured_error(
                "agent_log_inactive",
                "Agent log is not available for this review status.",
            )
        try:
            request = self.store.read_request(job_id)
        except (OSError, ValueError):
            return 404, structured_error("agent_log_unavailable", "Persisted review request is unavailable.")
        stderr_name = _AGENT_STDERR_BY_IDE.get(request.get("ide"))
        if stderr_name is None:
            return 409, structured_error("unsupported_ide", "The persisted review IDE has no agent log mapping.")
        run_dir_value = status.get("runDir")
        if not isinstance(run_dir_value, str) or not run_dir_value.strip():
            return 404, structured_error("agent_log_unavailable", "Persisted run directory is unavailable.")
        try:
            log_path = (Path(run_dir_value).expanduser().resolve() / stderr_name).resolve()
        except OSError:
            return 404, structured_error("agent_log_unavailable", "Persisted agent log path is unavailable.")
        if not log_path.is_file():
            return 404, structured_error("agent_log_unavailable", "Agent log file is unavailable.")
        try:
            self.file_opener(log_path)
        except Exception:
            return 500, structured_error("agent_log_open_failed", "The operating system could not open the agent log.")
        return 200, {"ok": True, "jobId": job_id}

    def _with_changes_action(self, status: dict[str, Any] | None) -> dict[str, Any] | None:
        if status is None:
            return None
        projected = dict(status)
        raw_actions = projected.get("availableActions")
        actions = [a for a in raw_actions if a != "open_changes"] if isinstance(raw_actions, list) else []
        projected["availableActions"] = actions
        if sys.platform != "win32":
            return projected
        task_dir = projected.get("taskDir")
        if task_dir and (Path(task_dir) / "_review_info" / "changes" / COMPARISON_LAUNCHER).is_file():
            actions.append("open_changes")
        return projected

    def open_changes(self, job_id: str) -> tuple[int, dict[str, Any]]:
        status = self.store.read_status(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        if sys.platform != "win32":
            return 501, structured_error("changes_platform_unsupported", "Запуск сравнения доступен в Windows.")
        task_dir = status.get("taskDir")
        if not task_dir:
            return 409, structured_error("changes_inactive", "Каталог задачи не определён.")
        script = Path(task_dir) / "_review_info" / "changes" / COMPARISON_LAUNCHER
        if not script.is_file():
            return 404, structured_error("changes_unavailable", "Launcher сравнения отсутствует.")
        try:
            self.changes_runner(script)
        except ComparisonLaunchError as exc:
            code = 424 if exc.code == "changes_tool_missing" else 500
            return code, structured_error(exc.code, str(exc))
        except Exception:
            return 500, structured_error("changes_open_failed", "Не удалось открыть сравнение.")
        return 200, {"ok": True, "jobId": job_id}

    def open_review_bsl(self, job_id: str) -> tuple[int, dict[str, Any]]:
        status = self.store.read_status(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        available_actions = status.get("availableActions")
        if (
            status.get("detailStatus") not in {"review_running", "done"}
            or not isinstance(available_actions, list)
            or "open_review_bsl" not in available_actions
        ):
            return 409, structured_error(
                "review_bsl_inactive",
                "Review BSL file is not available for this review status.",
            )
        task_dir_value = status.get("taskDir")
        if not isinstance(task_dir_value, str) or not task_dir_value.strip():
            return 404, structured_error("review_bsl_unavailable", "Persisted task directory is unavailable.")
        try:
            review_path = (Path(task_dir_value).expanduser() / "_review_info" / "review_bsl.md").resolve()
            if not review_path.is_file():
                return 404, structured_error("review_bsl_unavailable", "Review BSL file is unavailable.")
        except (OSError, ValueError, RuntimeError):
            return 404, structured_error("review_bsl_unavailable", "Persisted review BSL path is unavailable.")
        try:
            self.file_opener(review_path)
        except Exception:
            return 500, structured_error("review_bsl_open_failed", "The operating system could not open the review BSL file.")
        return 200, {"ok": True, "jobId": job_id}

    def open_job_log(self, job_id: str, payload: Any) -> tuple[int, dict[str, Any]]:
        status = self.store.read_status(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        if not isinstance(payload, dict) or not isinstance(payload.get("logRef"), str):
            return 400, structured_error("invalid_log_ref", "logRef must be a string.")
        log_ref = payload["logRef"]
        run_dir_value = status.get("runDir")
        if not isinstance(run_dir_value, str) or not run_dir_value.strip():
            return 404, structured_error("job_log_unavailable", "Persisted run directory is unavailable.")
        path = job_log_path(Path(run_dir_value), log_ref)
        if path is None:
            return 400, structured_error("invalid_log_ref", "Unknown or unsafe job log reference.")
        if not path.is_file():
            return 404, structured_error("job_log_unavailable", "Job log file is unavailable.")
        try:
            self.file_opener(path)
        except Exception:
            return 500, structured_error("job_log_open_failed", "The operating system could not open the job log.")
        return 200, {"ok": True, "jobId": job_id, "logRef": log_ref}

    def cancel(self, job_id: str) -> tuple[int, dict[str, Any]]:
        status = self.orchestrator.cancel(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        return 200, {"ok": True, "status": status_to_popup(status)}

    def diagnostics(self, job_id: str) -> tuple[int, dict[str, Any]]:
        status = self.store.read_status(job_id)
        if status is None:
            return 404, structured_error("not_found", f"Unknown jobId: {job_id}")
        run_dir = Path(str(status.get("runDir") or ""))
        return 200, {
            "ok": True,
            "jobId": job_id,
            "runDir": str(run_dir),
            "initStderrTail": safe_tail(run_dir / "init.stderr.log"),
            "codexStderrTail": safe_tail(run_dir / "codex.stderr.log"),
            "codexFinalTail": safe_tail(run_dir / "codex.final.md"),
            "cursorStderrTail": safe_tail(run_dir / "cursor.stderr.log"),
            "cursorStdoutTail": safe_tail(run_dir / "cursor.stdout.log"),
            "opencodeStderrTail": safe_tail(run_dir / "opencode.stderr.log"),
            "opencodeStdoutTail": safe_tail(run_dir / "opencode.stdout.log"),
            "cursorArgv": status.get("cursorArgv"),
            "opencodeArgv": status.get("opencodeArgv"),
            "entries": build_log_diagnostics(run_dir, [source.ref for source in JOB_LOG_SOURCES]),
        }

    @staticmethod
    def decode_json_body(raw: bytes) -> Any:
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _project_issue_status(self, latest: dict[str, Any] | None) -> dict[str, Any] | None:
        if latest is None:
            return None
        if latest.get("detailStatus") != "cancelled":
            return self._enrich_completed_status(latest)
        fallback = self._find_completed_fallback(latest)
        if fallback is None:
            return latest
        projected = self._enrich_completed_status(deepcopy(fallback), persist=False)
        if projected is None or not self._has_readable_path(projected.get("reportPath")):
            return latest
        if not self._has_readable_path(projected.get("reasoningPath")):
            projected["reasoningPath"] = None
        projected["detailText"] = _CANCELLED_REPEAT_DETAIL_TEXT
        projected["latestAttempt"] = self._latest_attempt_payload(latest)
        return projected

    def _find_completed_fallback(self, latest: dict[str, Any]) -> dict[str, Any] | None:
        issue_key = latest.get("issueKey")
        if not isinstance(issue_key, str) or not issue_key:
            return None
        candidates = [
            status for status in self.store.iter_statuses()
            if self._is_completed_fallback_candidate(status, latest)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: str(item.get("finishedAt") or item.get("updatedAt") or item.get("startedAt") or ""))

    def _is_completed_fallback_candidate(self, candidate: dict[str, Any], latest: dict[str, Any]) -> bool:
        if candidate.get("jobId") == latest.get("jobId"):
            return False
        if candidate.get("issueKey") != latest.get("issueKey"):
            return False
        if candidate.get("detailStatus") not in DONE_DETAIL_STATUSES:
            return False
        if not self._same_review_target(candidate, latest):
            return False
        return self._has_readable_path(candidate.get("reportPath"))

    def _same_review_target(self, candidate: dict[str, Any], latest: dict[str, Any]) -> bool:
        return self._selected_mr_identity(candidate) == self._selected_mr_identity(latest)

    def _selected_mr_identity(self, status: dict[str, Any]) -> tuple[str | None, int, str] | None:
        identity = self._selected_mr_identity_from_mapping(status.get("source"))
        if identity is not None:
            return identity
        job_id = status.get("jobId")
        if not isinstance(job_id, str) or not job_id:
            return None
        try:
            request = self.store.read_request(job_id)
        except (OSError, json.JSONDecodeError):
            return None
        return self._selected_mr_identity_from_mapping(request.get("source")) or self._selected_mr_identity_from_mapping(request.get("init"))

    @staticmethod
    def _selected_mr_identity_from_mapping(value: Any) -> tuple[str | None, int, str] | None:
        if not isinstance(value, dict):
            return None
        mr_id = value.get("mrId")
        gitlab_project_path = value.get("gitlabProjectPath")
        gitlab_instance_id = value.get("gitlabInstanceId")
        if not isinstance(mr_id, int) or isinstance(mr_id, bool) or mr_id <= 0:
            return None
        if not isinstance(gitlab_project_path, str) or not gitlab_project_path.strip():
            return None
        instance_id = gitlab_instance_id.strip() if isinstance(gitlab_instance_id, str) and gitlab_instance_id.strip() else None
        return instance_id, mr_id, gitlab_project_path.strip().lower()

    def _validate_selected_mr_request(self, request: dict[str, Any]) -> None:
        init = request.get("init") if isinstance(request.get("init"), dict) else {}
        if not init.get("mrId") or init.get("mrFromTasks"):
            return
        instances = self._gitlab_instances()
        if not instances:
            return
        instance_id = init.get("gitlabInstanceId")
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ApiError(
                "invalid_request",
                "init.gitlabInstanceId is required when GitLab instances are configured.",
                details={"field": "init.gitlabInstanceId"},
            )
        if instance_id not in {instance.id for instance in instances}:
            raise ApiError(
                "invalid_request",
                "init.gitlabInstanceId must match a configured GitLab instance.",
                details={"gitlabInstanceId": instance_id},
            )
        mr_url = init.get("mrUrl")
        project_path = init.get("gitlabProjectPath")
        if not isinstance(mr_url, str) or not isinstance(project_path, str):
            raise ApiError(
                "invalid_request",
                "init.mrUrl and init.gitlabProjectPath are required for selected MR review.",
            )
        parsed = parse_gitlab_mr_url_ref(mr_url, instances)
        if parsed is None:
            raise ApiError(
                "invalid_request",
                "init.mrUrl must match a configured GitLab instance.",
                details={"mrUrl": mr_url},
            )
        if parsed.instance_id != instance_id or parsed.project_path != project_path or parsed.mr_id != init["mrId"]:
            raise ApiError(
                "invalid_request",
                "Selected MR identity must match init.gitlabInstanceId, init.gitlabProjectPath and init.mrId.",
                details={
                    "gitlabInstanceId": instance_id,
                    "mrUrlInstanceId": parsed.instance_id,
                    "gitlabProjectPath": project_path,
                    "mrUrlProjectPath": parsed.project_path,
                    "mrId": init["mrId"],
                    "mrUrlMrId": parsed.mr_id,
                },
            )

    def _gitlab_instances(self):
        if self.effective_config is None:
            return ()
        return self.effective_config.gitlab_instances

    def _discover_gitlab_project_scope(self, entry: dict[str, Any], instances: tuple[Any, ...]) -> tuple[list[GitLabProjectKey], dict[str, Any]]:
        repositories = prefix_config.entry_repositories(entry)
        remote = str(entry.get("remote") or "origin")
        diagnostics: dict[str, Any] = {
            "selectedRemote": remote,
            "configuredInstances": [
                {"id": instance.id, "host": instance.host, "port": instance.port}
                for instance in instances
            ],
            "repositories": [],
        }
        try:
            expanded = prefix_config.resolve_repository_entries(repositories)
        except prefix_config.RepositoryDiscoveryError as exc:
            diagnostics["repositories"].append({"inspectionError": {"message": str(exc)}})
            return [], diagnostics

        by_key: dict[tuple[str, str], GitLabProjectKey] = {}
        for repo_path in expanded:
            lookup = resolve_git_remote(repo_path, remote, instances)
            if lookup.error is not None:
                diagnostics["repositories"].append(
                    self._remote_discovery_diagnostic(repo_path, remote, lookup, instances)
                )
                continue
            resolved = lookup.resolution
            if resolved is None or resolved.ref is None:
                diagnostics["repositories"].append(
                    self._remote_discovery_diagnostic(repo_path, remote, lookup, instances)
                )
                continue
            key = (resolved.ref.instance_id, resolved.ref.project_path)
            by_key[key] = GitLabProjectKey(instance_id=resolved.ref.instance_id, project_path=resolved.ref.project_path)
        return sorted(by_key.values(), key=lambda item: (item.instance_id, item.project_path)), diagnostics

    def _remote_discovery_diagnostic(
        self,
        repo_path: Path,
        selected_remote: str,
        lookup: Any,
        instances: tuple[Any, ...],
    ) -> dict[str, Any]:
        resolution = lookup.resolution
        if lookup.error is not None:
            return {
                "repoPath": str(repo_path),
                "selectedRemote": {
                    "name": selected_remote,
                    "reason": "missing_remote",
                },
                "alternativeRemotes": self._gitlab_remote_alternatives(repo_path, selected_remote, instances)[0],
            }
        params = resolution.params if resolution is not None and isinstance(resolution.params, dict) else {}
        selected: dict[str, Any] = {
            "name": selected_remote,
            "remoteUrl": sanitize_git_remote_url(lookup.remote_url),
            "reason": resolution.reason if resolution is not None else "empty_remote",
        }
        host = params.get("host")
        if isinstance(host, str) and host:
            selected["host"] = host
        alternatives, alternatives_error = self._gitlab_remote_alternatives(repo_path, selected_remote, instances)
        result: dict[str, Any] = {
            "repoPath": str(repo_path),
            "selectedRemote": selected,
        }
        if alternatives:
            result["alternativeRemotes"] = alternatives
        if alternatives_error is not None:
            result["alternativesInspectionError"] = {"message": alternatives_error}
        return result

    @staticmethod
    def _gitlab_remote_alternatives(
        repo_path: Path,
        selected_remote: str,
        instances: tuple[Any, ...],
    ) -> tuple[list[dict[str, str]], str | None]:
        remote_names, error = list_git_remotes(repo_path)
        if error is not None:
            return [], error
        alternatives: list[dict[str, str]] = []
        for remote_name in remote_names:
            if remote_name == selected_remote:
                continue
            lookup = resolve_git_remote(repo_path, remote_name, instances)
            if lookup.error is not None or lookup.resolution is None or lookup.resolution.ref is None:
                continue
            ref = lookup.resolution.ref
            alternatives.append(
                {
                    "name": remote_name,
                    "remoteUrl": sanitize_git_remote_url(lookup.remote_url),
                    "gitlabInstanceId": ref.instance_id,
                    "gitlabProjectPath": ref.project_path,
                }
            )
        return alternatives, None

    @staticmethod
    def _candidate_from_gitlab_item(
        instance_id: str,
        project_path: str,
        issue_key: str,
        item: dict[str, Any],
    ) -> dict[str, Any] | None:
        mr_id = item.get("iid")
        if not isinstance(mr_id, int) or isinstance(mr_id, bool) or mr_id <= 0:
            return None
        state = item.get("state")
        if isinstance(state, str) and state and state.lower() != "opened":
            return None
        title = str(item.get("title") or "")
        if not title_contains_issue_key(title, issue_key):
            return None
        mr_url = item.get("web_url")
        if not isinstance(mr_url, str) or not mr_url.strip():
            return None
        slug = project_path.rsplit("/", 1)[-1]
        return {
            "gitlabInstanceId": instance_id,
            "mrId": mr_id,
            "mrUrl": mr_url.strip(),
            "gitlabProjectPath": project_path,
            "title": title,
            "label": f"{slug} !{mr_id}",
        }

    @staticmethod
    def _sorted_warnings(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            warnings,
            key=lambda item: (
                str((item.get("params") or {}).get("instanceId") or ""),
                str((item.get("params") or {}).get("projectPath") or ""),
                str(item.get("code") or ""),
            ),
        )

    @staticmethod
    def _discovery_error(
        issue_key: str,
        code: str,
        params: dict[str, Any] | None = None,
        *,
        warnings: list[dict[str, Any]] | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "params": params or {}}
        if diagnostics:
            error["diagnostics"] = diagnostics
        return {
            "ok": False,
            "issueKey": issue_key,
            "state": "error",
            "candidates": [],
            "error": error,
            "warnings": warnings or [],
        }

    @staticmethod
    def _latest_attempt_payload(status: dict[str, Any]) -> dict[str, Any]:
        detail_status = str(status.get("detailStatus") or status.get("status") or "")
        return {
            "jobId": status.get("jobId"),
            "detailStatus": detail_status,
            "detailText": str(status.get("detailText") or DETAIL_TEXT.get(detail_status, detail_status)),
            "updatedAt": status.get("updatedAt"),
        }

    @staticmethod
    def _has_readable_path(value: Any) -> bool:
        return isinstance(value, str) and bool(value) and Path(value).is_file()

    def _enrich_completed_status(self, status: dict[str, Any] | None, *, persist: bool = True) -> dict[str, Any] | None:
        if status is None or status.get("detailStatus") not in DONE_DETAIL_STATUSES:
            return status
        summary = status.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        changed = False
        run_dir = Path(str(status.get("runDir") or ""))
        task_dir = Path(str(status.get("taskDir") or ""))

        summary_path = run_dir / "review_summary.json"
        if summary_path.exists():
            try:
                disk_summary = read_json(summary_path)
            except (OSError, json.JSONDecodeError):
                disk_summary = {}
            if isinstance(disk_summary, dict):
                merged = {**disk_summary, **summary}
                summary = merged

        if not summary.get("summaryText") and task_dir.exists():
            text = self._summary_text_from_yaml(task_dir)
            if text:
                summary["summaryText"] = text
                changed = True

        if not status.get("reasoningPath"):
            reasoning_path = summary.get("resultYamlPath")
            if not isinstance(reasoning_path, str) or not Path(reasoning_path).exists():
                reasoning_path = self._reasoning_path_from_task_dir(task_dir)
            if isinstance(reasoning_path, str) and Path(reasoning_path).exists():
                status["reasoningPath"] = reasoning_path
                summary["resultYamlPath"] = reasoning_path
                changed = True

        token_usage = status.get("tokenUsage") or summary.get("tokenUsage")
        if token_usage is None:
            token_usage = extract_token_usage(run_dir / "codex.stderr.log")
            if token_usage is None:
                token_usage = extract_cursor_token_usage(run_dir / "cursor.stderr.log", run_dir / "cursor.stdout.log")
            if token_usage is None:
                token_usage = extract_opencode_token_usage(run_dir / "opencode.stdout.log", run_dir / "opencode.stderr.log")
            if token_usage is not None:
                changed = True
        if token_usage is not None:
            summary["tokenUsage"] = token_usage
            status["tokenUsage"] = token_usage

        warnings = status.get("warnings") if isinstance(status.get("warnings"), list) else []
        if "warnings" not in summary:
            summary["warnings"] = warnings
            changed = True

        if summary:
            status["summary"] = summary

        if changed and status.get("jobId") and persist:
            self.store.write_status(status)
            if summary_path.exists() or run_dir.exists():
                from .storage import write_json

                write_json(summary_path, summary)
        return status

    @staticmethod
    def _summary_text_from_yaml(task_dir: Path) -> str | None:
        review_info = task_dir / "_review_info"
        yaml_paths = expand_review_result_inputs([review_info])
        if not yaml_paths:
            return None
        try:
            documents = read_review_yaml_documents(yaml_paths)
        except Exception:
            return None
        blocks = [doc.parsed.summary.strip() for doc in documents]
        blocks = [block for block in blocks if block]
        return "\n\n".join(blocks) if blocks else None

    @staticmethod
    def _reasoning_path_from_task_dir(task_dir: Path) -> str | None:
        if not task_dir.exists():
            return None
        yaml_paths = expand_review_result_inputs([task_dir / "_review_info"])
        if not yaml_paths:
            return None
        return str(yaml_paths[0])

    def _startup_reconcile(self) -> None:
        if not self.start_worker:
            return
        for status in self.store.iter_active_statuses():
            job_id = str(status.get("jobId") or "")
            if not job_id:
                continue
            detail_status = str(status.get("detailStatus") or status.get("status") or "")
            if detail_status == "queued":
                if self._worker_pool is not None:
                    self._worker_pool.enqueue(job_id)
                continue
            run_dir = Path(str(status.get("runDir") or ""))
            pid_path = run_dir / "pid.json"
            if detail_status in _AGENT_ACTIVE_DETAIL_STATUSES and pid_path.exists():
                try:
                    pid_data = read_json(pid_path)
                except (OSError, json.JSONDecodeError):
                    self._mark_interrupted(job_id, "pid.json unreadable during startup reconcile")
                    continue
                alive, reason = check_review_process_liveness(pid_data)
                if alive:
                    self.orchestrator.mark_for_reattach(job_id)
                    if self._worker_pool is not None:
                        self._worker_pool.enqueue(job_id)
                    continue
                self._mark_interrupted(job_id, reason)
                continue
            self._mark_interrupted(
                job_id,
                "review process did not survive service restart",
            )

    def _mark_interrupted(self, job_id: str, reason: str) -> None:
        status = self.store.read_status(job_id)
        if status is None:
            return
        issue_key = str(status.get("issueKey") or job_id)
        log_error(issue_key, f"interrupted: {reason}")
        self.store.update_status(
            job_id,
            status="interrupted",
            detailStatus="interrupted",
            detailText=DETAIL_TEXT["interrupted"],
            availableActions=["retry", "refresh"],
            finishedAt=now_iso(),
            error={"code": "interrupted", "message": reason},
        )

    def _on_job_skipped(self, job_id: str) -> None:
        self.orchestrator.interrupt_job(job_id, "service shutting down before job start")

    def _should_run_job(self, job_id: str) -> bool:
        status = self.store.read_status(job_id)
        if not is_active_status(status):
            return False
        return not bool(status.get("cancelRequested"))

    def _drain_in_flight_jobs(self, timeout_sec: int) -> None:
        if self._worker_pool is None:
            return
        drained = self._worker_pool.wait_for_idle(float(timeout_sec))
        if drained:
            return
        running_ids = self._worker_pool.running_job_ids()
        tracked_ids = self.orchestrator.tracked_job_ids()
        for job_id in sorted(running_ids | tracked_ids):
            self.orchestrator.interrupt_job(
                job_id,
                f"service shutdown drain exceeded {timeout_sec}s",
            )
