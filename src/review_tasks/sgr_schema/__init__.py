"""SGR YAML schema — single source of truth for review_result.yaml structure.

Public surface:

- ``ReviewResponse`` — renderer-owned compatibility projection.
- ``translate_validation_error`` — преобразует ``pydantic.ValidationError`` в
  ``ReviewYamlError`` с русскими сообщениями (используется
  ``review_tasks.review_result.review_yaml.validate_review_yaml_text``).
- ``FOCUS_AREA_CATALOG`` — упорядоченный каталог SGR-областей с
  описаниями и ссылками на источники стандартов.
- ``render_focus_areas_list`` — список областей из канонического каталога
  для ``_review_info/review_prompt_stage1.md``.
"""

from review_tasks.sgr_schema.enums import FOCUS_AREA_CATALOG
from review_tasks.sgr_schema.error_translation import (
    ReviewYamlError,
    translate_validation_error,
)
from review_tasks.sgr_schema.prompt_fragments import render_focus_areas_list
from review_tasks.sgr_schema.stage_results import (
    ReviewResponse,
    ReviewStage1Result,
    ReviewStage2Result,
    RiskCandidate,
    StandardHints,
)

__all__ = [
    "FOCUS_AREA_CATALOG",
    "ReviewResponse",
    "ReviewStage1Result",
    "ReviewStage2Result",
    "RiskCandidate",
    "StandardHints",
    "ReviewYamlError",
    "render_focus_areas_list",
    "translate_validation_error",
]
