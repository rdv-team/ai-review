from __future__ import annotations

import json
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from review_tasks.infrastructure.json_files import write_json

from .contracts import is_active_status, now_iso, safe_index_name


def default_service_root() -> Path:
    return Path.home() / ".review-tasks" / "local-service"


def compact_request_for_storage(request: dict[str, Any]) -> dict[str, Any]:
    compact = json.loads(json.dumps(request))
    jira = compact.get("jira")
    if isinstance(jira, dict):
        description = jira.pop("description", None)
        comments = jira.pop("comments", None)
        if isinstance(description, str):
            jira["descriptionLength"] = len(description)
            jira["descriptionStored"] = False
        if isinstance(comments, list):
            jira["commentsCount"] = len(comments)
            jira["commentsStored"] = False
    return compact


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_tail(path: Path | str | None, *, max_bytes: int = 4096) -> str | None:
    if path is None:
        return None
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        return None
    with file_path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        data = handle.read()
    return data.decode("utf-8", errors="replace")


class JobStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_service_root()).resolve()
        self.jobs_dir = self.root / "jobs"
        self.index_dir = self.root / "index"
        self._lock = threading.RLock()

    def create_job(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            job_id = uuid.uuid4().hex
            run_dir = self.jobs_dir / job_id
            now = now_iso()
            status = {
                "jobId": job_id,
                "issueKey": request["issueKey"],
                "projectKey": request["projectKey"],
                "status": "queued",
                "detailStatus": "queued",
                "detailText": "В очереди",
                "availableActions": ["cancel"],
                "startedAt": now,
                "updatedAt": now,
                "finishedAt": None,
                "durationSec": None,
                "taskDir": None,
                "runDir": str(run_dir),
                "reportPath": None,
                "reasoningPath": None,
                "summary": None,
                "tokenUsage": None,
                "warnings": [],
                "error": None,
                "cancelRequested": False,
            }
            write_json(run_dir / "request.json", compact_request_for_storage(request))
            self.write_status(status)
            self._write_index(status)
            return status

    def request_path(self, job_id: str) -> Path:
        return self.jobs_dir / job_id / "request.json"

    def read_request(self, job_id: str) -> dict[str, Any]:
        return read_json(self.request_path(job_id))

    def write_request(self, job_id: str, request: dict[str, Any]) -> None:
        with self._lock:
            write_json(self.request_path(job_id), compact_request_for_storage(request))

    def service_status_path(self, job_id: str) -> Path:
        return self.jobs_dir / job_id / "run_status.json"

    def read_status(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            service_path = self.service_status_path(job_id)
            if not service_path.exists():
                return None
            service_status = read_json(service_path)
            run_dir = service_status.get("runDir")
            if isinstance(run_dir, str):
                actual_path = Path(run_dir) / "run_status.json"
                if actual_path.exists() and actual_path != service_path:
                    return read_json(actual_path)
            return service_status

    def write_status(self, status: dict[str, Any]) -> None:
        with self._lock:
            run_dir = Path(str(status["runDir"]))
            status_path = run_dir / "run_status.json"
            write_json(status_path, status)
            service_path = self.service_status_path(str(status["jobId"]))
            if service_path != status_path:
                write_json(service_path, status)
            self._write_index(status)

    def update_status(self, job_id: str, **updates: Any) -> dict[str, Any]:
        with self._lock:
            status = self.read_status(job_id)
            if status is None:
                raise KeyError(job_id)
            status.update(updates)
            status["updatedAt"] = now_iso()
            self.write_status(status)
            return status

    def move_to_task_run_dir(self, job_id: str, task_dir: Path) -> dict[str, Any]:
        with self._lock:
            status = self.read_status(job_id)
            if status is None:
                raise KeyError(job_id)
            old_run_dir = Path(str(status["runDir"]))
            new_run_dir = task_dir / "_service_runs" / job_id
            new_run_dir.mkdir(parents=True, exist_ok=True)
            if old_run_dir.exists() and old_run_dir != new_run_dir:
                for item in old_run_dir.iterdir():
                    target = new_run_dir / item.name
                    if item.is_file() and not target.exists():
                        shutil.copy2(item, target)
            request_path = self.request_path(job_id)
            if request_path.exists():
                shutil.copy2(request_path, new_run_dir / "request.json")
            status["runDir"] = str(new_run_dir)
            status["taskDir"] = str(task_dir)
            status["updatedAt"] = now_iso()
            self.write_status(status)
            return status

    def mark_cancel_requested(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            status = self.read_status(job_id)
            if status is None:
                return None
            status["cancelRequested"] = True
            status["updatedAt"] = now_iso()
            self.write_status(status)
            return status

    def find_latest_by_issue(self, issue_key: str) -> dict[str, Any] | None:
        with self._lock:
            indexed = self._read_index(issue_key)
            if indexed:
                status = self.read_status(str(indexed.get("jobId")))
                if status:
                    return status
            candidates = [
                status for status in self.iter_statuses()
                if status.get("issueKey") == issue_key
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda item: str(item.get("updatedAt") or item.get("startedAt") or ""))

    def find_active_by_issue(self, issue_key: str) -> dict[str, Any] | None:
        latest = self.find_latest_by_issue(issue_key)
        if is_active_status(latest):
            return latest
        return None

    def iter_statuses(self) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        if not self.jobs_dir.exists():
            return statuses
        for path in self.jobs_dir.glob("*/run_status.json"):
            try:
                status = self.read_status(path.parent.name)
            except (OSError, json.JSONDecodeError):
                continue
            if status:
                statuses.append(status)
        return statuses

    def iter_active_statuses(self) -> list[dict[str, Any]]:
        return [status for status in self.iter_statuses() if is_active_status(status)]

    def _index_path(self, issue_key: str) -> Path:
        return self.index_dir / f"{safe_index_name(issue_key)}.json"

    def _write_index(self, status: dict[str, Any]) -> None:
        issue_key = status.get("issueKey")
        if not isinstance(issue_key, str):
            return
        write_json(
            self._index_path(issue_key),
            {
                "issueKey": issue_key,
                "jobId": status.get("jobId"),
                "runDir": status.get("runDir"),
                "updatedAt": status.get("updatedAt"),
            },
        )

    def _read_index(self, issue_key: str) -> dict[str, Any] | None:
        path = self._index_path(issue_key)
        if not path.exists():
            return None
        try:
            return read_json(path)
        except (OSError, json.JSONDecodeError):
            return None
