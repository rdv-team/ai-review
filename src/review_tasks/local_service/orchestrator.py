from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from review_tasks.cli import has_completed_result_status, read_text_safe, sanitize_task, task_to_kebab, validate_review_dir_name
from review_tasks.integrations.mcp_registry import (
    IdeAdapterError,
    load_mcp_registry,
    mcp_bearer_env_var_names,
    mcp_url_env_var_names,
)
from review_tasks.review_init import prefix_config
from review_tasks.review_result.review_yaml import expand_review_result_inputs, read_review_yaml_documents

from .contracts import ApiError, DETAIL_TEXT, actions_for, normalize_opencode_model, now_iso, status_to_popup
from .gitlab import GitLabInstance, GitRemoteCommandOutput, normalize_gitlab_project_path, resolve_git_remote
from .job_logs import build_detail_diagnostics, build_log_diagnostics
from .logging_setup import log_agent_end, log_agent_start, log_error, log_init_end, log_init_start
from .problem_details import (
    InitResultProtocolError,
    build_feedback,
    diagnostic_entry,
    load_init_result,
    unique_diagnostics,
)
from .process_liveness import check_review_process_liveness, pid_exists
from .storage import JobStore, read_json, safe_tail, write_json

_EXTERNALLY_FINALIZED_DETAIL_STATUSES = frozenset({"timeout", "cancelled", "interrupted"})

CODEX_SHELL_SNAPSHOT_DISABLE_ARGS = ["--disable", "shell_snapshot"]
CURSOR_DEFAULT_MODEL = "composer-2.5-fast"
CURSOR_FALLBACK_MODEL = "composer-2.5"
CURSOR_REVIEW_PROMPT = "Выполни /review-start-task"
OPENCODE_REVIEW_COMMAND = "review-start-task"
OPENCODE_REQUIRED_COMMON_RUN_FLAGS = ("--command", "--dir")
OPENCODE_PERMISSION_APPROVAL_FLAGS = ("--auto", "--dangerously-skip-permissions")
OPENCODE_AUTH_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "ROUTERAI_API_KEY",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int


class ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


@dataclass
class PidProcess:
    """Process handle reconstructed from a persisted pid (post-restart reattach)."""

    pid: int
    _returncode: int | None = None

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        alive, _ = pid_exists(self.pid)
        if not alive:
            self._returncode = 1
            return self._returncode
        return None

    def terminate(self) -> None:
        terminate_process_tree(self)

    def kill(self) -> None:
        terminate_process_tree(self)


class CommandBackend:
    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
        timeout: int | None = None,
    ) -> CommandResult:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            result = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
                check=False,
            )
        return CommandResult(result.returncode)

    def start(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> ProcessLike:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        return subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=stdout_path.open("wb"),
            stderr=stderr_path.open("wb"),
            creationflags=creationflags,
        )


class PreflightError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class JobOrchestrator:
    def __init__(
        self,
        store: JobStore,
        *,
        backend: CommandBackend | None = None,
        poll_interval: float = 2.0,
        gitlab_instances: tuple[GitLabInstance, ...] = (),
    ) -> None:
        self.store = store
        self.backend = backend or CommandBackend()
        self.poll_interval = poll_interval
        self.gitlab_instances = gitlab_instances
        self._processes: dict[str, ProcessLike] = {}
        self._processes_lock = threading.Lock()
        self._reattach_job_ids: set[str] = set()
        self._reattach_lock = threading.Lock()

    def run_job(self, job_id: str) -> None:
        try:
            self._run_job(job_id)
        except PreflightError as exc:
            if exc.code == "cancelled":
                return
            self._fail(job_id, exc.code, str(exc), detail_status=exc.code, details=exc.details)
        except subprocess.TimeoutExpired as exc:
            self._fail(job_id, "timeout", f"Process timeout: {exc}", detail_status="timeout")
        except Exception as exc:
            self._fail(job_id, "service_error", str(exc), detail_status="review_failed")

    def should_reattach(self, job_id: str) -> bool:
        with self._reattach_lock:
            return job_id in self._reattach_job_ids

    def mark_for_reattach(self, job_id: str) -> None:
        with self._reattach_lock:
            self._reattach_job_ids.add(job_id)

    def clear_reattach(self, job_id: str) -> None:
        with self._reattach_lock:
            self._reattach_job_ids.discard(job_id)

    def tracked_job_ids(self) -> set[str]:
        with self._processes_lock:
            return set(self._processes)

    def interrupt_job(self, job_id: str, message: str) -> dict[str, Any] | None:
        with self._processes_lock:
            process = self._processes.pop(job_id, None)
        if process is not None:
            terminate_process_tree(process)
        self.clear_reattach(job_id)
        status = self.store.read_status(job_id)
        if status is None:
            return None
        issue_key = str(status.get("issueKey") or job_id)
        log_error(issue_key, f"interrupted: {message}")
        return self.store.update_status(
            job_id,
            status="interrupted",
            detailStatus="interrupted",
            detailText=DETAIL_TEXT["interrupted"],
            availableActions=["retry", "refresh"],
            finishedAt=now_iso(),
            error={"code": "interrupted", "message": message},
        )

    def monitor_reattached_job(self, job_id: str) -> None:
        try:
            self._monitor_reattached_job(job_id)
        finally:
            self.clear_reattach(job_id)

    def _monitor_reattached_job(self, job_id: str) -> None:
        status = self.store.read_status(job_id)
        if status is None:
            return
        run_dir = Path(str(status.get("runDir") or ""))
        pid_path = run_dir / "pid.json"
        if not pid_path.exists():
            self.interrupt_job(job_id, "pid.json missing during reattach monitoring")
            return
        try:
            pid_data = read_json(pid_path)
        except (OSError, json.JSONDecodeError):
            self.interrupt_job(job_id, "pid.json unreadable during reattach monitoring")
            return
        alive, reason = check_review_process_liveness(pid_data)
        if not alive:
            self.interrupt_job(job_id, reason)
            return
        launcher_pid = pid_data.get("launcherPid")
        if not isinstance(launcher_pid, int):
            self.interrupt_job(job_id, "invalid launcherPid during reattach monitoring")
            return
        process = PidProcess(launcher_pid)
        with self._processes_lock:
            self._processes[job_id] = process
        request = self.store.read_request(job_id)
        detail_status = str(status.get("detailStatus") or "")
        runner_logs = _runner_log_paths(run_dir, str(request.get("ide") or "codex"))
        started = time.monotonic()
        timeout_sec = int(request.get("timeoutSec") or 1800)
        try:
            returncode = self._wait_process(
                job_id,
                process,
                run_dir,
                runner_logs["stdout"],
                runner_logs["stderr"],
                runner_logs.get("final"),
                timeout_sec,
                started,
            )
        finally:
            with self._processes_lock:
                self._processes.pop(job_id, None)
        current_status = self.store.read_status(job_id) or {}
        if current_status.get("detailStatus") in _EXTERNALLY_FINALIZED_DETAIL_STATUSES:
            return
        if returncode != 0:
            self._fail(
                job_id,
                "review_failed",
                f"Reattached review process exited with code {returncode}.",
                detail_status="review_failed",
            )
            return
        task_dir = Path(str(status.get("taskDir") or ""))
        if not task_dir.exists():
            self.interrupt_job(job_id, "task directory missing after reattached process exit")
            return
        self._postprocess(
            job_id,
            request,
            task_dir,
            run_dir,
            runner_logs.get("final"),
            runner=str(request.get("ide") or "codex"),
        )

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        status = self.store.mark_cancel_requested(job_id)
        with self._processes_lock:
            process = self._processes.get(job_id)
        if process is not None:
            terminate_process_tree(process)
        if status is None:
            return None
        return self.store.update_status(
            job_id,
            status="cancelled",
            detailStatus="cancelled",
            detailText=DETAIL_TEXT["cancelled"],
            availableActions=actions_for("cancelled"),
            finishedAt=now_iso(),
            feedback=build_feedback("cancelled", detail_status="cancelled"),
            error=None,
            diagnostics=[],
        )

    def _run_job(self, job_id: str) -> None:
        request = self.store.read_request(job_id)
        self.store.update_status(
            job_id,
            status="preflight_running",
            detailStatus="preflight_running",
            detailText=DETAIL_TEXT["preflight_running"],
            availableActions=["cancel"],
        )
        resolved_init = self._preflight(job_id, request)
        selected_source = resolved_init.get("source")
        if isinstance(selected_source, dict):
            request = self._persist_selected_mr_source(job_id, request, selected_source)

        self._check_cancel(job_id)
        run_dir = self._run_dir(job_id)
        init_stdout = run_dir / "init.stdout.log"
        init_stderr = run_dir / "init.stderr.log"
        init_result_path = run_dir / "init.result.json"
        init_argv = build_init_argv(
            request,
            resolved_init=resolved_init,
            executable=str(resolved_init["reviewTasksBin"]),
            result_path=init_result_path,
        )
        diagnostic_init_argv = redact_init_argv(init_argv)
        self.store.update_status(
            job_id,
            status="init_running",
            detailStatus="init_running",
            detailText=DETAIL_TEXT["init_running"],
            availableActions=["cancel"],
            initArgv=diagnostic_init_argv,
        )
        log_init_start(request["issueKey"], diagnostic_init_argv)
        init_exit_code = -1
        init_outcome = "failed"
        try:
            init_result = self.backend.run(
                init_argv,
                cwd=run_dir,
                env=controlled_env(),
                stdout_path=init_stdout,
                stderr_path=init_stderr,
                timeout=request["timeoutSec"],
            )
            init_exit_code = init_result.returncode
            try:
                init_contract = load_init_result(init_result_path, init_exit_code)
            except InitResultProtocolError as exc:
                init_outcome = "failed"
                self._fail(
                    job_id,
                    "init_result_protocol_error",
                    str(exc),
                    detail_status="init_failed",
                    details={"exitCode": init_result.returncode},
                    diagnostic_entries=[
                        diagnostic_entry(
                            "init.result.protocol",
                            "Протокол результата init",
                            "text",
                            value=str(exc),
                        ),
                        *build_log_diagnostics(run_dir, ["init.stderr"]),
                    ],
                )
                return

            comparisons = getattr(init_contract, "comparisons", [])
            expected_dir = Path(str(resolved_init["reviewsRoot"])) / str(resolved_init.get("reviewDirName") or request["issueKey"])
            matching = [item for item in comparisons if item.task == request["issueKey"]
                        and Path(item.task_dir).resolve() == expected_dir.resolve()]
            if len(matching) == 1:
                comparison = matching[0]
                self.store.update_status(job_id, comparison=comparison.model_dump(by_alias=True),
                                         taskDir=comparison.task_dir)

            if init_contract.outcome == "expected_stop":
                init_outcome = init_contract.code
                if init_contract.code == "already_done":
                    task_dir_value = init_contract.context.task_dir
                    task_dir = Path(task_dir_value).resolve() if task_dir_value else detect_task_dir(
                        init_stdout, request["issueKey"], resolved_init
                    )
                    self._complete_already_done(job_id, request, task_dir)
                else:
                    self._complete_expected_stop(job_id, init_contract.code)
                return
            if init_contract.outcome == "failure":
                init_outcome = "failed"
                self._fail(
                    job_id,
                    init_contract.code,
                    init_contract.message,
                    detail_status="init_failed",
                    details={"exitCode": init_result.returncode},
                    diagnostic_entries=build_log_diagnostics(run_dir, ["init.stderr"]),
                )
                return

            task_dir_value = init_contract.context.task_dir
            task_dir = Path(task_dir_value).resolve() if task_dir_value else detect_task_dir(
                init_stdout, request["issueKey"], resolved_init
            )
            artifact_error = verify_init_artifacts(task_dir, ide=request["ide"], mcp_mode=resolved_init["mcpMode"])
            if artifact_error:
                init_outcome = "failed"
                self._fail(
                    job_id,
                    "init_failed",
                    artifact_error,
                    detail_status="init_failed",
                    details={"taskDir": str(task_dir), "stderrPath": str(init_stderr)},
                    diagnostic_entries=build_log_diagnostics(run_dir, ["init.stderr"]),
                )
                return
            init_outcome = "ok"
        except PreflightError:
            init_outcome = "cancelled"
            raise
        finally:
            log_init_end(request["issueKey"], init_exit_code, init_outcome)

        self.store.move_to_task_run_dir(job_id, task_dir)
        run_dir = self._run_dir(job_id)
        self.store.update_status(
            job_id,
            status="init_done",
            detailStatus="init_done",
            detailText=DETAIL_TEXT["init_done"],
            availableActions=["cancel"],
            taskDir=str(task_dir),
            runDir=str(run_dir),
        )
        runner_mode = (request.get("runner") or {}).get("mode", "headless")
        if not self._post_init_mcp_preflight(job_id, task_dir, run_dir, mcp_mode=resolved_init["mcpMode"]):
            return
        if runner_mode == "handoff":
            self.store.update_status(
                job_id,
                status="handoff_required",
                detailStatus="handoff_required",
                detailText=DETAIL_TEXT["handoff_required"],
                availableActions=["submit", "refresh"],
                manualCommand=build_manual_runner_command(
                    request["ide"], task_dir, opencode_model=resolved_init["opencodeModel"]
                ),
                error=None,
            )
            return

        if request["ide"] == "cursor":
            self._run_cursor(
                job_id,
                request,
                task_dir,
                run_dir,
                str(resolved_init["cursorBin"]),
                str(resolved_init["cursorModel"]),
            )
            return

        if request["ide"] == "opencode":
            self._run_opencode(
                job_id,
                request,
                task_dir,
                run_dir,
                str(resolved_init["opencodeBin"]),
                str(resolved_init["opencodePermissionApprovalFlag"]),
                resolved_init["opencodeModel"],
            )
            return

        self._run_codex(job_id, request, task_dir, run_dir, str(resolved_init["codexBin"]))

    def _preflight(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        run_dir = self._run_dir(job_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        resolved = resolve_init_config(request)
        resolved["reviewTasksBin"] = self._resolve_executable("review-tasks", "REVIEW_TASKS_CLI_BIN")
        resolved["gitBin"] = self._resolve_executable("git", "REVIEW_TASKS_GIT_BIN")
        self._preflight_command(job_id, [str(resolved["reviewTasksBin"]), "--help"], run_dir, "review-tasks")
        self._preflight_command(job_id, [str(resolved["gitBin"]), "--version"], run_dir, "git")
        repo_paths = [Path(repo) for repo in prefix_config.split_repository_list(resolved["repoPath"])]
        try:
            expanded_repo_paths = prefix_config.resolve_repository_entries(
                [str(path) for path in repo_paths],
            )
        except prefix_config.RepositoryDiscoveryError as exc:
            raise PreflightError("preflight_failed", str(exc)) from exc

        init = request.get("init") or {}
        has_selected_mr = bool(
            init.get("mrId") and init.get("mrUrl") and init.get("gitlabProjectPath")
        )
        if len(expanded_repo_paths) > 1 and init.get("mrFromTasks"):
            raise PreflightError(
                "preflight_failed",
                "MR-режим (--mr-from-tasks) поддерживает только один репозиторий после разрешения путей.",
            )
        if len(expanded_repo_paths) > 1 and init.get("mrId") and not has_selected_mr:
            raise PreflightError(
                "preflight_failed",
                "MR-режим (--mr) поддерживает только один репозиторий после разрешения путей.",
            )

        resolved["repoPath"] = prefix_config.repositories_to_cli_arg(
            [str(path) for path in expanded_repo_paths]
        )
        repo_paths = expanded_repo_paths
        reviews_root = Path(str(resolved["reviewsRoot"]))
        for index, repo_path in enumerate(repo_paths, start=1):
            if not repo_path.exists():
                raise PreflightError("preflight_failed", f"Repository path does not exist: {repo_path}")
            self._preflight_command(
                job_id,
                [str(resolved["gitBin"]), "-C", str(repo_path), "rev-parse", "--show-toplevel"],
                run_dir,
                f"git-repo-{index}",
            )
        selected_source = self._resolve_selected_mr_source(job_id, request, resolved, repo_paths, run_dir)
        if selected_source is not None:
            resolved["repoPath"] = selected_source["repoPath"]
            resolved["reviewDirName"] = selected_source["reviewDirName"]
            resolved["source"] = selected_source
            self._persist_selected_mr_source(job_id, request, selected_source)
        try:
            reviews_root.mkdir(parents=True, exist_ok=True)
            probe = reviews_root / f".review-tasks-write-test-{job_id}"
            probe.write_text("ok\n", encoding="utf-8", newline="\n")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise PreflightError("preflight_failed", f"Reviews root is not writable: {reviews_root}") from exc
        runner_mode = (request.get("runner") or {}).get("mode", "headless")
        if runner_mode != "headless":
            return resolved
        if request["ide"] == "codex":
            resolved["codexBin"] = self._resolve_executable("codex", "REVIEW_TASKS_CODEX_BIN")
            self._preflight_command(job_id, [str(resolved["codexBin"]), "--version"], run_dir, "codex")
            self._preflight_command(job_id, [str(resolved["codexBin"]), "exec", "--help"], run_dir, "codex-exec")
            self._preflight_command(
                job_id,
                [str(resolved["codexBin"]), "exec", *CODEX_SHELL_SNAPSHOT_DISABLE_ARGS, "--help"],
                run_dir,
                "codex-exec-shell-snapshot",
            )
        elif request["ide"] == "cursor":
            resolved.update(self._preflight_cursor(job_id, run_dir))
        else:
            resolved.update(self._preflight_opencode(job_id, run_dir, resolved["opencodeModel"]))
        return resolved

    def _persist_selected_mr_source(
        self,
        job_id: str,
        request: dict[str, Any],
        selected_source: dict[str, Any],
    ) -> dict[str, Any]:
        request = dict(request)
        request["source"] = selected_source
        init = dict(request.get("init") or {})
        init["repoPath"] = selected_source["repoPath"]
        request["init"] = init
        self.store.write_request(job_id, request)
        self.store.update_status(job_id, source=selected_source)
        return request

    def _resolve_selected_mr_source(
        self,
        job_id: str,
        request: dict[str, Any],
        resolved: dict[str, Any],
        repo_paths: list[Path],
        run_dir: Path,
    ) -> dict[str, Any] | None:
        init = request.get("init") or {}
        if not (init.get("mrId") and init.get("mrUrl") and init.get("gitlabProjectPath")):
            return None

        requested_project = str(init["gitlabProjectPath"])
        requested_instance = init.get("gitlabInstanceId")
        if self.gitlab_instances and (not isinstance(requested_instance, str) or not requested_instance.strip()):
            raise PreflightError(
                "preflight_failed",
                "Selected MR request is missing GitLab instance identity.",
                details={"gitlabProjectPath": requested_project},
            )
        requested_instance_id = requested_instance.strip() if isinstance(requested_instance, str) else None
        remote = str(resolved["remote"])
        git_bin = str(resolved["gitBin"])
        checked: list[dict[str, Any]] = []
        matches: list[dict[str, Any]] = []
        for index, repo_path in enumerate(repo_paths, start=1):
            stdout = run_dir / f"preflight.git-remote-{index}.stdout.log"
            stderr = run_dir / f"preflight.git-remote-{index}.stderr.log"

            def run_git_remote(argv: list[str], timeout_sec: int) -> GitRemoteCommandOutput:
                result = self.backend.run(
                    argv,
                    cwd=run_dir,
                    env=controlled_env(),
                    stdout_path=stdout,
                    stderr_path=stderr,
                    timeout=timeout_sec,
                )
                return GitRemoteCommandOutput(
                    returncode=result.returncode,
                    stdout=read_text_safe(stdout),
                    stderr=safe_tail(stderr),
                )

            lookup = resolve_git_remote(
                repo_path,
                remote,
                self.gitlab_instances,
                git_bin=git_bin,
                runner=run_git_remote,
            )
            item: dict[str, Any] = {"repoPath": str(repo_path)}
            if lookup.error is None:
                remote_url = lookup.remote_url
                item["remoteUrl"] = remote_url
                if self.gitlab_instances and requested_instance_id:
                    resolved_remote = lookup.resolution
                    if resolved_remote is not None and resolved_remote.ref is not None:
                        item["gitlabInstanceId"] = resolved_remote.ref.instance_id
                        item["gitlabProjectPath"] = resolved_remote.ref.project_path
                        if (
                            resolved_remote.ref.instance_id == requested_instance_id
                            and resolved_remote.ref.project_path == requested_project
                        ):
                            matches.append(item)
                    else:
                        item["gitlabRemoteDiagnostic"] = {
                            "reason": resolved_remote.reason if resolved_remote is not None else "empty_remote",
                            "params": resolved_remote.params if resolved_remote is not None else {},
                        }
                else:
                    normalized = normalize_gitlab_project_path(remote_url) if remote_url else None
                    item["gitlabProjectPath"] = normalized
                    if normalized == requested_project:
                        matches.append(item)
            else:
                item["error"] = lookup.error
            checked.append(item)

        details = {
            "gitlabInstanceId": requested_instance_id,
            "gitlabProjectPath": requested_project,
            "remote": remote,
            "repositories": checked,
        }
        if not matches:
            raise PreflightError(
                "preflight_failed",
                f"No local repository matches selected MR project path: {requested_project}",
                details=details,
            )
        if len(matches) > 1:
            raise PreflightError(
                "preflight_failed",
                f"Multiple local repositories match selected MR project path: {requested_project}",
                details={**details, "matches": [match["repoPath"] for match in matches]},
            )

        selected = matches[0]
        repo_slug = requested_project.rsplit("/", 1)[-1]
        review_dir_name = f"{sanitize_task(request['issueKey'])}__{sanitize_task(repo_slug)}__mr-{init['mrId']}"
        validation_error = validate_review_dir_name(review_dir_name)
        if validation_error is not None:
            raise PreflightError("preflight_failed", validation_error, details={"reviewDirName": review_dir_name})
        return {
            "mode": "mr",
            **({"gitlabInstanceId": requested_instance_id} if requested_instance_id else {}),
            "mrId": init["mrId"],
            "mrUrl": init["mrUrl"],
            "gitlabProjectPath": requested_project,
            "repoPath": selected["repoPath"],
            "remote": remote,
            "reviewDirName": review_dir_name,
            **({"title": str(init["mrTitle"])} if init.get("mrTitle") else {}),
        }

    def _preflight_cursor(self, job_id: str, run_dir: Path) -> dict[str, Any]:
        cursor_bin = self._resolve_executable("agent", "REVIEW_TASKS_CURSOR_BIN")
        self._preflight_command(job_id, [str(cursor_bin), "--version"], run_dir, "cursor-agent")
        if not os.environ.get("CURSOR_API_KEY", "").strip():
            auth_text = self._preflight_command(job_id, [str(cursor_bin), "status"], run_dir, "cursor-agent-status")
            if "not logged in" in auth_text.lower():
                raise PreflightError("preflight_failed", "Cursor Agent is not logged in.")
        configured_model = os.environ.get("REVIEW_TASKS_CURSOR_MODEL", "").strip() or CURSOR_DEFAULT_MODEL
        models_text = self._preflight_command(job_id, [str(cursor_bin), "models"], run_dir, "cursor-agent-models")
        resolved_model = configured_model
        if configured_model not in models_text:
            resolved_model = CURSOR_FALLBACK_MODEL
            self._add_warning(
                job_id,
                "cursor_model_fallback",
                f"Cursor model '{configured_model}' is unavailable; using '{CURSOR_FALLBACK_MODEL}'.",
                source="cursor",
            )
        help_text = self._preflight_command(job_id, [str(cursor_bin), "-p", "--help"], run_dir, "cursor-agent-p-help")
        missing = [flag for flag in ("--trust", "--approve-mcps", "--force", "--workspace") if flag not in help_text]
        if missing:
            raise PreflightError(
                "preflight_failed",
                f"Cursor Agent CLI is missing required flag(s): {', '.join(missing)}.",
                details={"missingFlags": missing},
            )
        return {"cursorBin": cursor_bin, "cursorModel": resolved_model}

    def _preflight_opencode(self, job_id: str, run_dir: Path, model: str = "") -> dict[str, Any]:
        opencode_bin = self._resolve_executable("opencode", "REVIEW_TASKS_OPENCODE_BIN")
        self._preflight_command(job_id, [str(opencode_bin), "--version"], run_dir, "opencode")
        help_text = self._preflight_command(job_id, [str(opencode_bin), "run", "--help"], run_dir, "opencode-run-help")
        missing = [flag for flag in OPENCODE_REQUIRED_COMMON_RUN_FLAGS if flag not in help_text]
        if model and "--model" not in help_text:
            missing.append("--model")
        if missing:
            raise PreflightError(
                "preflight_failed",
                f"OpenCode CLI is missing required flag(s): {', '.join(missing)}.",
                details={"missingFlags": missing},
            )
        permission_approval_flag = select_opencode_permission_approval_flag(help_text)
        if permission_approval_flag is None:
            raise PreflightError(
                "preflight_failed",
                "OpenCode CLI capability preflight failed: opencode run --help does not list a supported "
                "permission approval flag (--auto or --dangerously-skip-permissions); the OpenCode CLI "
                "version may have changed.",
                details={"supportedPermissionApprovalFlags": list(OPENCODE_PERMISSION_APPROVAL_FLAGS)},
            )
        auth_code, auth_text, stdout_path, stderr_path = self._preflight_probe(
            [str(opencode_bin), "auth", "list"],
            run_dir,
            "opencode-auth-list",
        )
        if not opencode_auth_list_has_credentials(auth_code, auth_text) and not has_opencode_provider_auth_env():
            raise PreflightError(
                "preflight_failed",
                "OpenCode: нет настроенных провайдеров. Выполните `opencode auth login` "
                "или задайте API key в окружении пользователя, под которым запущен local service.",
                details={
                    "stdoutPath": str(stdout_path),
                    "stderrPath": str(stderr_path),
                    "stdoutTail": safe_tail(stdout_path),
                    "stderrTail": safe_tail(stderr_path),
                    "authEnvVars": list(OPENCODE_AUTH_ENV_VARS),
                },
            )
        return {
            "opencodeBin": opencode_bin,
            "opencodePermissionApprovalFlag": permission_approval_flag,
        }

    def _post_init_mcp_preflight(
        self, job_id: str, task_dir: Path, run_dir: Path, *, mcp_mode: str = "required"
    ) -> bool:
        if mcp_mode == "off":
            return True
        registry_path = task_mcp_registry_path(task_dir)
        try:
            servers = load_mcp_registry(registry_path)
        except IdeAdapterError as exc:
            stderr = run_dir / "preflight.mcp-registry.stderr.log"
            stderr.parent.mkdir(parents=True, exist_ok=True)
            stderr.write_text(f"{exc}\n", encoding="utf-8", newline="\n")
            self._fail(
                job_id,
                "mcp_registry_invalid",
                str(exc),
                detail_status="preflight_failed",
                details={
                    "registryPath": str(registry_path),
                    "stderrPath": str(stderr),
                    "stderrTail": safe_tail(stderr),
                },
            )
            return False

        missing = [name for name in mcp_bearer_env_var_names(servers) if not os.environ.get(name, "").strip()]
        if missing:
            stderr = run_dir / "preflight.mcp-auth-env.stderr.log"
            stderr.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                f"MCP auth env var '{name}' is missing or empty; runner will start but MCP bearer auth may fail."
                for name in missing
            ]
            stderr.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            status = self.store.read_status(job_id) or {}
            diagnostics = status.get("diagnostics") if isinstance(status.get("diagnostics"), list) else []
            self.store.update_status(
                job_id,
                diagnostics=unique_diagnostics([*diagnostics, *build_log_diagnostics(run_dir, ["mcp.auth.stderr"])]),
            )
            for name in missing:
                self._add_warning(
                    job_id,
                    "mcp_auth_env_missing",
                    f"MCP auth env var '{name}' is missing or empty; runner will start but MCP bearer auth may fail.",
                    source="mcp",
                )

        missing_url = [name for name in mcp_url_env_var_names(servers) if not os.environ.get(name, "").strip()]
        if missing_url:
            stderr = run_dir / "preflight.mcp-url-env.stderr.log"
            stderr.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                f"MCP url env var '{name}' is not set or empty; runner cannot start with an invalid MCP server URL."
                for name in missing_url
            ]
            stderr.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            self._fail(
                job_id,
                "mcp_url_env_missing",
                f"MCP url env var(s) not set: {', '.join(missing_url)}",
                detail_status="preflight_failed",
                details={
                    "missingVars": missing_url,
                    "stderrPath": str(stderr),
                    "stderrTail": safe_tail(stderr),
                },
            )
            return False

        return True

    def _resolve_executable(self, command: str, env_var: str) -> str:
        resolved = resolve_executable(command, env_var)
        if resolved is None:
            raise PreflightError(
                "preflight_failed",
                f"Command not available: {command}",
                details={"envVar": env_var, "path": os.environ.get("PATH", "")},
            )
        return resolved

    def _preflight_command(self, job_id: str, argv: list[str], run_dir: Path, name: str) -> str:
        stdout = run_dir / f"preflight.{name}.stdout.log"
        stderr = run_dir / f"preflight.{name}.stderr.log"
        try:
            result = self.backend.run(
                argv,
                cwd=run_dir,
                env=controlled_env(),
                stdout_path=stdout,
                stderr_path=stderr,
                timeout=30,
            )
        except OSError as exc:
            raise PreflightError("preflight_failed", f"Command not available: {argv[0]}") from exc
        if result.returncode != 0:
            raise PreflightError(
                "preflight_failed",
                f"Preflight command failed: {' '.join(argv)}",
                details={"stderrPath": str(stderr), "stderrTail": safe_tail(stderr)},
            )
        return "\n".join(part for part in (read_text_safe(stdout), read_text_safe(stderr)) if part)

    def _preflight_probe(self, argv: list[str], run_dir: Path, name: str) -> tuple[int, str, Path, Path]:
        stdout = run_dir / f"preflight.{name}.stdout.log"
        stderr = run_dir / f"preflight.{name}.stderr.log"
        try:
            result = self.backend.run(
                argv,
                cwd=run_dir,
                env=controlled_env(),
                stdout_path=stdout,
                stderr_path=stderr,
                timeout=30,
            )
        except OSError:
            return 127, "", stdout, stderr
        text = "\n".join(part for part in (read_text_safe(stdout), read_text_safe(stderr)) if part)
        return result.returncode, text, stdout, stderr

    def _run_codex(
        self,
        job_id: str,
        request: dict[str, Any],
        task_dir: Path,
        run_dir: Path,
        codex_bin: str,
    ) -> None:
        self._check_cancel(job_id)
        final_path = run_dir / "codex.final.md"
        stdout = run_dir / "codex.stdout.log"
        stderr = run_dir / "codex.stderr.log"
        argv = build_codex_argv(task_dir, run_dir, executable=codex_bin)
        self.store.update_status(
            job_id,
            status="review_starting",
            detailStatus="review_starting",
            detailText=DETAIL_TEXT["review_starting"],
            availableActions=["cancel"],
            codexArgv=argv,
        )
        log_agent_start(request["issueKey"], "codex", argv)
        process = self.backend.start(argv, cwd=task_dir, env=controlled_env(), stdout_path=stdout, stderr_path=stderr)
        with self._processes_lock:
            self._processes[job_id] = process
        write_json(
            run_dir / "pid.json",
            {
                "jobId": job_id,
                "launcherPid": process.pid,
                "childPids": [],
                "commandMarker": f"codex-review-task-{request['issueKey']}-{job_id}",
                "startedAt": now_iso(),
            },
        )
        self.store.update_status(
            job_id,
            status="review_running",
            detailStatus="review_running",
            detailText=DETAIL_TEXT["review_running"],
            availableActions=["cancel", "open_agent_log", "open_review_bsl"],
        )
        started = time.monotonic()
        try:
            returncode = self._wait_process(job_id, process, run_dir, stdout, stderr, final_path, request["timeoutSec"], started)
        finally:
            with self._processes_lock:
                self._processes.pop(job_id, None)
        current_status = self.store.read_status(job_id) or {}
        if current_status.get("detailStatus") in _EXTERNALLY_FINALIZED_DETAIL_STATUSES:
            self._log_agent_run_end(request["issueKey"], "codex", returncode, job_id)
            return
        if returncode != 0:
            self._log_agent_run_end(request["issueKey"], "codex", returncode, job_id, outcome="failed")
            self._fail(
                job_id,
                "review_failed",
                f"Codex exited with code {returncode}.",
                detail_status="review_failed",
                details={"stderrPath": str(stderr), "stderrTail": safe_tail(stderr), "finalPath": str(final_path)},
            )
            return
        self._log_agent_run_end(request["issueKey"], "codex", returncode, job_id, outcome="ok")
        self._postprocess(job_id, request, task_dir, run_dir, final_path, runner="codex")

    def _run_cursor(
        self,
        job_id: str,
        request: dict[str, Any],
        task_dir: Path,
        run_dir: Path,
        cursor_bin: str,
        cursor_model: str,
    ) -> None:
        self._check_cancel(job_id)
        stdout = run_dir / "cursor.stdout.log"
        stderr = run_dir / "cursor.stderr.log"
        argv = build_cursor_argv(task_dir, run_dir, executable=cursor_bin, model=cursor_model)
        self.store.update_status(
            job_id,
            status="review_starting",
            detailStatus="review_starting",
            detailText=DETAIL_TEXT["review_starting"],
            availableActions=["cancel"],
            cursorArgv=argv,
        )
        log_agent_start(request["issueKey"], "cursor", argv)
        process = self.backend.start(argv, cwd=task_dir, env=controlled_env(), stdout_path=stdout, stderr_path=stderr)
        with self._processes_lock:
            self._processes[job_id] = process
        write_json(
            run_dir / "pid.json",
            {
                "jobId": job_id,
                "launcherPid": process.pid,
                "childPids": [],
                "commandMarker": f"cursor-review-task-{request['issueKey']}-{job_id}",
                "startedAt": now_iso(),
            },
        )
        self.store.update_status(
            job_id,
            status="review_running",
            detailStatus="review_running",
            detailText=DETAIL_TEXT["review_running"],
            availableActions=["cancel", "open_agent_log", "open_review_bsl"],
        )
        started = time.monotonic()
        try:
            returncode = self._wait_process(job_id, process, run_dir, stdout, stderr, None, request["timeoutSec"], started)
        finally:
            with self._processes_lock:
                self._processes.pop(job_id, None)
        current_status = self.store.read_status(job_id) or {}
        if current_status.get("detailStatus") in _EXTERNALLY_FINALIZED_DETAIL_STATUSES:
            self._log_agent_run_end(request["issueKey"], "cursor", returncode, job_id)
            return
        if returncode != 0:
            self._log_agent_run_end(request["issueKey"], "cursor", returncode, job_id, outcome="failed")
            self._fail(
                job_id,
                "review_failed",
                f"Cursor Agent exited with code {returncode}.",
                detail_status="review_failed",
                details={"stderrPath": str(stderr), "stderrTail": safe_tail(stderr), "stdoutPath": str(stdout)},
            )
            return
        self._log_agent_run_end(request["issueKey"], "cursor", returncode, job_id, outcome="ok")
        for warning in collect_cursor_run_warnings(stdout, stderr):
            self._add_warning(job_id, warning["code"], warning["message"], source=warning.get("source"))
        self._postprocess(job_id, request, task_dir, run_dir, None, runner="cursor")

    def _run_opencode(
        self,
        job_id: str,
        request: dict[str, Any],
        task_dir: Path,
        run_dir: Path,
        opencode_bin: str,
        permission_approval_flag: str,
        model: str = "",
    ) -> None:
        self._check_cancel(job_id)
        stdout = run_dir / "opencode.stdout.log"
        stderr = run_dir / "opencode.stderr.log"
        argv = build_opencode_argv(
            task_dir,
            run_dir,
            executable=opencode_bin,
            permission_approval_flag=permission_approval_flag,
            model=model,
        )
        self.store.update_status(
            job_id,
            status="review_starting",
            detailStatus="review_starting",
            detailText=DETAIL_TEXT["review_starting"],
            availableActions=["cancel"],
            opencodeArgv=argv,
        )
        log_agent_start(request["issueKey"], "opencode", argv)
        process = self.backend.start(argv, cwd=task_dir, env=controlled_env(), stdout_path=stdout, stderr_path=stderr)
        with self._processes_lock:
            self._processes[job_id] = process
        write_json(
            run_dir / "pid.json",
            {
                "jobId": job_id,
                "launcherPid": process.pid,
                "childPids": [],
                "commandMarker": f"opencode-review-task-{request['issueKey']}-{job_id}",
                "startedAt": now_iso(),
            },
        )
        self.store.update_status(
            job_id,
            status="review_running",
            detailStatus="review_running",
            detailText=DETAIL_TEXT["review_running"],
            availableActions=["cancel", "open_agent_log", "open_review_bsl"],
        )
        started = time.monotonic()
        try:
            returncode = self._wait_process(job_id, process, run_dir, stdout, stderr, None, request["timeoutSec"], started)
        finally:
            with self._processes_lock:
                self._processes.pop(job_id, None)
        current_status = self.store.read_status(job_id) or {}
        if current_status.get("detailStatus") in _EXTERNALLY_FINALIZED_DETAIL_STATUSES:
            self._log_agent_run_end(request["issueKey"], "opencode", returncode, job_id)
            return
        if returncode != 0:
            self._log_agent_run_end(request["issueKey"], "opencode", returncode, job_id, outcome="failed")
            self._fail(
                job_id,
                "review_failed",
                f"OpenCode exited with code {returncode}.",
                detail_status="review_failed",
                details={
                    "stderrPath": str(stderr),
                    "stderrTail": safe_tail(stderr),
                    "stdoutPath": str(stdout),
                    "stdoutTail": safe_tail(stdout),
                    "opencodeStderrTail": safe_tail(stderr),
                    "opencodeStdoutTail": safe_tail(stdout),
                },
            )
            return
        self._log_agent_run_end(request["issueKey"], "opencode", returncode, job_id, outcome="ok")
        self._postprocess(job_id, request, task_dir, run_dir, None, runner="opencode")

    def _wait_process(
        self,
        job_id: str,
        process: ProcessLike,
        run_dir: Path,
        stdout: Path,
        stderr: Path,
        final_path: Path | None,
        timeout_sec: int,
        started: float,
    ) -> int:
        while True:
            self._check_cancel(job_id, process)
            returncode = process.poll()
            update_heartbeat(job_id, run_dir, process, stdout, stderr, final_path)
            if returncode is not None:
                return returncode
            if time.monotonic() - started > timeout_sec:
                terminate_process_tree(process)
                issue_key = str((self.store.read_status(job_id) or {}).get("issueKey") or job_id)
                log_error(issue_key, "review timeout exceeded")
                self.store.update_status(
                    job_id,
                    status="timeout",
                    detailStatus="timeout",
                    detailText=DETAIL_TEXT["timeout"],
                    availableActions=["retry", "refresh"],
                    finishedAt=now_iso(),
                    error={"code": "timeout", "message": "Review process exceeded timeoutSec."},
                )
                return 124
            time.sleep(self.poll_interval)

    def _postprocess(
        self,
        job_id: str,
        request: dict[str, Any],
        task_dir: Path,
        run_dir: Path,
        final_path: Path | None,
        *,
        runner: str,
    ) -> None:
        self.store.update_status(
            job_id,
            status="postprocess_running",
            detailStatus="postprocess_running",
            detailText=DETAIL_TEXT["postprocess_running"],
            availableActions=["cancel"],
        )
        if runner in {"cursor", "opencode"}:
            stdout_log = run_dir / f"{runner}.stdout.log"
            if not stdout_log.exists():
                self._fail(job_id, "review_failed", "runner stdout log is missing.", detail_status="review_failed")
                return
        elif final_path is None or not final_path.exists():
            self._fail(job_id, "review_failed", "runner final output is missing.", detail_status="review_failed")
            return
        review_info = task_dir / "_review_info"
        yaml_paths = expand_review_result_inputs([review_info])
        if not yaml_paths:
            self._fail(job_id, "review_failed", "review_result.yaml is missing.", detail_status="review_failed")
            return
        report_path = find_report_path(task_dir, request["issueKey"])
        if report_path is None:
            self._fail(job_id, "review_failed", "Rendered Markdown report is missing.", detail_status="review_failed")
            return
        meta_path = review_info / "meta.txt"
        if not has_completed_result_status(read_text_safe(meta_path)):
            self._fail(job_id, "review_failed", "meta.txt is not marked as completed.", detail_status="review_failed")
            return
        status = self.store.read_status(job_id) or {}
        summary = build_summary(job_id, request["issueKey"], yaml_paths, report_path, status, runner=runner)
        write_json(run_dir / "review_summary.json", summary)
        self.store.update_status(
            job_id,
            status="done",
            detailStatus="done",
            detailText=DETAIL_TEXT["done"],
            availableActions=["open_report", "open_reasoning", "open_agent_log", "open_review_bsl", "refresh"],
            finishedAt=now_iso(),
            reportPath=str(report_path),
            reasoningPath=str(yaml_paths[0]),
            summary=summary,
            tokenUsage=summary.get("tokenUsage"),
            warnings=summary.get("warnings", []),
            error=None,
        )

    def _check_cancel(self, job_id: str, process: ProcessLike | None = None) -> None:
        status = self.store.read_status(job_id)
        if status and status.get("cancelRequested"):
            if process is not None:
                terminate_process_tree(process)
            raise PreflightError("cancelled", "Review job cancelled by user.")

    def _log_agent_run_end(
        self,
        issue_key: str,
        ide: str,
        exit_code: int,
        job_id: str,
        *,
        outcome: str | None = None,
    ) -> None:
        if outcome is None:
            status = self.store.read_status(job_id) or {}
            detail_status = str(status.get("detailStatus") or "")
            if detail_status in _EXTERNALLY_FINALIZED_DETAIL_STATUSES:
                outcome = detail_status
            elif exit_code == 124:
                outcome = "timeout"
            elif exit_code != 0:
                outcome = "failed"
            else:
                outcome = "ok"
        log_agent_end(issue_key, ide, exit_code, outcome)

    def _fail(
        self,
        job_id: str,
        code: str,
        message: str,
        *,
        detail_status: str,
        details: dict[str, Any] | None = None,
        diagnostic_entries: list[dict[str, Any] | None] | None = None,
    ) -> None:
        status = self.store.read_status(job_id)
        if status is None:
            return
        issue_key = str(status.get("issueKey") or job_id)
        if detail_status != "cancelled":
            log_error(issue_key, f"{code}: {message}")
        run_dir = Path(str(status.get("runDir") or self.store.jobs_dir / job_id))
        warnings = status.get("warnings") if isinstance(status.get("warnings"), list) else []
        error = {"code": code, "message": message}
        if details:
            error.update(
                {
                    key: value
                    for key, value in details.items()
                    if not key.casefold().endswith("tail")
                    and key not in {"stderrPath", "stdoutPath", "finalPath", "registryPath", "taskDir", "path"}
                }
            )
        feedback = build_feedback(code, detail_status=detail_status)
        supplied_entries = list(diagnostic_entries or ())
        reason_is_already_present = any(
            isinstance(entry, dict)
            and entry.get("kind") == "text"
            and entry.get("value") == message
            for entry in supplied_entries
        )
        normalized_reason = message.strip().casefold().rstrip(".!:;")
        reason_is_feedback = normalized_reason in {
            feedback["title"].strip().casefold().rstrip(".!:;"),
            feedback["message"].strip().casefold().rstrip(".!:;"),
        }
        if not reason_is_already_present and not reason_is_feedback:
            supplied_entries.append(
                diagnostic_entry(
                    "error.reason",
                    "Причина ошибки",
                    "text",
                    value=message,
                )
            )
        entries = build_detail_diagnostics(
            details,
            supplied_entries if diagnostic_entries is not None else [
                *self._diagnostics_for_status(run_dir, detail_status),
                *supplied_entries,
            ],
        )
        self.store.update_status(
            job_id,
            status=detail_status,
            detailStatus=detail_status,
            detailText=DETAIL_TEXT.get(detail_status, message),
            availableActions=actions_for(detail_status),
            finishedAt=now_iso(),
            error=error,
            feedback=feedback,
            warnings=warnings,
            diagnostics=entries,
        )

    @staticmethod
    def _diagnostics_for_status(run_dir: Path, detail_status: str) -> list[dict[str, Any]]:
        if detail_status == "init_failed":
            refs = ["init.stderr"]
        elif detail_status == "review_failed":
            refs = ["codex.stderr", "codex.final", "cursor.stderr", "cursor.stdout", "opencode.stderr", "opencode.stdout"]
        elif detail_status == "preflight_failed":
            refs = ["mcp.registry.stderr", "mcp.auth.stderr", "mcp.url.stderr"]
        else:
            refs = []
        return build_log_diagnostics(run_dir, refs)

    def _complete_already_done(
        self,
        job_id: str,
        request: dict[str, Any],
        task_dir: Path,
    ) -> None:
        report_path = find_report_path(task_dir, request["issueKey"])
        yaml_paths = expand_review_result_inputs([task_dir / "_review_info"])
        self.store.update_status(
            job_id,
            status="already_done",
            detailStatus="already_done",
            detailText=DETAIL_TEXT["already_done"],
            availableActions=actions_for(
                "already_done",
                report_available=report_path is not None,
                reasoning_available=bool(yaml_paths),
            ),
            finishedAt=now_iso(),
            taskDir=str(task_dir),
            reportPath=str(report_path) if report_path is not None else None,
            reasoningPath=str(yaml_paths[0]) if yaml_paths else None,
            feedback=build_feedback("already_done", detail_status="already_done"),
            diagnostics=[],
            error=None,
        )

    def _complete_expected_stop(self, job_id: str, code: str) -> None:
        self.store.update_status(
            job_id,
            status=code,
            detailStatus=code,
            detailText=DETAIL_TEXT[code],
            availableActions=actions_for(code),
            finishedAt=now_iso(),
            feedback=build_feedback(code, detail_status=code),
            diagnostics=[],
            error=None,
        )

    def _run_dir(self, job_id: str) -> Path:
        status = self.store.read_status(job_id)
        if status is None:
            return self.store.jobs_dir / job_id
        return Path(str(status["runDir"]))

    def _add_warning(self, job_id: str, code: str, message: str, *, source: str | None = None) -> None:
        status = self.store.read_status(job_id)
        if status is None:
            return
        warnings = status.get("warnings") if isinstance(status.get("warnings"), list) else []
        item: dict[str, str] = {"code": code, "message": message}
        if source:
            item["source"] = source
        if not any(existing.get("code") == code and existing.get("message") == message for existing in warnings if isinstance(existing, dict)):
            self.store.update_status(job_id, warnings=[*warnings, item])


def controlled_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["NO_COLOR"] = "1"
    return env


def resolve_init_config(request: dict[str, Any]) -> dict[str, Any]:
    init = dict(request.get("init") or {})
    config = prefix_config.load_prefix_config(prefix_config.get_config_path())
    saved = config.get(request["projectKey"])
    if not isinstance(saved, dict):
        saved = {}
    opencode_model = ""
    if request["ide"] == "opencode":
        try:
            opencode_model = normalize_opencode_model(saved.get("opencode_model", ""))
        except ApiError as exc:
            raise PreflightError("preflight_failed", f"OpenCode project config: {exc}") from exc
    saved_repositories = prefix_config.entry_repositories(saved)
    repo_path = init.get("repoPath") or prefix_config.repositories_to_api_value(saved_repositories)
    reviews_root = init.get("reviewsRoot") or saved.get("reviews_dir")
    branch = init.get("branch") or saved.get("branch") or "origin/master"
    remote = init.get("remote") or saved.get("remote") or "origin"
    missing = []
    if not prefix_config.split_repository_list(repo_path):
        missing.append("repoPath")
    if not reviews_root:
        missing.append("reviewsRoot")
    if missing:
        raise PreflightError("config_missing", "Prefix config is missing required fields.", details={"missing": missing})
    return {
        "repoPath": str(repo_path),
        "reviewsRoot": str(reviews_root),
        "branch": str(branch),
        "remote": str(remote),
        "mcpMode": "required" if saved.get("mcp_enabled", True) else "off",
        "opencodeModel": opencode_model,
    }


def resolve_executable(command: str, env_var: str) -> str | None:
    override = os.environ.get(env_var)
    candidates = [override] if override else []
    candidates.append(command)
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_absolute() or path.parent != Path("."):
            if path.exists():
                return str(path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def build_init_argv(
    request: dict[str, Any],
    *,
    resolved_init: dict[str, Any] | None = None,
    executable: str = "review-tasks",
    result_path: Path | None = None,
) -> list[str]:
    argv = [executable, "init", "-t", request["issueKey"], "--ide", request["ide"]]
    argv.extend(["--mcp", (resolved_init or {}).get("mcpMode", "required")])
    init = dict(request.get("init") or {})
    if resolved_init is not None:
        for field in ("repoPath", "reviewsRoot", "branch", "remote", "reviewDirName", "configPath"):
            if field in resolved_init and resolved_init[field] is not None:
                init[field] = resolved_init[field]
    mapping = (
        ("repoPath", "-r"),
        ("reviewsRoot", "-d"),
        ("branch", "-b"),
        ("remote", "--remote"),
        ("mrId", "--mr"),
        ("reviewDirName", "--review-dir-name"),
        ("configPath", "--config"),
    )
    for field, flag in mapping:
        if field in init and init[field] is not None:
            value = str(init[field])
            if field == "repoPath":
                value = prefix_config.repositories_to_cli_arg(prefix_config.split_repository_list(value))
            argv.extend([flag, value])
    if init.get("mrFromTasks"):
        argv.append("--mr-from-tasks")
    if request.get("force"):
        argv.append("--force")
    if request.get("userInstruction") is not None:
        argv.extend(["--user-instruction", str(request["userInstruction"])])
    if result_path is not None:
        argv.extend(["--result-path", str(result_path), "--result-schema-version", "2"])
    return argv


REDACTED_ARGUMENT = "<redacted>"


def redact_init_argv(argv: list[str]) -> list[str]:
    """Return a diagnostic argv copy with user instruction values redacted."""
    result = list(argv)
    for index, item in enumerate(result[:-1]):
        if item == "--user-instruction":
            result[index + 1] = REDACTED_ARGUMENT
    return result


def build_codex_argv(task_dir: Path, run_dir: Path, *, executable: str = "codex") -> list[str]:
    return [
        executable,
        "-a",
        "never",
        "-s",
        "danger-full-access",
        "-C",
        str(task_dir),
        "exec",
        *CODEX_SHELL_SNAPSHOT_DISABLE_ARGS,
        "--skip-git-repo-check",
        "--output-last-message",
        str(run_dir / "codex.final.md"),
        "/review-start-task",
    ]


def build_cursor_argv(
    task_dir: Path,
    run_dir: Path,
    *,
    executable: str = "agent",
    model: str = CURSOR_DEFAULT_MODEL,
) -> list[str]:
    del run_dir
    return [
        executable,
        "-p",
        "--trust",
        "--approve-mcps",
        "--force",
        "--workspace",
        str(task_dir),
        "--model",
        model,
        "--output-format",
        "text",
        CURSOR_REVIEW_PROMPT,
    ]


def build_opencode_argv(
    task_dir: Path,
    run_dir: Path,
    *,
    executable: str = "opencode",
    permission_approval_flag: str,
    model: str = "",
) -> list[str]:
    del run_dir
    argv = [
        executable,
        "run",
        "--dir",
        str(task_dir),
        "--command",
        OPENCODE_REVIEW_COMMAND,
        permission_approval_flag,
    ]
    if model:
        argv.extend(["--model", model])
    return argv


def select_opencode_permission_approval_flag(help_text: str) -> str | None:
    return next((flag for flag in OPENCODE_PERMISSION_APPROVAL_FLAGS if flag in help_text), None)


def build_manual_runner_command(ide: str, task_dir: Path, *, opencode_model: str = "") -> str:
    if ide == "cursor":
        model = os.environ.get("REVIEW_TASKS_CURSOR_MODEL", "").strip() or CURSOR_DEFAULT_MODEL
        return (
            f'agent -p --trust --approve-mcps --force --workspace "{task_dir}" '
            f'--model {model} --output-format text "{CURSOR_REVIEW_PROMPT}"'
        )
    if ide == "opencode":
        if opencode_model:
            argv = ["opencode", "run", "--dir", str(task_dir), "--command", OPENCODE_REVIEW_COMMAND,
                    "--model", opencode_model]
            if os.name == "nt":
                return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
            return shlex.join(argv)
        return f'opencode run --dir "{task_dir}" --command {OPENCODE_REVIEW_COMMAND}'
    return subprocess.list2cmdline([
        "codex",
        "-a",
        "never",
        "-s",
        "danger-full-access",
        "-C",
        str(task_dir),
        "exec",
        *CODEX_SHELL_SNAPSHOT_DISABLE_ARGS,
        "--skip-git-repo-check",
        "/review-start-task",
    ])


def task_mcp_registry_path(task_dir: Path) -> Path:
    return task_dir / ".review-tasks" / "mcp" / "registry.json"


def detect_task_dir(stdout_path: Path, issue_key: str, resolved_init: dict[str, Any]) -> Path:
    stdout_text = read_text_safe(stdout_path)
    matches = re.findall(r"(?m)^Каталог ревью:\s*(.+?)\s*$", stdout_text)
    if matches:
        return Path(matches[-1].strip()).resolve()
    return (Path(str(resolved_init["reviewsRoot"])) / str(resolved_init.get("reviewDirName") or sanitize_task(issue_key))).resolve()


def verify_init_artifacts(task_dir: Path, *, ide: str, mcp_mode: str = "required") -> str | None:
    if not (task_dir / "AGENTS.md").is_file():
        return "Task AGENTS.md is missing."
    review_info = task_dir / "_review_info"
    if not (review_info / "meta.txt").is_file():
        return "_review_info/meta.txt is missing."
    staged_prompts = (
        "review_prompt_stage1.md",
        "review_prompt_stage2.md",
        "review_bsl.md",
    )
    for name in staged_prompts:
        if not (review_info / name).is_file():
            return f"Staged review artifact is missing: {name}."
    if (review_info / "review_prompt.md").is_file():
        return "Legacy review_prompt.md must not exist in staged catalogs."
    for path in review_info.glob("review_prompt_*.md"):
        if re.fullmatch(r"review_prompt_\d{3}\.md", path.name):
            return "Indexed review prompt files must not exist in staged catalogs."
    for path in review_info.glob("review_bsl*.md"):
        if path.name != "review_bsl.md" and re.fullmatch(r"review_bsl_\d{3}\.md", path.name):
            return "Indexed BSL artifacts must not exist in staged catalogs."
    if mcp_mode != "off" and ide == "cursor" and not (task_dir / ".cursor" / "mcp.json").is_file():
        return "Task .cursor/mcp.json is missing."
    if mcp_mode != "off" and ide == "codex" and not (task_dir / ".codex" / "config.toml").is_file():
        return "Task .codex/config.toml is missing."
    if ide == "opencode":
        if mcp_mode != "off" and not (task_dir / "opencode.json").is_file():
            return "Task opencode.json is missing."
        command_path = task_dir / ".opencode" / "commands" / f"{OPENCODE_REVIEW_COMMAND}.md"
        if not command_path.is_file():
            return f"Task .opencode/commands/{OPENCODE_REVIEW_COMMAND}.md is missing."
    return None


def find_report_path(task_dir: Path, issue_key: str) -> Path | None:
    preferred = task_dir / f"review-report-{task_to_kebab(issue_key)}.md"
    if preferred.is_file():
        return preferred
    reports = sorted(task_dir.glob("review-report-*.md"))
    return reports[0] if reports else None


def build_summary(
    job_id: str,
    issue_key: str,
    yaml_paths: list[Path],
    report_path: Path,
    status: dict[str, Any],
    *,
    runner: str,
) -> dict[str, Any]:
    documents = read_review_yaml_documents(yaml_paths)
    severity_counts = {"critical": 0, "important": 0, "desirable": 0}
    summary_blocks: list[str] = []
    for doc in documents:
        summary_text = doc.parsed.summary.strip()
        if summary_text:
            summary_blocks.append(summary_text)
        for finding in doc.parsed.findings:
            if finding.self_check.decision != "keep" or finding.severity is None:
                continue
            severity = finding.severity
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
    run_dir = Path(str(status.get("runDir") or ""))
    if runner == "cursor":
        token_usage = extract_cursor_token_usage(run_dir / "cursor.stderr.log", run_dir / "cursor.stdout.log")
    elif runner == "opencode":
        token_usage = extract_opencode_token_usage(run_dir / "opencode.stdout.log", run_dir / "opencode.stderr.log")
    else:
        token_usage = extract_token_usage(run_dir / "codex.stderr.log")
    warnings = status.get("warnings") if isinstance(status.get("warnings"), list) else []
    return {
        "jobId": job_id,
        "issueKey": issue_key,
        "status": "done",
        "summaryText": "\n\n".join(summary_blocks) if summary_blocks else None,
        "criticalCount": severity_counts["critical"],
        "majorCount": severity_counts["important"],
        "minorCount": severity_counts["desirable"],
        "hasBlockingIssues": severity_counts["critical"] > 0,
        "reportPath": str(report_path),
        "resultYamlPath": str(yaml_paths[0]) if yaml_paths else None,
        "startedAt": status.get("startedAt"),
        "finishedAt": now_iso(),
        "durationSec": status_to_popup(status).get("durationSec"),
        "tokenUsage": token_usage,
        "warnings": warnings,
    }


def extract_token_usage(stderr_path: Path) -> int | None:
    if not stderr_path.exists():
        return None
    text = read_text_safe(stderr_path)
    matches = re.findall(r"(?im)^\s*tokens used\s*$\s*^([0-9][0-9\s\u00a0.,]*)\s*$", text)
    if not matches:
        return None
    raw = matches[-1]
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else None


def extract_cursor_token_usage(stderr_path: Path, stdout_path: Path) -> int | None:
    del stderr_path, stdout_path
    return None


def extract_opencode_token_usage(stdout_path: Path, stderr_path: Path) -> int | None:
    # v1 accepts only JSON/JSONL machine-readable usage events from OpenCode logs.
    matches: list[int] = []
    for path in (stdout_path, stderr_path):
        if not path.exists():
            continue
        for line in read_text_safe(path).splitlines():
            stripped = line.strip()
            if not stripped.startswith("{") or not stripped.endswith("}"):
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            value = _token_usage_from_json_payload(payload)
            if value is not None:
                matches.append(value)
    return matches[-1] if matches else None


def _token_usage_from_json_payload(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("tokenUsage", "token_usage", "usage"):
        if key in payload:
            value = _token_usage_value(payload[key])
            if value is not None:
                return value
    return None


def _token_usage_value(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if not isinstance(value, dict):
        return None
    for key in ("total", "totalTokens", "total_tokens", "tokens"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            return item
    input_tokens = value.get("input_tokens", value.get("inputTokens"))
    output_tokens = value.get("output_tokens", value.get("outputTokens"))
    if (
        isinstance(input_tokens, int)
        and not isinstance(input_tokens, bool)
        and input_tokens >= 0
        and isinstance(output_tokens, int)
        and not isinstance(output_tokens, bool)
        and output_tokens >= 0
    ):
        return input_tokens + output_tokens
    return None


def opencode_auth_list_has_credentials(returncode: int, text: str) -> bool:
    if returncode != 0:
        return False
    matches = re.findall(r"(?i)(\d+)\s+credentials", text)
    return bool(matches and int(matches[-1]) > 0)


def has_opencode_provider_auth_env() -> bool:
    return any(os.environ.get(name, "").strip() for name in OPENCODE_AUTH_ENV_VARS)


def collect_cursor_run_warnings(stdout_path: Path, stderr_path: Path) -> list[dict[str, str]]:
    text = "\n".join(read_text_safe(path) for path in (stdout_path, stderr_path) if path.exists())
    lowered = text.lower()
    warnings: list[dict[str, str]] = []
    if "warning" in lowered or "warn:" in lowered:
        warnings.append({
            "code": "cursor_agent_warning",
            "message": "Cursor Agent reported warnings in run logs.",
            "source": "cursor",
        })
    if "mcp" in lowered and any(marker in lowered for marker in ("degraded", "unverified", "needs approval", "approval")):
        warnings.append({
            "code": "cursor_mcp_degraded",
            "message": "Cursor Agent reported degraded MCP behavior.",
            "source": "cursor",
        })
    return warnings


def update_heartbeat(
    job_id: str,
    run_dir: Path,
    process: ProcessLike,
    stdout: Path,
    stderr: Path,
    final_path: Path | None,
) -> None:
    observed = now_iso()
    last_write = None
    tracked_paths = [stdout, stderr]
    if final_path is not None:
        tracked_paths.append(final_path)
    for path in tracked_paths:
        if path.exists():
            mtime = path.stat().st_mtime
            if last_write is None or mtime > last_write:
                last_write = mtime
    write_json(
        run_dir / "heartbeat.json",
        {
            "jobId": job_id,
            "observedAt": observed,
            "processAlive": process.poll() is None,
            "lastLogWriteAt": datetime_from_timestamp(last_write) if last_write else None,
            "stdoutBytes": stdout.stat().st_size if stdout.exists() else 0,
            "stderrBytes": stderr.stat().st_size if stderr.exists() else 0,
            "finalExists": final_path.exists() if final_path is not None else False,
        },
    )


def datetime_from_timestamp(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(value))


def _runner_log_paths(run_dir: Path, ide: str) -> dict[str, Path | None]:
    if ide == "cursor":
        return {"stdout": run_dir / "cursor.stdout.log", "stderr": run_dir / "cursor.stderr.log", "final": None}
    if ide == "opencode":
        return {"stdout": run_dir / "opencode.stdout.log", "stderr": run_dir / "opencode.stderr.log", "final": None}
    return {"stdout": run_dir / "codex.stdout.log", "stderr": run_dir / "codex.stderr.log", "final": run_dir / "codex.final.md"}


def terminate_process_tree(process: ProcessLike) -> None:
    pid = getattr(process, "pid", None)
    if pid and sys.platform.startswith("win"):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        process.terminate()
    except Exception:
        pass
    time.sleep(0.2)
    try:
        if process.poll() is None:
            process.kill()
    except Exception:
        pass
