"""Capsule feedback and its independent, user-triggered memory projection."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_tasks.infrastructure.json_files import write_json
from .report_view import ReportSnapshot, build_report_snapshot


class FeedbackError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.extra = status, code, extra

    def response(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self)}, **self.extra}


class FeedbackReadError(Exception):
    """The independent feedback memory for one task cannot be read."""


class FeedbackMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reportId: str = Field(min_length=1)
    findingId: str = Field(min_length=1)
    decision: Literal["accepted", "rejected"] | None
    comment: str
    expectedRevision: int = Field(ge=0)
    submitToMemory: bool = True


class FeedbackEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["accepted", "rejected"] | None
    comment: str
    revision: int = Field(ge=0)
    updatedAt: str | None


class FeedbackState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: Literal[1]
    reportId: str
    reportVersion: str
    findings: dict[str, FeedbackEntry]
    memoryFindings: dict[str, FeedbackEntry] = Field(default_factory=dict)


class MemoryRecord(BaseModel):
    """Validate existing memory before replacing its current projection."""
    model_config = ConfigDict(extra="forbid", strict=True)
    task: str
    reportId: str
    findingId: str
    number: int = Field(ge=1)
    file: str
    startLine: int = Field(ge=1)
    endLine: int = Field(ge=1)
    severity: Literal["critical", "important", "desirable"]
    observation: str
    suggestion: str | None
    decision: Literal["accepted", "rejected"] | None
    comment: str
    updatedAt: str
    sourceUrl: str | None = None


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _capsule_lock(task_dir: Path) -> threading.RLock:
    key = os.path.normcase(str(task_dir.resolve()))
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _empty_entry() -> dict[str, Any]:
    return {"decision": None, "comment": "", "revision": 0, "updatedAt": None}


def _read_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON object expected: {path}")
    return data


def _task_segment(task: str) -> str:
    # Known task keys retain their readable name; arbitrary legacy keys cannot
    # escape the root or collide after character replacement.
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*-\d+", task):
        return task
    from hashlib import sha256
    label = re.sub(r"[^A-Za-z0-9_-]", "_", task)[:60] or "task"
    return label + "-" + sha256(task.encode("utf-8")).hexdigest()[:16]


def read_task_feedback(task: str, memory_root: Path | None = None) -> str:
    """Return every saved feedback document for one task as a single text block."""
    try:
        root = (memory_root if memory_root is not None else
                Path.home() / ".review-tasks" / "feedback").expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        location = memory_root if memory_root is not None else "~/.review-tasks/feedback"
        raise FeedbackReadError(
            f"Не удалось определить каталог памяти feedback '{location}': {exc}"
        ) from exc
    task_dir = root / _task_segment(task)
    try:
        documents = sorted(
            (entry for entry in task_dir.iterdir() if entry.is_file() and entry.suffix == ".json"),
            key=lambda entry: entry.name,
        )
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise FeedbackReadError(f"Не удалось прочитать память feedback '{task}': {exc}") from exc
    parts: list[str] = []
    for path in documents:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise FeedbackReadError(
                f"Не удалось прочитать файл feedback '{path}': {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise FeedbackReadError(f"Файл feedback не содержит JSON-объект: {path}")
        parts.append(raw.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n"))
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


class ReviewReportFeature:
    def __init__(self, memory_root: Path | None = None):
        self.memory_root = (memory_root if memory_root is not None else
                            Path.home() / ".review-tasks" / "feedback").expanduser().resolve()

    def _snapshot(self, task_dir: Path, task: str) -> ReportSnapshot:
        try:
            return build_report_snapshot(task_dir, task)
        except Exception as exc:
            raise FeedbackError(409, "feedback_unavailable",
                                f"Интерактивный результат недоступен: {exc}") from exc

    def _state(self, task_dir: Path, snapshot: ReportSnapshot) -> dict[str, Any]:
        path = task_dir / "_review_info" / "review-feedback.json"
        if path.exists():
            try:
                raw = _read_object(path)
                if type(raw.get("schemaVersion")) is not int:
                    raise ValueError("Invalid schemaVersion")
                # Before explicit submission was introduced every qualifying
                # saved decision was also submitted. Derive only in memory on
                # reads; the next mutation persists the compatible extension.
                state = FeedbackState.model_validate(raw).model_dump()
                if "memoryFindings" not in raw:
                    state["memoryFindings"] = {
                        finding_id: entry.copy() for finding_id, entry in state["findings"].items()
                        if entry["decision"] is not None and entry["comment"].strip()
                    }
                if str(uuid.UUID(state["reportId"])) != state["reportId"]:
                    raise ValueError("Invalid reportId")
                if not re.fullmatch(r"[0-9a-f]{64}", state["reportVersion"]):
                    raise ValueError("Invalid reportVersion")
            except (OSError, ValueError, ValidationError) as exc:
                raise FeedbackError(500, "feedback_state_read_failed",
                                    f"Не удалось прочитать состояние отчёта: {exc}") from exc
            if state["reportVersion"] == snapshot.version:
                valid_ids = {item["id"] for item in snapshot.view["published"]}
                if (not set(state["findings"]).issubset(valid_ids)
                        or not set(state["memoryFindings"]).issubset(valid_ids)):
                    raise FeedbackError(500, "feedback_state_read_failed", "Состояние содержит неизвестные замечания.")
                return state
        state = {"schemaVersion": 1, "reportId": str(uuid.uuid4()),
                 "reportVersion": snapshot.version, "findings": {},
                 "memoryFindings": {}}
        try:
            write_json(path, state)
        except OSError as exc:
            raise FeedbackError(500, "feedback_state_write_failed", str(exc),
                                stateSaved=False, memorySaved=False) from exc
        return state

    def get_report(self, task_dir: Path, task: str) -> dict[str, Any]:
        with _capsule_lock(task_dir):
            snapshot = self._snapshot(task_dir, task)
            state = self._state(task_dir, snapshot)
            return {**snapshot.view, "reportId": state["reportId"]}

    def get_feedback(self, task_dir: Path, task: str) -> dict[str, Any]:
        with _capsule_lock(task_dir):
            snapshot = self._snapshot(task_dir, task)
            state = self._state(task_dir, snapshot)
            findings = {item["id"]: state["findings"].get(item["id"], _empty_entry())
                        for item in snapshot.view["published"]}
            return {"ok": True, "reportId": state["reportId"], "findings": findings}

    def put_feedback(self, task_dir: Path, task: str, payload: Any) -> dict[str, Any]:
        try:
            mutation = FeedbackMutation.model_validate(payload)
        except ValidationError as exc:
            raise FeedbackError(400, "invalid_feedback", str(exc)) from exc
        with _capsule_lock(task_dir):
            snapshot = self._snapshot(task_dir, task)
            state = self._state(task_dir, snapshot)
            if mutation.reportId != state["reportId"]:
                raise FeedbackError(409, "report_changed", "Отчёт изменился. Обновите страницу.")
            published = {item["id"]: item for item in snapshot.view["published"]}
            if mutation.findingId not in published:
                raise FeedbackError(400, "invalid_feedback", "Изменять можно только опубликованное замечание.")
            current = state["findings"].get(mutation.findingId, _empty_entry())
            same = current["decision"] == mutation.decision and current["comment"] == mutation.comment
            # Only the immediately preceding accepted request is an idempotent
            # retry. A much older revision remains a conflict even if text matches.
            retry = same and current["revision"] == mutation.expectedRevision + 1
            if current["revision"] != mutation.expectedRevision and not retry:
                raise FeedbackError(409, "feedback_conflict", "Замечание изменено в другой вкладке.",
                                    reportId=state["reportId"], findingId=mutation.findingId, finding=current)
            if not same:
                current = {"decision": mutation.decision, "comment": mutation.comment,
                           "revision": current["revision"] + 1,
                           "updatedAt": datetime.now(timezone.utc).isoformat()}
                state["findings"][mutation.findingId] = current
            explicit_submission = (mutation.submitToMemory
                                   and "submitToMemory" in mutation.model_fields_set)
            eligible = bool(current["comment"].strip()) and (
                current["decision"] is not None or explicit_submission)
            submitted = state["memoryFindings"]
            cleanup = not eligible and mutation.findingId in submitted
            memory_changed = False
            if cleanup:
                # Retain the withdrawn submission as a tombstone, so retrying
                # deletion after a partial failure never needs to read memory.
                tombstone = {**current, "comment": ""}
                if submitted[mutation.findingId] != tombstone:
                    submitted[mutation.findingId] = tombstone
                    memory_changed = True
            elif mutation.submitToMemory and eligible:
                if submitted.get(mutation.findingId) != current:
                    submitted[mutation.findingId] = current.copy()
                    memory_changed = True
            should_sync = mutation.submitToMemory or cleanup
            if not same or memory_changed:
                try:
                    write_json(task_dir / "_review_info" / "review-feedback.json", state)
                except OSError as exc:
                    raise FeedbackError(500, "feedback_state_write_failed", str(exc),
                                        stateSaved=False, memorySaved=False) from exc
            response = {"ok": True, "reportId": state["reportId"],
                        "findingId": mutation.findingId, "finding": current,
                        "stateSaved": True, "memorySaved": None}
            if not should_sync:
                return response
            try:
                self._sync_memory(task, state, published)
                response["memorySaved"] = True
            except (OSError, ValueError) as exc:
                response.pop("ok")
                response["memorySaved"] = False
                raise FeedbackError(500, "feedback_memory_write_failed",
                                    f"Состояние сохранено, память не обновлена: {exc}", **response) from exc
            return response

    def _sync_memory(self, task: str, state: dict, published: dict) -> None:
        path = self.memory_root / _task_segment(task) / f"{state['reportId']}.json"
        exists = path.exists()
        if exists:
            previous = _read_object(path)
            if (type(previous.get("schemaVersion")) is not int or previous.get("schemaVersion") != 1
                    or set(previous) != {"schemaVersion", "task", "reportId", "records"}
                    or previous.get("task") != task
                    or previous.get("reportId") != state["reportId"]
                    or not isinstance(previous.get("records"), dict)
                    or any(not isinstance(record, dict) for record in previous["records"].values())):
                raise ValueError("Неподдерживаемый формат аналитической памяти.")
            for finding_id, raw_record in previous["records"].items():
                record = MemoryRecord.model_validate(raw_record)
                if (record.findingId != finding_id or record.task != task
                        or record.reportId != state["reportId"] or not record.comment.strip()
                        or record.startLine > record.endLine):
                    raise ValueError("Некорректная запись аналитической памяти.")
        records = {}
        for finding_id, entry in state["memoryFindings"].items():
            if entry["comment"].strip():
                snapshot = published[finding_id]
                record = {key: snapshot[key] for key in
                          ("number", "file", "startLine", "endLine", "severity", "observation", "suggestion")}
                record.update(task=task, reportId=state["reportId"], findingId=finding_id,
                              decision=entry["decision"], comment=entry["comment"], updatedAt=entry["updatedAt"])
                if "sourceUrl" in snapshot:
                    record["sourceUrl"] = snapshot["sourceUrl"]
                records[finding_id] = record
        if exists or records:
            write_json(path, {"schemaVersion": 1, "task": task,
                              "reportId": state["reportId"], "records": records})
