"""Versioned machine-readable result contract for ``review-tasks init``."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from review_tasks.review_init.comparison import ComparisonSummary


INIT_RESULT_SCHEMA_VERSION = 1
InitOutcome = Literal["success", "expected_stop", "failure"]
EXPECTED_STOP_CODES = frozenset({"already_done", "no_commits", "no_reviewable_files"})


class InitResultContext(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    task_dir: str | None = Field(default=None, alias="taskDir", min_length=1)
    review_dirs: list[str] = Field(default_factory=list, alias="reviewDirs")


class InitResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal[1] = Field(default=INIT_RESULT_SCHEMA_VERSION, alias="schemaVersion")
    command: Literal["init"] = "init"
    outcome: InitOutcome
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    context: InitResultContext = Field(default_factory=InitResultContext)

    @model_validator(mode="after")
    def validate_semantics(self) -> "InitResultEnvelope":
        if self.outcome == "success" and self.code != "success":
            raise ValueError("successful init result must use code 'success'")
        if self.outcome == "expected_stop" and self.code not in EXPECTED_STOP_CODES:
            raise ValueError(f"unsupported expected init outcome: {self.code}")
        if self.outcome == "failure" and self.code in EXPECTED_STOP_CODES | {"success"}:
            raise ValueError(f"failure init result cannot use reserved code: {self.code}")
        if self.code == "already_done" and self.context.task_dir is None:
            raise ValueError("already_done init result requires context.taskDir")
        if self.context.task_dir is not None and self.context.review_dirs:
            if self.context.task_dir not in self.context.review_dirs:
                raise ValueError("context.taskDir must be included in context.reviewDirs")
        return self


class InitResultEnvelopeV2(InitResultEnvelope):
    schema_version: Literal[2] = Field(default=2, alias="schemaVersion")
    comparisons: list[ComparisonSummary]


def parse_init_result(payload: dict) -> InitResultEnvelope | InitResultEnvelopeV2:
    if not isinstance(payload, dict):
        raise ValueError("init result must be a JSON object")
    model = InitResultEnvelopeV2 if payload.get("schemaVersion") == 2 else InitResultEnvelope
    return model.model_validate(payload)


def write_init_result(path: Path, result: InitResultEnvelope | InitResultEnvelopeV2) -> None:
    """Atomically write UTF-8 JSON with LF line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        result.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
