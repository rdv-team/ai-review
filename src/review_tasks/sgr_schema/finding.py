"""Pydantic-модели Finding, SelfCheck, Verification (SGR-голова, фаза 6).

Это центральная фаза SGR: каждое наблюдение проходит обязательный
self-check, опциональную внешнюю верификацию и публикуется в отчёт
только при ``decision: keep``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import StrictBool, model_validator

from review_tasks.sgr_schema.enums import (
    ConfidenceLevel,
    FalsePositiveRisk,
    FindingSource,
    FocusArea,
    SelfCheckDecision,
    Severity,
    TraceStatus,
    VerificationStatus,
    VerificationSummary,
)
from review_tasks.sgr_schema.primitives import (
    LineNumber,
    NonBlankStr,
    CandidateName,
    ReviewSourcePath,
    SgrBaseModel,
)


class VerificationChannel(SgrBaseModel):
    """Канал внешней верификации (MCP, std-документ, ручная проверка)."""

    capability: str | None = None
    hypothesis: str | None = None
    status: VerificationStatus | None = None
    result_note: str | None = None


class Verification(SgrBaseModel):
    """Блок внешней верификации finding'а.

    ``needed`` объявлен как ``StrictBool``: PyYAML легко превращает
    ``yes``/``no``/``"true"`` в bool, что искажает сигнал ревью; строгое
    сравнение по типу повторяет историческое поведение ``_is_bool``
    (Decision 10 в design.md).
    """

    needed: StrictBool | None = None
    channels: list[VerificationChannel] | None = None
    verification_summary: VerificationSummary | None = None

    @model_validator(mode="after")
    def _needed_channels_consistent(self) -> Verification:
        if self.needed is False and self.channels:
            raise ValueError(
                "при `verification.needed: false` список `channels` "
                "должен быть пуст или отсутствовать"
            )
        if self.needed is True and self.channels:
            for index, channel in enumerate(self.channels):
                if channel.status not in ("applied", "unavailable"):
                    raise ValueError(
                        f"`verification.channels[{index}].status` "
                        "должен быть `applied` или `unavailable`"
                    )
        return self


class Trace(SgrBaseModel):
    """Трасса обязательства: точка A создаёт обязательство, точка B закрывает его."""

    established_at: LineNumber
    obligation: NonBlankStr
    satisfied_at: LineNumber | None = None
    status: TraceStatus

    @model_validator(mode="after")
    def _status_matches_satisfied_at(self) -> Trace:
        if self.status == "satisfied" and self.satisfied_at is None:
            raise ValueError(
                "`traces[].status: satisfied` требует непустой `satisfied_at`"
            )
        return self


class SelfCheck(SgrBaseModel):
    """Блок self-check finding'а: внутреннее решение keep/drop.

    Бул-поля используют ``StrictBool``, чтобы случайный
    YAML-coerce ``"true" → True`` не прошёл валидацию.

    Cross-field правила:

    - ``decision: drop`` ⇒ ``drop_reason`` не null и не пустая строка.

    Правила ``keep``/``drop`` для ``suggestion`` и ``severity`` реализованы
    на уровне ``Finding`` (Decision 2 в design.md).
    """

    false_positive_risk: FalsePositiveRisk
    alternative_interpretation: str | None = None
    decision: SelfCheckDecision
    drop_reason: str | None = None

    @model_validator(mode="after")
    def _drop_requires_reason(self) -> SelfCheck:
        if self.decision == "drop" and (
            self.drop_reason is None or not self.drop_reason.strip()
        ):
            raise ValueError(
                "`self_check.drop_reason` обязателен при `self_check.decision: drop`"
            )
        return self


class Finding(SgrBaseModel):
    """Одно наблюдение ревью с обязательным self-check.

    Контракт ``review_yaml.py`` требует перечисленные ниже поля.
    `verification` остаётся необязательным. ``traces`` обязателен как
    ключ, но может быть пустым списком для находок без межстрочной
    трассы обязательства. ``severity`` объявлен как
    ``Severity | None`` без значения по умолчанию: поле обязано
    присутствовать в YAML, но может быть ``null`` при ``decision: drop``.

    Cross-field правила:

    - ``start_line`` ≤ ``end_line``;
    - ``decision: keep`` ⇒ ``suggestion`` непуст, ``drop_reason == null``,
      ``severity != null``;
    - ``decision: drop`` ⇒ ``suggestion == null``, ``drop_reason`` непуст,
      ``severity == null``.
    """

    area: FocusArea
    name: CandidateName
    origin: Literal["stage2"] | None = None
    file: ReviewSourcePath
    start_line: LineNumber
    end_line: LineNumber
    observation: NonBlankStr
    question_to_author: NonBlankStr | None = None
    source: FindingSource
    confidence: ConfidenceLevel
    self_check: SelfCheck
    severity: Severity | None
    suggestion: NonBlankStr | None
    verification: Verification | None = None
    traces: list[Trace]

    @model_validator(mode="after")
    def _line_range_consistent(self) -> Finding:
        if self.start_line > self.end_line:
            raise ValueError(
                "`start_line` должен быть <= `end_line`"
            )
        return self

    @model_validator(mode="after")
    def _decision_rules(self) -> Finding:
        decision = self.self_check.decision
        if decision == "keep":
            if self.suggestion is None or not str(self.suggestion).strip():
                raise ValueError(
                    "`suggestion` обязателен при `self_check.decision: keep`"
                )
            if self.self_check.drop_reason is not None:
                raise ValueError(
                    "`self_check.drop_reason` должен быть null "
                    "при `self_check.decision: keep`"
                )
            if self.severity is None:
                raise ValueError(
                    "`severity` обязателен при `self_check.decision: keep`"
                )
        elif decision == "drop":
            if self.suggestion is not None:
                raise ValueError(
                    "`suggestion` должен быть null при `self_check.decision: drop`"
                )
            if self.severity is not None:
                raise ValueError(
                    "`severity` должен быть null при `self_check.decision: drop`"
                )
        return self


__all__ = [
    "Finding",
    "SelfCheck",
    "Trace",
    "Verification",
    "VerificationChannel",
]
