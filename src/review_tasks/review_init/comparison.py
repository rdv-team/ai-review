"""Task-scoped full-file comparison for review initialization and local viewing."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from review_tasks.infrastructure.git import ToolError, run_git
from review_tasks.review_init.prompt_config import DiffViewerConfig
from review_tasks.review_init.task_scope import (
    DeletedSpan, DiffHunk, DiffLine, NetOldEntry, NetOldLineState, ScopeAmbiguity,
    TaskScopeError, TaskScopeIntegrityError, apply_net_old_hunks, materialize_before,
    parse_final_blame, parse_unified_diff_hunks, quarantine_origins,
)


@dataclass(frozen=True)
class ComparisonSource:
    repository: Path
    base: str | None
    target: str
    task_shas: frozenset[str]


class ComparisonWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    repository: str | None = None
    paths: list[str] = Field(default_factory=list)
    reason: str | None = None


class ComparisonError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ComparisonSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    task: str = Field(min_length=1)
    task_dir: str = Field(alias="taskDir", min_length=1)
    state: Literal["ready", "empty", "unavailable"]
    warnings: list[ComparisonWarning] = Field(default_factory=list)
    error: ComparisonError | None = None


TOOL_MISSING_EXIT_CODE = 24

COMPARISON_LAUNCHER = "open-changes.cmd"


def render_launcher(viewer: DiffViewerConfig) -> str:
    """Build the only owner of application lookup and fixed viewer arguments."""
    applications = {
        "kdiff3": ("kdiff3.exe", "KDiff3", " --L1 Before --L2 After"),
        "meld": ("Meld.exe", "Meld", ""),
        "winmerge": ("WinMergeU.exe", "WinMerge", ""),
    }
    executable, directory, suffix = applications[viewer.tool]
    locations = [
        rf"%ProgramFiles%\{directory}\{executable}",
        rf"%ProgramFiles(x86)%\{directory}\{executable}",
        rf"%LocalAppData%\Programs\{directory}\{executable}",
        rf"%LocalAppData%\{directory}\{executable}",
    ]
    candidates = " ".join(f'"{path}"' for path in locations)
    # Quotes/control characters are rejected by the typed configuration.
    # Batch percent doubling and disabled delayed expansion preserve literal
    # %, !, ^, &, parentheses, spaces, and Unicode without a second CMD parse.
    override = viewer.executable.replace("%", "%%")
    prefix = " /r" if viewer.tool == "winmerge" else ""
    script = f'''@echo off
setlocal DisableDelayedExpansion
"%SystemRoot%\\System32\\chcp.com" 65001 >nul
set "DIFF_VIEWER_EXE={override}"
if defined DIFF_VIEWER_EXE goto check
for %%I in ({executable}) do set "DIFF_VIEWER_EXE=%%~$PATH:I"
if defined DIFF_VIEWER_EXE goto check
for %%I in ({candidates}) do if exist "%%~I" if not defined DIFF_VIEWER_EXE set "DIFF_VIEWER_EXE=%%~I"
:check
if not defined DIFF_VIEWER_EXE goto missing
if not exist "%DIFF_VIEWER_EXE%" goto missing
if exist "%DIFF_VIEWER_EXE%\\" goto missing
start "" "%DIFF_VIEWER_EXE%"{prefix} "%~dp0before" "%~dp0after"{suffix}
if errorlevel 1 exit /b 1
exit /b 0
:missing
echo Selected diff viewer {viewer.tool} is unavailable. Configure diff_viewer.executable or install the selected application. 1>&2
if /I not "%~1"=="--service" pause
exit /b 24
'''
    # CMD can misread UTF-8 batch files with LF after a codepage switch/GOTO.
    # CRLF is a platform requirement of this executable artifact, not of data.
    return script.replace("\n", "\r\n")


class ComparisonLaunchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def launch_comparison(script: Path) -> None:
    if sys.platform != "win32":
        raise ComparisonLaunchError("changes_platform_unsupported", "Запуск внешнего сравнения доступен в Windows.")
    processor = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
    # Use a fixed basename and cwd so metacharacters in the task path never enter CMD syntax.
    result = subprocess.run(
        [str(processor), "/d", "/c", f"{COMPARISON_LAUNCHER} --service"],
        cwd=script.parent, timeout=10, check=False,
        env=dict(os.environ),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode == TOOL_MISSING_EXIT_CODE:
        raise ComparisonLaunchError("changes_tool_missing", "Выбранное приложение сравнения не найдено. Проверьте diff_viewer.executable или установите выбранный инструмент.")
    if result.returncode != 0:
        raise ComparisonLaunchError("changes_open_failed", "Не удалось запустить выбранное приложение сравнения.")


@contextmanager
def task_lock(task_dir: Path, *, timeout: float = 5.0) -> Iterator[None]:
    task_dir = task_dir.resolve()
    task_dir.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file: unlinking it could let a third process lock another inode.
    lock_path = task_dir.parent / f".{task_dir.name}.changes.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Каталог сравнения занят: {task_dir}")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


TASK_SCOPE = ComparisonWarning(
    code="task_scope",
    message="Diff показывает сохранившийся вклад выбранных коммитов. After соответствует TARGET; before может быть синтетическим.",
)


def _git(source: ComparisonSource, args: list[str]) -> bytes:
    return run_git(args, cwd=source.repository, timeout=60)


def _safe_path(raw: bytes) -> str:
    path = raw.decode("utf-8", errors="strict")
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or "\\" in path or any(p in {".", ".."} for p in path.split("/")):
        raise ValueError(f"Небезопасный путь Git: {path!r}")
    if os.name == "nt":
        for part in parts:
            if any(ord(c) < 32 or c in '<>:"|?*' for c in part) or part.endswith((" ", ".")) or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
                raise ValueError(f"Путь нельзя представить в Windows: {path!r}")
    return path


def raw_entries(raw: bytes) -> list[tuple[str, str, str, str, str]]:
    fields = raw.split(b"\0")
    entries = []
    for i in range(0, len(fields) - 1, 2):
        header = fields[i].decode("ascii").split()
        if len(header) != 5 or not header[0].startswith(":"):
            raise ValueError("Некорректный raw diff Git")
        old_mode, new_mode, old_id, new_id, _status = header
        entries.append((_safe_path(fields[i + 1]), old_mode[1:], new_mode, old_id, new_id))
    return entries


@dataclass(frozen=True)
class FileChange:
    sha: str
    parent: str | None
    merge: bool
    old_mode: str
    new_mode: str
    old_id: str
    new_id: str


class UnsupportedEntry(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _path_histories(source: ComparisonSource) -> dict[str, list[FileChange]]:
    """Read selected paths, then replay their first-parent states up to TARGET."""
    range_arg = source.target if source.base is None else f"{source.base}..{source.target}"
    records = [line.split() for line in _git(
        source, ["rev-list", "--reverse", "--topo-order", "--parents", range_arg, "--"],
    ).decode("ascii").splitlines()]
    parents = {record[0]: record[1:] for record in records}
    selected = {sha.lower() for sha in source.task_shas}
    if not selected <= parents.keys():
        raise ValueError("Выбранные коммиты отсутствуют в разрешённой истории BASE/TARGET.")
    cache: dict[str, list[tuple[str, str, str, str, str]]] = {}

    def changes(sha: str) -> list[tuple[str, str, str, str, str]]:
        if sha not in cache:
            args = ["--raw", "--no-abbrev", "--no-renames", "--no-ext-diff", "--no-textconv", "-z"]
            if parents[sha]:
                command = ["diff", *args, parents[sha][0], sha, "--"]
            else:
                # rev-list hides parents at a shallow boundary; it is not a root.
                commit = _git(source, ["cat-file", "commit", sha])
                if any(line.startswith(b"parent ") for line in commit.split(b"\n\n", 1)[0].splitlines()):
                    raise ValueError("Необходимая история недоступна на границе shallow-репозитория.")
                command = ["diff-tree", "--root", "--no-commit-id", "-r", *args, sha, "--"]
            cache[sha] = raw_entries(_git(source, command))
        return cache[sha]

    candidates = {entry[0] for sha in sorted(selected) for entry in changes(sha)}
    histories: dict[str, list[FileChange]] = {path: [] for path in sorted(candidates)}
    chain: list[str] = []
    sha = source.target
    while sha in parents:
        chain.append(sha)
        sha = parents[sha][0] if parents[sha] else ""
    for sha in reversed(chain):
        for path, old_mode, new_mode, old_id, new_id in changes(sha):
            if path in histories:
                histories[path].append(FileChange(
                    sha, parents[sha][0] if parents[sha] else None, len(parents[sha]) > 1,
                    old_mode, new_mode, old_id, new_id,
                ))
    return histories


def _byte_hunks(source: ComparisonSource, path: str, change: FileChange,
                old: bytes, new: bytes) -> list[DiffHunk[bytes]]:
    """Use Git's hunk coordinates, but take every payload directly from blobs."""
    args = ["--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", "--unified=0"]
    if change.parent is None:
        command = ["show", "--format=", "--root", *args, change.sha, "--", f":(literal){path}"]
    else:
        command = ["diff", *args, change.parent, change.sha, "--", f":(literal){path}"]
    raw = _git(source, command)
    old_lines, new_lines = _blob_lines(old), _blob_lines(new)
    hunks: list[DiffHunk[bytes]] = []
    for hunk in parse_unified_diff_hunks(raw.decode("utf-8", errors="replace")):
        old_start = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        new_start = hunk.new_start if hunk.new_count == 0 else hunk.new_start - 1
        lines = [DiffLine("-", line) for line in old_lines[old_start:old_start + hunk.old_count]]
        lines.extend(DiffLine("+", line) for line in new_lines[new_start:new_start + hunk.new_count])
        hunks.append(DiffHunk(
            hunk.header, hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count, "", lines,
        ))
    return hunks


def _blob_lines(content: bytes) -> list[bytes]:
    # Git counts LF-delimited lines; a bare CR is content, not a new line.
    parts = content.split(b"\n")
    return [part + b"\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _current_generation(history: list[FileChange], selected: set[str]) -> list[FileChange]:
    """Discard foreign lifecycle predecessors before content capability checks."""
    for index in range(len(history) - 1, -1, -1):
        change = history[index]
        if change.sha not in selected and (change.old_mode == "000000" or change.new_mode == "000000"):
            return history[index + 1:]
    return history


def _file_pair(source: ComparisonSource, path: str,
               history: list[FileChange],
               ambiguities: list[ScopeAmbiguity]) -> tuple[bytes | None, bytes | None]:
    if not history:
        raise TaskScopeError("Путь выбранного коммита не имеет однозначной истории до TARGET.")
    selected = {sha.lower() for sha in source.task_shas}
    history = _current_generation(history, selected)
    if not history:
        return None, None
    supported = {"000000", "100644", "100755"}
    if any(change.old_mode not in supported or change.new_mode not in supported for change in history):
        raise UnsupportedEntry("unsupported_git_type")
    if any(change.merge for change in history):
        raise TaskScopeError("Изменение пути при merge требует неоднозначной атрибуции.")
    if all(change.old_id == change.new_id for change in history):
        raise UnsupportedEntry("mode_only")
    blobs: dict[str, bytes] = {}

    def blob(oid: str, mode: str) -> bytes:
        if mode == "000000":
            return b""
        if oid not in blobs:
            blobs[oid] = _git(source, ["cat-file", "blob", oid])
        return blobs[oid]

    old = blob(history[0].old_id, history[0].old_mode)
    versions = [old] + [blob(change.new_id, change.new_mode) for change in history]
    try:
        for version in versions:
            if b"\0" in version:
                raise UnicodeError
            version.decode("utf-8", errors="strict")
    except UnicodeError:
        if any(change.sha not in selected for change in history):
            raise UnsupportedEntry("unsupported_content")
        before = old if history[0].old_mode != "000000" else None
        after = versions[-1] if history[-1].new_mode != "000000" else None
        return before, after

    return _replay_text_pair(source, path, history, versions, selected, ambiguities)


def _replay_text_pair(
    source: ComparisonSource,
    path: str,
    history: list[FileChange],
    versions: list[bytes],
    selected: set[str],
    ambiguities: list[ScopeAmbiguity],
) -> tuple[bytes | None, bytes | None]:
    """Adapt exact blob slices to shared replay, then validate and materialize."""
    old = versions[0]
    states = [NetOldLineState(text=line) for line in _blob_lines(old)]
    entries: list[NetOldEntry[bytes]] = []
    deletions: list[DeletedSpan] = []
    before_exists = history[0].old_mode != "000000"
    task_created_generation = False
    for change, new in zip(history, versions[1:]):
        own = change.sha in selected
        if own and change.old_mode == "000000" and change.new_mode != "000000":
            task_created_generation = True
        if change.old_id != change.new_id:
            states = apply_net_old_hunks(
                states, _byte_hunks(source, path, change, old, new),
                is_task_commit=own, entries=entries, deletions=deletions, strict=True,
                ambiguities=ambiguities,
            )
            if b"".join(state.text for state in states) != new:
                raise TaskScopeIntegrityError("История строк не воспроизводит точный TARGET.")
        old = new
    # Blame follows renames to the old path, while this view intentionally
    # represents a selected create/rename as an addition at the new path.
    # Its provenance comes from that creation and the same shared replay,
    # including any subsequent foreign changes in the created generation.
    if history[-1].new_mode != "000000" and not task_created_generation:
        blame = parse_final_blame(_git(source, [
            "blame", "--line-porcelain", "--no-textconv", source.target, "--", path,
        ]), selected)
        expected = {line.target_line - 1 for line in blame}
        if any(index < 0 or index >= len(states) for index in expected):
            raise TaskScopeIntegrityError("Координаты final blame не совпадают с точным TARGET.")
        actual = {index for index, state in enumerate(states) if state.from_task}
        mismatch = {index for index in expected ^ actual if not states[index].ambiguous}
        if mismatch:
            affected = {entry for index in mismatch for entry in states[index].old_entry_ids}
            states = quarantine_origins(states, deletions, affected, ambiguities, positions=mismatch)
    before = materialize_before(states, entries, deletions, ambiguities=ambiguities)
    # Foreign context in a file originally created by the task requires a
    # synthetic existing before rather than attributing the whole file to it.
    before_exists = before_exists or bool(before)
    after_exists = history[-1].new_mode != "000000"
    return before if before_exists else None, old if after_exists else None


def _partial_warning(source: ComparisonSource, path: str,
                     ambiguities: list[ScopeAmbiguity]) -> ComparisonWarning:
    ranges: list[tuple[int, int]] = []
    for span in sorted({(item.start, item.end) for item in ambiguities}):
        if ranges and span[0] <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], span[1]))
        else:
            ranges.append(span)
    locations = ", ".join(
        f"TARGET gap after line {start}" if start == end else f"TARGET L{start + 1}–L{end}"
        for start, end in ranges
    )
    return ComparisonWarning(
        code="task_scope_partial", repository=str(source.repository), paths=[path],
        reason="ambiguous_region",
        message=f"Частичный результат {path}: неоднозначные группы оставлены TARGET-контекстом с обеих сторон; покрывающие области с учётом общих границ: {locations}.",
    )


def write_comparison(task: str, task_dir: Path, sources: list[ComparisonSource], *,
                     viewer: DiffViewerConfig | None = None) -> ComparisonSummary:
    """Write under the caller's task_lock, after the task directory's final reset."""
    task_dir = task_dir.resolve()
    changes = task_dir / "_review_info" / "changes"
    warnings = [TASK_SCOPE.model_copy(deep=True)]
    try:
        changes.mkdir(parents=True)
        for side in ("before", "after"):
            (changes / side).mkdir()
        # Preserve CMD-required CRLF; the shared LF writer is for other text.
        (changes / COMPARISON_LAUNCHER).write_bytes(
            render_launcher(viewer or DiffViewerConfig()).encode("utf-8"),
        )
        exported = 0
        unique = list(dict.fromkeys(sources))
        for index, source in enumerate(unique, 1):
            basename = re.sub(r"[^\w.-]", "_", source.repository.name) or "repository"
            directory = "" if len(unique) == 1 else f"repo-{index:03d}-{basename}"
            seen: dict[str, str] = {}
            for path, history in _path_histories(source).items():
                # Detect case and file/directory aliases before creating either side.
                for part in [str(p) for p in PurePosixPath(path).parents if str(p) != "."] + [path]:
                    key = os.path.normcase(part)
                    if key in seen and seen[key] != part:
                        raise ValueError(f"Коллизия путей: {seen[key]!r}, {part!r}")
                    seen[key] = part
                try:
                    ambiguities: list[ScopeAmbiguity] = []
                    before, after = _file_pair(source, path, history, ambiguities)
                except (TaskScopeError, UnsupportedEntry) as exc:
                    reason = exc.reason if isinstance(exc, UnsupportedEntry) else "ambiguous_history"
                    code = "unsupported_entry" if reason in {"mode_only", "unsupported_git_type"} else "task_scope_unresolved"
                    warnings.append(ComparisonWarning(
                        code=code, repository=str(source.repository), paths=[path], reason=reason,
                        message=f"Путь исключён из сравнения: {path} ({reason}): {exc}",
                    ))
                    continue
                if ambiguities:
                    warnings.append(_partial_warning(source, path, ambiguities))
                if before == after:
                    continue
                for side, content in (("before", before), ("after", after)):
                    if content is None:
                        continue
                    output = changes / side / directory / path
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(content)
                    exported += 1
        state = "ready" if exported else "empty"
        return ComparisonSummary(task=task, task_dir=str(task_dir), state=state, warnings=warnings)
    except (OSError, ValueError, ToolError, subprocess.TimeoutExpired) as exc:
        return ComparisonSummary(task=task, task_dir=str(task_dir), state="unavailable", warnings=warnings,
                                 error=ComparisonError(code="comparison_export_failed", message=str(exc)))
