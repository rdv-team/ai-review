from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from review_tasks.review_init import prefix_config
from review_tasks.task_capsule.user_instruction import UserInstructionError, validate_user_instruction
from .gitlab import (
    normalize_gitlab_project_path,
    parse_gitlab_mr_url as parse_gitlab_mr_url_ref,
)

ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
SAFE_INDEX_RE = re.compile(r"[^A-Za-z0-9_.-]+")
ALLOWED_IDES = {"codex", "cursor", "opencode"}
ALLOWED_IDES_TEXT = "codex, cursor or opencode"

ALLOWED_REVIEW_FIELDS = {
    "issueKey",
    "projectKey",
    "ide",
    "timeoutSec",
    "force",
    "userInstruction",
    "init",
    "runner",
    "jira",
}
ALLOWED_INIT_FIELDS = {
    "repoPath",
    "reviewsRoot",
    "branch",
    "remote",
    "mrId",
    "mrUrl",
    "gitlabProjectPath",
    "gitlabInstanceId",
    "mrTitle",
    "mrFromTasks",
    "configPath",
}
ALLOWED_RUNNER_FIELDS = {"mode", "codexPrompt"}
ALLOWED_JIRA_FIELDS = {"url", "title", "description", "comments"}
FORBIDDEN_FIELD_NAMES = {
    "prompt",
    "customPrompt",
    "model",
    "agent",
    "apiKey",
    "api_key",
    "Authorization",
    "authorization",
    "bearerToken",
    "oauth",
    "providerHeaders",
    "permissionJson",
    "permissions",
    "cliFlags",
    "flags",
    "sandbox",
    "args",
    "argv",
    "command",
    "commandArgs",
    "workingDirectory",
    "cwd",
    "addDir",
    "--add-dir",
    "gitPush",
    "publish",
}

ACTIVE_DETAIL_STATUSES = {
    "queued",
    "preflight_running",
    "init_running",
    "init_done",
    "review_starting",
    "review_running",
    "postprocess_running",
}
DONE_DETAIL_STATUSES = {"done", "done_with_warnings", "already_done", "summary_unavailable"}
FAILED_DETAIL_STATUSES = {
    "previous_failed",
    "init_failed",
    "review_failed",
    "timeout",
    "cancelled",
    "previous_cancelled",
    "interrupted",
}

DETAIL_TEXT = {
    "service_unavailable": "Локальный сервис не запущен",
    "not_jira_issue": "Открыта не JIRA-задача",
    "issue_detected": "Задача найдена",
    "config_missing": "Нужно заполнить prefix config",
    "config_ready": "Готово к ревью",
    "previous_failed": "Предыдущий запуск завершился ошибкой",
    "previous_cancelled": "Предыдущий запуск отменен",
    "queued": "В очереди",
    "preflight_running": "Проверяем окружение",
    "init_running": "Готовим каталог ревью",
    "init_done": "Каталог ревью подготовлен",
    "review_starting": "Запускаем агента",
    "review_running": "Агент выполняет ревью",
    "postprocess_running": "Проверяем результат",
    "handoff_required": "Каталог подготовлен, автоматический запуск не выполнялся",
    "done": "Ревью выполнено",
    "done_with_warnings": "Ревью выполнено с предупреждениями",
    "already_done": "Ревью уже было выполнено",
    "no_commits": "Нет новых коммитов для ревью",
    "no_reviewable_files": "Нет подходящих файлов для ревью",
    "summary_unavailable": "Отчет есть, сводка недоступна",
    "init_failed": "Инициализация не выполнена",
    "review_failed": "Ревью завершилось ошибкой",
    "timeout": "Превышен timeout",
    "cancelled": "Запуск отменен",
    "interrupted": "Запуск прерван",
    "already_running": "Ревью уже выполняется",
}

DETAIL_ACTIONS = {
    "service_unavailable": ["refresh"],
    "not_jira_issue": [],
    "issue_detected": ["refresh"],
    "config_missing": ["save_config"],
    "config_ready": ["submit", "refresh"],
    "previous_failed": ["retry", "refresh"],
    "previous_cancelled": ["submit", "refresh"],
    "queued": ["cancel"],
    "preflight_running": ["cancel"],
    "init_running": ["cancel"],
    "init_done": ["cancel"],
    "review_starting": ["cancel"],
    "review_running": ["cancel"],
    "postprocess_running": ["cancel"],
    "handoff_required": ["submit", "refresh"],
    "done": ["open_report", "open_reasoning", "refresh"],
    "done_with_warnings": ["open_report", "open_reasoning", "refresh"],
    "already_done": ["open_report", "open_reasoning", "retry", "refresh"],
    "no_commits": ["retry", "refresh"],
    "no_reviewable_files": ["retry", "refresh"],
    "summary_unavailable": ["open_report", "open_reasoning", "refresh"],
    "init_failed": ["retry", "refresh"],
    "review_failed": ["retry", "refresh"],
    "timeout": ["retry", "refresh"],
    "cancelled": ["submit", "refresh"],
    "interrupted": ["retry", "refresh"],
    "already_running": ["cancel", "refresh"],
}


class ApiError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


def package_version() -> str:
    try:
        return version("review-tasks")
    except PackageNotFoundError:
        return "1.1.1"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_index_name(value: str) -> str:
    cleaned = SAFE_INDEX_RE.sub("_", value.upper()).strip("._")
    return cleaned or "unknown"


def structured_error(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"ok": False, "error": error}


def api_error_response(error: ApiError) -> dict[str, Any]:
    return structured_error(error.code, str(error), details=error.details)


def require_object(value: Any, code: str = "invalid_request") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApiError(code, "JSON body must be an object.")
    return value


def reject_forbidden_fields(payload: dict[str, Any], allowed: set[str], *, path: str) -> None:
    unknown = sorted(set(payload) - allowed)
    forbidden = sorted(set(payload) & FORBIDDEN_FIELD_NAMES)
    if unknown or forbidden:
        fields = sorted(set(unknown) | set(forbidden))
        raise ApiError(
            "unsupported_field",
            f"Unsupported field(s) at {path}: {', '.join(fields)}.",
            details={"path": path, "fields": fields},
        )


def normalize_issue_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ApiError("invalid_issue_key", "issueKey must be a string.")
    issue_key = value.strip().upper()
    if not ISSUE_KEY_RE.fullmatch(issue_key):
        raise ApiError("invalid_issue_key", "issueKey must look like ABC-123.")
    return issue_key


def normalize_project_key(value: Any, issue_key: str) -> str:
    if value is None:
        return prefix_config.extract_prefix(issue_key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError("invalid_project_key", "projectKey must be a non-empty string.")
    project_key = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]+", project_key):
        raise ApiError("invalid_project_key", "projectKey contains unsupported characters.")
    return project_key


def normalize_bool(value: Any, *, field: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ApiError("invalid_request", f"{field} must be boolean.")
    return value


def normalize_timeout(value: Any) -> int:
    if value is None:
        return 1800
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ApiError("invalid_timeout", "timeoutSec must be a positive integer.")
    return min(value, 24 * 60 * 60)


def normalize_path_string(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApiError("invalid_request", f"{field} must be a non-empty string.")
    return str(Path(value).expanduser())


def normalize_repo_path_text(value: Any, *, field: str, code: str = "invalid_request") -> str | None:
    if value is None:
        return None
    repositories = prefix_config.split_repository_list(value)
    if not repositories:
        raise ApiError(code, f"{field} must contain at least one repository path.")
    return prefix_config.repositories_to_api_value([str(Path(repo).expanduser()) for repo in repositories])


def normalize_string(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApiError("invalid_request", f"{field} must be a non-empty string.")
    return value.strip()


def parse_gitlab_mr_url(value: str) -> tuple[str, int] | None:
    parsed = parse_gitlab_mr_url_ref(value)
    if parsed is None:
        return None
    return parsed.project_path, parsed.mr_id


def normalize_init_fields(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    payload = require_object(value)
    reject_forbidden_fields(payload, ALLOWED_INIT_FIELDS, path="init")
    result: dict[str, Any] = {}
    repo_path = normalize_repo_path_text(payload.get("repoPath"), field="init.repoPath")
    if repo_path is not None:
        result["repoPath"] = repo_path
    for field in ("reviewsRoot", "configPath"):
        normalized = normalize_path_string(payload.get(field), field=f"init.{field}")
        if normalized is not None:
            result[field] = normalized
    for field in ("branch", "remote"):
        normalized = normalize_string(payload.get(field), field=f"init.{field}")
        if normalized is not None:
            result[field] = normalized
    if "mrId" in payload and payload["mrId"] is not None:
        mr_id = payload["mrId"]
        if not isinstance(mr_id, int) or isinstance(mr_id, bool) or mr_id <= 0:
            raise ApiError("invalid_request", "init.mrId must be a positive integer.")
        result["mrId"] = mr_id
    selected_mr_fields = any(
        field in payload and payload[field] is not None
        for field in ("mrUrl", "gitlabProjectPath", "gitlabInstanceId")
    )
    if selected_mr_fields:
        if "mrId" not in result:
            raise ApiError(
                "invalid_request",
                "init.mrUrl, init.gitlabProjectPath and init.gitlabInstanceId require positive init.mrId.",
            )
        mr_url = normalize_string(payload.get("mrUrl"), field="init.mrUrl")
        project_path_raw = normalize_string(payload.get("gitlabProjectPath"), field="init.gitlabProjectPath")
        instance_id = normalize_string(payload.get("gitlabInstanceId"), field="init.gitlabInstanceId")
        if mr_url is None or project_path_raw is None:
            raise ApiError(
                "invalid_request",
                "init.mrUrl and init.gitlabProjectPath must be provided together.",
            )
        parsed_mr = parse_gitlab_mr_url(mr_url)
        project_path = normalize_gitlab_project_path(project_path_raw)
        if parsed_mr is None or project_path is None:
            raise ApiError("invalid_request", "init.mrUrl must be a GitLab merge request URL and init.gitlabProjectPath must be a GitLab project path.")
        parsed_project_path, parsed_mr_id = parsed_mr
        if parsed_mr_id != result["mrId"]:
            raise ApiError(
                "invalid_request",
                "init.mrUrl merge request ID must match init.mrId.",
                details={"mrId": result["mrId"], "mrUrlMrId": parsed_mr_id},
            )
        if parsed_project_path != project_path:
            raise ApiError(
                "invalid_request",
                "init.mrUrl project path must match init.gitlabProjectPath.",
                details={"mrUrlProjectPath": parsed_project_path, "gitlabProjectPath": project_path},
            )
        result["mrUrl"] = mr_url
        result["gitlabProjectPath"] = project_path
        if instance_id is not None:
            result["gitlabInstanceId"] = instance_id
    if "mrTitle" in payload and payload["mrTitle"] is not None:
        if "mrId" not in result:
            raise ApiError("invalid_request", "init.mrTitle requires positive init.mrId.")
        mr_title = normalize_string(payload.get("mrTitle"), field="init.mrTitle")
        if mr_title is not None:
            result["mrTitle"] = mr_title
    if "mrFromTasks" in payload:
        result["mrFromTasks"] = normalize_bool(payload["mrFromTasks"], field="init.mrFromTasks")
    return result


def normalize_runner_fields(value: Any, *, ide: str) -> dict[str, Any]:
    del ide
    if value is None:
        return {"mode": "headless", "codexPrompt": "review-start-task"}
    payload = require_object(value)
    reject_forbidden_fields(payload, ALLOWED_RUNNER_FIELDS, path="runner")
    mode = payload.get("mode", "headless")
    if mode not in ("headless", "handoff"):
        raise ApiError("invalid_request", "runner.mode must be headless or handoff.")
    prompt = payload.get("codexPrompt")
    if prompt is not None and prompt not in ("review-start-task", "/review-start-task"):
        raise ApiError("unsupported_field", "runner.codexPrompt cannot be arbitrary.")
    return {"mode": mode, "codexPrompt": "review-start-task"}


def normalize_jira_fields(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    payload = require_object(value)
    reject_forbidden_fields(payload, ALLOWED_JIRA_FIELDS, path="jira")
    result: dict[str, Any] = {}
    for field in ("url", "title", "description"):
        item = payload.get(field)
        if item is None:
            continue
        if not isinstance(item, str):
            raise ApiError("invalid_request", f"jira.{field} must be a string.")
        result[field] = item
    comments = payload.get("comments")
    if comments is not None:
        if not isinstance(comments, list) or not all(isinstance(item, str) for item in comments):
            raise ApiError("invalid_request", "jira.comments must be a list of strings.")
        result["comments"] = comments
    return result


def normalize_review_request(value: Any) -> dict[str, Any]:
    payload = require_object(value)
    reject_forbidden_fields(payload, ALLOWED_REVIEW_FIELDS, path="$")
    issue_key = normalize_issue_key(payload.get("issueKey"))
    project_key = normalize_project_key(payload.get("projectKey"), issue_key)
    ide = payload.get("ide", "codex")
    if ide not in ALLOWED_IDES:
        raise ApiError("invalid_request", f"ide must be {ALLOWED_IDES_TEXT}.")
    result = {
        "issueKey": issue_key,
        "projectKey": project_key,
        "ide": ide,
        "timeoutSec": normalize_timeout(payload.get("timeoutSec")),
        "force": normalize_bool(payload.get("force"), field="force"),
        "init": normalize_init_fields(payload.get("init")),
        "runner": normalize_runner_fields(payload.get("runner"), ide=ide),
        "jira": normalize_jira_fields(payload.get("jira")),
    }
    if "userInstruction" in payload:
        try:
            user_instruction = validate_user_instruction(payload["userInstruction"])
        except UserInstructionError as exc:
            raise ApiError("invalid_request", str(exc)) from exc
        if user_instruction is None:
            raise ApiError("invalid_request", "userInstruction must be a string.")
        result["userInstruction"] = user_instruction
    if result["init"].get("mrId") and result["init"].get("mrFromTasks"):
        raise ApiError("invalid_request", "init.mrId and init.mrFromTasks cannot be used together.")
    return result


def normalize_prefix_payload(value: Any) -> dict[str, Any]:
    payload = require_object(value, code="invalid_prefix_config")
    allowed = {"repoPath", "reviewsRoot", "branch", "remote", "ide", "timeoutSec", "useMcp"}
    reject_forbidden_fields(payload, allowed, path="$")
    result: dict[str, Any] = {}
    use_mcp = payload.get("useMcp", True)
    if not isinstance(use_mcp, bool):
        raise ApiError("invalid_prefix_config", "useMcp must be a boolean.")
    result["useMcp"] = use_mcp
    repo_path = normalize_repo_path_text(payload.get("repoPath"), field="repoPath", code="invalid_prefix_config")
    if repo_path is None:
        raise ApiError("invalid_prefix_config", "repoPath is required.")
    result["repoPath"] = repo_path
    for field in ("reviewsRoot", "branch", "remote"):
        item = payload.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ApiError("invalid_prefix_config", f"{field} is required.")
        result[field] = item.strip()
    if "ide" in payload:
        ide = payload.get("ide")
        if ide not in ALLOWED_IDES:
            raise ApiError("invalid_prefix_config", f"ide must be {ALLOWED_IDES_TEXT}.")
        result["ide"] = ide
    if "timeoutSec" in payload:
        result["timeoutSec"] = normalize_timeout(payload.get("timeoutSec"))
    return result


def prefix_entry_to_api(prefix: str, entry: dict[str, Any]) -> dict[str, Any]:
    repositories = prefix_config.entry_repositories(entry)
    api_entry: dict[str, Any] = {
        "prefix": prefix,
        "repoPath": prefix_config.repositories_to_api_value(repositories),
        "reviewsRoot": entry.get("reviews_dir"),
        "branch": entry.get("branch"),
        "remote": entry.get("remote", "origin"),
        "useMcp": entry.get("mcp_enabled", True),
    }
    ide = entry.get("ide")
    if ide in ALLOWED_IDES:
        api_entry["ide"] = ide
    timeout_sec = entry.get("timeout_sec")
    if isinstance(timeout_sec, int) and not isinstance(timeout_sec, bool) and timeout_sec > 0:
        api_entry["timeoutSec"] = timeout_sec
    return api_entry


def prefix_entry_from_api(config: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "repositories": prefix_config.split_repository_list(config["repoPath"]),
        "reviews_dir": config["reviewsRoot"],
        "branch": config["branch"],
        "remote": config["remote"],
        "mcp_enabled": config.get("useMcp", True),
    }
    ide = config.get("ide")
    if ide in ALLOWED_IDES:
        entry["ide"] = ide
    if "timeoutSec" in config:
        entry["timeout_sec"] = normalize_timeout(config.get("timeoutSec"))
    return entry


def display_status_for(detail_status: str) -> str:
    if detail_status in ACTIVE_DETAIL_STATUSES or detail_status == "already_running":
        return "Выполняется"
    if detail_status in DONE_DETAIL_STATUSES:
        return "Выполнено"
    return "Новая"


def actions_for(
    detail_status: str,
    *,
    report_available: bool | None = None,
    reasoning_available: bool | None = None,
) -> list[str]:
    actions = list(DETAIL_ACTIONS.get(detail_status, ["refresh"]))
    if report_available is False:
        actions = [action for action in actions if action != "open_report"]
    if reasoning_available is False:
        actions = [action for action in actions if action != "open_reasoning"]
    return actions


def duration_seconds(status: dict[str, Any]) -> int | None:
    detail_status = str(status.get("detailStatus") or status.get("status") or "")
    if detail_status not in ACTIVE_DETAIL_STATUSES and detail_status != "already_running":
        if isinstance(status.get("durationSec"), int):
            return status["durationSec"]
    started = parse_iso(status.get("startedAt"))
    if started is None:
        return None
    if detail_status in ACTIVE_DETAIL_STATUSES or detail_status == "already_running":
        finished = datetime.now(timezone.utc)
    else:
        finished = parse_iso(status.get("finishedAt")) or parse_iso(status.get("updatedAt")) or datetime.now(timezone.utc)
    return max(0, int((finished - started).total_seconds()))


def status_to_popup(status: dict[str, Any] | None, *, issue_key: str | None = None) -> dict[str, Any]:
    if status is None:
        detail_status = "issue_detected" if issue_key else "not_jira_issue"
        return {
            "displayStatus": display_status_for(detail_status),
            "detailStatus": detail_status,
            "detailText": DETAIL_TEXT[detail_status],
            "availableActions": actions_for(detail_status),
            "issueKey": issue_key,
            "jobId": None,
            "startedAt": None,
            "updatedAt": None,
            "durationSec": None,
            "taskDir": None,
            "runDir": None,
            "reportPath": None,
            "reasoningPath": None,
            "summary": None,
            "tokenUsage": None,
            "warnings": [],
            "manualCommand": None,
            "source": None,
            "latestAttempt": None,
            "error": None,
            "feedback": None,
            "diagnostics": [],
        }

    detail_status = str(status.get("detailStatus") or status.get("status") or "issue_detected")
    detail_text = str(status.get("detailText") or DETAIL_TEXT.get(detail_status, detail_status))
    from .problem_details import canonical_status_contract

    feedback, diagnostics = canonical_status_contract(status)
    return {
        "displayStatus": display_status_for(detail_status),
        "detailStatus": detail_status,
        "detailText": detail_text,
        "availableActions": status.get("availableActions") or actions_for(detail_status),
        "issueKey": status.get("issueKey") or issue_key,
        "jobId": status.get("jobId"),
        "startedAt": status.get("startedAt"),
        "updatedAt": status.get("updatedAt"),
        "durationSec": duration_seconds(status),
        "taskDir": status.get("taskDir"),
        "runDir": status.get("runDir"),
        "reportPath": status.get("reportPath"),
        "reasoningPath": status.get("reasoningPath"),
        "summary": status.get("summary"),
        "tokenUsage": status.get("tokenUsage"),
        "warnings": status.get("warnings") if isinstance(status.get("warnings"), list) else [],
        "manualCommand": status.get("manualCommand"),
        "source": status.get("source"),
        "latestAttempt": status.get("latestAttempt") if isinstance(status.get("latestAttempt"), dict) else None,
        "error": status.get("error"),
        "feedback": feedback,
        "diagnostics": diagnostics,
    }


def is_active_status(status: dict[str, Any] | None) -> bool:
    if not status:
        return False
    detail = str(status.get("detailStatus") or status.get("status") or "")
    return detail in ACTIVE_DETAIL_STATUSES


def is_loopback_address(value: str | None) -> bool:
    if not value:
        return False
    host = value.strip()
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
