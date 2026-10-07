"""Валидация и рендер YAML-результата ревью.

Этот модуль — тонкая оболочка над пакетом ``sgr_schema``: основная схема,
кросс-полевые инварианты и перевод ошибок Pydantic v2 живут там. Здесь
остаются:

- text-level pre-validation (отказ от markdown fence-обёртки и проверка,
  что YAML парсится в объект);
- нормализация к чистому контракту перед ``model_validate``;
- сборка финального Markdown-отчёта из ``findings[]`` с ``decision: keep``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from review_tasks.sgr_schema import (
    ReviewResponse,
    ReviewStage1Result,
    ReviewStage2Result,
    ReviewYamlError,
    translate_validation_error,
)
from review_tasks.sgr_schema.normalization import normalize_review_result_mapping
from review_tasks.review_result.stage1_storage import (
    STAGE1_NAME,
    STAGE2_NAME,
    load_stage1,
    _write_atomic,
)
from review_tasks.integrations.gitlab_links import (
    build_gitlab_blob_url,
    is_git_commit_sha,
    normalize_git_remote_project_url,
    normalize_source_path,
)

SEVERITY_TITLES = {
    "critical": "Критично",
    "important": "Важно",
    "desirable": "Желательно",
}

DEFAULT_REPORT_TEMPLATE = """# RDV AIR — Отчёт ревью — {{TASK}}

## Сводка

{{SUMMARY}}

## 🔴 Критично

{{CRITICAL_ISSUES}}

## 🟡 Важно

{{IMPORTANT_ISSUES}}

## 💭 Желательно

{{DESIRABLE_ISSUES}}

## Вопросы к автору

{{QUESTIONS_TO_AUTHOR}}

## Контекст, которого не хватило

{{MISSING_CONTEXT}}
"""


__all__ = [
    "DEFAULT_REPORT_TEMPLATE",
    "ReviewYamlDocument",
    "ReviewYamlError",
    "ReviewYamlValidationResult",
    "SEVERITY_TITLES",
    "expand_review_result_inputs",
    "extract_review_yaml_text",
    "read_review_yaml_documents",
    "render_review_report",
    "validate_review_yaml_file",
    "validate_review_yaml_text",
    "read_stage_pair_for_render",
    "combined_mapping",
    "write_combined_result",
]


@dataclass(frozen=True)
class ReviewYamlDocument:
    """Один YAML-результат ревью, типизированный через ``ReviewResponse``.

    Поле ``parsed`` — единственный источник истины для всех последующих
    стадий обработки (рендер отчётов, агрегация warnings).
    """

    source: Path
    parsed: ReviewStage2Result | ReviewResponse


@dataclass(frozen=True)
class ReviewYamlValidationResult:
    """Результат валидации одного YAML-файла."""

    parsed: ReviewStage1Result | ReviewStage2Result | ReviewResponse
    warnings: tuple[str, ...] = ()


_FENCED_YAML_BLOCK_RE = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_FENCED_BLOCK_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_YAML_DOC_START_RE = re.compile(
    r"^(?:findings|summary|questions_to_author|missing_context):",
    re.MULTILINE,
)


def extract_review_yaml_text(text: str, *, source: str = "<input>") -> str:
    """Extract YAML payload from staged agent output or bare YAML input."""
    del source
    matches = list(_FENCED_YAML_BLOCK_RE.finditer(text))
    if matches:
        return matches[-1].group(1).strip()

    doc_start = _YAML_DOC_START_RE.search(text)
    if doc_start is not None:
        preamble = text[: doc_start.start()]
        body = text[doc_start.start() :]
        preamble_without_fences = _FENCED_BLOCK_RE.sub("", preamble)
        return (preamble_without_fences + body).strip()

    return text.strip()


def _load_stage2_text(text: str, *, source: str) -> ReviewStage2Result:
    """Parse the Stage 2 payload; its final fenced YAML block is authoritative."""
    extracted = extract_review_yaml_text(text, source=source)
    try:
        loaded = yaml.safe_load(extracted)
    except yaml.YAMLError as exc:
        raise ReviewYamlError(f"{source}: YAML не разбирается: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ReviewYamlError(f"{source}: YAML должен быть объектом верхнего уровня.")
    try:
        return ReviewStage2Result.model_validate(normalize_review_result_mapping(loaded, source=source))
    except ValidationError as exc:
        raise translate_validation_error(exc, source=source) from exc


def validate_review_yaml_text(text: str, *, source: str = "<input>") -> ReviewYamlValidationResult:
    """Validate the renderer-owned combined payload for compatibility consumers."""
    extracted = extract_review_yaml_text(text, source=source)
    try:
        loaded = yaml.safe_load(extracted)
    except yaml.YAMLError as exc:
        raise ReviewYamlError(f"{source}: YAML не разбирается: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ReviewYamlError(f"{source}: YAML должен быть объектом верхнего уровня.")
    try:
        parsed = ReviewResponse.model_validate(
            normalize_review_result_mapping(loaded, source=source)
        )
    except ValidationError as exc:
        raise translate_validation_error(exc, source=source) from exc
    return ReviewYamlValidationResult(parsed=parsed)


def _normalize_finding_path(value: str) -> str:
    """Привести путь к posix-форме для сопоставления finding.file ↔ ключ карты.

    `BslFilePath` хранит исходную строку без нормализации слэшей/регистра
    (см. `primitives._validate_bsl_file_path`), поэтому матчинг ведём по
    приведённому виду: backslash→slash, casefold, срезанный ведущий `./`.
    Промах матчинга означает тихий пропуск проверки, поэтому нормализуем
    агрессивно — два пути, различающиеся только регистром, в git не сосуществуют.
    """
    normalized = value.replace("\\", "/").casefold()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _load_line_bounds(sidecar: Path) -> dict[str, tuple[int, int]]:
    """Загрузить sidecar-карту границ строк; ключи нормализованы для матчинга."""
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewYamlError(
            f"{sidecar}: не удалось прочитать карту границ строк review_lines.json: {exc}"
        ) from exc
    bounds: dict[str, tuple[int, int]] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if (
                isinstance(value, list)
                and len(value) == 2
                and all(isinstance(item, int) for item in value)
            ):
                bounds[_normalize_finding_path(str(key))] = (value[0], value[1])
    return bounds


def _check_finding_line_bounds(
    path: Path,
    parsed: ReviewStage2Result,
    stage1: ReviewStage1Result,
) -> None:
    """Отклонить finding со строками вне `[min, max]` файла из соседней карты.

    Карта `review_lines.json` ищется как сосед `path`. Если её нет — проверка
    границ пропускается (автономный `validate-review-yaml` для «голого» YAML вне
    каталога задачи остаётся рабочим). Файл, отсутствующий в карте, не
    проверяется. Внутрифайловая проверка `start_line <= end_line` живёт в
    `Finding` и применяется независимо от наличия артефакта.
    """
    sidecar = path.parent / "review_lines.json"
    if not sidecar.is_file():
        raise ReviewYamlError(f"Не найдена обязательная карта границ строк: {sidecar}")
    bounds = _load_line_bounds(sidecar)
    candidates = {candidate.name: candidate for candidate in stage1.risk_candidates.items}
    for index, finding in enumerate(parsed.findings):
        file_bounds = bounds.get(_normalize_finding_path(finding.file))
        if file_bounds is None:
            raise ReviewYamlError(f"findings[{index}]: файл `{finding.file}` отсутствует в review_lines.json")
        low, high = file_bounds
        if finding.start_line < low or finding.end_line > high:
            raise ReviewYamlError(
                f"findings[{index}] candidate `{finding.name}`, файл `{finding.file}`: строки "
                f"{finding.start_line}-{finding.end_line} вне допустимого диапазона {low}-{high}; "
                f"измените только findings[{index}].start_line/end_line по видимым TARGET-координатам, "
                "Не пересоздавайте YAML целиком и повторно запустите валидацию."
            )
        candidate = candidates.get(finding.name)
        if candidate is None:
            continue
        if _normalize_finding_path(finding.file) != _normalize_finding_path(candidate.file):
            raise ReviewYamlError(
                f"findings[{index}].file=`{finding.file}`, ожидался файл `{candidate.file}` "
                f"для candidate `{finding.name}`; измените только findings[{index}].file. "
                "Не пересоздавайте YAML целиком. Исправьте только указанную запись и повторите валидацию."
            )


def validate_review_yaml_file(path: Path) -> ReviewYamlValidationResult:
    if path.name == STAGE1_NAME:
        parsed = load_stage1(cwd=path.parent.parent)
        return ReviewYamlValidationResult(parsed=parsed)
    if path.name == STAGE2_NAME:
        stage1 = load_stage1(cwd=path.parent.parent)
        parsed = _load_stage2_text(path.read_text(encoding="utf-8"), source=str(path))
        _validate_stage_coverage(stage1, parsed)
        _check_finding_line_bounds(path, parsed, stage1)
        return ReviewYamlValidationResult(parsed=parsed)
    # Compatibility reader for the unchanged local service.
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ReviewYamlError(f"{path}: YAML должен быть объектом верхнего уровня.")
        normalized = normalize_review_result_mapping(raw, source=str(path))
        # Existing local-service fixtures and already completed task catalogs may
        # contain the historical public-only projection. New renderer output is
        # always identified by risk_candidates and validated by the strict
        # five-section compatibility model.
        if "risk_candidates" in normalized:
            parsed = ReviewResponse.model_validate(normalized)
        else:
            # Historical service-only public projections used the previous
            # numeric finding identifier format.
            # Adapt them in memory; canonical stage files and renderer output
            # never pass through this compatibility branch.
            for finding in normalized.get("findings", []):
                if isinstance(finding, dict) and isinstance(finding.get("name"), str):
                    match = re.fullmatch(r"risk-([1-9][0-9]*)", finding["name"])
                    if match:
                        finding["name"] = f"candidate-{match.group(1)}"
            parsed = ReviewStage2Result.model_validate(normalized)
    except yaml.YAMLError as exc:
        raise ReviewYamlError(f"{path}: YAML не разбирается: {exc}") from exc
    except ValidationError as exc:
        raise translate_validation_error(exc, source=str(path)) from exc
    return ReviewYamlValidationResult(parsed=parsed)


def _validate_stage_coverage(stage1: ReviewStage1Result, stage2: ReviewStage2Result) -> None:
    candidates = {candidate.name: candidate for candidate in stage1.risk_candidates.items}
    seen: dict[str, int] = {}
    for index, finding in enumerate(stage2.findings):
        candidate = candidates.get(finding.name)
        if finding.name in seen:
            raise ReviewYamlError(
                f"finding `{finding.name}` повторяется в findings[{seen[finding.name]}] и findings[{index}]; "
                f"исправьте только один из этих records, Не пересоздавайте YAML целиком и повторно запустите валидацию."
            )
        seen[finding.name] = index
        if candidate is not None and finding.area != candidate.area:
            raise ReviewYamlError(
                f"findings[{index}].area=`{finding.area}`, ожидалось `{candidate.area}` для candidate `{finding.name}`; "
                f"исправьте только findings[{index}].area, Не пересоздавайте YAML целиком и повторно запустите валидацию."
            )
    for candidate in stage1.risk_candidates.items:
        if candidate.name not in seen:
            raise ReviewYamlError(
                f"candidate `{candidate.name}` не имеет finding; добавьте ровно один findings[] с name `{candidate.name}` "
                f"и area `{candidate.area}`, Не пересоздавайте YAML целиком и повторно запустите валидацию."
            )


def expand_review_result_inputs(inputs: list[Path]) -> list[Path]:
    expanded: list[Path] = []
    for path in inputs:
        if path.is_dir():
            expanded.extend(_expand_review_info_dir(path))
        else:
            expanded.append(path)
    return expanded


def _expand_review_info_dir(path: Path) -> list[Path]:
    single = path / "review_result.yaml"
    indexed = sorted(path.glob("review_result_*.yaml"))
    indexed = [item for item in indexed if re.fullmatch(r"review_result_\d{3}\.yaml", item.name)]
    if single.exists() and indexed:
        raise ReviewYamlError("В каталоге _review_info смешаны одиночный и sequence режимы результатов.")
    if single.exists():
        return [single]
    if not indexed:
        return []

    result: list[Path] = []
    index = 1
    while True:
        candidate = path / f"review_result_{index:03d}.yaml"
        if not candidate.exists():
            break
        result.append(candidate)
        index += 1
    return result


def discover_stage_pair(path: Path) -> tuple[Path, Path]:
    """Renderer-only discovery. Generic compatibility discovery remains above."""
    stage1 = path / STAGE1_NAME
    stage2 = path / STAGE2_NAME
    if not stage1.is_file() or not stage2.is_file():
        missing = STAGE1_NAME if not stage1.is_file() else STAGE2_NAME
        raise ReviewYamlError(f"Для рендера не найден обязательный файл пары: {missing}")
    allowed = {STAGE1_NAME, STAGE2_NAME, "review_result.yaml"}
    unexpected = sorted(item.name for item in path.glob("*.yaml") if item.name not in allowed)
    if unexpected:
        raise ReviewYamlError(
            "Для рендера допустима только каноническая stage-пара; лишние YAML: "
            + ", ".join(unexpected)
        )
    return stage1, stage2


def read_stage_pair_for_render(path: Path) -> tuple[ReviewStage1Result, ReviewYamlDocument]:
    stage1_path, stage2_path = discover_stage_pair(path)
    stage1_result = validate_review_yaml_file(stage1_path).parsed
    stage2_result = validate_review_yaml_file(stage2_path).parsed
    assert isinstance(stage1_result, ReviewStage1Result)
    assert isinstance(stage2_result, ReviewStage2Result)
    return stage1_result, ReviewYamlDocument(source=stage2_path, parsed=stage2_result)


def combined_mapping(stage1: ReviewStage1Result, stage2: ReviewStage2Result) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    for finding in stage2.findings:
        serialized = finding.model_dump(mode="python")
        if "origin" not in finding.model_fields_set:
            serialized.pop("origin", None)
        findings.append(serialized)
    return {
        "risk_candidates": stage1.risk_candidates.model_dump(mode="python"),
        "findings": findings,
        "summary": stage2.summary,
        "questions_to_author": stage2.questions_to_author,
        "missing_context": stage2.missing_context,
    }


def write_combined_result(review_info: Path, stage1: ReviewStage1Result, stage2: ReviewStage2Result) -> None:
    """Atomically persist the deterministic renderer-owned compatibility result."""
    _write_atomic(review_info / "review_result.yaml", combined_mapping(stage1, stage2))


def read_review_yaml_documents(paths: list[Path]) -> list[ReviewYamlDocument]:
    documents: list[ReviewYamlDocument] = []
    for path in paths:
        result = validate_review_yaml_file(path)
        documents.append(ReviewYamlDocument(source=path, parsed=result.parsed))
    return documents


def render_review_report(
    documents: list[ReviewYamlDocument],
    *,
    task: str,
    template_text: str | None = None,
) -> str:
    source_links = _load_source_links_for_documents(documents)
    summary = _render_summary(documents)
    issue_sections: dict[str, str] = {}
    next_number = 1
    for severity in ("critical", "important", "desirable"):
        issue_sections[severity], next_number = _render_issues(
            documents, severity, source_links, next_number
        )
    questions = _render_string_list(
        [item for doc in documents for item in doc.parsed.questions_to_author],
        empty="_Нет вопросов._",
    )
    missing = _render_string_list(
        [item for doc in documents for item in doc.parsed.missing_context],
        empty="_Не зафиксирован._",
    )
    if template_text is None:
        template_text = DEFAULT_REPORT_TEMPLATE
    values = {
        "TASK": task,
        "SUMMARY": summary,
        "CRITICAL_ISSUES": issue_sections["critical"],
        "IMPORTANT_ISSUES": issue_sections["important"],
        "DESIRABLE_ISSUES": issue_sections["desirable"],
        "QUESTIONS_TO_AUTHOR": questions,
        "MISSING_CONTEXT": missing,
    }
    rendered = template_text
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered.rstrip() + "\n"


def _source_label(path: Path) -> str:
    match = re.search(r"review_result_(\d{3})\.yaml$", path.name)
    if match:
        return f"prompt {match.group(1)}"
    return path.name


def _render_suggestion_markdown(suggestion: str) -> list[str]:
    """Emit GFM-safe Предложение block: label line, blank line, then suggestion body."""
    body = suggestion.strip()
    lines = ["- **Предложение**:", ""]
    if body:
        lines.append(body)
    return lines


def _render_summary(documents: list[ReviewYamlDocument]) -> str:
    blocks: list[str] = []
    for doc in documents:
        summary = doc.parsed.summary.strip()
        if summary:
            blocks.append(f"### {_source_label(doc.source)}\n\n{summary}")
    return "\n\n".join(blocks) if blocks else "_Нет замечаний по сводке._"


SourceLink = tuple[str, str, str]


def _load_source_links_for_documents(
    documents: list[ReviewYamlDocument],
) -> dict[Path, dict[str, SourceLink]]:
    cache: dict[Path, dict[str, SourceLink]] = {}
    for document in documents:
        directory = document.source.parent
        if directory not in cache:
            cache[directory] = _load_source_links(directory / "review_source_links.json")
    return cache


def _load_source_links(sidecar: Path) -> dict[str, SourceLink]:
    try:
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return {}
    raw_files = raw.get("files")
    if not isinstance(raw_files, dict):
        return {}

    links: dict[str, SourceLink] = {}
    conflicts: set[str] = set()
    for path, entry in raw_files.items():
        if not isinstance(path, str) or not isinstance(entry, dict):
            continue
        normalized_path = normalize_source_path(path)
        if normalized_path is None or normalized_path != path:
            continue
        project_url = entry.get("project_url")
        target = entry.get("target")
        if not isinstance(project_url, str) or not isinstance(target, str):
            continue
        if normalize_git_remote_project_url(project_url) != project_url.rstrip("/"):
            continue
        if project_url != project_url.rstrip("/") or not is_git_commit_sha(target):
            continue
        lookup_key = _normalize_finding_path(path)
        if lookup_key in links:
            conflicts.add(lookup_key)
            links.pop(lookup_key, None)
            continue
        if lookup_key not in conflicts:
            links[lookup_key] = (project_url, target.lower(), normalized_path)
    return links


def _escape_markdown_link_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _finding_heading(
    document: ReviewYamlDocument,
    finding: object,
    source_links: dict[Path, dict[str, SourceLink]],
    number: int,
) -> str:
    file = finding.file
    start_line = finding.start_line
    end_line = finding.end_line
    label = f"{file}:{start_line}-{end_line}"
    prefix = f"### №{number} — "
    entry = source_links.get(document.source.parent, {}).get(_normalize_finding_path(file))
    if entry is None:
        return f"{prefix}{label}"
    project_url, target, source_path = entry
    destination = build_gitlab_blob_url(project_url, target, source_path, start_line, end_line)
    if destination is None:
        return f"{prefix}{label}"
    return f"{prefix}[{_escape_markdown_link_label(label)}]({destination})"


def _render_issues(
    documents: list[ReviewYamlDocument],
    severity: str,
    source_links: dict[Path, dict[str, SourceLink]],
    next_number: int,
) -> tuple[str, int]:
    blocks: list[str] = []
    for doc in documents:
        for finding in doc.parsed.findings:
            if finding.self_check.decision != "keep":
                continue
            if finding.severity != severity:
                continue
            header = _finding_heading(doc, finding, source_links, next_number)
            body = [header]
            body.append(f"- **Риск**: {SEVERITY_TITLES[severity].lower()}")
            body.append(f"- **Суть**: {finding.observation}")
            if finding.suggestion:
                body.extend(_render_suggestion_markdown(finding.suggestion))
            blocks.append("\n".join(body))
            next_number += 1
    return ("\n\n".join(blocks) if blocks else "_Нет замечаний._"), next_number


def _render_string_list(items: list[str], *, empty: str) -> str:
    cleaned = [item.strip() for item in items if item.strip()]
    if not cleaned:
        return empty
    return "\n".join(f"- {item}" for item in cleaned)
