"""Canonical user feedback and technical diagnostic contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from review_tasks.review_init.result import InitResultEnvelope, parse_init_result
from review_tasks.review_init.comparison import ComparisonSummary

from .storage import read_json


class InitResultProtocolError(ValueError):
    pass


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    tone: Literal["info", "warning", "error"]
    title: str = Field(min_length=1)
    message: str = Field(min_length=1)


class DiagnosticEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    kind: Literal["text", "log", "path", "data"]
    value: Any | None = None
    log_ref: str | None = Field(default=None, alias="logRef")

    @model_validator(mode="after")
    def require_content(self) -> "DiagnosticEntry":
        value = self.value
        has_value = value is not None and (not isinstance(value, str) or bool(value.strip()))
        if not has_value and self.log_ref is None:
            raise ValueError("diagnostic entry requires value or logRef")
        return self


_FEEDBACK_PRESENTATION: dict[str, tuple[str, str, str]] = {
    "already_done": (
        "info",
        "Ревью уже выполнено",
        "Для этой задачи уже существует завершённое ревью. Откройте результат или запустите ревью повторно.",
    ),
    "cancelled": (
        "info",
        "Ревью отменено",
        "Запуск остановлен. При необходимости ревью можно запустить повторно.",
    ),
    "previous_cancelled": (
        "info",
        "Предыдущий запуск отменён",
        "Ревью можно запустить повторно.",
    ),
    "init_failed": (
        "error",
        "Не удалось подготовить каталог ревью",
        "Проверьте технические детали и повторите запуск после устранения причины.",
    ),
    "no_commits": (
        "info",
        "Нет новых коммитов для ревью",
        "В выбранном диапазоне не найдено коммитов. Проверьте ветку или источник изменений.",
    ),
    "no_reviewable_files": (
        "info",
        "Нет подходящих файлов для ревью",
        "Изменения найдены, но среди них нет поддерживаемых файлов для ревью.",
    ),
    "init_result_protocol_error": (
        "error",
        "Не удалось определить результат инициализации",
        "Команда init вернула некорректный служебный результат. Откройте лог инициализации.",
    ),
    "preflight_failed": (
        "error",
        "Не удалось проверить окружение",
        "Проверьте технические детали и настройки локального сервиса.",
    ),
    "review_failed": (
        "error",
        "Ревью завершилось ошибкой",
        "Откройте технические детали или лог агента и повторите запуск после устранения причины.",
    ),
    "timeout": (
        "error",
        "Превышено время выполнения",
        "Ревью не завершилось за отведённое время. Повторите запуск или увеличьте timeout.",
    ),
    "interrupted": (
        "error",
        "Запуск был прерван",
        "Локальный сервис прервал незавершённый запуск. Его можно повторить.",
    ),
    "previous_failed": (
        "error",
        "Предыдущий запуск завершился ошибкой",
        "Откройте технические детали или повторите ревью.",
    ),
}


def build_feedback(code: str, *, detail_status: str, message: str | None = None) -> dict[str, str]:
    tone, title, default_message = _FEEDBACK_PRESENTATION.get(
        code,
        _FEEDBACK_PRESENTATION.get(
            detail_status,
            ("error", "Не удалось выполнить операцию", "Проверьте технические детали и повторите попытку."),
        ),
    )
    return Feedback(code=code, tone=tone, title=title, message=message or default_message).model_dump()


def diagnostic_entry(
    entry_id: str,
    label: str,
    kind: str,
    *,
    value: Any | None = None,
    log_ref: str | None = None,
) -> dict[str, Any] | None:
    if value is None and log_ref is None:
        return None
    if isinstance(value, str) and not value.strip() and log_ref is None:
        return None
    try:
        entry = DiagnosticEntry(id=entry_id, label=label, kind=kind, value=value, log_ref=log_ref)
    except ValidationError:
        return None
    return entry.model_dump(mode="json", by_alias=True, exclude_none=True)


def unique_diagnostics(entries: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id or entry_id in seen:
            continue
        seen.add(entry_id)
        result.append(entry)
    return result


def load_init_result(path, exit_code: int) -> InitResultEnvelope:
    try:
        result = parse_init_result(read_json(path))
    except (OSError, ValueError, ValidationError) as exc:
        raise InitResultProtocolError(f"init result is missing or invalid: {exc}") from exc
    if result.outcome == "success" and exit_code != 0:
        raise InitResultProtocolError("init result reports success with non-zero exit code")
    if result.outcome in {"expected_stop", "failure"} and exit_code == 0:
        raise InitResultProtocolError(f"init result reports {result.outcome} with zero exit code")
    return result


def legacy_feedback(status: dict[str, Any]) -> dict[str, str] | None:
    error = status.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if not isinstance(code, str) or not code:
        code = str(status.get("detailStatus") or "service_error")
    return build_feedback(code, detail_status=str(status.get("detailStatus") or code))


def legacy_diagnostics(status: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any] | None] = []
    error = status.get("error")
    if isinstance(error, dict):
        for key, value in error.items():
            if key in {"code", "message", "stderrTail", "stderrPath"}:
                continue
            entries.append(diagnostic_entry(f"error.{key}", key, "data", value=value))
        stderr_tail = error.get("stderrTail")
        stderr_path = error.get("stderrPath")
        if stderr_tail or stderr_path:
            entries.append(diagnostic_entry("legacy.stderr", "Лог ошибки", "log", value=stderr_tail or stderr_path))
    diagnostics = status.get("diagnostics")
    if isinstance(diagnostics, dict):
        for key, value in diagnostics.items():
            if value is None or value == "" or (key == "initStderrTail" and any(e and e.get("id") == "legacy.stderr" for e in entries)):
                continue
            entries.append(diagnostic_entry(f"legacy.{key}", key, "data", value=value))
    return unique_diagnostics(entries)


def canonical_status_contract(status: dict[str, Any]) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    """Validate current status fields or adapt one persisted legacy payload."""
    raw_feedback = status.get("feedback")
    try:
        feedback = Feedback.model_validate(raw_feedback).model_dump() if isinstance(raw_feedback, dict) else legacy_feedback(status)
    except ValidationError:
        feedback = legacy_feedback(status)

    comparison_entries = comparison_diagnostics(status)
    if feedback is not None and feedback.get("tone") == "info":
        return feedback, comparison_entries

    raw_diagnostics = status.get("diagnostics")
    if not isinstance(raw_diagnostics, list):
        return feedback, unique_diagnostics([*legacy_diagnostics(status), *comparison_entries])
    entries: list[dict[str, Any] | None] = []
    for raw_entry in raw_diagnostics:
        try:
            entry = DiagnosticEntry.model_validate(raw_entry)
        except ValidationError:
            continue
        entries.append(entry.model_dump(mode="json", by_alias=True, exclude_none=True))
    return feedback, unique_diagnostics([*entries, *comparison_entries])


def comparison_diagnostics(status: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        summary = ComparisonSummary.model_validate(status.get("comparison"))
    except (ValueError, TypeError):
        return []
    entries = [diagnostic_entry(f"comparison.{index}", "Сравнение файлов", "data",
               value=warning.model_dump(exclude_none=True)) for index, warning in enumerate(summary.warnings)]
    if summary.error:
        entries.append(diagnostic_entry("comparison.error", "Сравнение недоступно", "text", value=summary.error.message))
    return entries
