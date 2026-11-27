"""Prepare and atomically replace the files of an edited modern report."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from review_tasks.sgr_schema import ReviewStage1Result

from .review_yaml import (
    ReviewYamlDocument, ReviewYamlError, combined_mapping, read_stage_pair_for_render,
    read_review_yaml_documents, render_review_report,
)
from .report_view import ReportSnapshot, build_report_snapshot


class SuggestionMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reportId: str = Field(min_length=1)
    findingId: str = Field(min_length=1)
    suggestion: str
    expectedSuggestion: str

    @field_validator("suggestion")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Предложение не должно быть пустым.")
        return value.replace("\r\n", "\n").replace("\r", "\n")


def editable_pair(task_dir: Path) -> tuple[ReviewStage1Result, ReviewYamlDocument, ReviewYamlDocument]:
    info = task_dir / "_review_info"
    stage1, document = read_stage_pair_for_render(info)
    current = read_review_yaml_documents([info / "review_result.yaml"])[0]
    expected = type(current.parsed).model_validate(combined_mapping(stage1, document.parsed))
    if current.parsed.model_dump() != expected.model_dump() or not hasattr(current.parsed, "risk_candidates"):
        raise ReviewYamlError("Итоговый результат не соответствует паре стадий.")
    return stage1, document, current


def can_edit_suggestions(task_dir: Path) -> bool:
    try:
        editable_pair(task_dir)
        return True
    except (OSError, ValueError, ReviewYamlError, AssertionError):
        return False


def prepare_suggestion(task_dir: Path, task: str, report_path: Path, name: str,
                       text: str, *, stage1: ReviewStage1Result, document: ReviewYamlDocument,
                       current: ReviewYamlDocument) -> tuple[dict[Path, bytes], ReportSnapshot, str]:
    stage2 = document.parsed.model_copy(deep=True)
    finding = next(item for item in stage2.findings if item.name == name)
    finding.suggestion = text
    # Validate the complete modified result before touching any destination.
    stage2 = type(stage2).model_validate(stage2.model_dump(exclude_unset=True))
    combined = combined_mapping(stage1, stage2)
    parsed = type(current.parsed).model_validate(combined)
    snapshot = build_report_snapshot(task_dir, task, documents=[ReviewYamlDocument(current.source, parsed)])
    content = render_review_report([ReviewYamlDocument(document.source, stage2)], task=task).replace("\r\n", "\n").replace("\r", "\n")

    def encode_yaml(data: dict[str, Any]) -> bytes:
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000).encode("utf-8")
    return {
        document.source: encode_yaml(stage2.model_dump(mode="python", exclude_unset=True)),
        current.source: encode_yaml(combined), report_path: content.encode("utf-8"),
    }, snapshot, content


def replace_report_files(payloads: dict[Path, bytes]) -> None:
    originals = {path: path.read_bytes() if path.exists() else None for path in payloads}
    prepared: dict[Path, Path] = {}
    replaced: list[Path] = []

    def temporary(path: Path, content: bytes) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return temp

    try:
        for path, content in payloads.items():
            prepared[path] = temporary(path, content)
        try:
            for path, temp in prepared.items():
                os.replace(temp, path)
                replaced.append(path)
        except OSError as exc:
            failures = []
            for path in reversed(replaced):
                try:
                    if originals[path] is None:
                        path.unlink()
                    else:
                        temp = temporary(path, originals[path])
                        try:
                            os.replace(temp, path)
                        finally:
                            temp.unlink(missing_ok=True)
                except OSError as rollback:
                    failures.append(f"{path}: {rollback}")
            if failures:
                raise OSError(f"{exc}; не удалось восстановить файлы: {'; '.join(failures)}") from exc
            raise
    finally:
        for temp in prepared.values():
            temp.unlink(missing_ok=True)
