"""Строгие контракты двух стадий и renderer-owned совместимого результата."""

from __future__ import annotations

from pydantic import model_validator

from review_tasks.sgr_schema.enums import FocusArea
from review_tasks.sgr_schema.finding import Finding
from review_tasks.sgr_schema.primitives import (
    CandidateName,
    LineNumber,
    NonBlankStr,
    ReviewSourcePath,
    SgrBaseModel,
)


class StandardHints(SgrBaseModel):
    dev_standard_ids: list[NonBlankStr]
    platform_index_files: list[NonBlankStr]
    bsp_files: list[NonBlankStr]

    @model_validator(mode="after")
    def _unique_lists(self) -> "StandardHints":
        for field in ("dev_standard_ids", "platform_index_files", "bsp_files"):
            values = getattr(self, field)
            if len(set(values)) != len(values):
                raise ValueError(f"`standard_hints.{field}` не должен содержать дубли")
        return self


class RiskCandidate(SgrBaseModel):
    name: CandidateName
    area: FocusArea
    file: ReviewSourcePath
    start_line: LineNumber
    end_line: LineNumber
    observation: NonBlankStr
    standard_hints: StandardHints

    @model_validator(mode="after")
    def _line_range(self) -> "RiskCandidate":
        if self.start_line > self.end_line:
            raise ValueError("`start_line` должен быть <= `end_line`")
        return self


class RiskCandidates(SgrBaseModel):
    items: list[RiskCandidate]

    @model_validator(mode="after")
    def _unique_names(self) -> "RiskCandidates":
        names = [item.name for item in self.items]
        if len(names) != len(set(names)):
            raise ValueError("поле `risk_candidates.items[].name` должно содержать уникальные значения")
        return self


class ReviewStage1Result(SgrBaseModel):
    risk_candidates: RiskCandidates


class ReviewStage2Result(SgrBaseModel):
    findings: list[Finding]
    missing_context: list[str]


class ReviewResponse(SgrBaseModel):
    """Совместимая производная проекция, записываемая только renderer-ом."""

    # Field declaration order is the persisted compatibility contract.
    risk_candidates: RiskCandidates
    findings: list[Finding]
    missing_context: list[str]

    @model_validator(mode="after")
    def _cross_file_coverage(self) -> "ReviewResponse":
        candidates = {item.name: item for item in self.risk_candidates.items}
        seen: set[str] = set()
        for index, finding in enumerate(self.findings):
            candidate = candidates.get(finding.name)
            if finding.name in seen:
                raise ValueError(f"finding `{finding.name}` повторяется")
            seen.add(finding.name)
            if candidate is not None and finding.area != candidate.area:
                raise ValueError(
                    f"findings[{index}].area `{finding.area}` не совпадает с candidate "
                    f"`{finding.name}` (ожидалось `{candidate.area}`)"
                )
            if candidate is not None and _normalized_path(finding.file) != _normalized_path(candidate.file):
                raise ValueError(
                    f"findings[{index}].file `{finding.file}` не совпадает с candidate "
                    f"`{finding.name}` (ожидалось `{candidate.file}`)"
                )
        for name in candidates:
            if name not in seen:
                raise ValueError(f"для candidate `{name}` отсутствует finding")
        return self


def _normalized_path(value: str) -> str:
    normalized = value.replace("\\", "/").casefold()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


__all__ = [
    "ReviewResponse",
    "ReviewStage1Result",
    "ReviewStage2Result",
    "RiskCandidate",
    "RiskCandidates",
    "StandardHints",
]
