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
from .review_init.agent_skills import SkillProjectionError, project_agent_skills
from .review_init.prompt_config import ConfigError, EffectiveConfig, ReviewPromptConfig, load_effective_config
from .review_init.result import InitResultContext, InitResultEnvelope, InitResultEnvelopeV2, write_init_result
from .review_result.review_yaml import (
    ReviewYamlError,
    expand_review_result_inputs,
    read_review_yaml_documents,
    render_review_report,
    validate_review_yaml_file,
    read_stage_pair_for_render,
    write_combined_result,
)
from .sgr_schema import (
    render_field_constraints_note,
    render_focus_areas_list,
    render_sgr_response_skeleton_body,
)
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

    def record_expected_stop(self, code: str, message: str, task_dir: Path) -> None:
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
        if self.outcome == "expected_stop" and self.code and self.message:
            outcome = "expected_stop"
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
        model = InitResultEnvelopeV2 if schema_version == 2 else InitResultEnvelope
        extra = {"comparisons": self.comparisons} if schema_version == 2 else {}
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
# Ref / репозиторий
# ---------------------------------------------------------------------------

def resolve_ref(ref: str) -> str:
    if git_ok(["rev-parse", "--verify", "--quiet", ref]):
        return ref
    for alt in ("origin/master", "origin/main", "master", "main", "HEAD"):
        if git_ok(["rev-parse", "--verify", "--quiet", alt]):
            return alt
    raise ToolError(f"Не удалось найти рабочий ref (начали с {ref}).", EXIT_USAGE)


# ---------------------------------------------------------------------------
# Коммиты и пути
# ---------------------------------------------------------------------------

@dataclass
class Commit:
    sha: str
    date: str
    subject: str
    body: str

    @property
    def short_sha(self) -> str:
        return self.sha[:12]


@dataclass
class SourceResolution:
    """Результат шага resolve-source: общий вход для конвейера ревью.

    Поле `source_kind` различает источник коммитов задачи: `"task"` (отбор по
    `git log --grep TASK`) или `"mr"` (диапазон `BASE..MR_HEAD` на ref MR).
    Остальные поля одинаковы по форме для обоих источников и потребляются
    дальнейшими шагами без изменения.
    """
    task: str
    ref: str
    target: str
    base: str
    commits: list[Commit]
    task_shas: set[str]
    source_kind: str  # "task" | "mr"
    mr_id: int | None = None
    mr_ref: str | None = None
    review_dir_name: str | None = None

    def comparison_source(self, repository: Path) -> ComparisonSource:
        """Adapt the same resolved TASK/MR commits used by AI material."""
        base = self.base
        if self.source_kind == "task" and base == self.commits[-1].sha:
            base = None
        return ComparisonSource(repository, base, self.target, frozenset(self.task_shas))


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


@dataclass(frozen=True)
class RepositoryContext:
    requested_path: Path
    root: Path
    ref: str


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


@dataclass(frozen=True)
class MrRefCandidate:
    mr_id: int
    mr_ref: str


@dataclass(frozen=True)
class LocalReviewBranch:
    name: str
    ref: str


@dataclass(frozen=True)
class BranchUpstream:
    remote: str
    merge_ref: str

    @property
    def remote_branch(self) -> str:
        prefix = "refs/heads/"
        if self.merge_ref.startswith(prefix):
            return self.merge_ref[len(prefix):]
        return self.merge_ref

    @property
    def short_ref(self) -> str:
        return f"{self.remote}/{self.remote_branch}"

    @property
    def tracking_ref(self) -> str:
        return f"refs/remotes/{self.remote}/{self.remote_branch}"


@dataclass
class MrRefResolution:
    mr_id: int
    mr_ref: str
    target: str
    base: str
    commits: list[Commit]
    task_shas: set[str]


def local_review_branch(ref: str) -> LocalReviewBranch | None:
    """Вернуть локальную ветку, если выбранный review ref указывает на refs/heads/*."""
    result = git_result(["rev-parse", "--verify", "--quiet", "--symbolic-full-name", ref])
    if result.returncode != 0:
        return None
    full_ref = result.stdout.decode("utf-8", errors="replace").strip()
    if not full_ref.startswith("refs/heads/"):
        return None
    return LocalReviewBranch(
        name=full_ref.removeprefix("refs/heads/"),
        ref=full_ref,
    )


def branch_upstream(branch: LocalReviewBranch) -> BranchUpstream | None:
    remote_result = git_result(["config", "--get", f"branch.{branch.name}.remote"])
    merge_result = git_result(["config", "--get", f"branch.{branch.name}.merge"])
    if remote_result.returncode != 0 or merge_result.returncode != 0:
        return None
    remote = remote_result.stdout.decode("utf-8", errors="replace").strip()
    merge_ref = merge_result.stdout.decode("utf-8", errors="replace").strip()
    if not remote or not merge_ref:
        return None
    return BranchUpstream(remote=remote, merge_ref=merge_ref)


def current_local_branch_ref() -> str | None:
    result = git_result(["symbolic-ref", "-q", "HEAD"])
    if result.returncode != 0:
        return None
    ref = result.stdout.decode("utf-8", errors="replace").strip()
    return ref or None


def rev_parse_sha(ref: str) -> str:
    return git_text(["rev-parse", "--verify", ref]).strip()


def ensure_fast_forward_possible(branch: LocalReviewBranch, upstream: BranchUpstream) -> tuple[str, str]:
    try:
        local_sha = rev_parse_sha(branch.ref)
        upstream_sha = rev_parse_sha(upstream.tracking_ref)
    except ToolError as exc:
        raise ToolError(
            f"Не удалось актуализировать локальную ветку '{branch.name}' "
            f"от '{upstream.short_ref}': upstream-ref недоступен.\n{exc}",
            EXIT_USAGE,
        ) from exc

    if local_sha == upstream_sha:
        return local_sha, upstream_sha

    ancestor_result = git_result(["merge-base", "--is-ancestor", local_sha, upstream_sha])
    if ancestor_result.returncode != 0:
        raise ToolError(
            f"Локальную ветку '{branch.name}' нельзя безопасно fast-forward'ить "
            f"до '{upstream.short_ref}'. Выполните ручную синхронизацию ветки.",
            EXIT_USAGE,
        )
    return local_sha, upstream_sha


def fast_forward_local_branch(branch: LocalReviewBranch, upstream: BranchUpstream) -> None:
    current_ref = current_local_branch_ref()
    if current_ref == branch.ref:
        result = git_result(["merge", "--ff-only", upstream.tracking_ref])
    else:
        result = git_result(["branch", "-f", branch.name, upstream.tracking_ref])

    if result.returncode != 0:
        stderr = decode_process_stderr(result)
        details = f"\n{stderr}" if stderr else ""
        raise ToolError(
            f"Не удалось безопасно актуализировать ветку '{branch.name}' "
            f"до '{upstream.short_ref}'.{details}",
            EXIT_USAGE,
        )


def refresh_local_review_branch(
    branch: LocalReviewBranch | None,
    remote: str,
    fetch_result: subprocess.CompletedProcess,
) -> None:
    """Fast-forward выбранную локальную review-ветку после общего fetch."""
    if branch is None:
        return

    if fetch_result.returncode != 0:
        stderr = decode_process_stderr(fetch_result)
        details = f"\n{stderr}" if stderr else ""
        raise ToolError(
            f"Не удалось актуализировать локальную ветку '{branch.name}' "
            f"с remote '{remote}'.{details}",
            EXIT_USAGE,
        )

    upstream = branch_upstream(branch)
    if upstream is None:
        raise ToolError(
            f"У локальной ветки '{branch.name}' не настроен upstream для remote '{remote}'.",
            EXIT_USAGE,
        )
    if upstream.remote != remote:
        raise ToolError(
            f"Upstream локальной ветки '{branch.name}' указывает на remote "
            f"'{upstream.remote}', ожидался '{remote}'.",
            EXIT_USAGE,
        )

    local_sha, upstream_sha = ensure_fast_forward_possible(branch, upstream)
    if local_sha == upstream_sha:
        print(f"Локальная ветка '{branch.name}' актуальна относительно '{upstream.short_ref}'")
        return

    fast_forward_local_branch(branch, upstream)
    print(f"Локальная ветка '{branch.name}' обновлена до '{upstream.short_ref}'")


def find_commits_for_tasks(ref: str, tasks: list[str]) -> list[Commit]:
    if not tasks:
        return []
    args = ["log", ref]
    for task in tasks:
        args.extend(["--grep", task])
    args.extend([
        "--fixed-strings", "--regexp-ignore-case",
        f"--format={LOG_RECORD_FORMAT}",
    ])
    out = git_text(args)
    return _parse_log_records(out)


def find_commits(ref: str, task: str) -> list[Commit]:
    return find_commits_for_tasks(ref, [task])


def fetch_mr_ref(remote: str, mr_id: int) -> str:
    """Force-fetch GitLab MR-ref в локальный tracking-ref и вернуть его имя.

    Выполняет `git fetch <remote> +refs/merge-requests/<ID>/head:refs/remotes/<remote>/mr/<ID>`
    с `check=True`. Префикс `+` гарантирует force-update при rebase MR на удалённой
    стороне. Refspec передаётся одноразово в командной строке — `.git/config` не
    модифицируется.

    При ошибке fetch (нет сети, MR отсутствует, нет прав) поднимается
    `ToolError` с русским сообщением и stderr git; вызывающий код возвращает
    `EXIT_USAGE`.
    """
    refspec = f"+refs/merge-requests/{mr_id}/head:refs/remotes/{remote}/mr/{mr_id}"
    try:
        run_git(["fetch", remote, refspec], check=True)
    except ToolError as exc:
        raise ToolError(
            f"Не удалось получить ref MR {mr_id} с remote {remote}.\n{exc}",
            EXIT_USAGE,
        ) from exc
    return f"refs/remotes/{remote}/mr/{mr_id}"


def fetch_mr_discovery_refs(remote: str) -> None:
    """Force-fetch all GitLab MR heads into refs/remotes/<remote>/merge-requests/*."""
    refspec = f"+refs/merge-requests/*/head:refs/remotes/{remote}/merge-requests/*"
    try:
        run_git(["fetch", remote, refspec], check=True)
    except ToolError as exc:
        raise ToolError(
            f"Не удалось получить список MR refs с remote {remote}.\n{exc}",
            EXIT_USAGE,
        ) from exc


def list_mr_discovery_refs(remote: str) -> list[MrRefCandidate]:
    """Return locally available refs/remotes/<remote>/merge-requests/<ID> refs."""
    prefix = f"refs/remotes/{remote}/merge-requests"
    out = git_text(["for-each-ref", "--format=%(refname)", prefix])
    candidates: list[MrRefCandidate] = []
    for raw_ref in out.splitlines():
        mr_ref = raw_ref.strip()
        if not mr_ref:
            continue
        match = re.search(r"/merge-requests/(\d+)$", mr_ref)
        if not match:
            continue
        candidates.append(MrRefCandidate(mr_id=int(match.group(1)), mr_ref=mr_ref))
    return sorted(candidates, key=lambda item: item.mr_id)


# На Windows CreateProcess не допускает NUL в аргументах, поэтому в качестве
# разделителей полей/записей `git log --format` используем ASCII-управляющие
# 0x1f/0x1e. Единый литерал для всех источников коммитов (task и MR).
LOG_SEP_FIELD = "\x1f"
LOG_SEP_COMMIT = "\x1e"
LOG_RECORD_FORMAT = f"%H{LOG_SEP_FIELD}%cs{LOG_SEP_FIELD}%s{LOG_SEP_FIELD}%B{LOG_SEP_COMMIT}"


def _parse_log_records(out: str) -> list[Commit]:
    """Разобрать `git log --format=...` с разделителями 0x1f / 0x1e."""
    commits: list[Commit] = []
    for raw in out.split(LOG_SEP_COMMIT):
        raw = raw.strip("\n")
        if not raw:
            continue
        parts = raw.split(LOG_SEP_FIELD, 3)
        if len(parts) != 4:
            continue
        sha, date, subject, body = parts
        commits.append(Commit(sha=sha, date=date, subject=subject, body=body))
    return commits


def resolve_task_source(
    ref: str,
    task: str,
    *,
    candidate_commits: list[Commit] | None = None,
) -> SourceResolution:
    """Стратегия источника `task`: коммиты по `git log --grep TASK`.

    Полностью повторяет текущее поведение task-режима: target = newest,
    base = `resolve_merge_base(oldest, ref)`. Пустой список коммитов возвращается
    как есть; `EXIT_NO_COMMITS` обрабатывается выше по стеку.

    `git log --grep` — грубый префильтр; точный отбор выполняет
    `filter_commits_for_task` с граничной семантикой ключа (D2), единой с
    выбором MR-кандидатов: `PROSV-50` не матчит `PROSV-503`.
    """
    candidates = find_commits(ref, task) if candidate_commits is None else candidate_commits
    commits = filter_commits_for_task(candidates, task)
    if commits:
        target = commits[0].sha
        base = resolve_merge_base(commits[-1].sha, ref)
    else:
        target = ""
        base = ""
    task_shas = {c.sha.lower() for c in commits}
    return SourceResolution(
        task=task,
        ref=ref,
        target=target,
        base=base,
        commits=commits,
        task_shas=task_shas,
        source_kind="task",
        mr_id=None,
        mr_ref=None,
    )


def resolve_mr_ref_source(ref: str, mr_ref: str, mr_id: int) -> MrRefResolution:
    """Resolve already fetched MR ref into BASE..MR_HEAD commit data.

    Шаги:
    1. `MR_HEAD = git rev-parse <mr_ref>`;
    2. `BASE = git merge-base <REF> <MR_HEAD>`; при отсутствии merge-base —
       `ToolError(EXIT_NO_COMMITS)` с отдельным сообщением «MR не имеет общей
       истории с REF» (D4), без тихого фолбэка `BASE=MR_HEAD`;
    3. `commits = git log --format=<HE_FMT> <BASE>..<MR_HEAD>` с теми же
       ASCII-разделителями (0x1f/0x1e), что и в task-режиме.
    """
    try:
        mr_head = git_text(["rev-parse", mr_ref]).strip()
    except ToolError as exc:
        raise ToolError(
            f"Локальный ref {mr_ref} недоступен после fetch.\n{exc}",
            EXIT_NO_COMMITS,
        ) from exc
    if not mr_head:
        raise ToolError(
            f"Локальный ref {mr_ref} не вернул SHA после fetch.",
            EXIT_NO_COMMITS,
        )

    base_proc = git_result(["merge-base", ref, mr_head])
    base = base_proc.stdout.decode("utf-8", errors="replace").strip() if base_proc.returncode == 0 else ""
    if not base:
        # Нет merge-base между REF и MR_HEAD — это отдельная диагностика (D4),
        # а не тихий фолбэк BASE=MR_HEAD и ложный «пустой набор коммитов».
        raise ToolError(
            f"MR {mr_id} не имеет общей истории с {ref}. Проверьте базовый ref "
            f"(-b/--branch) или соответствие MR базовой ветке.",
            EXIT_NO_COMMITS,
        )

    out = git_text(["log", f"{base}..{mr_head}", f"--format={LOG_RECORD_FORMAT}"])
    commits = _parse_log_records(out)
    task_shas = {c.sha.lower() for c in commits}
    return MrRefResolution(
        mr_id=mr_id,
        mr_ref=mr_ref,
        target=mr_head,
        base=base,
        commits=commits,
        task_shas=task_shas,
    )


def mr_resolution_to_source(
    resolution: MrRefResolution,
    ref: str,
    task: str,
    *,
    review_dir_name: str | None = None,
) -> SourceResolution:
    # Состав коммитов MR-источника — полный диапазон BASE..MR_HEAD (D11); ключ
    # задачи не сужает набор (правило «один MR = одна задача»). Предикат
    # `commit_matches_task` остаётся только в `mr_matches_task` — выборе
    # MR-кандидатов в discovery.
    return SourceResolution(
        task=task,
        ref=ref,
        target=resolution.target,
        base=resolution.base,
        commits=resolution.commits,
        task_shas=resolution.task_shas,
        source_kind="mr",
        mr_id=resolution.mr_id,
        mr_ref=resolution.mr_ref,
        review_dir_name=review_dir_name,
    )


def resolve_mr_source(remote: str, ref: str, task: str, mr_id: int) -> SourceResolution:
    """Стратегия explicit `mr`: коммиты диапазона `BASE..MR_HEAD`.

    Никаких HTTP-вызовов и токенов GitLab не используется.
    """
    mr_ref = fetch_mr_ref(remote, mr_id)
    resolution = resolve_mr_ref_source(ref, mr_ref, mr_id)
    return mr_resolution_to_source(resolution, ref, task)


def task_key_pattern(task: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(task)}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def commit_matches_task(commit: Commit, task: str) -> bool:
    pattern = task_key_pattern(task)
    return bool(pattern.search(commit.subject) or pattern.search(commit.body))


def filter_commits_for_task(commits: list[Commit], task: str) -> list[Commit]:
    return [commit for commit in commits if commit_matches_task(commit, task)]


def mr_matches_task(resolution: MrRefResolution, task: str) -> bool:
    return any(commit_matches_task(commit, task) for commit in resolution.commits)


def resolve_mr_sources_from_tasks(
    remote: str,
    ref: str,
    tasks: list[str],
) -> tuple[list[SourceResolution], list[str]]:
    """Find all MR refs matching tasks and return sources ordered by task, then MR ID."""
    fetch_mr_discovery_refs(remote)
    candidates = list_mr_discovery_refs(remote)

    resolved: list[MrRefResolution] = []
    for candidate in candidates:
        try:
            resolved.append(resolve_mr_ref_source(ref, candidate.mr_ref, candidate.mr_id))
        except ToolError:
            # MR без общей истории с базовым ref (или недоступный локальный ref) не
            # может относиться к запрошенной задаче — пропускаем кандидата, не
            # прерывая discovery остальных MR (D4).
            continue

    matches_by_task: dict[str, list[MrRefResolution]] = {task: [] for task in tasks}
    for resolution in resolved:
        for task in tasks:
            if mr_matches_task(resolution, task):
                matches_by_task[task].append(resolution)

    sources: list[SourceResolution] = []
    missing: list[str] = []
    for task in tasks:
        matches = sorted(matches_by_task[task], key=lambda item: item.mr_id)
        if not matches:
            missing.append(task)
            continue
        for resolution in matches:
            sources.append(
                mr_resolution_to_source(
                    resolution,
                    ref,
                    task,
                    review_dir_name=f"{sanitize_task(task)}__mr-{resolution.mr_id}",
                )
            )
    return sources, missing


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
    if "{{SGR_RESPONSE_SKELETON_BODY}}" in template:
        values["SGR_RESPONSE_SKELETON_BODY"] = render_sgr_response_skeleton_body()
    if "{{FIELD_CONSTRAINTS_NOTE}}" in template:
        values["FIELD_CONSTRAINTS_NOTE"] = render_field_constraints_note()
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


def resolve_merge_base(first_commit: str, ref: str) -> str:
    parent = f"{first_commit}^"
    # Фолбэк, если у коммита нет родителя (root commit).
    if not git_ok(["rev-parse", "--verify", "--quiet", parent]):
        parent = first_commit
    result = git_result(["merge-base", parent, ref])
    if result.returncode == 0:
        base = result.stdout.decode("utf-8", errors="replace").strip()
        if base:
            return base
    # merge-base не вычислился — применяем фолбэк (родитель старейшего коммита
    # задачи, либо сам коммит для root). Делаем фолбэк видимым (D4): значение базы
    # не меняем, но печатаем предупреждение с фактически применённой базой (SHA).
    resolved = git_result(["rev-parse", "--verify", parent])
    applied = resolved.stdout.decode("utf-8", errors="replace").strip() if resolved.returncode == 0 else parent
    _eprint(
        f"Предупреждение: git merge-base для '{parent}' и '{ref}' не вычислен; "
        f"применена база {applied}."
    )
    return parent

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
        "--result-schema-version", type=int, choices=(1, 2), default=None,
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
) -> tuple[int, PreparedReview | None]:
    safe_task = review_dir_name or sanitize_task(task)
    kebab_task = task_to_kebab(task)
    review_dir = reviews_root / safe_task
    with task_lock(review_dir):
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

        reset_review_dir(review_dir)
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
                reset_review_dir(review_dir)
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


def _resolve_repo_root(repo: Path) -> Path:
    repo_root_str = git_text(["rev-parse", "--show-toplevel"], cwd=repo).strip()
    return Path(repo_root_str)


def _fetch_and_resolve_repository(
    repo_root: Path, ref_arg: str, remote: str, *, branch_explicit: bool
) -> str:
    os.chdir(repo_root)
    branch_to_refresh = local_review_branch(ref_arg)
    sync_result = git_result(["fetch", remote, "--prune"])
    if sync_result.returncode != 0:
        stderr = decode_process_stderr(sync_result)
        details = f"\n{stderr}" if stderr else ""
        raise ToolError(f"Не удалось синхронизировать репозиторий с remote '{remote}'.{details}", EXIT_USAGE)
    print(f"Синхронизация с remote '{remote}': ok")
    refresh_local_review_branch(branch_to_refresh, remote, sync_result)
    return resolve_target_ref(ref_arg, remote, branch_explicit=branch_explicit)


def resolve_target_ref(ref: str, remote: str, *, branch_explicit: bool) -> str:
    """Резолв target-ref с учётом явности (D1).

    Явно заданный ref (`-b` или prefix-config `branch`), не резолвящийся после
    синхронизации, — ошибка кода 1 без подмены другим ref. Фолбэк-цепочка
    `origin/master → origin/main → master → main → HEAD` применяется только для
    встроенного умолчания (`branch_explicit=False`).
    """
    if branch_explicit:
        if git_ok(["rev-parse", "--verify", "--quiet", ref]):
            return ref
        raise ToolError(
            f"Явно заданный ref '{ref}' не найден после синхронизации с remote "
            f"'{remote}'. Проверьте значение -b/--branch или ветку в prefix-config; "
            f"фолбэк на origin/master для явного ref не применяется.",
            EXIT_USAGE,
        )
    return resolve_ref(ref)


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
) -> tuple[int, tuple[PreparedReview, ...]]:
    first_nonzero = EXIT_OK
    prepared_reviews: list[PreparedReview] = []
    candidates_by_repository: dict[tuple[Path, str], list[Commit] | ToolError] = {}
    for context in contexts:
        key = (context.root, context.ref)
        if key in candidates_by_repository:
            continue
        try:
            os.chdir(context.root)
            candidates_by_repository[key] = find_commits_for_tasks(context.ref, group.tasks)
        except ToolError as exc:
            candidates_by_repository[key] = exc

    for task in group.tasks:
        collected: list[CollectedSource] = []
        comparison_sources: list[ComparisonSource] = []
        errors: list[tuple[int, str]] = []
        for context in contexts:
            try:
                os.chdir(context.root)
                candidates = candidates_by_repository[(context.root, context.ref)]
                if isinstance(candidates, ToolError):
                    raise candidates
                source = resolve_task_source(
                    context.ref,
                    task,
                    candidate_commits=candidates,
                )
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
        source = resolve_mr_source(group.remote, context.ref, group.tasks[0], args.mr)
        if args.review_dir_name is not None:
            source.review_dir_name = args.review_dir_name
        sources.append(source)
    elif args.mr_from_tasks:
        sources, missing_mr_tasks = resolve_mr_sources_from_tasks(group.remote, context.ref, group.tasks)
    else:
        return EXIT_OK, ()

    batch_mode = len(sources) > 1 or bool(missing_mr_tasks)
    first_nonzero = EXIT_NO_COMMITS if missing_mr_tasks else EXIT_OK
    prepared_reviews: list[PreparedReview] = []
    for task in missing_mr_tasks:
        _eprint(f"MR для задачи '{task}' не найдены.", task=task, batch=batch_mode)

    for source in sources:
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
            first_nonzero, prepared_reviews = _run_task_sources_across_repositories(
                group,
                resolved_contexts,
                config=effective_config,
                counter=counter,
                batch_mode=len(group.tasks) > 1,
                force=args.force,
                user_instruction=args.user_instruction,
                result_recorder=result_recorder,
            )

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


def run_read_feedback(argv: list[str]) -> int:
    from .review_result import FeedbackReadError, read_task_feedback

    # Captured Windows streams otherwise use the legacy system encoding.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8", newline="\n")

    parser = _task_command_parser(
        "read-feedback",
        "Вывести все сохранённые отзывы специалиста по задаче одним текстом.",
    )
    parser.add_argument("--task", required=True, metavar="TASK", help="Ключ задачи.")
    args, parse_code = parse_args_or_usage(parser, argv)
    if args is None:
        return parse_code if parse_code is not None else EXIT_USAGE
    task = args.task.strip()
    if not task:
        print("Параметр --task не должен быть пустым.", file=sys.stderr)
        return EXIT_USAGE
    try:
        text = read_task_feedback(task)
    except FeedbackReadError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    sys.stdout.write(text)
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
    "read-feedback": CommandSpec(run_read_feedback, "Вывести сохранённые отзывы специалиста по задаче.", "general"),
    "set-status": CommandSpec(run_set_status, "Записать completion metadata задачи.", "general"),
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
