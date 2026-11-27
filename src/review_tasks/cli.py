#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI для подготовки самодостаточного каталога ревью по задаче.

Запуск из произвольного каталога:

    review-tasks init -t <TASK[,TASK2,...]> -r <РЕПОЗИТОРИЙ> -d <КАТАЛОГ> [-b <REF>] [--mr <ID> | --mr-from-tasks] [--remote <NAME>]
    python -m review_tasks init -t <TASK[,TASK2,...]> -r <РЕПОЗИТОРИЙ> -d <КАТАЛОГ> [-b <REF>] [--mr <ID> | --mr-from-tasks] [--remote <NAME>]

Несколько задач через запятую в -t дают отдельный полный прогон
и отдельный каталог под каждый ключ; код выхода — первый ненулевой по порядку.
В режиме --mr допускается ровно одна задача (правило: один MR = одна задача).
Флаг --mr-from-tasks включает поиск всех MR по переданным задачам без изменения
обычного task-режима.

Аргументы:
    -t / --tasks       Идентификатор задачи или список через запятую (обязательный)
    -r / --repo        Путь к целевому git-репозиторию (обязательный)
    -d / --destination Каталог для записи артефактов ревью; создаётся при отсутствии (обязательный)
    -b / --branch      Ref-источник коммитов (необязательный; по умолчанию: origin/master)
    -m / --mr          Идентификатор GitLab MR (целое > 0). Включает MR-режим: коммиты
                       берутся из refs/merge-requests/<ID>/head, а не по `git log --grep`.
    --mr-from-tasks    Найти все локально доступные MR по задачам из -t и подготовить
                       отдельный каталог <TASK>__mr-<ID> для каждого найденного MR.
    --remote           Имя remote для fetch и MR-ref (по умолчанию: origin).

Коды возврата:
    0 — успех (все задачи при списке завершились успешно)
    1 — не указаны обязательные флаги, путь к репозиторию не является git-репозиторием,
        неверный --mr (<= 0), нарушено правило «один MR = одна задача»,
        конфликт --mr/--mr-from-tasks, ошибка fetch MR-ref
    2 — коммиты не найдены (в task-режиме — по строке TASK; в MR-режиме — пустой BASE..MR_HEAD)
    3 — коммиты найдены, но список изменённых путей пуст
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from io import TextIOWrapper
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .adapters import IDE_CHOICES, IdeAdapterError, get_adapter
from .review_init.comparison import ComparisonSource, ComparisonSummary, task_lock, write_comparison
from .review_init.task_scope import (
    BLAME_HEADER_RE, HUNK_HEADER_RE, BlameLine, DiffHunk, DiffLine, NetOldEntry, NetOldLineState, PromptLine,
    apply_net_old_hunks as replay_task_hunks,
    build_net_old_hunk_segment, hunk_old_index, parse_final_blame, parse_unified_diff_hunks,
)
from .infrastructure.git import (
    ToolError,
    _run,
    decode_process_stderr,
    git_ok,
    git_result,
    git_text,
    run_git,
)
from .infrastructure.text_files import read_text_safe, write_text_lf
from .repository_context import prepare_sources, write_sources
from .integrations.gitlab_links import (
    is_git_commit_sha,
    normalize_git_remote_project_url,
    normalize_source_path,
    resolve_git_remote_project_url,
)
from .integrations.mcp_registry import load_mcp_registry
from .index_json import ObjectIndexes, ObjectMetadata, write_indexes_objects, write_metadata_objects
from .index_json.build import indexes_from_parsed, iter_parsed_sources, object_metadata_from
from .index_json.config_xml import GitConfigSource, compute_config_root
from .reference_catalog import ReferenceCatalogError, write_reference_catalog
from .review_init import prefix_config
from .review_init.commit_source import (
    Commit, RepositoryContext, SourceResolution, _fetch_and_resolve_repository,
    _resolve_repo_root, build_commit_selection, find_commits_for_tasks,
    normalize_after_commits, normalize_repository_path, restrict_sources, resolve_mr_source,
    resolve_mr_sources_from_tasks, resolve_task_source,
    validate_source_identity,
)
from .review_init.agent_skills import SkillProjectionError, project_agent_skills
from .review_init.prompt_config import ConfigError, EffectiveConfig, ReviewPromptConfig, load_effective_config
from .review_init.result import InitResultContext, InitResultEnvelope, InitResultEnvelopeV2, InitResultEnvelopeV3, write_init_result
from .review_result.review_yaml import (
    ReviewYamlError,
    expand_review_result_inputs,
    read_review_yaml_documents,
    render_review_report,
    validate_review_yaml_file,
    read_stage_pair_for_render,
    write_combined_result,
)
from .review_result.history import archive_report, preserve_report_history, restore_report_history
from .sgr_schema import render_focus_areas_list
from .task_capsule.user_instruction import (
    UserInstructionError,
    validate_user_instruction,
    write_user_instruction,
)


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"

_REVIEW_BSL_TEMPLATE = """# BSL-блоки ревью — {{TASK}}

## BSL-блоки

{{PROMPT_BSL_BLOCKS}}

## Сводка токенов

{{PROMPT_TOKEN_SUMMARY}}
"""

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_COMMITS = 2
EXIT_NO_PATHS = 3


@dataclass
class InitResultRecorder:
    outcome: str | None = None
    code: str | None = None
    message: str | None = None
    review_dirs: list[Path] = field(default_factory=list)
    comparisons: list[ComparisonSummary] = field(default_factory=list)
    commit_selection: dict | None = None

    def record_expected_stop(self, code: str, message: str, task_dir: Path) -> None:
        if code == 'already_done':
            # The requested source was resolved, but the existing capsule was
            # not rebuilt. It must never acquire the newly requested scope.
            self.commit_selection = None
        if self.outcome == "expected_stop":
            return
        self.outcome = "expected_stop"
        self.code = code
        self.message = message
        self.review_dirs = [task_dir]

    def record_review_dir(self, review_dir: Path) -> None:
        if review_dir not in self.review_dirs:
            self.review_dirs.append(review_dir)

    def envelope(self, exit_code: int, schema_version: int = 1) -> InitResultEnvelope:
        if self.outcome in {"expected_stop", "failure"} and self.code and self.message:
            outcome = self.outcome
            code = self.code
            message = self.message
        elif exit_code == EXIT_OK:
            outcome = "success"
            code = "success"
            message = "Каталог ревью подготовлен."
        else:
            code = {
                EXIT_NO_COMMITS: "no_commits",
                EXIT_NO_PATHS: "no_reviewable_files",
            }.get(exit_code, "init_failed")
            outcome = "expected_stop" if code in {"no_commits", "no_reviewable_files"} else "failure"
            message = {
                EXIT_NO_COMMITS: "Не найдены коммиты для подготовки ревью.",
                EXIT_NO_PATHS: "Не найдены поддерживаемые изменения для ревью.",
            }.get(exit_code, "Не удалось подготовить каталог ревью.")
        task_dir = self.review_dirs[0] if len(self.review_dirs) == 1 else None
        model = {2: InitResultEnvelopeV2, 3: InitResultEnvelopeV3}.get(schema_version, InitResultEnvelope)
        extra = {"comparisons": self.comparisons} if schema_version >= 2 else {}
        if schema_version == 3:
            extra['commit_selection'] = self.commit_selection
        return model(
            **extra,
            outcome=outcome,
            code=code,
            message=message,
            context=InitResultContext(
                task_dir=str(task_dir) if task_dir is not None else None,
                review_dirs=[str(path) for path in self.review_dirs],
            ),
        )

TASK_MCP_REGISTRY_RELATIVE = Path(".review-tasks") / "mcp" / "registry.json"

# Распознаём и русские, и английские ключевые слова 1С/BSL.
METHOD_START_RE = re.compile(
    r"^\s*(Функция|Процедура|Function|Procedure)\s+"
    r"([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)\s*\(",
    re.UNICODE | re.IGNORECASE,
)
COMPLETED_STATUS_RE = re.compile(r"(?im)^\s*status\s*[:=]\s*completed\s*$")


# ---------------------------------------------------------------------------
# Файлы и имена
# ---------------------------------------------------------------------------

def has_completed_result_status(meta_text: str) -> bool:
    return COMPLETED_STATUS_RE.search(meta_text) is not None


def sanitize_task(task: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", task)


def validate_review_dir_name(name: str) -> str | None:
    if not name or name != name.strip():
        return "Флаг --review-dir-name требует непустое имя каталога без пробелов по краям."
    if "\x00" in name:
        return "Флаг --review-dir-name не должен содержать NUL-символ."
    if name in {".", ".."}:
        return "Флаг --review-dir-name должен быть безопасным именем каталога без traversal-сегментов."
    if re.search(r'[\\/:*?"<>|]', name):
        return "Флаг --review-dir-name должен быть одним сегментом пути без символов \\ / : * ? \" < > |."
    return None


def task_to_kebab(task: str) -> str:
    """TASK → kebab-case нижнего регистра. Пример: PROSV-503 → prosv-503."""
    s = sanitize_task(task).lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = re.sub(r'-+', '-', s)
    return s.strip('-') or s


def posix(p: str) -> str:
    return p.replace("\\", "/")


def render(template: str, **kwargs: str) -> str:
    """Минимальная подстановка меток {{KEY}} -> value."""
    result = template
    for key, value in kwargs.items():
        result = result.replace("{{" + key + "}}", value)
    return result


@lru_cache(maxsize=None)
def load_template(name: str) -> str:
    """Загрузить шаблон один раз на процесс (packaged-файлы статичны за прогон)."""
    path = RUNTIME_DIR / name
    if not path.exists():
        raise ToolError(f"Шаблон не найден: {path}")
    return read_text_safe(path)


# ---------------------------------------------------------------------------
# Коммиты и пути
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InitGroup:
    prefix: str
    tasks: list[str]
    repositories: tuple[Path, ...]
    branch: str
    reviews_root: Path
    remote: str
    branch_explicit: bool = False

    @property
    def task_spec(self) -> str:
        return ",".join(self.tasks)

    @property
    def repo(self) -> Path:
        return self.repositories[0]


@dataclass
class CollectedSource:
    source: SourceResolution
    repo_root: Path
    prompt_files: list[PromptFile]
    timestamp: str
    bsl_paths: list[str]
    project_url: str | None = None
    # Индексы БД объектов из изменённых запросов этого репозитория. Строятся в
    # `collect_source_prompt_data`, пока жив временный worktree с BSL и доступен
    # реальный target-ref; агрегируются в `run_for_collected_sources`.
    object_indexes: list[ObjectIndexes] = field(default_factory=list)
    # Структура тех же объектов метаданных для review-контекста.
    object_metadata: list[ObjectMetadata] = field(default_factory=list)


@dataclass(frozen=True)
class PreparedReview:
    task: str
    review_dir: Path
    batch_mode: bool
    source_ref: str
    target: str
    commits_count: int
    source_kind: str
    mr_id: int | None
    mr_ref: str | None


def collect_changed_paths(shas: list[str]) -> list[str]:
    if not shas:
        return []

    all_paths: set[str] = set()
    stdin_data = ("\n".join(shas) + "\n").encode("ascii")
    raw = run_git(
        ["diff-tree", "--stdin", "--no-commit-id", "-r", "--name-only", "-z"],
        input_bytes=stdin_data,
    )
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        try:
            p = chunk.decode("utf-8")
        except UnicodeDecodeError:
            p = chunk.decode("utf-8", errors="replace")
        all_paths.add(posix(p))
    return sorted(all_paths)


def filter_existing_at(target: str, paths: list[str]) -> list[str]:
    if not paths:
        return []

    object_specs = [f"{target}:{path}" for path in paths]
    stdin_data = ("\x00".join(object_specs) + "\x00").encode("utf-8")
    raw = run_git(
        ["cat-file", "--batch-check=%(objectname)", "-z"],
        input_bytes=stdin_data,
    )
    records = raw.decode("utf-8", errors="replace").splitlines()
    if len(records) != len(paths):
        raise ToolError(
            "git cat-file вернул неожиданное число ответов: "
            f"ожидалось {len(paths)}, получено {len(records)}"
        )
    return [
        path
        for path, record in zip(paths, records)
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", record)
    ]


# ---------------------------------------------------------------------------
# Worktree lifecycle
# ---------------------------------------------------------------------------

def reset_review_dir(review_dir: Path) -> None:
    # prune удаляет только записи worktree с несуществующими каталогами;
    # каталоги других задач остаются нетронутыми.
    _run(["git", "worktree", "prune"], check=False)
    _run(["git", "worktree", "remove", "--force", str(review_dir)], check=False)
    if review_dir.exists():
        shutil.rmtree(review_dir, ignore_errors=True)
    _run(["git", "worktree", "prune"], check=False)


def create_worktree(review_dir: Path, target: str, paths: list[str]) -> None:
    review_dir.parent.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "add", "--force", "--detach", "--no-checkout",
             str(review_dir), target])
    stdin_data = (("\n".join(paths) + "\n") if paths else "").encode("utf-8")
    run_git(["sparse-checkout", "set", "--no-cone", "--no-sparse-index", "--stdin"],
            cwd=review_dir, input_bytes=stdin_data)
    run_git(["checkout", "--detach", target], cwd=review_dir)


def is_bsl(path: str) -> bool:
    return path.lower().endswith(".bsl")


def is_report_template(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return (
        bool(parts)
        and parts[-1].lower() == "template.xml"
        and any(part.lower() == "reports" for part in parts[:-1])
    )


# ---------------------------------------------------------------------------
# Review prompt: BSL-блоки, token budget, split
# ---------------------------------------------------------------------------

@dataclass
class TargetChangeGroup:
    first_changed: int
    last_changed: int
    top: int
    bottom: int
    changed_lines: set[int]


@dataclass(frozen=True)
class BlockContextLimit:
    top: int = 0
    bottom: int = 0


@dataclass
class PromptBlock:
    path: str
    block_index: int
    header: str
    new_lines: list[PromptLine]
    top_context_lines: list[PromptLine]
    core_new_lines: list[PromptLine]
    bottom_context_lines: list[PromptLine]
    old_lines: list[PromptLine]
    rendered: str
    token_count: int
    target_lines_count: int


@dataclass
class PromptFile:
    path: str
    blocks: list[PromptBlock]
    token_count: int
    target_lines_count: int


@dataclass(frozen=True)
class LineProjection:
    lines: dict[int, str]
    ranges: dict[int, tuple[int, int]]


@dataclass
class PromptSlice:
    selected: list[tuple[int, int]]
    omitted_reasons: dict[tuple[int, int], str]
    hard_budget_used: bool = False
    context_limits: dict[tuple[int, int], BlockContextLimit] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptGenerationResult:
    priority: int
    input_truncated: bool


@dataclass
class TokenCounter:
    model: str
    encoding_name: str
    encoder: object

    def count(self, text: str) -> int:
        return len(self.encoder.encode(text))  # type: ignore[attr-defined]


def make_token_counter(model: str) -> TokenCounter:
    import tiktoken

    try:
        encoder = tiktoken.encoding_for_model(model)
        encoding_name = getattr(encoder, "name", model)
    except KeyError:
        encoder = tiktoken.get_encoding("o200k_base")
        encoding_name = "o200k_base"
    return TokenCounter(model=model, encoding_name=encoding_name, encoder=encoder)


def format_commits_bodies(commits: list[Commit]) -> str:
    bodies_parts: list[str] = []
    for c in commits:
        body = c.body.rstrip("\n")
        bodies_parts.append(
            f"## {c.short_sha} {c.date} {c.subject}\n\n"
            f"{body}\n\n"
            f"---\n"
        )
    return "\n".join(bodies_parts).rstrip() + "\n" if bodies_parts else "_Нет коммитов._\n"


def build_prompt_files(
    review_dir: Path,
    *,
    target: str,
    commits: list[Commit],
    task_shas: set[str],
    reviewable_paths: list[str],
    config: ReviewPromptConfig,
    counter: TokenCounter,
) -> list[PromptFile]:
    files: list[PromptFile] = []
    for path in sorted(reviewable_paths):
        blame_lines = load_final_blame_lines(target, path, task_shas)
        if not blame_lines:
            continue
        target_lines = read_text_safe(review_dir / path).splitlines()
        projection = project_reviewable_lines(path, target_lines)
        if not projection.lines:
            continue
        blame_lines = [
            BlameLine(
                sha=line.sha,
                source_line=line.source_line,
                target_line=line.target_line,
                text=projection.lines[line.target_line],
            )
            for line in blame_lines
            if line.target_line in projection.lines
        ]
        if not blame_lines:
            continue
        blocks = build_task_scoped_prompt_blocks(
            path=path,
            target=target,
            commits=commits,
            blame_lines=blame_lines,
            target_lines=target_lines,
            projection=projection,
            config=config,
            counter=counter,
        )
        if not blocks:
            continue
        files.append(PromptFile(
            path=path,
            blocks=blocks,
            token_count=sum(block.token_count for block in blocks),
            target_lines_count=sum(block.target_lines_count for block in blocks),
        ))
    return files


QUERY_TAG_RE = re.compile(r"</?query(?:\s[^>]*)?>")
XML_ENTITY_RE = re.compile(
    r"&(?P<entity>amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);"
)
XML_PREDEFINED_ENTITIES = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
}


def decode_xml_text(value: str) -> str | None:
    def replace_entity(match: re.Match[str]) -> str:
        entity = match.group("entity")
        if entity in XML_PREDEFINED_ENTITIES:
            return XML_PREDEFINED_ENTITIES[entity]
        base = 16 if entity.startswith("#x") else 10
        offset = 2 if base == 16 else 1
        return chr(int(entity[offset:], base))

    if "&" in XML_ENTITY_RE.sub("", value):
        return None
    try:
        return XML_ENTITY_RE.sub(replace_entity, value)
    except (ValueError, OverflowError):
        return None


def project_reviewable_lines(path: str, source_lines: list[str]) -> LineProjection:
    if not is_report_template(path):
        lines = {lineno: text for lineno, text in enumerate(source_lines, start=1)}
        return LineProjection(
            lines=lines,
            ranges={lineno: (1, len(source_lines)) for lineno in lines},
        )

    projected: dict[int, str] = {}
    range_lines: list[list[int]] = []
    active: list[int] | None = None
    for lineno, raw in enumerate(source_lines, start=1):
        position = 0
        pieces: list[str] = []
        for match in QUERY_TAG_RE.finditer(raw):
            closing = match.group().startswith("</")
            if closing:
                if active is None or lineno in projected:
                    return LineProjection(lines={}, ranges={})
                pieces.append(raw[position:match.start()])
                if lineno not in active:
                    active.append(lineno)
                decoded = decode_xml_text("".join(pieces))
                if decoded is None:
                    return LineProjection(lines={}, ranges={})
                projected[lineno] = decoded
                range_lines.append(active)
                active = None
            else:
                if active is not None or lineno in projected:
                    return LineProjection(lines={}, ranges={})
                active = [lineno]
                pieces = []
            position = match.end()
        if active is not None:
            pieces.append(raw[position:])
            if lineno not in active:
                active.append(lineno)
            decoded = decode_xml_text("".join(pieces))
            if decoded is None:
                return LineProjection(lines={}, ranges={})
            projected[lineno] = decoded
    if active is not None:
        return LineProjection(lines={}, ranges={})

    ranges: dict[int, tuple[int, int]] = {}
    for lines in range_lines:
        bounds = (lines[0], lines[-1])
        ranges.update({lineno: bounds for lineno in lines})
    return LineProjection(lines=projected, ranges=ranges)


def load_final_blame_lines(target: str, path: str, task_shas: set[str]) -> list[BlameLine]:
    """Вернуть TARGET-строки, финальный blame которых относится к выбранным SHA."""
    try:
        out = run_git(["blame", "--line-porcelain", target, "--", path])
    except ToolError:
        return []
    return parse_final_blame(out, task_shas)


def build_task_scoped_prompt_blocks(
    *,
    path: str,
    target: str,
    commits: list[Commit],
    blame_lines: list[BlameLine],
    target_lines: list[str],
    projection: LineProjection,
    config: ReviewPromptConfig,
    counter: TokenCounter,
) -> list[PromptBlock]:
    changed_by_line = {line.target_line: line.text for line in blame_lines}
    groups = group_target_change_lines(
        blame_lines,
        target_lines,
        config,
        projection,
        dynamic_context=is_bsl(path),
    )
    if not groups:
        return []
    old_lines_by_group = collect_old_lines_by_group(
        path,
        target,
        commits,
        groups,
        project_query=is_report_template(path),
    )

    blocks: list[PromptBlock] = []
    for group_index, group in enumerate(groups):
        added_lines = {
            lineno: changed_by_line[lineno]
            for lineno in sorted(group.changed_lines)
            if lineno in changed_by_line
        }
        if not added_lines:
            continue
        header = task_scoped_hunk_header(group)
        block = build_prompt_block_from_changes(
            path=path,
            block_index=len(blocks) + 1,
            header=header,
            added_lines=added_lines,
            old_lines=old_lines_by_group.get(group_index, []),
            target_lines=target_lines,
            config=config,
            counter=counter,
            projection=projection,
            context_top=group.top,
            context_bottom=group.bottom,
        )
        if block is not None:
            blocks.append(block)
    return blocks


def group_target_change_lines(
    blame_lines: list[BlameLine],
    target_lines: list[str],
    config: ReviewPromptConfig,
    projection: LineProjection | None = None,
    *,
    dynamic_context: bool = True,
) -> list[TargetChangeGroup]:
    groups: list[TargetChangeGroup] = []
    for lineno in sorted({line.target_line for line in blame_lines}):
        bounds = projection.ranges.get(lineno) if projection is not None else None
        if bounds is None:
            top = prompt_context_top(target_lines, lineno, config)
            bottom = max(lineno, min(len(target_lines), lineno + config.fixed_lines_after))
        else:
            range_top, range_bottom = bounds
            if dynamic_context:
                top = prompt_context_top(target_lines, lineno, config)
            else:
                top = max(range_top, lineno - config.fixed_lines_before)
            bottom = min(range_bottom, lineno + config.fixed_lines_after)
        same_range = (
            projection is None
            or not groups
            or projection.ranges.get(groups[-1].last_changed) == projection.ranges.get(lineno)
        )
        if groups and same_range and top <= groups[-1].bottom + 1:
            group = groups[-1]
            group.first_changed = min(group.first_changed, lineno)
            group.last_changed = max(group.last_changed, lineno)
            group.top = min(group.top, top)
            group.bottom = max(group.bottom, bottom)
            group.changed_lines.add(lineno)
            continue
        groups.append(TargetChangeGroup(
            first_changed=lineno,
            last_changed=lineno,
            top=top,
            bottom=bottom,
            changed_lines={lineno},
        ))
    return groups


def collect_old_lines_by_group(
    path: str,
    target: str,
    commits: list[Commit],
    groups: list[TargetChangeGroup],
    *,
    project_query: bool = False,
) -> dict[int, list[PromptLine]]:
    if not commits:
        return {}

    task_shas = {commit.sha.lower() for commit in commits}
    start = commit_parent_or_none(commits[-1].sha)
    states = load_net_old_initial_state(start, path)
    history = load_net_old_history_patches(start, target, path)
    entries: list[NetOldEntry[str]] = []

    for sha, hunks in history:
        if not hunks:
            continue
        is_task_commit = sha.lower() in task_shas
        states = apply_net_old_hunks(
            states,
            hunks,
            is_task_commit=is_task_commit,
            entries=entries,
            project_query=project_query,
        )

    entries_by_group: dict[int, list[tuple[int, int, list[PromptLine]]]] = defaultdict(list)
    seen_by_group: dict[int, set[int]] = defaultdict(set)
    for target_lineno, state in enumerate(states, start=1):
        if not state.old_entry_ids:
            continue
        for group_index, group in enumerate(groups):
            if target_lineno not in group.changed_lines:
                continue
            for entry_id in state.old_entry_ids:
                if entry_id in seen_by_group[group_index] or entry_id >= len(entries):
                    continue
                seen_by_group[group_index].add(entry_id)
                entries_by_group[group_index].append((target_lineno, entry_id, entries[entry_id].lines))

    old_lines_by_group: dict[int, list[PromptLine]] = {}
    for group_index, entries in entries_by_group.items():
        lines: list[PromptLine] = []
        for _first_target, _commit_index, old_lines in sorted(entries, key=lambda item: (item[0], item[1])):
            lines.extend(old_lines)
        old_lines_by_group[group_index] = lines
    return old_lines_by_group


def commit_parent_or_none(sha: str) -> str | None:
    parent = f"{sha}^"
    if git_ok(["rev-parse", "--verify", "--quiet", parent]):
        return parent
    return None


def load_net_old_initial_state(start: str | None, path: str) -> list[NetOldLineState[str]]:
    if start is None:
        return []
    try:
        raw = run_git(["show", f"{start}:{path}"])
    except ToolError:
        return []
    return [NetOldLineState(text=line) for line in raw.decode("utf-8", errors="replace").splitlines()]


NET_OLD_COMMIT_SEPARATOR = "\x00"


def load_net_old_history_patches(
    start: str | None,
    target: str,
    path: str,
) -> list[tuple[str, list[DiffHunk[str]]]]:
    range_arg = target if start is None else f"{start}..{target}"
    try:
        raw = git_text([
            "log",
            "--reverse",
            "-p",
            "--no-color",
            "--unified=0",
            "--format=%x00%H%x00",
            range_arg,
            "--",
            path,
        ])
    except ToolError:
        return []

    chunks = raw.split(NET_OLD_COMMIT_SEPARATOR)
    history: list[tuple[str, list[DiffHunk[str]]]] = []
    for index in range(1, len(chunks) - 1, 2):
        sha = chunks[index].strip()
        if not sha:
            continue
        history.append((sha, parse_unified_diff_hunks(chunks[index + 1])))
    return history


def apply_net_old_hunks(
    states: list[NetOldLineState[str]],
    hunks: list[DiffHunk[str]],
    *,
    is_task_commit: bool,
    entries: list[NetOldEntry[str]],
    project_query: bool = False,
) -> list[NetOldLineState[str]]:
    project = (
        lambda current: project_reviewable_lines(
            "Reports/Template.xml", [state.text for state in current],
        ).lines
    ) if project_query else None
    return replay_task_hunks(
        states, hunks, is_task_commit=is_task_commit, entries=entries, project=project, strict=False,
    )


def format_hunk_range(start: int, count: int) -> str:
    return f"{start}" if count == 1 else f"{start},{count}"


def task_scoped_hunk_header(group: TargetChangeGroup) -> str:
    new_count = group.last_changed - group.first_changed + 1
    return f"@@ -0,0 +{format_hunk_range(group.first_changed, new_count)} @@"


def build_prompt_block_from_changes(
    *,
    path: str,
    block_index: int,
    header: str,
    added_lines: dict[int, str],
    old_lines: list[PromptLine],
    target_lines: list[str],
    config: ReviewPromptConfig,
    counter: TokenCounter,
    projection: LineProjection | None = None,
    context_top: int | None = None,
    context_bottom: int | None = None,
) -> PromptBlock | None:
    if not added_lines:
        return None

    first_changed = min(added_lines)
    last_changed = max(added_lines)
    top = context_top if context_top is not None else prompt_context_top(target_lines, first_changed, config)
    bottom = (
        context_bottom
        if context_bottom is not None
        else max(last_changed, min(len(target_lines), last_changed + config.fixed_lines_after))
    )
    new_lines: list[PromptLine] = []
    for lineno in range(top, bottom + 1):
        if lineno < 1:
            continue
        if lineno > len(target_lines) and lineno not in added_lines:
            continue
        sign = "+" if lineno in added_lines else " "
        if projection is not None and lineno not in projection.lines:
            continue
        text = (
            added_lines[lineno]
            if lineno in added_lines
            else (
                projection.lines[lineno]
                if projection is not None
                else target_lines[lineno - 1]
            )
        )
        new_lines.append(PromptLine(sign=sign, text=text, target_line=lineno))

    new_lines = filter_mass_comment_lines(
        new_lines,
        threshold=config.ignore_consecutive_comment_lines_threshold,
        changed_sign="+",
    )
    old_lines = filter_mass_comment_lines(
        old_lines,
        threshold=config.ignore_consecutive_comment_lines_threshold,
        changed_sign="-",
    )
    if not new_lines:
        return None

    top_context_lines = [
        line for line in new_lines
        if line.target_line is not None and line.target_line < first_changed
    ]
    core_new_lines = [
        line for line in new_lines
        if line.target_line is not None and first_changed <= line.target_line <= last_changed
    ]
    bottom_context_lines = [
        line for line in new_lines
        if line.target_line is not None and line.target_line > last_changed
    ]
    rendered = render_bsl_block(header, new_lines, old_lines)
    return PromptBlock(
        path=path,
        block_index=block_index,
        header=header,
        new_lines=new_lines,
        top_context_lines=top_context_lines,
        core_new_lines=core_new_lines,
        bottom_context_lines=bottom_context_lines,
        old_lines=old_lines,
        rendered=rendered,
        token_count=counter.count(rendered),
        target_lines_count=len(new_lines),
    )


def prompt_context_top(
    target_lines: list[str],
    first_changed: int,
    config: ReviewPromptConfig,
) -> int:
    if config.dynamic_context_enabled:
        lower_bound = max(1, first_changed - config.dynamic_context_max_lines_up)
        upper_bound = min(first_changed, len(target_lines))
        for lineno in range(upper_bound, lower_bound - 1, -1):
            if METHOD_START_RE.match(target_lines[lineno - 1]):
                return lineno
    return max(1, first_changed - config.fixed_lines_before)


def filter_mass_comment_lines(
    lines: list[PromptLine],
    *,
    threshold: int,
    changed_sign: str,
) -> list[PromptLine]:
    if threshold <= 0:
        return lines
    omit: set[int] = set()
    series: list[int] = []

    def flush() -> None:
        nonlocal series
        if len(series) >= threshold:
            omit.update(series)
        series = []

    for idx, line in enumerate(lines):
        is_comment = line.text.lstrip().startswith("//")
        if line.sign == changed_sign and is_comment:
            series.append(idx)
            continue
        if line.sign != changed_sign and is_comment:
            continue
        flush()
    flush()
    return [line for idx, line in enumerate(lines) if idx not in omit]


def render_bsl_block(
    header: str,
    new_lines: list[PromptLine],
    old_lines: list[PromptLine],
) -> str:
    out = [
        header,
        "-- new bsl --",
    ]
    out.extend(f"{line.target_line} {line.sign}{line.text}" for line in new_lines)
    out.extend(["", "-- old bsl --"])
    if old_lines:
        out.extend(f"{line.sign}{line.text}" for line in old_lines)
    else:
        out.append("_Нет удалённых строк._")
    return "\n".join(out).rstrip() + "\n"


def full_context_limit(block: PromptBlock) -> BlockContextLimit:
    return BlockContextLimit(
        top=len(block.top_context_lines),
        bottom=len(block.bottom_context_lines),
    )


def render_prompt_block(block: PromptBlock, context_limit: BlockContextLimit | None = None) -> str:
    if context_limit is None:
        return block.rendered
    top_count = max(0, min(context_limit.top, len(block.top_context_lines)))
    bottom_count = max(0, min(context_limit.bottom, len(block.bottom_context_lines)))
    top_lines = block.top_context_lines[-top_count:] if top_count else []
    bottom_lines = block.bottom_context_lines[:bottom_count]
    return render_bsl_block(
        block.header,
        [*top_lines, *block.core_new_lines, *bottom_lines],
        block.old_lines,
    )


def generate_review_prompts_from_files(
    review_dir: Path,
    *,
    task: str,
    ref: str,
    target: str,
    base: str,
    commits: list[Commit],
    files: list[PromptFile],
    timestamp: str,
    source_kind: str,
    mr_id: int | None,
    mr_ref: str | None,
    config: ReviewPromptConfig,
    counter: TokenCounter,
) -> PromptGenerationResult:
    """Сгенерировать staged-промпты, вернуть приоритет и полноту отбора."""
    common = prompt_common_values(
        task=task,
        ref=ref,
        target=target,
        base=base,
        commits=commits,
        timestamp=timestamp,
        source_kind=source_kind,
        mr_id=mr_id,
        mr_ref=mr_ref,
    )
    input_truncated = write_staged_review_prompts(
        review_dir,
        files=files,
        common=common,
        config=config,
        counter=counter,
    )
    return PromptGenerationResult(review_priority_from_prompt_files(files), input_truncated)


def write_staged_review_prompts(
    review_dir: Path,
    *,
    files: list[PromptFile],
    common: dict[str, str],
    config: ReviewPromptConfig,
    counter: TokenCounter,
) -> bool:
    """Write staged artifacts and report omission of any reviewable block."""
    stage1_workflow = load_template("task/workflow/review_prompt_stage1.md")
    stage2_workflow = load_template("task/workflow/review_prompt_stage2.md")
    prompt_slice = select_single_prompt_slice(files, common, config, counter)
    prompt_slice = trim_slice_to_budget(
        files=files,
        common=common,
        config=config,
        counter=counter,
        stage1_workflow=stage1_workflow,
        stage2_workflow=stage2_workflow,
        prompt_slice=prompt_slice,
    )
    bsl_blocks = render_selected_blocks(files, prompt_slice.selected, prompt_slice.context_limits)
    provisional_tokens = rendered_staged_token_count(
        files=files,
        common=common,
        counter=counter,
        selected=prompt_slice.selected,
        omitted=prompt_slice.omitted_reasons,
        hard_budget_used=prompt_slice.hard_budget_used,
        context_limits=prompt_slice.context_limits,
        stage1_workflow=stage1_workflow,
        stage2_workflow=stage2_workflow,
        token_summary="",
    )
    token_summary = render_token_summary(
        counter=counter,
        config=config,
        prompt_tokens=provisional_tokens,
        hard_budget_used=prompt_slice.hard_budget_used,
    )
    final_tokens = rendered_staged_token_count(
        files=files,
        common=common,
        counter=counter,
        selected=prompt_slice.selected,
        omitted=prompt_slice.omitted_reasons,
        hard_budget_used=prompt_slice.hard_budget_used,
        context_limits=prompt_slice.context_limits,
        stage1_workflow=stage1_workflow,
        stage2_workflow=stage2_workflow,
        token_summary=token_summary,
    )
    token_summary = render_token_summary(
        counter=counter,
        config=config,
        prompt_tokens=final_tokens,
        hard_budget_used=prompt_slice.hard_budget_used,
    )
    stage1_content = render_stage_prompt_template(stage1_workflow, common)
    stage2_content = render_stage_prompt_template(stage2_workflow, common)
    bsl_content = render_bsl_artifact_template(
        _REVIEW_BSL_TEMPLATE,
        common,
        bsl_blocks=bsl_blocks,
        token_summary=token_summary,
    )
    review_info = review_dir / "_review_info"
    write_text_lf(review_info / "review_prompt_stage1.md", stage1_content)
    write_text_lf(review_info / "review_prompt_stage2.md", stage2_content)
    write_text_lf(review_info / "review_bsl.md", bsl_content)
    # Sidecar-карта границ TARGET-строк рядом с review_bsl.md: validate-review-yaml
    # отклоняет находки с номерами вне [min, max] показанного файла. Свёртка по уже
    # построенным блокам — без отдельного прохода и без правок render_*.
    line_bounds = {
        file.path: [min(shown_lines), max(shown_lines)]
        for file in files
        if (shown_lines := [
            line.target_line
            for block in file.blocks
            for line in block.new_lines
            if line.target_line is not None
        ])
    }
    write_text_lf(
        review_info / "review_lines.json",
        json.dumps(line_bounds, ensure_ascii=False, indent=2) + "\n",
    )
    all_refs = {
        (file_index, block_index)
        for file_index, file in enumerate(files)
        for block_index, _ in enumerate(file.blocks)
    }
    return bool(all_refs.difference(prompt_slice.selected))


def review_priority_from_prompt_files(files: list[PromptFile]) -> int:
    return sum(block.target_lines_count + len(block.old_lines) for file in files for block in file.blocks)


def trim_slice_to_budget(
    *,
    files: list[PromptFile],
    common: dict[str, str],
    config: ReviewPromptConfig,
    counter: TokenCounter,
    stage1_workflow: str,
    stage2_workflow: str,
    prompt_slice: PromptSlice,
) -> PromptSlice:
    budget = config.hard_input_budget if prompt_slice.hard_budget_used else config.soft_input_budget
    selected = list(prompt_slice.selected)
    omitted = dict(prompt_slice.omitted_reasons)
    while selected:
        context_limits = fit_context_limits_to_budget(
            files,
            common,
            config,
            counter=counter,
            selected=selected,
            omitted=omitted,
            budget=budget,
            hard_budget_used=prompt_slice.hard_budget_used,
            stage1_workflow=stage1_workflow,
            stage2_workflow=stage2_workflow,
        )
        if context_limits is not None:
            return PromptSlice(
                selected=selected,
                omitted_reasons=omitted,
                hard_budget_used=prompt_slice.hard_budget_used,
                context_limits=context_limits,
            )
        omitted[selected.pop()] = "не помещается в token budget"
    return PromptSlice(
        selected=selected,
        omitted_reasons=omitted,
        hard_budget_used=prompt_slice.hard_budget_used,
        context_limits={},
    )


def prompt_common_values(
    *,
    task: str,
    ref: str,
    target: str,
    base: str,
    commits: list[Commit],
    timestamp: str,
    source_kind: str,
    mr_id: int | None,
    mr_ref: str | None,
) -> dict[str, str]:
    return {
        "TASK": task,
        "KEBAB_TASK": task_to_kebab(task),
        "REF": ref,
        "TARGET": target,
        "TIMESTAMP": timestamp,
        "COMMITS_COUNT": str(len(commits)),
        "COMMITS_BODIES": format_commits_bodies(commits),
        "SOURCE": source_kind,
        "MR_ID": str(mr_id) if mr_id is not None else "",
        "MR_REF": mr_ref or "",
        "BASE_SHORT": base[:12] if base else "",
        "MR_HEAD_SHORT": target[:12] if source_kind == "mr" else "",
    }


def select_single_prompt_slice(
    files: list[PromptFile],
    common: dict[str, str],
    config: ReviewPromptConfig,
    counter: TokenCounter,
) -> PromptSlice:
    stage1_workflow = load_template("task/workflow/review_prompt_stage1.md")
    stage2_workflow = load_template("task/workflow/review_prompt_stage2.md")
    all_refs = [
        (file_idx, block_idx)
        for file_idx, file in enumerate(files)
        for block_idx, _ in enumerate(file.blocks)
    ]
    if not all_refs:
        return PromptSlice(selected=[], omitted_reasons={})

    all_context_limits = fit_context_limits_to_budget(
        files,
        common,
        config,
        counter=counter,
        selected=all_refs,
        omitted={},
        budget=config.soft_input_budget,
        hard_budget_used=False,
        stage1_workflow=stage1_workflow,
        stage2_workflow=stage2_workflow,
    )
    if all_context_limits is not None:
        return PromptSlice(
            selected=all_refs,
            omitted_reasons={},
            context_limits=all_context_limits,
        )

    selected: list[tuple[int, int]] = []
    context_limits: dict[tuple[int, int], BlockContextLimit] = {}
    for ref in all_refs:
        candidate = selected + [ref]
        candidate_context_limits = fit_context_limits_to_budget(
            files,
            common,
            config,
            counter=counter,
            selected=candidate,
            omitted={},
            budget=config.hard_input_budget,
            hard_budget_used=True,
            stage1_workflow=stage1_workflow,
            stage2_workflow=stage2_workflow,
        )
        if candidate_context_limits is not None:
            selected = candidate
            context_limits = candidate_context_limits
            continue
        break

    return PromptSlice(
        selected=selected,
        omitted_reasons={},
        hard_budget_used=True,
        context_limits=context_limits,
    )


def fit_context_limits_to_budget(
    files: list[PromptFile],
    common: dict[str, str],
    config: ReviewPromptConfig,
    *,
    counter: TokenCounter,
    selected: list[tuple[int, int]],
    omitted: dict[tuple[int, int], str],
    budget: int,
    hard_budget_used: bool,
    stage1_workflow: str,
    stage2_workflow: str,
) -> dict[tuple[int, int], BlockContextLimit] | None:
    context_limits = {
        ref: BlockContextLimit()
        for ref in selected
    }
    if rendered_staged_token_count(
        files=files,
        common=common,
        counter=counter,
        selected=selected,
        omitted=omitted,
        hard_budget_used=hard_budget_used,
        context_limits=context_limits,
        stage1_workflow=stage1_workflow,
        stage2_workflow=stage2_workflow,
        token_summary="",
    ) > budget:
        return None

    full_context_limits = {
        ref: full_context_limit(files[ref[0]].blocks[ref[1]])
        for ref in selected
    }
    if rendered_staged_token_count(
        files=files,
        common=common,
        counter=counter,
        selected=selected,
        omitted=omitted,
        hard_budget_used=hard_budget_used,
        context_limits=full_context_limits,
        stage1_workflow=stage1_workflow,
        stage2_workflow=stage2_workflow,
        token_summary="",
    ) <= budget:
        return full_context_limits

    for ref, side in iter_context_expansion_steps(files, selected):
        current = context_limits[ref]
        candidate_limit = (
            BlockContextLimit(top=current.top + 1, bottom=current.bottom)
            if side == "top"
            else BlockContextLimit(top=current.top, bottom=current.bottom + 1)
        )
        candidate_limits = dict(context_limits)
        candidate_limits[ref] = candidate_limit
        if rendered_staged_token_count(
            files=files,
            common=common,
            counter=counter,
            selected=selected,
            omitted=omitted,
            hard_budget_used=hard_budget_used,
            context_limits=candidate_limits,
            stage1_workflow=stage1_workflow,
            stage2_workflow=stage2_workflow,
            token_summary="",
        ) <= budget:
            context_limits = candidate_limits
    return context_limits


def iter_context_expansion_steps(
    files: list[PromptFile],
    selected: list[tuple[int, int]],
) -> Iterable[tuple[tuple[int, int], str]]:
    blocks = [
        (ref, files[ref[0]].blocks[ref[1]])
        for ref in selected
    ]
    max_depth = max(
        (
            max(len(block.top_context_lines), len(block.bottom_context_lines))
            for _ref, block in blocks
        ),
        default=0,
    )
    for depth in range(1, max_depth + 1):
        for ref, block in blocks:
            if depth <= len(block.top_context_lines):
                yield ref, "top"
            if depth <= len(block.bottom_context_lines):
                yield ref, "bottom"


def rendered_staged_token_count(
    *,
    files: list[PromptFile],
    common: dict[str, str],
    counter: TokenCounter,
    selected: list[tuple[int, int]],
    omitted: dict[tuple[int, int], str],
    hard_budget_used: bool,
    stage1_workflow: str,
    stage2_workflow: str,
    token_summary: str,
    context_limits: dict[tuple[int, int], BlockContextLimit] | None = None,
) -> int:
    bsl_blocks = render_selected_blocks(files, selected, context_limits)
    stage1_content = render_stage_prompt_template(stage1_workflow, common)
    stage2_content = render_stage_prompt_template(stage2_workflow, common)
    bsl_content = render_bsl_artifact_template(
        _REVIEW_BSL_TEMPLATE,
        common,
        bsl_blocks=bsl_blocks,
        token_summary=token_summary,
    )
    return counter.count(stage1_content) + counter.count(stage2_content) + counter.count(bsl_content)


def render_selected_blocks(
    files: list[PromptFile],
    selected: list[tuple[int, int]],
    context_limits: dict[tuple[int, int], BlockContextLimit] | None = None,
) -> str:
    if not selected:
        return "_Нет reviewable BSL-изменений в этом prompt-файле._\n"

    selected_by_file: dict[int, list[int]] = {}
    for file_idx, block_idx in selected:
        selected_by_file.setdefault(file_idx, []).append(block_idx)

    sections: list[str] = []
    for file_idx, block_indexes in selected_by_file.items():
        file = files[file_idx]
        blocks = [
            render_prompt_block(
                file.blocks[block_idx],
                (context_limits or {}).get((file_idx, block_idx)),
            ).rstrip()
            for block_idx in block_indexes
        ]
        sections.append(
            "\n\n".join([
                f"## File: '{file.path}'",
                *blocks,
            ]).rstrip()
        )
    return "\n\n".join(sections).rstrip() + "\n"


def render_token_summary(
    *,
    counter: TokenCounter,
    config: ReviewPromptConfig,
    prompt_tokens: int,
    hard_budget_used: bool,
) -> str:
    budget_name = "hard" if hard_budget_used else "soft"
    budget = config.hard_input_budget if hard_budget_used else config.soft_input_budget
    lines = [
        f"- model: {config.model}",
        f"- encoding: {counter.encoding_name}",
        f"- max_model_tokens: {config.max_model_tokens}",
        f"- output_soft_token_buffer: {config.output_soft_token_buffer}",
        f"- output_hard_token_buffer: {config.output_hard_token_buffer}",
        f"- soft_input_budget: {config.soft_input_budget}",
        f"- hard_input_budget: {config.hard_input_budget}",
        f"- applied_budget: {budget_name} ({budget})",
        f"- prompt_tokens: {prompt_tokens}",
        "- counted_components: stage1,stage2,bsl,metadata,commits",
    ]
    return "\n".join(lines) + "\n"


def render_stage_prompt_template(
    template: str,
    common: dict[str, str],
) -> str:
    values = dict(common)
    if "{{FOCUS_AREAS_LIST}}" in template:
        values["FOCUS_AREAS_LIST"] = render_focus_areas_list()
    return render(template, **values)


def render_bsl_artifact_template(
    template: str,
    common: dict[str, str],
    *,
    bsl_blocks: str,
    token_summary: str,
) -> str:
    values = dict(common)
    values.update({
        "PROMPT_BSL_BLOCKS": bsl_blocks,
        "PROMPT_TOKEN_SUMMARY": token_summary,
    })
    return render(template, **values)


def generate_meta(review_dir: Path, task: str, ref: str, target: str,
                  commits_count: int, timestamp: str,
                  priority: int,
                  source_kind: str = "task",
                  mr_id: int | None = None,
                  mr_ref: str | None = None,
                  input_truncated: bool = False) -> None:
    """Записать `_review_info/meta.txt` в детерминированном порядке полей.

    Порядок: task / ref / target / commits_count / timestamp / source /
    priority / input_truncated / mr_id? / mr_ref?. Поля `mr_id` и `mr_ref` пишутся только в
    MR-режиме. `status: completed` добавляется ИИ-ревьюером после успешного
    отчёта и здесь не управляется.
    """
    lines = [
        f"task: {task}",
        f"ref: {ref}",
        f"target: {target}",
        f"commits_count: {commits_count}",
        f"timestamp: {timestamp}",
        f"source: {source_kind}",
        f"priority: {priority}",
        f"input_truncated: {str(input_truncated).lower()}",
    ]
    if mr_id is not None:
        lines.append(f"mr_id: {mr_id}")
    if mr_ref is not None:
        lines.append(f"mr_ref: {mr_ref}")
    meta = "\n".join(lines) + "\n"
    write_text_lf(review_dir / "_review_info" / "meta.txt", meta)


def copy_template_tree(review_dir: Path, dirname: str) -> None:
    """Копирует package runtime/task/<dirname>/ в <TASK>/<dirname>/ (LF, без BOM)."""
    src_root = RUNTIME_DIR / "task" / dirname
    if not src_root.is_dir():
        return
    dst_root = review_dir / dirname
    for path in sorted(src_root.rglob("*")):
        rel = path.relative_to(src_root)
        dst = dst_root / rel
        if path.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            write_text_lf(dst, read_text_safe(path))


def copy_dev_standarts(review_dir: Path) -> None:
    copy_template_tree(review_dir, "dev-standarts")


def copy_platform_database_indexes(review_dir: Path) -> None:
    copy_template_tree(review_dir, "platform-database-indexes")


def generate_task_agents(review_dir: Path, task: str, kebab_task: str) -> None:
    tpl = load_template("task/AGENTS-TASK.md")

    content = render(
        tpl,
        TASK=task,
        KEBAB_TASK=kebab_task,
    )
    write_text_lf(review_dir / "AGENTS.md", content)


# ---------------------------------------------------------------------------
# Оркестрация
# ---------------------------------------------------------------------------

def _eprint(msg: str, *, task: str | None = None, batch: bool = False) -> None:
    if batch and task is not None:
        print(f"[{task}] {msg}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


def _oprint(msg: str, *, task: str | None = None, batch: bool = False) -> None:
    if not msg:
        print()
        return
    if batch and task is not None:
        print(f"[{task}] {msg}")
    else:
        print(msg)


def build_init_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-tasks init",
        description="Сформировать каталог ревью по задаче из коммитов git.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "-t", "--tasks",
        required=True,
        metavar="ЗАДАЧИ",
        help="Идентификатор задачи или несколько через запятую (подстроки в сообщениях коммитов).",
    )
    parser.add_argument(
        "-r", "--repo",
        default=None,
        metavar="ПУТЬ",
        help=(
            "Путь к git-репозиторию, каталогу-контейнеру с вложенными repos или "
            "несколько путей через запятую; если не указан, берётся из prefix config."
        ),
    )
    parser.add_argument(
        "-d", "--destination",
        default=None,
        metavar="ПУТЬ",
        help="Каталог для записи артефактов ревью; если не указан, берётся из prefix config.",
    )
    parser.add_argument(
        "-b", "--branch",
        default=None,
        metavar="REF",
        help=(
            "Явный ref-источник коммитов; если ref не найден после fetch — ошибка без фолбэка. "
            "Если флаг не задан и branch отсутствует в prefix config: origin/master с фолбэком "
            "origin/main -> master -> main -> HEAD."
        ),
    )
    parser.add_argument(
        "-m", "--mr",
        type=int,
        default=None,
        metavar="ID",
        help=(
            "Идентификатор GitLab Merge Request (целое > 0). Включает MR-режим: "
            "коммиты берутся из refs/merge-requests/<ID>/head; правило «один MR = одна задача»."
        ),
    )
    parser.add_argument(
        "--mr-from-tasks",
        action="store_true",
        help=(
            "Найти все GitLab MR по задачам из -t и собрать отдельный каталог "
            "<TASK>__mr-<ID> для каждого найденного MR."
        ),
    )
    parser.add_argument(
        "--remote",
        default=None,
        metavar="ИМЯ",
        help="Имя remote для fetch и MR-ref (по умолчанию: origin или значение из prefix config).",
    )
    parser.add_argument(
        "--review-dir-name",
        default=None,
        metavar="ИМЯ",
        help=(
            "Service-owned имя каталога ревью для explicit --mr. "
            "TASK остаётся смысловым идентификатором задачи."
        ),
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Принудительно перезаписать каталог ревью, даже если оно уже завершено.",
    )
    parser.add_argument(
        "--ide",
        choices=IDE_CHOICES,
        default="codex",
        metavar="IDE",
        help="IDE-адаптер для служебных файлов: cursor, codex или opencode (по умолчанию: codex).",
    )
    parser.add_argument(
        "--mcp", choices=("off", "required"), default="required",
        help="Внешняя MCP-проверка: off или required (по умолчанию: required).",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Явный TOML-конфиг review-tasks; перекрывает <repo>/.review-tasks.toml и packaged defaults.",
    )
    parser.add_argument(
        "--result-path",
        default=None,
        metavar="PATH",
        help="Записать versioned machine-readable результат init в JSON-файл.",
    )
    parser.add_argument(
        "--after-commit", nargs=2, action="append", default=None, metavar=("REPO_PATH", "SHA"),
        help="Проверить коммиты после указанного полного SHA в настроенном репозитории; граница исключается.",
    )
    parser.add_argument('--source-identity', default=None, metavar='JSON',
                        help='Service-owned identity выбранного explicit MR.')
    parser.add_argument(
        "--result-schema-version", type=int, choices=(1, 2, 3), default=None,
        help="Версия JSON-результата init (по умолчанию 1; версия 2 включает сравнение файлов).",
    )
    parser.add_argument(
        "--user-instruction",
        default=None,
        metavar="TEXT",
        help=(
            "Raw Markdown-инструкция агенту для текущего запуска: многострочный текст, "
            "LF-канонизация, не более 2000 Unicode-символов."
        ),
    )
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    return build_init_parser().parse_args(_bind_user_instruction_value(argv))


def _bind_user_instruction_value(argv: list[str]) -> list[str]:
    """Bind the token after the exact option even when it looks like another option."""
    result: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--user-instruction" and index + 1 < len(argv):
            result.append(f"--user-instruction={argv[index + 1]}")
            index += 2
            continue
        result.append(item)
        index += 1
    return result


def parse_args_or_usage(
    parser: argparse.ArgumentParser, argv: list[str]
) -> tuple[argparse.Namespace | None, int | None]:
    """Единый разбор аргументов субкоманды (D9).

    Успех → `(namespace, None)`. `SystemExit` argparse маппится в код: `-h/--help`
    (код 0) сохраняется как `EXIT_OK`, любой ненулевой argparse-код (в т.ч. 2 при
    неверном флаге) → `EXIT_USAGE=1`, чтобы не коллидировать с `EXIT_NO_COMMITS=2`.
    """
    try:
        return parser.parse_args(argv), None
    except SystemExit as exc:
        if exc.code in (0, None):
            return None, EXIT_OK
        return None, EXIT_USAGE


def parse_init_args(argv: list[str]) -> tuple[argparse.Namespace | None, int | None]:
    return parse_args_or_usage(build_init_parser(), _bind_user_instruction_value(argv))


def run_validate_review_yaml(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="review-tasks validate-review-yaml",
        description="Проверить YAML-результат ревью.",
        allow_abbrev=False,
    )
    parser.add_argument("--input", required=True, metavar="PATH", help="Путь к YAML-файлу результата ревью.")
    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE
    try:
        input_path = Path(args.input)
        if input_path.name not in {"review_result_stage1.yaml", "review_result_stage2.yaml"}:
            raise ReviewYamlError("--input должен указывать review_result_stage1.yaml или review_result_stage2.yaml.")
        result = validate_review_yaml_file(input_path)
    except (OSError, ReviewYamlError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    for warning in result.warnings:
        print(warning, file=sys.stderr)
    print("YAML результата ревью корректен")
    return EXIT_OK


def run_render_review_report(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="review-tasks render-review-report",
        description="Собрать Markdown-отчёт ревью из YAML-результатов.",
        allow_abbrev=False,
    )
    parser.add_argument("--input", action="append", required=True, metavar="PATH", help="YAML-файл или каталог _review_info.")
    parser.add_argument("--output", required=True, metavar="PATH", help="Путь для Markdown-отчёта.")
    parser.add_argument("--template", default=None, metavar="PATH", help="Опциональный Markdown-шаблон отчёта.")
    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE
    input_paths = [Path(item) for item in args.input]
    output_path = Path(args.output)
    try:
        pairs = []
        documents = []
        for input_path in input_paths:
            review_info = input_path if input_path.is_dir() else input_path.parent
            if not input_path.is_dir() and input_path.name != "review_result_stage2.yaml":
                raise ReviewYamlError("Для рендера укажите каталог _review_info или review_result_stage2.yaml.")
            stage1, document = read_stage_pair_for_render(review_info)
            pairs.append((review_info, stage1, document.parsed))
            documents.append(document)
        template_text = read_text_safe(Path(args.template)) if args.template else None
        task = infer_report_task(output_path)
        report = render_review_report(documents, task=task, template_text=template_text)
        write_text_lf(output_path, report)
        for review_info, stage1, stage2 in pairs:
            write_combined_result(review_info, stage1, stage2)
    except (OSError, ReviewYamlError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    print(f"Отчёт сформирован: {output_path}")
    return EXIT_OK


def run_mcp_registry_entry(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="review-tasks mcp-registry-entry",
        description="Вывести одну запись task-local MCP registry по server id.",
        allow_abbrev=False,
    )
    parser.add_argument("--server-id", required=True, metavar="ID", help="Точный id MCP server из текущей задачи.")
    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE
    server_id = args.server_id
    if not server_id:
        print("server_id_required", file=sys.stderr)
        return EXIT_USAGE

    registry_path = Path.cwd() / TASK_MCP_REGISTRY_RELATIVE
    if not registry_path.is_file():
        print("registry_missing", file=sys.stderr)
        return EXIT_USAGE

    try:
        entries = load_mcp_registry(registry_path)
    except IdeAdapterError:
        print("registry_invalid", file=sys.stderr)
        return EXIT_USAGE

    entry = next((item for item in entries if item.server_id == server_id), None)
    if entry is None:
        print("server_id_not_found", file=sys.stderr)
        return EXIT_USAGE

    result = {
        "server_id": entry.server_id,
        "url": entry.url,
        "headers": entry.headers,
        "expectedTools": list(entry.expected_tools),
    }
    if entry.connection_id is not None:
        result["connection_id"] = entry.connection_id
    print(compact_json(result))
    return EXIT_OK


def infer_report_task(output_path: Path) -> str:
    stem = output_path.stem
    prefix = "review-report-"
    if stem.startswith(prefix) and len(stem) > len(prefix):
        return stem[len(prefix):]
    return "TASK"


def source_timestamp(target: str) -> str:
    try:
        return git_text(["log", "-1", "--format=%cI", target]).strip()
    except ToolError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


NO_REVIEWABLE_FILES_HINT = (
    "Поддерживаются модули .bsl и изменённые тексты запросов в макетах отчётов СКД."
)


def report_reviewable_file_boundary(
    *,
    all_paths: list[str],
    existing_paths: list[str],
    prompt_files: list[PromptFile],
    task: str,
    batch_mode: bool,
) -> None:
    if all_paths and len(all_paths) == len(existing_paths) and not prompt_files:
        file_count = len(all_paths)
        if file_count == 1:
            message = "Изменён 1 файл, но он не подходит для ревью."
        else:
            message = (
                f"{russian_file_verb(file_count)} {file_count} {russian_file_word(file_count)}, "
                "но они не подходят для ревью."
            )
        _eprint(message, task=task, batch=batch_mode)
        _eprint("Подходят для ревью: 0.", task=task, batch=batch_mode)
        return

    message = (
        f"Изменённых файлов: {len(all_paths)}. "
        f"Удалено: {len(all_paths) - len(existing_paths)}. "
        f"Подходят для ревью: {len(prompt_files)}."
    )
    _eprint(message, task=task, batch=batch_mode)


def russian_file_word(count: int) -> str:
    remainder = count % 100
    if 11 <= remainder <= 14:
        return "файлов"
    remainder %= 10
    if remainder == 1:
        return "файл"
    if 2 <= remainder <= 4:
        return "файла"
    return "файлов"


def russian_file_verb(count: int) -> str:
    remainder = count % 100
    if 11 <= remainder <= 14:
        return "Изменено"
    remainder %= 10
    if remainder == 1:
        return "Изменён"
    if 2 <= remainder <= 4:
        return "Изменены"
    return "Изменено"


def collect_source_prompt_data(
    source: SourceResolution,
    repo_root: Path,
    *,
    remote: str,
    config: ReviewPromptConfig,
    counter: TokenCounter,
    batch_mode: bool = False,
) -> tuple[CollectedSource | None, int | None, str | None]:
    task = source.task
    commits = source.commits
    if not commits:
        if source.source_kind == "mr":
            return None, EXIT_NO_COMMITS, f"MR {source.mr_id}: набор коммитов BASE..MR_HEAD пуст."
        return None, EXIT_NO_COMMITS, f"Коммиты по строке '{task}' в '{source.ref}' не найдены."

    all_paths = collect_changed_paths([c.sha for c in commits])
    existing_paths = filter_existing_at(source.target, all_paths) if all_paths else []
    bsl_paths = [p for p in existing_paths if is_bsl(p)]
    template_candidates = [p for p in existing_paths if is_report_template(p)]
    checkout_paths = sorted(set(bsl_paths + template_candidates))
    if not checkout_paths:
        report_reviewable_file_boundary(
            all_paths=all_paths,
            existing_paths=existing_paths,
            prompt_files=[],
            task=task,
            batch_mode=batch_mode,
        )
        return None, EXIT_NO_PATHS, NO_REVIEWABLE_FILES_HINT

    temp_root = Path(tempfile.mkdtemp(prefix="review-tasks-worktree-"))
    temp_worktree = temp_root / "worktree"
    try:
        create_worktree(temp_worktree, source.target, checkout_paths)
        prompt_files = build_prompt_files(
            temp_worktree,
            target=source.target,
            commits=commits,
            task_shas=source.task_shas,
            reviewable_paths=checkout_paths,
            config=config,
            counter=counter,
        )
        # Структурные артефакты строим здесь: BSL-файлы ещё лежат в worktree, а
        # `source.target` и `repo_root` — реальные ревизия и корень этого
        # репозитория (в multi-repo агрегированный target = "multi-repository" уже
        # непригоден для git show). Общий проход парсит XML объекта один раз и
        # кормит оба хвоста: indexes.json и metadata.json. `GitConfigSource` и
        # `config_root` создаём один раз на источник — кэш дерева ревизии
        # разделяется между обоими хвостами.
        config_source = GitConfigSource(source.target, cwd=repo_root)
        config_root = compute_config_root(bsl_paths)
        try:
            parsed_sources = list(
                iter_parsed_sources(
                    temp_worktree,
                    bsl_paths,
                    source=config_source,
                    config_root=config_root,
                )
            )
        except Exception as exc:
            _eprint(
                f"indexes.json/metadata.json: разбор источников деградировал до "
                f"пустого артефакта ({type(exc).__name__}: {exc})",
                task=task,
                batch=batch_mode,
            )
            parsed_sources = []

        try:
            object_indexes = indexes_from_parsed(
                parsed_sources,
                source=config_source,
                config_root=config_root,
            )
        except Exception as exc:
            _eprint(
                f"indexes.json: построение деградировало до пустого артефакта "
                f"({type(exc).__name__}: {exc})",
                task=task,
                batch=batch_mode,
            )
            object_indexes = []

        try:
            object_metadata = [
                object_metadata_from(parsed_source)
                for parsed_source in parsed_sources
            ]
        except Exception as exc:
            _eprint(
                f"metadata.json: построение деградировало до пустого артефакта "
                f"({type(exc).__name__}: {exc})",
                task=task,
                batch=batch_mode,
            )
            object_metadata = []
    finally:
        reset_review_dir(temp_worktree)
        shutil.rmtree(temp_root, ignore_errors=True)

    report_reviewable_file_boundary(
        all_paths=all_paths,
        existing_paths=existing_paths,
        prompt_files=prompt_files,
        task=task,
        batch_mode=batch_mode,
    )
    if not prompt_files:
        return None, EXIT_NO_PATHS, NO_REVIEWABLE_FILES_HINT

    return CollectedSource(
        source=source,
        repo_root=repo_root,
        prompt_files=prompt_files,
        timestamp=source_timestamp(source.target),
        bsl_paths=bsl_paths,
        project_url=resolve_git_remote_project_url(repo_root, remote),
        object_indexes=object_indexes,
        object_metadata=object_metadata,
    ), None, None


def duplicate_prompt_paths(files: list[PromptFile]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for file in files:
        if file.path in seen:
            duplicates.add(file.path)
        seen.add(file.path)
    return sorted(duplicates)


def aggregate_timestamp(collected: list[CollectedSource]) -> str:
    timestamps = [item.timestamp for item in collected if item.timestamp]
    if not timestamps:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return max(timestamps)


def write_review_source_links(review_dir: Path, collected: list[CollectedSource]) -> Path:
    """Write deterministic per-file GitLab TARGET context for the renderer."""
    files: dict[str, dict[str, str]] = {}
    for item in collected:
        project_url = (
            normalize_git_remote_project_url(item.project_url)
            if item.project_url is not None
            else None
        )
        if project_url is None or not is_git_commit_sha(item.source.target):
            continue
        for prompt_file in item.prompt_files:
            path = prompt_file.path
            normalized = normalize_source_path(path)
            if normalized is not None:
                files[normalized] = {
                    "project_url": project_url,
                    "target": item.source.target.lower(),
                }
    payload = {
        "schema_version": 1,
        "files": {path: files[path] for path in sorted(files)},
    }
    output = review_dir / "_review_info" / "review_source_links.json"
    write_text_lf(output, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return output


def run_for_collected_sources(
    task: str,
    collected: list[CollectedSource],
    errors: list[tuple[int, str]],
    reviews_root: Path,
    *,
    config: EffectiveConfig,
    counter: TokenCounter,
    review_dir_name: str | None = None,
    batch_mode: bool = False,
    force: bool = False,
    user_instruction: str | None = None,
    result_recorder: InitResultRecorder | None = None,
    comparison_sources: list[ComparisonSource] | None = None,
    commit_selection: dict | None = None,
    context_repositories: list[tuple[Path, str]] | None = None,
) -> tuple[int, PreparedReview | None]:
    safe_task = review_dir_name or sanitize_task(task)
    kebab_task = task_to_kebab(task)
    review_dir = reviews_root / safe_task

    def history_failure(exc: Exception) -> tuple[int, None]:
        message = f"Не удалось сохранить историю отчётов: {exc}"
        _eprint(message, task=task, batch=batch_mode)
        if result_recorder is not None:
            result_recorder.outcome = "failure"
            result_recorder.code = "report_archive_failed"
            result_recorder.message = message
            result_recorder.record_review_dir(review_dir.resolve())
        return EXIT_USAGE, None

    with task_lock(review_dir):
        try:
            restore_report_history(review_dir)
        except Exception as exc:
            return history_failure(exc)
        if not force:
            meta_file = review_dir / "_review_info" / "meta.txt"
            if meta_file.exists() and has_completed_result_status(read_text_safe(meta_file)):
                message = f"Ревью для задачи '{task}' уже завершено. Используйте флаг --force для перезаписи."
                _eprint(
                    message,
                    task=task,
                    batch=batch_mode,
                )
                if result_recorder is not None:
                    result_recorder.record_expected_stop("already_done", message, review_dir.resolve())
                return EXIT_USAGE, None

        if not collected:
            code = next(
                (error_code for error_code, _ in errors if error_code not in {EXIT_NO_COMMITS, EXIT_NO_PATHS}),
                EXIT_NO_COMMITS if errors and all(error_code == EXIT_NO_COMMITS for error_code, _ in errors) else EXIT_NO_PATHS,
            )
            if errors:
                _eprint(errors[0][1], task=task, batch=batch_mode)
            if code != EXIT_NO_PATHS or not comparison_sources:
                return code, None

        files = [file for item in collected for file in item.prompt_files]
        duplicates = duplicate_prompt_paths(files)
        if duplicates:
            _eprint(
                "Найдены дублирующиеся BSL-пути в PromptFile.path: " + ", ".join(duplicates) + ". Пути должны быть уникальны во всех репозиториях задачи.",
                task=task,
                batch=batch_mode,
            )
            return EXIT_USAGE, None

        try:
            material_targets = {item.repo_root: item.source.target for item in collected}
            context_sources = prepare_sources(
                context_repositories if context_repositories is not None else list(material_targets.items()),
                material_targets,
            ) if collected else []
        except (ToolError, OSError) as exc:
            _eprint(str(exc), task=task, batch=batch_mode)
            return EXIT_USAGE, None

        try:
            archive_report(review_dir, f"review-report-{kebab_task}.md")
            with preserve_report_history(review_dir):
                reset_review_dir(review_dir)
        except Exception as exc:
            return history_failure(exc)
        if commit_selection is not None and commit_selection['mode'] == 'after':
            selection_path = review_dir / '_review_info' / 'commit_selection.json'
            write_text_lf(selection_path, json.dumps(commit_selection, ensure_ascii=False, indent=2) + '\n')
        prepared = None
        if collected:
            source0 = collected[0].source
            commits = [commit for item in collected for commit in item.source.commits]
            target = source0.target if len(collected) == 1 else "multi-repository"
            base = source0.base if len(collected) == 1 else ""
            source_kind = source0.source_kind if len(collected) == 1 else "task"
            mr_id = source0.mr_id if len(collected) == 1 else None
            mr_ref = source0.mr_ref if len(collected) == 1 else None
            timestamp = aggregate_timestamp(collected)

            (review_dir / "_review_info").mkdir(parents=True, exist_ok=True)
            write_user_instruction(review_dir, user_instruction)

            try:
                prompt_result = generate_review_prompts_from_files(
                    review_dir,
                    task=task,
                    ref=source0.ref,
                    target=target,
                    base=base,
                    commits=commits,
                    files=files,
                    timestamp=timestamp,
                    source_kind=source_kind,
                    mr_id=mr_id,
                    mr_ref=mr_ref,
                    config=config.review_prompt,
                    counter=counter,
                )
            except (ConfigError, ToolError, ImportError) as exc:
                _eprint(str(exc), task=task, batch=batch_mode)
                return EXIT_USAGE, None

            # Детерминированный артефакт индексов БД для ревью производительности запросов.
            # Индексы каждого репозитория уже собраны в `collect_source_prompt_data`
            # (там доступны worktree с BSL и реальный target-ref); здесь агрегируем и
            # пишем одним файлом — единый путь для single- и multi-repo (аддитивно).
            write_indexes_objects(
                review_dir,
                [obj for item in collected for obj in item.object_indexes],
            )
            write_metadata_objects(
                review_dir,
                [obj for item in collected for obj in item.object_metadata],
            )
            write_review_source_links(review_dir, collected)
            write_sources(review_dir, context_sources)

            generate_meta(
                review_dir,
                task=task,
                ref=source0.ref,
                target=target,
                commits_count=len(commits),
                timestamp=timestamp,
                priority=prompt_result.priority,
                input_truncated=prompt_result.input_truncated,
                source_kind=source_kind,
                mr_id=mr_id,
                mr_ref=mr_ref,
            )

            copy_dev_standarts(review_dir)
            try:
                write_reference_catalog(review_dir)
            except ReferenceCatalogError as exc:
                _eprint(str(exc), task=task, batch=batch_mode)
                try:
                    with preserve_report_history(review_dir):
                        reset_review_dir(review_dir)
                except Exception as history_exc:
                    return history_failure(history_exc)
                return EXIT_USAGE, None
            copy_platform_database_indexes(review_dir)
            generate_task_agents(review_dir, task=task, kebab_task=kebab_task)

            prepared = PreparedReview(
                task=task,
                review_dir=review_dir,
                batch_mode=batch_mode,
                source_ref=source0.ref,
                target=target,
                commits_count=len(commits),
                source_kind=source_kind,
                mr_id=mr_id,
                mr_ref=mr_ref,
            )

        if comparison_sources:
            summary = write_comparison(task, review_dir, comparison_sources, viewer=config.diff_viewer)
            if result_recorder is not None:
                result_recorder.comparisons.append(summary)
            _eprint(f"Сравнение файлов: {summary.state}. Каталог: {review_dir / '_review_info' / 'changes'}",
                    task=task, batch=batch_mode)
            for warning in summary.warnings:
                _eprint(warning.message, task=task, batch=batch_mode)
            if summary.error:
                _eprint(summary.error.message, task=task, batch=batch_mode)
        return EXIT_OK if prepared is not None else EXIT_NO_PATHS, prepared


def publish_prepared_review(
    prepared: PreparedReview,
    *,
    result_recorder: InitResultRecorder | None = None,
) -> None:
    task = prepared.task
    batch_mode = prepared.batch_mode
    review_dir = prepared.review_dir
    _oprint("", task=task, batch=batch_mode)
    _oprint("Готово.", task=task, batch=batch_mode)
    _oprint(f"Каталог ревью: {review_dir}", task=task, batch=batch_mode)
    _oprint(f"Задача: {task}", task=task, batch=batch_mode)
    _oprint(f"Источник: {prepared.source_ref}", task=task, batch=batch_mode)
    _oprint(f"Целевой коммит: {prepared.target}", task=task, batch=batch_mode)
    if prepared.source_kind == "mr":
        _oprint(f"MR: {prepared.mr_id} ({prepared.mr_ref})", task=task, batch=batch_mode)
    _oprint(
        f"Количество найденных коммитов: {prepared.commits_count}",
        task=task,
        batch=batch_mode,
    )
    _oprint(f"Точка входа для ИИ: {review_dir / 'AGENTS.md'}", task=task, batch=batch_mode)
    _oprint(f"Служебная информация: {review_dir / '_review_info'}", task=task, batch=batch_mode)
    if result_recorder is not None:
        result_recorder.record_review_dir(review_dir.resolve())


def compact_json(data: object) -> str:
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"))


def _path_from_invocation(value: str, invocation_cwd: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = invocation_cwd / path
    return path.resolve()


def _optional_path_from_invocation(value: str | None, invocation_cwd: Path) -> str | None:
    if value is None:
        return None
    return str(_path_from_invocation(value, invocation_cwd))


def _saved_prefix_entry(config: dict[str, object], prefix: str) -> dict[str, object]:
    value = config.get(prefix)
    return value if isinstance(value, dict) else {}


def _entry_string(entry: dict[str, object], key: str) -> str | None:
    value = entry.get(key)
    return value if isinstance(value, str) and value else None


def _resolve_init_groups(
    args: argparse.Namespace,
    grouped_tasks: dict[str, list[str]],
    config: dict[str, object],
    invocation_cwd: Path,
) -> tuple[list[InitGroup], list[str]]:
    groups: list[InitGroup] = []
    errors: list[str] = []
    for prefix, tasks in grouped_tasks.items():
        saved = _saved_prefix_entry(config, prefix)
        repo_values = (
            prefix_config.split_repository_list(args.repo)
            if args.repo
            else prefix_config.entry_repositories(saved)
        )
        reviews_raw = args.destination or _entry_string(saved, "reviews_dir")
        branch_value = args.branch or _entry_string(saved, "branch")
        branch_explicit = branch_value is not None
        branch = branch_value or "origin/master"
        remote = args.remote or _entry_string(saved, "remote") or "origin"

        missing: list[str] = []
        if not repo_values:
            missing.append("не указан репозиторий (-r/--repo)")
        if reviews_raw is None:
            missing.append("не указан каталог ревью (-d/--destination)")
        if missing:
            errors.append(f"Для префикса {prefix}: {', '.join(missing)}.")
            continue

        expansion_events: list[prefix_config.RepositoryExpansionEvent] = []
        try:
            resolved_repositories = prefix_config.resolve_repository_entries(
                repo_values,
                invocation_cwd=invocation_cwd,
                expansion_events=expansion_events,
            )
        except prefix_config.RepositoryDiscoveryError as exc:
            errors.append(f"Для префикса {prefix}: {exc}")
            continue

        for event in expansion_events:
            print(prefix_config.format_expansion_event_log(event), file=sys.stderr)

        groups.append(
            InitGroup(
                prefix=prefix,
                tasks=tasks,
                repositories=tuple(resolved_repositories),
                branch=branch,
                reviews_root=_path_from_invocation(reviews_raw, invocation_cwd),
                remote=remote,
                branch_explicit=branch_explicit,
            )
        )
    return groups, errors


def _validate_init_args(args: argparse.Namespace, task_count: int) -> int | None:
    if args.source_identity is not None:
        try:
            args.source_identity = validate_source_identity(json.loads(args.source_identity), args.mr)
        except (ValueError, TypeError) as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
    if args.after_commit is not None:
        if task_count != 1 or args.mr_from_tasks:
            print('--after-commit поддерживает только одну задачу или explicit MR.', file=sys.stderr)
            return EXIT_USAGE
        try:
            args.after_boundaries = normalize_after_commits(args.after_commit, Path.cwd())
        except (ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
    else:
        args.after_boundaries = {}
    if args.result_schema_version is not None and args.result_path is None:
        print("Флаг --result-schema-version требует --result-path.", file=sys.stderr)
        return EXIT_USAGE
    if args.review_dir_name is not None:
        error = validate_review_dir_name(args.review_dir_name)
        if error is not None:
            print(error, file=sys.stderr)
            return EXIT_USAGE
        if args.mr is None:
            print("Флаг --review-dir-name используется только вместе с --mr.", file=sys.stderr)
            return EXIT_USAGE
    if args.mr is None:
        return None
    if args.mr <= 0:
        print("Флаг --mr требует положительный целочисленный идентификатор MR.", file=sys.stderr)
        return EXIT_USAGE
    if args.mr_from_tasks:
        print("Флаги --mr и --mr-from-tasks нельзя использовать вместе.", file=sys.stderr)
        return EXIT_USAGE
    if task_count > 1:
        print(
            "В режиме --mr допускается ровно одна задача в -t "
            "(правило: один MR = одна задача).",
            file=sys.stderr,
        )
        return EXIT_USAGE
    return None


def _run_task_sources_across_repositories(
    group: InitGroup,
    contexts: list[RepositoryContext],
    *,
    config: EffectiveConfig,
    counter: TokenCounter,
    batch_mode: bool,
    force: bool,
    user_instruction: str | None,
    result_recorder: InitResultRecorder | None = None,
    boundaries: dict[str, str] | None = None,
) -> tuple[int, tuple[PreparedReview, ...]]:
    first_nonzero = EXIT_OK
    prepared_reviews: list[PreparedReview] = []
    candidates_by_repository: dict[tuple[Path, str], list[Commit] | ToolError] = {}
    for context in contexts:
        key = (context.root, context.ref)
        if key in candidates_by_repository:
            continue
        try:
            candidates_by_repository[key] = find_commits_for_tasks(context.ref, group.tasks, cwd=context.root)
        except ToolError as exc:
            candidates_by_repository[key] = exc

    for task in group.tasks:
        collected: list[CollectedSource] = []
        comparison_sources: list[ComparisonSource] = []
        errors: list[tuple[int, str]] = []
        records: list[tuple[RepositoryContext, SourceResolution]] = []
        for context in contexts:
            candidates = candidates_by_repository[(context.root, context.ref)]
            if isinstance(candidates, ToolError):
                if boundaries:
                    raise candidates
                continue
            records.append((context, resolve_task_source(
                context.ref, task, candidate_commits=candidates, cwd=context.root,
            )))
        if boundaries:
            records = restrict_sources(records, boundaries)
            if not any(source.commits for _, source in records):
                if result_recorder is not None:
                    result_recorder.outcome = 'failure'
                    result_recorder.code = 'no_new_commits'
                    result_recorder.message = 'После выбранной границы нет новых коммитов.'
                return EXIT_USAGE, ()
        selection = build_commit_selection(task, records, boundaries or {}) if records else None
        if result_recorder is not None:
            result_recorder.commit_selection = selection
        source_by_root = {context.root: source for context, source in records}
        for context in contexts:
            try:
                os.chdir(context.root)
                candidates = candidates_by_repository[(context.root, context.ref)]
                if isinstance(candidates, ToolError):
                    raise candidates
                source = source_by_root[context.root]
                if boundaries and not source.commits:
                    continue
                if source.commits:
                    comparison_sources.append(source.comparison_source(context.root))
                item, code, message = collect_source_prompt_data(
                    source,
                    context.root,
                    remote=group.remote,
                    config=config.review_prompt,
                    counter=counter,
                    batch_mode=batch_mode,
                )
            except (ConfigError, ToolError, ImportError) as exc:
                _eprint(str(exc), task=task, batch=batch_mode)
                code = EXIT_USAGE
                item = None
                message = str(exc)
            if item is not None:
                collected.append(item)
            else:
                errors.append((code or EXIT_USAGE, message or str(code)))
        code, prepared = run_for_collected_sources(
            task,
            collected,
            errors,
            group.reviews_root,
            config=config,
            counter=counter,
            batch_mode=batch_mode,
            force=force,
            user_instruction=user_instruction,
            result_recorder=result_recorder,
            comparison_sources=comparison_sources,
            commit_selection=selection,
            context_repositories=[(context.root, context.ref) for context in contexts],
        )
        if code == EXIT_OK and prepared is not None:
            prepared_reviews.append(prepared)
        if first_nonzero == EXIT_OK and code != EXIT_OK:
            first_nonzero = code
    return first_nonzero, tuple(prepared_reviews)


def _run_mr_sources_single_repository(
    args: argparse.Namespace,
    group: InitGroup,
    context: RepositoryContext,
    *,
    config: EffectiveConfig,
    counter: TokenCounter,
    result_recorder: InitResultRecorder | None = None,
) -> tuple[int, tuple[PreparedReview, ...]]:
    os.chdir(context.root)
    sources: list[SourceResolution] = []
    missing_mr_tasks: list[str] = []
    if args.mr is not None:
        source = resolve_mr_source(group.remote, context.ref, group.tasks[0], args.mr, cwd=context.root)
        if args.review_dir_name is not None:
            source.review_dir_name = args.review_dir_name
        sources.append(source)
    elif args.mr_from_tasks:
        sources, missing_mr_tasks = resolve_mr_sources_from_tasks(group.remote, context.ref, group.tasks, cwd=context.root)
    else:
        return EXIT_OK, ()

    batch_mode = len(sources) > 1 or bool(missing_mr_tasks)
    first_nonzero = EXIT_NO_COMMITS if missing_mr_tasks else EXIT_OK
    prepared_reviews: list[PreparedReview] = []
    for task in missing_mr_tasks:
        _eprint(f"MR для задачи '{task}' не найдены.", task=task, batch=batch_mode)

    for source in sources:
        records = restrict_sources([(context, source)], args.after_boundaries) if args.after_boundaries else [(context, source)]
        source = records[0][1]
        if args.after_boundaries and not source.commits:
            if result_recorder is not None:
                result_recorder.outcome = 'failure'
                result_recorder.code = 'no_new_commits'
                result_recorder.message = 'После выбранной границы нет новых коммитов.'
            return EXIT_USAGE, ()
        selection = build_commit_selection(source.task, records, args.after_boundaries,
                                           source_identity=args.source_identity, remote=group.remote)
        if result_recorder is not None:
            result_recorder.commit_selection = selection
        item, code, message = collect_source_prompt_data(
            source,
            context.root,
            remote=group.remote,
            config=config.review_prompt,
            counter=counter,
            batch_mode=batch_mode,
        )
        errors = [] if item is not None else [(code or EXIT_USAGE, message or str(code))]
        code, prepared = run_for_collected_sources(
            source.task,
            [item] if item is not None else [],
            errors,
            group.reviews_root,
            config=config,
            counter=counter,
            review_dir_name=source.review_dir_name,
            batch_mode=batch_mode,
            force=args.force,
            user_instruction=args.user_instruction,
            result_recorder=result_recorder,
            comparison_sources=[source.comparison_source(context.root)] if source.commits else [],
            commit_selection=selection,
            context_repositories=[(context.root, context.ref)],
        )
        if code == EXIT_OK and prepared is not None:
            prepared_reviews.append(prepared)
        if first_nonzero == EXIT_OK and code != EXIT_OK:
            first_nonzero = code
    return first_nonzero, tuple(prepared_reviews)


def _run_init_group(
    args: argparse.Namespace,
    group: InitGroup,
    config_path: str | None,
    *,
    result_recorder: InitResultRecorder | None = None,
) -> int:
    if len(group.repositories) > 1 and (args.mr is not None or args.mr_from_tasks):
        print("MR-режим поддерживает только один репозиторий. Укажите один путь в -r/--repo.", file=sys.stderr)
        return EXIT_USAGE

    original_cwd = Path.cwd()
    try:
        contexts: list[RepositoryContext] = []
        for repo in group.repositories:
            try:
                repo_root = _resolve_repo_root(repo)
            except (ToolError, OSError):
                print(f"Путь '{repo}' не является git-репозиторием.", file=sys.stderr)
                return EXIT_USAGE
            contexts.append(RepositoryContext(requested_path=repo, root=repo_root, ref=""))

        aliases = {normalize_repository_path(path): normalize_repository_path(context.root)
                   for context in contexts for path in (context.requested_path, context.root)}
        if set(args.after_boundaries) - set(aliases):
            print('Граница указывает на репозиторий вне настроенного source.', file=sys.stderr)
            return EXIT_USAGE
        bound_roots = [aliases[path] for path in args.after_boundaries]
        if len(set(bound_roots)) != len(bound_roots):
            print('Повтор границы для одного Git-репозитория.', file=sys.stderr)
            return EXIT_USAGE
        args.after_boundaries = {aliases[path]: sha for path, sha in args.after_boundaries.items()}

        try:
            effective_config = load_effective_config(contexts[0].root, config_path)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE

        # Перечень фактически применённых источников конфигурации: repo-конфиг
        # берётся из первого репозитория группы.
        applied_config_sources = ["встроенные настройки"]
        repo_config_path = contexts[0].root / ".review-tasks.toml"
        if repo_config_path.exists():
            applied_config_sources.append(str(repo_config_path))
        if config_path is not None:
            applied_config_sources.append(str(Path(config_path)))
        print("Источники настроек: " + ", ".join(applied_config_sources) + ".", file=sys.stderr)

        # Один счётчик на группу; ранняя диагностика отсутствия tiktoken (D8) —
        # до fetch и перезаписи каталогов, без traceback.
        try:
            counter = make_token_counter(effective_config.review_prompt.model)
        except ImportError:
            print(
                "Не найден пакет tiktoken, необходимый для подсчёта токенов промпта. "
                "Установите его (pip install tiktoken) и повторите запуск.",
                file=sys.stderr,
            )
            return EXIT_USAGE

        resolved_contexts: list[RepositoryContext] = []
        for context in contexts:
            try:
                ref = _fetch_and_resolve_repository(
                    context.root, group.branch, group.remote,
                    branch_explicit=group.branch_explicit,
                )
            except ToolError as exc:
                print(str(exc), file=sys.stderr)
                return exc.exit_code
            resolved_contexts.append(RepositoryContext(context.requested_path, context.root, ref))

        group.reviews_root.mkdir(parents=True, exist_ok=True)
        prepared_reviews: tuple[PreparedReview, ...]
        if args.mr is not None or args.mr_from_tasks:
            try:
                first_nonzero, prepared_reviews = _run_mr_sources_single_repository(
                    args,
                    group,
                    resolved_contexts[0],
                    config=effective_config,
                    counter=counter,
                    result_recorder=result_recorder,
                )
            except ToolError as exc:
                print(str(exc), file=sys.stderr)
                return exc.exit_code
        else:
            try:
                first_nonzero, prepared_reviews = _run_task_sources_across_repositories(
                    group,
                    resolved_contexts,
                    config=effective_config,
                    counter=counter,
                    batch_mode=len(group.tasks) > 1,
                    force=args.force,
                    user_instruction=args.user_instruction,
                    result_recorder=result_recorder,
                    boundaries=args.after_boundaries,
                )
            except ToolError as exc:
                print(str(exc), file=sys.stderr)
                return exc.exit_code

        if prepared_reviews:
            try:
                selected_task_dirs = project_agent_skills(
                    group.reviews_root,
                    RUNTIME_DIR,
                    task_dirs=tuple(prepared.review_dir for prepared in prepared_reviews),
                    mcp_mode=args.mcp,
                )
                adapter = get_adapter(args.ide)
                adapter.generate_files(
                    group.reviews_root,
                    RUNTIME_DIR,
                    RUNTIME_DIR.parent,
                    task_dirs=selected_task_dirs,
                    mcp_enabled=args.mcp != "off",
                )
            except (IdeAdapterError, SkillProjectionError, OSError, UnicodeError) as exc:
                print(str(exc), file=sys.stderr)
                return EXIT_USAGE
            for prepared in prepared_reviews:
                publish_prepared_review(prepared, result_recorder=result_recorder)
                print(f"MCP: {args.mcp}. Откройте {prepared.review_dir.resolve()} в {args.ide} и запустите review-start-task.")
            if args.mcp == "off":
                print("Внешняя MCP-проверка отключена; локальная проверка кода доступна.")
        return first_nonzero
    finally:
        os.chdir(original_cwd)


def _run_init_parsed(args: argparse.Namespace, *, result_recorder: InitResultRecorder) -> int:
    grouped_tasks = prefix_config.group_tasks_by_prefix(args.tasks)
    task_count = sum(len(tasks) for tasks in grouped_tasks.values())
    if task_count == 0:
        print("Не указан ни один ключ задачи.", file=sys.stderr)
        return EXIT_USAGE

    validation_code = _validate_init_args(args, task_count)
    if validation_code is not None:
        return validation_code

    invocation_cwd = Path.cwd()
    user_config_path = prefix_config.get_config_path()
    stored_config = prefix_config.load_prefix_config(user_config_path)
    groups, errors = _resolve_init_groups(args, grouped_tasks, stored_config, invocation_cwd)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return EXIT_USAGE

    config_path = _optional_path_from_invocation(args.config, invocation_cwd)
    first_nonzero = EXIT_OK
    for group in groups:
        code = _run_init_group(args, group, config_path, result_recorder=result_recorder)
        if first_nonzero == EXIT_OK and code != EXIT_OK:
            first_nonzero = code
    return first_nonzero


def run_init(argv: list[str]) -> int:
    invocation_cwd = Path.cwd()
    args, parse_code = parse_init_args(argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE

    try:
        args.user_instruction = validate_user_instruction(args.user_instruction)
    except UserInstructionError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    result_path = (
        _path_from_invocation(args.result_path, invocation_cwd)
        if args.result_path is not None
        else None
    )
    recorder = InitResultRecorder()
    try:
        exit_code = _run_init_parsed(args, result_recorder=recorder)
    except (ToolError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        exit_code = exc.exit_code if isinstance(exc, ToolError) else EXIT_USAGE
    if result_path is not None:
        write_init_result(result_path, recorder.envelope(exit_code, args.result_schema_version or 1))
    return exit_code


def run_prefix_config(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="review-tasks prefix-config",
        description="Служебные операции с конфигурацией префиксов задач.",
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("load", help="Вывести текущую конфигурацию JSON-объектом.")

    extract_parser = subparsers.add_parser("extract", help="Сгруппировать задачи по префиксам.")
    extract_parser.add_argument("tasks", help="Список задач через запятую.")

    save_parser = subparsers.add_parser("save", help="Сохранить конфигурацию одного префикса.")
    save_parser.add_argument("prefix", help="Префикс задачи.")
    save_parser.add_argument("repo", help="Путь к репозиторию.")
    save_parser.add_argument("branch", help="Ветка или ref.")
    save_parser.add_argument("reviews_dir", help="Каталог ревью.")
    save_parser.add_argument("remote", nargs="?", default="origin", help="Git remote (по умолчанию: origin).")

    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE

    path = prefix_config.get_config_path()
    if args.command == "load":
        print(compact_json(prefix_config.load_prefix_config(path)))
        return EXIT_OK
    if args.command == "extract":
        print(compact_json(prefix_config.group_tasks_by_prefix(args.tasks)))
        return EXIT_OK
    if args.command == "save":
        config = prefix_config.load_prefix_config(path)
        prefix = prefix_config.extract_prefix(args.prefix)
        repositories = prefix_config.split_repository_list(args.repo)
        if not repositories:
            print("Не указан ни один репозиторий.", file=sys.stderr)
            return EXIT_USAGE
        config[prefix] = {
            "repositories": repositories,
            "branch": args.branch,
            "reviews_dir": args.reviews_dir,
            "remote": args.remote,
        }
        try:
            prefix_config.save_prefix_config(path, config)
        except OSError as exc:
            print(f"Не удалось сохранить конфигурацию префиксов: {exc}", file=sys.stderr)
            return EXIT_USAGE
        print(compact_json({"ok": True, "prefix": prefix}))
        return EXIT_OK
    return EXIT_USAGE


def _run_local_service(argv: list[str]) -> int:
    from .local_service.http_server import run_local_service

    return run_local_service(argv)


def _task_command_parser(command: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog=f"review-tasks {command}",
        description=description,
        allow_abbrev=False,
    )


def run_check_ready(argv: list[str]) -> int:
    from .task_capsule.metadata import TaskMetadataError
    from .task_capsule.start import TaskStartOutcome, prepare_task_start

    parser = _task_command_parser(
        "check-ready",
        "Проверить готовность текущего каталога задачи и автоматически отметить "
        "repeat_review_skipped для completed metadata в _review_info/meta.txt.",
    )
    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE

    try:
        outcome = prepare_task_start(Path.cwd())
    except TaskMetadataError as exc:
        _print_task_metadata_error(exc)
        return EXIT_USAGE
    if outcome is TaskStartOutcome.READY:
        print("True")
        return EXIT_OK
    if outcome is TaskStartOutcome.REPEAT_REVIEW_SKIPPED:
        print("False")
        return EXIT_OK

    diagnostics = {
        TaskStartOutcome.REVIEW_ROOT: (
            "Это корневой каталог ревью. Открой выбранный каталог задачи как workspace "
            "и запусти review-start-task"
        ),
        TaskStartOutcome.INCOMPLETE: (
            "Структура каталога задачи неполная или повреждена: "
            "отсутствуют обязательные файлы ревью"
        ),
        TaskStartOutcome.NOT_PREPARED: "Каталог задачи не подготовлен для ревью",
    }
    print("False")
    print(diagnostics[outcome], file=sys.stderr)
    return EXIT_USAGE


def run_mcp_ready(argv: list[str]) -> int:
    from .task_capsule.metadata import TaskMetadataError, is_mcp_enabled

    parser = _task_command_parser("mcp-ready", "Проверить, разрешён ли MCP в текущей задаче по metadata.")
    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE
    try:
        enabled = is_mcp_enabled(Path.cwd())
    except TaskMetadataError as exc:
        print("False")
        _print_task_metadata_error(exc)
        return EXIT_USAGE
    print(enabled)
    return EXIT_OK


def _print_task_metadata_error(exc: Exception) -> None:
    from .task_capsule.metadata import (
        InvalidPromptLayoutError,
        MetadataNotUtf8Error,
        MetadataReadError,
        MetadataWriteError,
    )

    if isinstance(exc, MetadataNotUtf8Error):
        message = "_review_info/meta.txt не является корректным UTF-8 text"
    elif isinstance(exc, MetadataReadError):
        message = "Не удалось прочитать _review_info/meta.txt"
    elif isinstance(exc, MetadataWriteError):
        message = "Не удалось записать _review_info/meta.txt"
    elif isinstance(exc, InvalidPromptLayoutError):
        message = (
            "Структура каталога задачи неполная или повреждена: "
            "отсутствуют обязательные файлы ревью"
        )
    else:
        raise TypeError(f"Unsupported task metadata error: {type(exc).__name__}")
    print(message, file=sys.stderr)


def run_read_meta_summary(argv: list[str]) -> int:
    from .task_capsule.metadata import TaskMetadataError, read_meta_summary

    parser = _task_command_parser(
        "read-meta-summary",
        "Прочитать compact metadata из _review_info/meta.txt текущего каталога задачи.",
    )
    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE
    try:
        summary = read_meta_summary(Path.cwd())
    except TaskMetadataError as exc:
        _print_task_metadata_error(exc)
        return EXIT_USAGE
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return EXIT_OK


def _memory_command_parser(command: str, description: str) -> argparse.ArgumentParser:
    # Captured Windows streams otherwise use the legacy system encoding.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8", newline="\n")

    parser = _task_command_parser(command, description)
    parser.add_argument("--task", required=True, metavar="TASK", help="Ключ задачи.")
    return parser


def run_memory_read(argv: list[str]) -> int:
    from .review_result import MemoryOperationError, read_task_memory

    parser = _memory_command_parser("memory-read", "Вывести всю память правил задачи в JSON.")
    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE
    try:
        text = read_task_memory(args.task)
    except MemoryOperationError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    sys.stdout.write(text)
    return EXIT_OK


def run_memory_add(argv: list[str]) -> int:
    from .review_result import MemoryOperationError, add_task_memory

    parser = _memory_command_parser(
        "memory-add", "Сохранить правило обсуждения или заменить существующее правило без истории.",
    )
    parser.add_argument("--input", required=True, type=Path, metavar="JSON",
                        help="UTF-8 JSON с текстом правила для будущих ревью задачи и необязательным контекстом исходного замечания.")
    parser.add_argument("--replace", metavar="ID", help="ID заменяемой записи; новая получит другой ID.")
    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE
    try:
        result = add_task_memory(args.task, args.input, replace_id=args.replace)
    except MemoryOperationError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return EXIT_OK


def run_memory_delete(argv: list[str]) -> int:
    from .review_result import MemoryOperationError, delete_task_memory

    parser = _memory_command_parser("memory-delete", "Удалить одну запись памяти любого источника по ID.")
    parser.add_argument("--id", required=True, metavar="ID", help="ID удаляемой записи.")
    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE
    try:
        result = delete_task_memory(args.task, args.id)
    except MemoryOperationError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return EXIT_OK


def run_set_status(argv: list[str]) -> int:
    from .task_capsule.metadata import RUN_STATUSES, TaskMetadataError, set_completed

    parser = _task_command_parser(
        "set-status",
        "Записать status completed и last_review_run_status review_completed "
        "в _review_info/meta.txt текущего каталога задачи.",
    )
    parser.add_argument(
        "--run-status",
        choices=RUN_STATUSES,
        default="review_completed",
        help="Статус запуска (по умолчанию: review_completed).",
    )
    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE
    try:
        set_completed(Path.cwd(), run_status=args.run_status)
    except TaskMetadataError as exc:
        _print_task_metadata_error(exc)
        return EXIT_USAGE
    return EXIT_OK


@dataclass(frozen=True)
class CommandSpec:
    runner: Callable[[list[str]], int]
    help: str
    group: str


def _run_stage1(entrypoint: str, argv: list[str]) -> int:
    from .review_result import stage1_cli

    return getattr(stage1_cli, entrypoint)(argv)


def _run_stage2(entrypoint: str, argv: list[str]) -> int:
    from .review_result import stage2_cli

    return getattr(stage2_cli, entrypoint)(argv)


def _run_repository_context(command: str, argv: list[str]) -> int:
    from .repository_context.cli import main

    return main(command, argv)


COMMAND_GROUPS = {
    "general": "Общие команды",
    "stage1": "Stage 1",
    "stage2": "Stage 2",
}

# Единый grouped registry питает dispatch, root usage, root help и diagnostics.
COMMAND_TABLE: dict[str, CommandSpec] = {
    "init": CommandSpec(run_init, "Подготовить каталог ревью.", "general"),
    "prefix-config": CommandSpec(run_prefix_config, "Операции с конфигурацией префиксов.", "general"),
    "validate-review-yaml": CommandSpec(run_validate_review_yaml, "Проверить YAML-результат ревью.", "general"),
    "render-review-report": CommandSpec(run_render_review_report, "Собрать Markdown-отчёт ревью.", "general"),
    "local-service": CommandSpec(_run_local_service, "Запустить локальный loopback review service.", "general"),
    "mcp-registry-entry": CommandSpec(run_mcp_registry_entry, "Вывести одну запись task-local MCP registry.", "general"),
    "check-ready": CommandSpec(run_check_ready, "Проверить готовность текущей task capsule.", "general"),
    "mcp-ready": CommandSpec(run_mcp_ready, "Проверить разрешение MCP в текущей задаче.", "general"),
    "read-meta-summary": CommandSpec(run_read_meta_summary, "Прочитать compact metadata задачи.", "general"),
    "memory-read": CommandSpec(run_memory_read, "Вывести сохранённые правила задачи.", "general"),
    "memory-add": CommandSpec(run_memory_add, "Добавить или заменить правило задачи.", "general"),
    "memory-delete": CommandSpec(run_memory_delete, "Удалить правило задачи по ID.", "general"),
    "set-status": CommandSpec(run_set_status, "Записать completion metadata задачи.", "general"),
    "code-search": CommandSpec(lambda argv: _run_repository_context("code-search", argv), "Найти текст в TARGET подготовленного ревью.", "general"),
    "file-find": CommandSpec(lambda argv: _run_repository_context("file-find", argv), "Найти путь файла в TARGET подготовленного ревью.", "general"),
    "file-read": CommandSpec(lambda argv: _run_repository_context("file-read", argv), "Прочитать строки файла в TARGET подготовленного ревью.", "general"),
    "stage1-add-candidate": CommandSpec(lambda argv: _run_stage1("add_main", argv), "Добавить кандидата.", "stage1"),
    "stage1-remove-candidate": CommandSpec(lambda argv: _run_stage1("remove_main", argv), "Удалить кандидата до Stage 2.", "stage1"),
    "stage1-list-candidates": CommandSpec(lambda argv: _run_stage1("list_main", argv), "Вывести кандидатов.", "stage1"),
    "stage1-link-dev-standard": CommandSpec(lambda argv: _run_stage1("link_dev_main", argv), "Привязать стандарт разработки.", "stage1"),
    "stage1-link-platform-index": CommandSpec(lambda argv: _run_stage1("link_platform_main", argv), "Привязать индекс платформы.", "stage1"),
    "stage1-link-bsp-file": CommandSpec(lambda argv: _run_stage1("link_bsp_main", argv), "Привязать файл БСП.", "stage1"),
    "stage1-unlink-dev-standard": CommandSpec(lambda argv: _run_stage1("unlink_dev_main", argv), "Отвязать стандарт разработки.", "stage1"),
    "stage1-unlink-platform-index": CommandSpec(lambda argv: _run_stage1("unlink_platform_main", argv), "Отвязать индекс платформы.", "stage1"),
    "stage1-unlink-bsp-file": CommandSpec(lambda argv: _run_stage1("unlink_bsp_main", argv), "Отвязать файл БСП.", "stage1"),
    "stage2-upsert-finding": CommandSpec(lambda argv: _run_stage2("finding_main", argv), "Добавить или обновить finding.", "stage2"),
    "stage2-upsert-verification-channel": CommandSpec(lambda argv: _run_stage2("verification_channel_main", argv), "Добавить или обновить verification channel.", "stage2"),
    "stage2-upsert-trace": CommandSpec(lambda argv: _run_stage2("trace_main", argv), "Добавить или обновить trace.", "stage2"),
    "stage2-set-result": CommandSpec(lambda argv: _run_stage2("set_result_main", argv), "Записать публичный результат.", "stage2"),
}


def _format_command_catalog() -> str:
    sections: list[str] = []
    for group, title in COMMAND_GROUPS.items():
        lines = [f"{title}:"]
        lines.extend(
            f"  {name:<36} {spec.help}"
            for name, spec in COMMAND_TABLE.items()
            if spec.group == group
        )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def build_root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-tasks",
        description="Диспетчер команд review-tasks.",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_format_command_catalog(),
    )
    parser.add_argument("command", nargs="?", metavar="COMMAND", help=argparse.SUPPRESS)
    return parser


def print_root_usage(file: object = sys.stderr) -> None:
    parser = build_root_parser()
    parser.print_usage(file=file)
    print(_format_command_catalog(), file=file)
    print("Инициализация каталога: review-tasks init -t ... -r ... -d ...", file=file)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        print_root_usage(sys.stderr)
        return EXIT_USAGE
    if argv[0] in ("-h", "--help"):
        build_root_parser().print_help()
        return EXIT_OK
    command = argv[0]
    entry = COMMAND_TABLE.get(command)
    if entry is not None:
        return entry.runner(argv[1:])

    print_root_usage(sys.stderr)
    if command.startswith("-"):
        print("Прямой root-запуск генератора отключён. Используйте: review-tasks init -t ... -r ... -d ...", file=sys.stderr)
    else:
        print(f"Неизвестная команда: {command}", file=sys.stderr)
    return EXIT_USAGE


def cli() -> None:
    """Entry point для console_scripts (pyproject.toml)."""
    try:
        sys.exit(main())
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        print("Прервано пользователем.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    cli()
