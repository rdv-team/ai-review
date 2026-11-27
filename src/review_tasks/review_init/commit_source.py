"""Shared TASK/MR commit-source resolution and ancestry selection.

All Git operations accept an explicit repository directory; callers never change
process cwd to discover candidates or resolve scope.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from review_tasks.infrastructure.git import ToolError, decode_process_stderr, git_ok, git_result, git_text, run_git
from review_tasks.review_init.comparison import ComparisonSource
from review_tasks.integrations.gitlab_links import resolve_git_remote_project_url

EXIT_USAGE = 1
EXIT_NO_COMMITS = 2
# Windows CreateProcess rejects NUL arguments; TASK and MR logs share these
# field/record separators instead of embedding NUL in the format argument.
LOG_SEP_FIELD = "\x1f"
LOG_SEP_COMMIT = "\x1e"
LOG_RECORD_FORMAT = f"%H{LOG_SEP_FIELD}%cs{LOG_SEP_FIELD}%s{LOG_SEP_FIELD}%B{LOG_SEP_COMMIT}"


def resolve_ref(ref: str, *, cwd: Path) -> str:
    if git_ok(['rev-parse', '--verify', '--quiet', ref], cwd=cwd):
        return ref
    for alt in ('origin/master', 'origin/main', 'master', 'main', 'HEAD'):
        if git_ok(['rev-parse', '--verify', '--quiet', alt], cwd=cwd):
            return alt
    raise ToolError(f'Не удалось найти рабочий ref (начали с {ref}).', EXIT_USAGE)


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
    source_kind: str
    mr_id: int | None = None
    mr_ref: str | None = None
    review_dir_name: str | None = None
    root_base: bool = False

    def comparison_source(self, repository: Path) -> ComparisonSource:
        """Adapt the same resolved TASK/MR commits used by AI material."""
        base = self.base
        if self.source_kind == 'task' and (self.root_base or (bool(self.commits) and base == self.commits[-1].sha)):
            base = None
        return ComparisonSource(repository, base, self.target, frozenset(self.task_shas))



@dataclass(frozen=True)
class RepositoryContext:
    requested_path: Path
    root: Path
    ref: str


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
        prefix = 'refs/heads/'
        if self.merge_ref.startswith(prefix):
            return self.merge_ref[len(prefix):]
        return self.merge_ref

    @property
    def short_ref(self) -> str:
        return f'{self.remote}/{self.remote_branch}'

    @property
    def tracking_ref(self) -> str:
        return f'refs/remotes/{self.remote}/{self.remote_branch}'


@dataclass
class MrRefResolution:
    mr_id: int
    mr_ref: str
    target: str
    base: str
    commits: list[Commit]
    task_shas: set[str]


def local_review_branch(ref: str, *, cwd: Path) -> LocalReviewBranch | None:
    """Вернуть локальную ветку, если выбранный review ref указывает на refs/heads/*."""
    result = git_result(['rev-parse', '--verify', '--quiet', '--symbolic-full-name', ref], cwd=cwd)
    if result.returncode != 0:
        return None
    full_ref = result.stdout.decode('utf-8', errors='replace').strip()
    if not full_ref.startswith('refs/heads/'):
        return None
    return LocalReviewBranch(name=full_ref.removeprefix('refs/heads/'), ref=full_ref)


def branch_upstream(branch: LocalReviewBranch, *, cwd: Path) -> BranchUpstream | None:
    remote_result = git_result(['config', '--get', f'branch.{branch.name}.remote'], cwd=cwd)
    merge_result = git_result(['config', '--get', f'branch.{branch.name}.merge'], cwd=cwd)
    if remote_result.returncode != 0 or merge_result.returncode != 0:
        return None
    remote = remote_result.stdout.decode('utf-8', errors='replace').strip()
    merge_ref = merge_result.stdout.decode('utf-8', errors='replace').strip()
    if not remote or not merge_ref:
        return None
    return BranchUpstream(remote=remote, merge_ref=merge_ref)


def current_local_branch_ref(*, cwd: Path) -> str | None:
    result = git_result(['symbolic-ref', '-q', 'HEAD'], cwd=cwd)
    if result.returncode != 0:
        return None
    ref = result.stdout.decode('utf-8', errors='replace').strip()
    return ref or None


def rev_parse_sha(ref: str, *, cwd: Path) -> str:
    return git_text(['rev-parse', '--verify', ref], cwd=cwd).strip()


def ensure_fast_forward_possible(branch: LocalReviewBranch, upstream: BranchUpstream, *, cwd: Path) -> tuple[str, str]:
    try:
        local_sha = rev_parse_sha(branch.ref, cwd=cwd)
        upstream_sha = rev_parse_sha(upstream.tracking_ref, cwd=cwd)
    except ToolError as exc:
        raise ToolError(f"Не удалось актуализировать локальную ветку '{branch.name}' от '{upstream.short_ref}': upstream-ref недоступен.\n{exc}", EXIT_USAGE) from exc
    if local_sha == upstream_sha:
        return (local_sha, upstream_sha)
    ancestor_result = git_result(['merge-base', '--is-ancestor', local_sha, upstream_sha], cwd=cwd)
    if ancestor_result.returncode != 0:
        raise ToolError(f"Локальную ветку '{branch.name}' нельзя безопасно fast-forward'ить до '{upstream.short_ref}'. Выполните ручную синхронизацию ветки.", EXIT_USAGE)
    return (local_sha, upstream_sha)


def fast_forward_local_branch(branch: LocalReviewBranch, upstream: BranchUpstream, *, cwd: Path) -> None:
    current_ref = current_local_branch_ref(cwd=cwd)
    if current_ref == branch.ref:
        result = git_result(['merge', '--ff-only', upstream.tracking_ref], cwd=cwd)
    else:
        result = git_result(['branch', '-f', branch.name, upstream.tracking_ref], cwd=cwd)
    if result.returncode != 0:
        stderr = decode_process_stderr(result)
        details = f'\n{stderr}' if stderr else ''
        raise ToolError(f"Не удалось безопасно актуализировать ветку '{branch.name}' до '{upstream.short_ref}'.{details}", EXIT_USAGE)


def refresh_local_review_branch(branch: LocalReviewBranch | None, remote: str, fetch_result: subprocess.CompletedProcess, *, cwd: Path) -> None:
    """Fast-forward выбранную локальную review-ветку после общего fetch."""
    if branch is None:
        return
    if fetch_result.returncode != 0:
        stderr = decode_process_stderr(fetch_result)
        details = f'\n{stderr}' if stderr else ''
        raise ToolError(f"Не удалось актуализировать локальную ветку '{branch.name}' с remote '{remote}'.{details}", EXIT_USAGE)
    upstream = branch_upstream(branch, cwd=cwd)
    if upstream is None:
        raise ToolError(f"У локальной ветки '{branch.name}' не настроен upstream для remote '{remote}'.", EXIT_USAGE)
    if upstream.remote != remote:
        raise ToolError(f"Upstream локальной ветки '{branch.name}' указывает на remote '{upstream.remote}', ожидался '{remote}'.", EXIT_USAGE)
    local_sha, upstream_sha = ensure_fast_forward_possible(branch, upstream, cwd=cwd)
    if local_sha == upstream_sha:
        print(f"Локальная ветка '{branch.name}' актуальна относительно '{upstream.short_ref}'")
        return
    fast_forward_local_branch(branch, upstream, cwd=cwd)
    print(f"Локальная ветка '{branch.name}' обновлена до '{upstream.short_ref}'")


def find_commits_for_tasks(ref: str, tasks: list[str], *, cwd: Path) -> list[Commit]:
    if not tasks:
        return []
    args = ['log', ref]
    for task in tasks:
        args.extend(['--grep', task])
    args.extend(['--fixed-strings', '--regexp-ignore-case', f'--format={LOG_RECORD_FORMAT}'])
    out = git_text(args, cwd=cwd)
    return _parse_log_records(out)


def find_commits(ref: str, task: str, *, cwd: Path) -> list[Commit]:
    return find_commits_for_tasks(ref, [task], cwd=cwd)


def fetch_mr_ref(remote: str, mr_id: int, *, cwd: Path) -> str:
    """Force-fetch GitLab MR-ref в локальный tracking-ref и вернуть его имя.

    Выполняет `git fetch <remote> +refs/merge-requests/<ID>/head:refs/remotes/<remote>/mr/<ID>`
    с `check=True`. Префикс `+` гарантирует force-update при rebase MR на удалённой
    стороне. Refspec передаётся одноразово в командной строке — `.git/config` не
    модифицируется.

    При ошибке fetch (нет сети, MR отсутствует, нет прав) поднимается
    `ToolError` с русским сообщением и stderr git; вызывающий код возвращает
    `EXIT_USAGE`.
    """
    refspec = f'+refs/merge-requests/{mr_id}/head:refs/remotes/{remote}/mr/{mr_id}'
    try:
        run_git(['fetch', remote, refspec], check=True, cwd=cwd)
    except ToolError as exc:
        raise ToolError(f'Не удалось получить ref MR {mr_id} с remote {remote}.\n{exc}', EXIT_USAGE) from exc
    return f'refs/remotes/{remote}/mr/{mr_id}'


def fetch_mr_discovery_refs(remote: str, *, cwd: Path) -> None:
    """Force-fetch all GitLab MR heads into refs/remotes/<remote>/merge-requests/*."""
    refspec = f'+refs/merge-requests/*/head:refs/remotes/{remote}/merge-requests/*'
    try:
        run_git(['fetch', remote, refspec], check=True, cwd=cwd)
    except ToolError as exc:
        raise ToolError(f'Не удалось получить список MR refs с remote {remote}.\n{exc}', EXIT_USAGE) from exc


def list_mr_discovery_refs(remote: str, *, cwd: Path) -> list[MrRefCandidate]:
    """Return locally available refs/remotes/<remote>/merge-requests/<ID> refs."""
    prefix = f'refs/remotes/{remote}/merge-requests'
    out = git_text(['for-each-ref', '--format=%(refname)', prefix], cwd=cwd)
    candidates: list[MrRefCandidate] = []
    for raw_ref in out.splitlines():
        mr_ref = raw_ref.strip()
        if not mr_ref:
            continue
        match = re.search('/merge-requests/(\\d+)$', mr_ref)
        if not match:
            continue
        candidates.append(MrRefCandidate(mr_id=int(match.group(1)), mr_ref=mr_ref))
    return sorted(candidates, key=lambda item: item.mr_id)


def _parse_log_records(out: str) -> list[Commit]:
    """Разобрать `git log --format=...` с разделителями 0x1f / 0x1e."""
    commits: list[Commit] = []
    for raw in out.split(LOG_SEP_COMMIT):
        raw = raw.strip('\n')
        if not raw:
            continue
        parts = raw.split(LOG_SEP_FIELD, 3)
        if len(parts) != 4:
            continue
        sha, date, subject, body = parts
        commits.append(Commit(sha=sha, date=date, subject=subject, body=body))
    return commits


def resolve_task_source(ref: str, task: str, *, candidate_commits: list[Commit] | None = None, cwd: Path) -> SourceResolution:
    """Стратегия источника `task`: коммиты по `git log --grep TASK`.

    Полностью повторяет текущее поведение task-режима: target = newest,
    base = `resolve_merge_base(oldest, ref)`. Пустой список коммитов возвращается
    как есть; `EXIT_NO_COMMITS` обрабатывается выше по стеку.

    `git log --grep` — грубый префильтр; точный отбор выполняет
    `filter_commits_for_task` с граничной семантикой ключа (D2), единой с
    выбором MR-кандидатов: `PROSV-50` не матчит `PROSV-503`.
    """
    candidates = find_commits(ref, task, cwd=cwd) if candidate_commits is None else candidate_commits
    commits = filter_commits_for_task(candidates, task)
    if commits:
        target = commits[0].sha
        base = resolve_merge_base(commits[-1].sha, ref, cwd=cwd)
    else:
        target = ''
        base = ''
    task_shas = {c.sha.lower() for c in commits}
    root_base = base in task_shas and not git_ok(['rev-parse', '--verify', '--quiet', f'{base}^'], cwd=cwd)
    return SourceResolution(
        task=task, ref=ref, target=target, base=base,
        commits=commits, task_shas=task_shas, source_kind='task',
        root_base=root_base,
    )


def resolve_mr_ref_source(ref: str, mr_ref: str, mr_id: int, *, cwd: Path) -> MrRefResolution:
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
        mr_head = git_text(['rev-parse', mr_ref], cwd=cwd).strip()
    except ToolError as exc:
        raise ToolError(f'Локальный ref {mr_ref} недоступен после fetch.\n{exc}', EXIT_NO_COMMITS) from exc
    if not mr_head:
        raise ToolError(f'Локальный ref {mr_ref} не вернул SHA после fetch.', EXIT_NO_COMMITS)
    base_proc = git_result(['merge-base', ref, mr_head], cwd=cwd)
    base = base_proc.stdout.decode('utf-8', errors='replace').strip() if base_proc.returncode == 0 else ''
    if not base:
        raise ToolError(f'MR {mr_id} не имеет общей истории с {ref}. Проверьте базовый ref (-b/--branch) или соответствие MR базовой ветке.', EXIT_NO_COMMITS)
    out = git_text(['log', f'{base}..{mr_head}', f'--format={LOG_RECORD_FORMAT}'], cwd=cwd)
    commits = _parse_log_records(out)
    task_shas = {c.sha.lower() for c in commits}
    return MrRefResolution(mr_id=mr_id, mr_ref=mr_ref, target=mr_head, base=base, commits=commits, task_shas=task_shas)


def mr_resolution_to_source(resolution: MrRefResolution, ref: str, task: str, *, review_dir_name: str | None = None) -> SourceResolution:
    return SourceResolution(
        task=task, ref=ref, target=resolution.target, base=resolution.base,
        commits=resolution.commits, task_shas=resolution.task_shas,
        source_kind='mr', mr_id=resolution.mr_id, mr_ref=resolution.mr_ref,
        review_dir_name=review_dir_name,
    )


def resolve_mr_source(remote: str, ref: str, task: str, mr_id: int, *, cwd: Path, refresh: bool = True) -> SourceResolution:
    """Стратегия explicit `mr`: коммиты диапазона `BASE..MR_HEAD`.

    Никаких HTTP-вызовов и токенов GitLab не используется.
    """
    if refresh:
        mr_ref = fetch_mr_ref(remote, mr_id, cwd=cwd)
    else:
        mr_ref = f'refs/remotes/{remote}/mr/{mr_id}'
        if not git_ok(['rev-parse', '--verify', '--quiet', f'{mr_ref}^{{commit}}'], cwd=cwd):
            raise ToolError(f'Локальный ref MR {mr_id} недоступен. Нажмите «Обновить список», чтобы получить коммиты с remote {remote}.', EXIT_USAGE)
    resolution = resolve_mr_ref_source(ref, mr_ref, mr_id, cwd=cwd)
    return mr_resolution_to_source(resolution, ref, task)


def task_key_pattern(task: str) -> re.Pattern[str]:
    return re.compile(f'(?<![A-Za-z0-9]){re.escape(task)}(?![A-Za-z0-9])', re.IGNORECASE)


def commit_matches_task(commit: Commit, task: str) -> bool:
    pattern = task_key_pattern(task)
    return bool(pattern.search(commit.subject) or pattern.search(commit.body))


def filter_commits_for_task(commits: list[Commit], task: str) -> list[Commit]:
    return [commit for commit in commits if commit_matches_task(commit, task)]


def mr_matches_task(resolution: MrRefResolution, task: str) -> bool:
    return any((commit_matches_task(commit, task) for commit in resolution.commits))


def resolve_mr_sources_from_tasks(remote: str, ref: str, tasks: list[str], *, cwd: Path) -> tuple[list[SourceResolution], list[str]]:
    """Find all MR refs matching tasks and return sources ordered by task, then MR ID."""
    fetch_mr_discovery_refs(remote, cwd=cwd)
    candidates = list_mr_discovery_refs(remote, cwd=cwd)
    resolved: list[MrRefResolution] = []
    for candidate in candidates:
        try:
            resolved.append(resolve_mr_ref_source(ref, candidate.mr_ref, candidate.mr_id, cwd=cwd))
        except ToolError:
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
            safe_task = re.sub(r'[\\/:*?"<>|]', '_', task)
            sources.append(mr_resolution_to_source(resolution, ref, task, review_dir_name=f'{safe_task}__mr-{resolution.mr_id}'))
    return (sources, missing)


def resolve_merge_base(first_commit: str, ref: str, *, cwd: Path) -> str:
    parent = f'{first_commit}^'
    if not git_ok(['rev-parse', '--verify', '--quiet', parent], cwd=cwd):
        parent = first_commit
    result = git_result(['merge-base', parent, ref], cwd=cwd)
    if result.returncode == 0:
        base = result.stdout.decode('utf-8', errors='replace').strip()
        if base:
            return base
    resolved = git_result(['rev-parse', '--verify', parent], cwd=cwd)
    applied = resolved.stdout.decode('utf-8', errors='replace').strip() if resolved.returncode == 0 else parent
    print(f"Предупреждение: git merge-base для '{parent}' и '{ref}' не вычислен; применена база {applied}.", file=sys.stderr)
    return parent


def _resolve_repo_root(repo: Path) -> Path:
    repo_root_str = git_text(['rev-parse', '--show-toplevel'], cwd=repo).strip()
    return Path(repo_root_str)


def _fetch_and_resolve_repository(repo_root: Path, ref_arg: str, remote: str, *, branch_explicit: bool) -> str:
    cwd = repo_root
    branch_to_refresh = local_review_branch(ref_arg, cwd=cwd)
    sync_result = git_result(['fetch', remote, '--prune'], cwd=cwd)
    if sync_result.returncode != 0:
        stderr = decode_process_stderr(sync_result)
        details = f'\n{stderr}' if stderr else ''
        raise ToolError(f"Не удалось синхронизировать репозиторий с remote '{remote}'.{details}", EXIT_USAGE)
    print(f"Синхронизация с remote '{remote}': ok")
    refresh_local_review_branch(branch_to_refresh, remote, sync_result, cwd=cwd)
    return resolve_target_ref(ref_arg, remote, branch_explicit=branch_explicit, cwd=cwd)


def resolve_target_ref(ref: str, remote: str, *, branch_explicit: bool, cwd: Path) -> str:
    """Резолв target-ref с учётом явности (D1).

    Явно заданный ref (`-b` или prefix-config `branch`), не резолвящийся после
    синхронизации, — ошибка кода 1 без подмены другим ref. Фолбэк-цепочка
    `origin/master → origin/main → master → main → HEAD` применяется только для
    встроенного умолчания (`branch_explicit=False`).
    """
    if branch_explicit:
        if git_ok(['rev-parse', '--verify', '--quiet', ref], cwd=cwd):
            return ref
        raise ToolError(f"Явно заданный ref '{ref}' не найден после синхронизации с remote '{remote}'. Проверьте значение -b/--branch или ветку в prefix-config; фолбэк на origin/master для явного ref не применяется.", EXIT_USAGE)
    return resolve_ref(ref, cwd=cwd)



def normalize_repository_path(value: str | Path, invocation_cwd: Path | None = None) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (invocation_cwd or Path.cwd()) / path
    return os.path.normcase(str(path.resolve()))



def validate_commit_sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-fA-F]{40}', value) is None:
        raise ValueError('Граница требует полный 40-символьный SHA коммита.')
    return value.lower()



def normalize_after_commits(entries: list, invocation_cwd: Path | None = None) -> dict[str, str]:
    """Validate boundary entries without running Git or reading repository data."""
    if not isinstance(entries, list) or not entries:
        raise ValueError('afterCommits требует непустой массив границ.')
    result: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, dict):
            if set(entry) != {'repoPath', 'sha'}:
                raise ValueError('Граница требует только repoPath и sha.')
            path, sha = entry['repoPath'], entry['sha']
        elif isinstance(entry, (tuple, list)) and len(entry) == 2:
            path, sha = entry
        else:
            raise ValueError('Неверный формат границы коммитов.')
        if not isinstance(path, str) or not path.strip() or '\x00' in path:
            raise ValueError('Граница требует непустой путь репозитория.')
        key = normalize_repository_path(path, invocation_cwd)
        if key in result:
            raise ValueError('Повтор границы для одного репозитория.')
        result[key] = validate_commit_sha(sha)
    return result



def prepare_repository_contexts(
    repositories: list[Path] | tuple[Path, ...], ref: str, remote: str,
    *, branch_explicit: bool, refresh: bool = True,
) -> list[RepositoryContext]:
    contexts = []
    for repository in repositories:
        root = _resolve_repo_root(Path(repository))
        resolved = (_fetch_and_resolve_repository(root, ref, remote, branch_explicit=branch_explicit)
                    if refresh else resolve_target_ref(ref, remote, branch_explicit=branch_explicit, cwd=root))
        contexts.append(RepositoryContext(Path(repository), root, resolved))
    return contexts



def resolve_commit_sources(
    task: str, repositories: list[Path] | tuple[Path, ...], ref: str,
    remote: str, branch_explicit: bool, mr_id: int | None = None, *, refresh: bool = True,
) -> list[tuple[RepositoryContext, SourceResolution]]:
    if mr_id is not None and len(repositories) != 1:
        raise ToolError('MR-режим поддерживает только один репозиторий.')
    contexts = prepare_repository_contexts(repositories, ref, remote, branch_explicit=branch_explicit, refresh=refresh)
    return [(context, resolve_task_source(context.ref, task, cwd=context.root)
             if mr_id is None else resolve_mr_source(remote, context.ref, task, mr_id, cwd=context.root, refresh=refresh))
            for context in contexts]



def _after_shas(context: RepositoryContext, source: SourceResolution, boundary: str) -> set[str]:
    return set(git_text(['rev-list', f'{boundary}..{source.target}', '--'], cwd=context.root).lower().split())



def restrict_sources(
    records: list[tuple[RepositoryContext, SourceResolution]], boundaries: dict[str, str],
) -> list[tuple[RepositoryContext, SourceResolution]]:
    """Validate every boundary before producing any effective source records."""
    known = {normalize_repository_path(context.root) for context, _ in records}
    if set(boundaries) - known:
        raise ToolError('Граница указывает на репозиторий вне настроенного source.')
    for context, source in records:
        boundary = boundaries.get(normalize_repository_path(context.root))
        if boundary is not None and boundary not in source.task_shas:
            raise ToolError(f'Граница {boundary} отсутствует в source репозитория {context.root}.')
    effective = []
    for context, source in records:
        boundary = boundaries.get(normalize_repository_path(context.root))
        root_base = source.root_base or (source.source_kind == 'task' and bool(source.commits)
                                         and source.base == source.commits[-1].sha)
        allowed = _after_shas(context, source, boundary) if boundary is not None else source.task_shas
        commits = [commit for commit in source.commits if commit.sha.lower() in allowed]
        effective.append((context, replace(source, commits=commits,
                                          task_shas={commit.sha.lower() for commit in commits}, root_base=root_base)))
    return effective



def project_commit_candidates(records: list[tuple[RepositoryContext, SourceResolution]]) -> list[dict]:
    return [{'repoPath': str(context.root), 'target': source.target,
             'commits': [{'sha': commit.sha, 'subject': commit.subject, 'date': commit.date,
                          'afterCount': len(source.task_shas & _after_shas(context, source, commit.sha))}
                         for commit in source.commits]}
            for context, source in records]



def build_commit_selection(
    task: str, records: list[tuple[RepositoryContext, SourceResolution]], boundaries: dict[str, str],
    *, source_identity: dict | None = None, remote: str = 'origin',
) -> dict:
    source = records[0][1]
    identity = {'mode': source.source_kind}
    if source.mr_id is not None:
        identity['mrId'] = source.mr_id
        if source_identity is not None:
            identity = source_identity
        else:
            project_url = resolve_git_remote_project_url(records[0][0].root, remote)
            if project_url:
                identity['projectUrl'] = project_url
    return {'schemaVersion': 1, 'mode': 'after' if boundaries else 'all', 'task': task,
            'source': identity,
            'boundaries': [{'repoPath': path, 'sha': sha} for path, sha in boundaries.items()],
            'repositories': [{'repoPath': str(context.root), 'target': item.target,
                              'commits': [commit.sha.lower() for commit in item.commits]}
                             for context, item in records]}



def validate_source_identity(value: object, mr_id: int | None) -> dict:
    allowed = {'mode', 'mrId', 'gitlabInstanceId', 'gitlabProjectPath'}
    required = {'mode', 'mrId', 'gitlabProjectPath'}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - allowed:
        raise ValueError('Source identity требует mode, mrId и gitlabProjectPath.')
    if value['mode'] != 'mr' or type(value['mrId']) is not int or value['mrId'] != mr_id:
        raise ValueError('Source identity не соответствует explicit MR.')
    for field in ('gitlabInstanceId', 'gitlabProjectPath'):
        if field not in value:
            continue
        text = value[field]
        if not isinstance(text, str) or not text.strip() or any(ord(char) < 32 for char in text):
            raise ValueError('Неверное значение source identity.')
    return dict(value)
