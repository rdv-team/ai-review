"""Prepare immutable, task-local data for a new review discussion."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_tasks.infrastructure.text_files import write_text_lf
from .memory import ReportStateError, ReviewReportFeature


class _AddressedDiscussion(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    reportId: str = Field(min_length=1)
    findingId: str = Field(min_length=1)


def validate_discussion_payload(payload: Any) -> None:
    if isinstance(payload, dict) and not payload:
        return
    try:
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object")
        value = _AddressedDiscussion.model_validate(payload)
        if not value.reportId.strip() or not value.findingId.strip():
            raise ValueError("Empty identifier")
    except (ValidationError, ValueError) as exc:
        raise ReportStateError(400, "invalid_discussion",
                            "Ожидается {} или непустая пара reportId и findingId.") from exc


def prepare_discussion(task_dir: Path, task: str, report_path: Path, payload: Any,
                       report_feature: ReviewReportFeature) -> Path:
    validate_discussion_payload(payload)
    finding = report_feature.discussion_snapshot(
        task_dir, task, report_path, payload.get("reportId"), payload.get("findingId"))
    data: dict[str, Any] = {"task": task, "reportPath": str(report_path)}
    if finding is not None:
        data.update(reportId=payload["reportId"], findingId=payload["findingId"],
                    finding={key: finding[key] for key in (
                        "number", "file", "startLine", "endLine", "severity", "observation",
                        "suggestion", "sourceUrl") if key in finding})
    # JSON preserves multiline finding text without Markdown fence escaping.
    context = task_dir / "_review_info" / "discussions" / str(uuid.uuid4()) / "context.md"
    try:
        write_text_lf(context, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise ReportStateError(500, "discussion_open_failed",
                            f"Не удалось сохранить контекст обсуждения: {exc}") from exc
    return context
