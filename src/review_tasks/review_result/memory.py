"""Task memory and the independent state of interactive report cards."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from review_tasks.infrastructure.json_files import write_json
from .report_view import ReportSnapshot, build_report_snapshot
from .review_yaml import ReviewYamlDocument, ReviewYamlError
from .suggestions import SuggestionMutation, can_edit_suggestions, editable_pair, prepare_suggestion, replace_report_files


class ReportStateError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.extra = status, code, extra

    def response(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self)}, **self.extra}


class MemoryOperationError(Exception):
    """Task memory cannot be read, validated or updated."""


class ReportStateMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reportId: str = Field(min_length=1)
    findingId: str = Field(min_length=1)
    decision: Literal["accepted", "rejected"] | None
    comment: str
    expectedRevision: int = Field(ge=0)
    submitToMemory: bool = False


class ReportStateEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["accepted", "rejected"] | None
    comment: str
    revision: int = Field(ge=0)
    updatedAt: str | None
    memoryId: str | None = None


class ReportState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: Literal[1]
    reportId: str
    reportVersion: str
    findings: dict[str, ReportStateEntry]


class FindingContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reportId: str | None = None
    findingId: str | None = None
    number: int | None = Field(default=None, ge=1)
    file: str | None = None
    startLine: int | None = Field(default=None, ge=1)
    endLine: int | None = Field(default=None, ge=1)
    severity: Literal["critical", "important", "desirable"] | None = None
    observation: str | None = None
    suggestion: str | None = None
    sourceUrl: str | None = None

    @model_validator(mode="after")
    def valid_lines(self) -> FindingContext:
        if self.startLine is not None and self.endLine is not None and self.startLine > self.endLine:
            raise ValueError("Начало диапазона строк больше конца.")
        return self


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    comment: str
    context: FindingContext | None = None

    @model_validator(mode="after")
    def nonempty_comment(self) -> MemoryInput:
        if not self.comment.strip():
            raise ValueError("Текст правила не должен быть пустым.")
        return self


class MemoryRecord(MemoryInput):
    id: str
    source: Literal["report", "discussion"]
    createdAt: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_identifier(self) -> MemoryRecord:
        if str(uuid.UUID(self.id)) != self.id:
            raise ValueError("Некорректный ID правила.")
        datetime.fromisoformat(self.createdAt)
        return self


class TaskMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: Literal[1]
    task: str
    records: dict[str, MemoryRecord]


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _capsule_lock(task_dir: Path) -> threading.RLock:
    key = os.path.normcase(str(task_dir.resolve()))
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _empty_entry() -> dict[str, Any]:
    return {"decision": None, "comment": "", "revision": 0, "updatedAt": None, "memoryId": None}


def _read_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Ожидается JSON-объект: {path}")
    return data


def _task_segment(task: str) -> str:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*-\d+", task):
        return task
    from hashlib import sha256
    label = re.sub(r"[^A-Za-z0-9_-]", "_", task)[:60] or "task"
    return label + "-" + sha256(task.encode("utf-8")).hexdigest()[:16]


def _normalize_task(task: str) -> str:
    task = task.strip()
    if not task:
        raise MemoryOperationError("Параметр --task должен содержать непустой ключ задачи.")
    return task


def _memory_path(task: str, memory_root: Path | None) -> Path:
    try:
        root = (memory_root if memory_root is not None else
                Path.home() / ".review-tasks" / "memory").expanduser().resolve()
        return root / _task_segment(task) / "memory.json"
    except (OSError, RuntimeError) as exc:
        location = memory_root if memory_root is not None else "~/.review-tasks/memory"
        raise MemoryOperationError(f"Не удалось определить каталог памяти '{location}': {exc}") from exc


def _read_memory(path: Path, task: str) -> dict[str, Any]:
    try:
        raw = _read_object(path)
        if type(raw.get("schemaVersion")) is not int:
            raise ValueError("Неверная версия документа памяти.")
        data = TaskMemory.model_validate(raw)
        if data.task != task or any(key != record.id for key, record in data.records.items()):
            raise ValueError("Задача или идентификаторы правил не соответствуют документу.")
        return raw
    except FileNotFoundError:
        return {"schemaVersion": 1, "task": task, "records": {}}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise MemoryOperationError(f"Не удалось прочитать память '{path}': {exc}") from exc


def read_task_memory(task: str, memory_root: Path | None = None) -> str:
    """Read one complete validated document without creating or repairing files."""
    task = _normalize_task(task)
    data = _read_memory(_memory_path(task, memory_root), task)
    if not data["records"]:
        return ""
    return json.dumps(data, ensure_ascii=False, indent=2).replace("\r\n", "\n").replace("\r", "\n") + "\n"


def _save_memory(path: Path, data: dict[str, Any]) -> None:
    try:
        write_json(path, data)
    except OSError as exc:
        raise MemoryOperationError(f"Не удалось записать память '{path}': {exc}") from exc


def _put_memory_record(task: str, record: dict[str, Any], *, memory_root: Path | None,
                       replace_id: str | None = None) -> dict[str, Any]:
    task = _normalize_task(task)
    path = _memory_path(task, memory_root)
    data = _read_memory(path, task)
    records = data["records"]
    if replace_id is not None:
        if replace_id not in records:
            raise MemoryOperationError(f"Правило памяти с ID '{replace_id}' не найдено.")
        del records[replace_id]
    previous = records.get(record["id"])
    # Preserve the creation time when the explicit report action is repeated.
    if previous is not None:
        record["createdAt"] = previous["createdAt"]
    changed = previous != record or replace_id is not None
    if changed:
        records[record["id"]] = record
        _save_memory(path, data)
    return {"ok": True, "operation": "replace" if replace_id is not None else "add",
            "changed": changed, "id": record["id"]}


def add_task_memory(task: str, input_path: Path, *, replace_id: str | None = None,
                    memory_root: Path | None = None) -> dict[str, Any]:
    task = _normalize_task(task)
    try:
        value = MemoryInput.model_validate(_read_object(input_path))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise MemoryOperationError(f"Некорректный файл правила '{input_path}': {exc}") from exc
    record = value.model_dump(exclude_unset=True)
    if record.get("context") is None:
        record.pop("context", None)
    record.update(id=str(uuid.uuid4()), source="discussion", createdAt=datetime.now(timezone.utc).isoformat())
    return _put_memory_record(task, record, memory_root=memory_root, replace_id=replace_id)


def delete_task_memory(task: str, record_id: str, *, memory_root: Path | None = None) -> dict[str, Any]:
    task = _normalize_task(task)
    path = _memory_path(task, memory_root)
    data = _read_memory(path, task)
    changed = record_id in data["records"]
    if changed:
        del data["records"][record_id]
        _save_memory(path, data)
    return {"ok": True, "operation": "delete", "changed": changed, "id": record_id}


def read_existing_report_state(task_dir: Path, snapshot: ReportSnapshot, *,
                               allow_changed: bool = False) -> dict[str, Any] | None:
    """Validate saved state without reading or changing independent task memory."""
    path = task_dir / "_review_info" / "report-state.json"
    try:
        raw = _read_object(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ReportStateError(500, "report_state_read_failed", f"Не удалось прочитать состояние отчёта: {exc}") from exc
    try:
        if type(raw.get("schemaVersion")) is not int:
            raise ValueError("Неверная версия состояния отчёта.")
        state = ReportState.model_validate(raw).model_dump()
        if str(uuid.UUID(state["reportId"])) != state["reportId"]:
            raise ValueError("Invalid reportId")
        if not re.fullmatch(r"[0-9a-f]{64}", state["reportVersion"]):
            raise ValueError("Invalid reportVersion")
        for entry in state["findings"].values():
            if entry["memoryId"] is not None and str(uuid.UUID(entry["memoryId"])) != entry["memoryId"]:
                raise ValueError("Invalid memoryId")
    except (ValueError, ValidationError) as exc:
        raise ReportStateError(500, "report_state_read_failed", f"Не удалось прочитать состояние отчёта: {exc}") from exc
    if state["reportVersion"] == snapshot.version:
        valid_ids = {item["id"] for item in snapshot.view["published"]}
        if not set(state["findings"]).issubset(valid_ids):
            raise ReportStateError(500, "report_state_read_failed", "Состояние содержит неизвестные замечания.")
        return state
    if not allow_changed:
        raise ReportStateError(409, "report_state_unavailable", "Версия состояния не соответствует отчёту.")
    return None


class ReviewReportFeature:
    def __init__(self, memory_root: Path | None = None):
        # Resolving memory is deferred: viewing a report never depends on it.
        self.memory_root = memory_root

    def _snapshot(self, task_dir: Path, task: str, *,
                  documents: list[ReviewYamlDocument] | None = None) -> ReportSnapshot:
        try:
            return build_report_snapshot(task_dir, task, documents=documents)
        except Exception as exc:
            raise ReportStateError(409, "report_state_unavailable", f"Интерактивный результат недоступен: {exc}") from exc

    def _state(self, task_dir: Path, snapshot: ReportSnapshot, *, persist: bool = True) -> dict[str, Any]:
        state = read_existing_report_state(task_dir, snapshot, allow_changed=True)
        if state is not None:
            return state
        state = {"schemaVersion": 1, "reportId": str(uuid.uuid4()), "reportVersion": snapshot.version, "findings": {}}
        if persist:
            self._write_state(task_dir, state)
        return state

    def _write_state(self, task_dir: Path, state: dict[str, Any]) -> None:
        try:
            write_json(task_dir / "_review_info" / "report-state.json", state)
        except OSError as exc:
            raise ReportStateError(500, "report_state_write_failed", str(exc), stateSaved=False, memorySaved=False) from exc

    def get_report(self, task_dir: Path, task: str) -> dict[str, Any]:
        with _capsule_lock(task_dir):
            snapshot = self._snapshot(task_dir, task)
            state = self._state(task_dir, snapshot)
            return {**snapshot.view, "reportId": state["reportId"]}

    def can_edit_suggestions(self, task_dir: Path) -> bool:
        with _capsule_lock(task_dir):
            return can_edit_suggestions(task_dir)

    def put_report_suggestion(self, task_dir: Path, task: str, report_path: Path,
                              payload: Any) -> dict[str, Any]:
        try:
            mutation = SuggestionMutation.model_validate(payload)
        except ValidationError as exc:
            raise ReportStateError(400, "report_suggestion_invalid", str(exc)) from exc
        with _capsule_lock(task_dir):
            try:
                stage1, document, current = editable_pair(task_dir)
            except (OSError, ValueError, ReviewYamlError, AssertionError) as exc:
                raise ReportStateError(409, "report_state_unavailable", "Редактирование этого результата недоступно.") from exc
            snapshot = self._snapshot(task_dir, task, documents=[current])
            state = self._state(task_dir, snapshot, persist=False)
            if state["reportId"] != mutation.reportId:
                raise ReportStateError(409, "report_changed", "Отчёт изменился. Обновите страницу.")
            finding = next((item for item in snapshot.view["published"] if item["id"] == mutation.findingId), None)
            if finding is None:
                raise ReportStateError(400, "report_suggestion_invalid", "Изменять можно только опубликованное замечание.")
            if finding["suggestion"] not in (mutation.expectedSuggestion, mutation.suggestion):
                raise ReportStateError(409, "report_suggestion_conflict", "Предложение изменено в другой вкладке. Обновите страницу.",
                                       suggestion=finding["suggestion"])
            try:
                if finding["suggestion"] == mutation.suggestion:
                    content = report_path.read_text(encoding="utf-8")
                else:
                    files, updated, content = prepare_suggestion(
                        task_dir, task, report_path, finding["name"], mutation.suggestion,
                        stage1=stage1, document=document, current=current,
                    )
                    state["reportVersion"] = updated.version
                    files[task_dir / "_review_info" / "report-state.json"] = (
                        json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                    replace_report_files(files)
            except OSError as exc:
                raise ReportStateError(500, "report_suggestion_write_failed", f"Не удалось сохранить предложение: {exc}") from exc
            return {"ok": True, "reportId": state["reportId"], "findingId": mutation.findingId,
                    "suggestion": mutation.suggestion, "content": content}

    def discussion_snapshot(self, task_dir: Path, task: str, report_path: Path,
                            report_id: str | None, finding_id: str | None) -> dict[str, Any] | None:
        with _capsule_lock(task_dir):
            if not report_path.is_file():
                raise ReportStateError(409, "discussion_unavailable", "Готовый отчёт недоступен.")
            if report_id is None:
                return None
            try:
                snapshot = self._snapshot(task_dir, task)
                state = self._state(task_dir, snapshot, persist=False)
            except ReportStateError as exc:
                raise ReportStateError(409, "discussion_unavailable", str(exc)) from exc
            if report_id != state["reportId"]:
                raise ReportStateError(409, "report_changed", "Отчёт изменился. Обновите страницу.")
            finding = next((item for item in snapshot.view["published"] if item["id"] == finding_id), None)
            if finding is None:
                raise ReportStateError(400, "invalid_discussion", "Обсуждать отдельно можно только опубликованное замечание.")
            return finding.copy()

    def get_report_state(self, task_dir: Path, task: str) -> dict[str, Any]:
        with _capsule_lock(task_dir):
            snapshot = self._snapshot(task_dir, task)
            state = self._state(task_dir, snapshot)
            findings = {item["id"]: state["findings"].get(item["id"], _empty_entry()) for item in snapshot.view["published"]}
            return {"ok": True, "reportId": state["reportId"], "findings": findings}

    def put_report_state(self, task_dir: Path, task: str, payload: Any) -> dict[str, Any]:
        try:
            mutation = ReportStateMutation.model_validate(payload)
        except ValidationError as exc:
            raise ReportStateError(400, "report_state_invalid", str(exc)) from exc
        with _capsule_lock(task_dir):
            snapshot = self._snapshot(task_dir, task)
            state = self._state(task_dir, snapshot, persist=False)
            if mutation.reportId != state["reportId"]:
                raise ReportStateError(409, "report_changed", "Отчёт изменился. Обновите страницу.")
            published = {item["id"]: item for item in snapshot.view["published"]}
            if mutation.findingId not in published:
                raise ReportStateError(400, "report_state_invalid", "Изменять можно только опубликованное замечание.")
            current = state["findings"].get(mutation.findingId, _empty_entry())
            same = current["decision"] == mutation.decision and current["comment"] == mutation.comment
            retry = same and current["revision"] == mutation.expectedRevision + 1
            if current["revision"] != mutation.expectedRevision and not retry:
                raise ReportStateError(409, "report_state_conflict", "Замечание изменено в другой вкладке.",
                                       reportId=state["reportId"], findingId=mutation.findingId, finding=current)
            state_changed = not same
            if not same:
                current = {**current, "decision": mutation.decision, "comment": mutation.comment,
                           "revision": current["revision"] + 1, "updatedAt": datetime.now(timezone.utc).isoformat()}
            submit = mutation.submitToMemory and bool(current["comment"].strip())
            # An empty reset can be retried after a partial deletion failure.
            cleanup = (not current["comment"].strip() and current["memoryId"] is not None
                       and (mutation.submitToMemory or mutation.decision is None))
            if submit and current["memoryId"] is None:
                current = {**current, "memoryId": str(uuid.uuid4())}
                state_changed = True
            if state_changed:
                state["findings"][mutation.findingId] = current
                self._write_state(task_dir, state)
            response = {"ok": True, "reportId": state["reportId"], "findingId": mutation.findingId,
                        "finding": current, "stateSaved": True, "memorySaved": None}
            if not submit and not cleanup:
                return response
            try:
                if cleanup:
                    delete_task_memory(task, current["memoryId"], memory_root=self.memory_root)
                else:
                    finding = published[mutation.findingId]
                    context = {key: finding[key] for key in FindingContext.model_fields if key in finding}
                    context.update(reportId=state["reportId"], findingId=mutation.findingId)
                    record = {"id": current["memoryId"], "comment": current["comment"], "source": "report",
                              "context": context, "createdAt": datetime.now(timezone.utc).isoformat()}
                    _put_memory_record(task, record, memory_root=self.memory_root)
                response["memorySaved"] = True
            except MemoryOperationError as exc:
                response.pop("ok")
                response["memorySaved"] = False
                raise ReportStateError(500, "memory_write_failed", f"Состояние сохранено, память не обновлена: {exc}", **response) from exc
            return response
