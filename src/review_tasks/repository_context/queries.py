"""Ограниченный поиск и чтение из TARGET подготовленного ревью."""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import BinaryIO

from ..infrastructure.git import ToolError, git_result, run_git, stream_git
from .sources import RepositorySource, normalize_path, source_label, validate_source


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise ToolError("Превышено время ожидания Git-команды. Сузьте запрос или укажите --repository.")
    return remaining


def code_search(source: RepositorySource | list[RepositorySource], search_text: str, *,
                file_patterns: list[str] | None = None, case_sensitive: bool = False,
                use_perl_regexp: bool = False) -> str:
    if not search_text.strip() or "\0" in search_text:
        raise ToolError("search-text не должен быть пустым или содержать NUL.")
    patterns = [normalize_path(pattern, pattern=True) for pattern in file_patterns or [] if pattern]
    args = ["--no-pager", "grep"]
    if not case_sensitive:
        args.append("-i")
    args.extend(["-P" if use_perl_regexp else "-F", "-n", "--no-color",
                 "--max-count", "101", "-e", search_text])
    sources = source if isinstance(source, list) else [source]
    deadline = monotonic() + 10
    files: dict[tuple[str, str], list[tuple[int, str]]] = {}
    matched_files: set[tuple[str, str]] = set()
    count = 0
    truncated = False
    for item in sources:
        validate_source(item, timeout=_remaining(deadline))
        try:
            result = git_result([*args, item.target, "--", *patterns], cwd=Path(item.repo_path),
                                timeout=_remaining(deadline), local_only=True)
        except ToolError as exc:
            raise ToolError(f"{source_label(item, sources)}: {exc} Сузьте --file-pattern или --repository.") from exc
        if result.returncode == 1 and not result.stderr:
            continue
        if result.returncode:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise ToolError(f"{source_label(item, sources)}: ошибка git grep: {error}")
        for line in result.stdout.decode("utf-8", errors="replace").rstrip("\n").split("\n"):
            parts = line.split(":", 3)
            if len(parts) != 4 or not parts[2].isdigit():
                continue
            _, path, number, content = parts
            key = (item.id, path)
            matched_files.add(key)
            if count >= 100:
                truncated = True
                continue
            files.setdefault(key, []).append((int(number), content.rstrip("\r")))
            count += 1
    _remaining(deadline)
    if not files:
        return "No matches found"
    output: list[str] = []
    if truncated:
        output.append(
            f"Note: Showing the first 100 matches across {len(matched_files)} matching files. "
            "Some files are partially shown or omitted entirely. Narrow file_patterns or --repository to see the rest.\n"
        )
    labels = {item.id: source_label(item, sources) for item in sources}
    previous = None
    for (identifier, path), matches in files.items():
        if len(sources) > 1 and identifier != previous:
            output.append(f"Repository: {labels[identifier]}\n")
        previous = identifier
        output.append(f"File: {path}\nMatch lines: {len(matches)}\n")
        output.extend(f"{number}|{content}\n" for number, content in matches)
        output.append("\n")
    return "".join(output)


def file_find(source: RepositorySource | list[RepositorySource], query_name: str, *, case_sensitive: bool = False) -> str:
    missing = "// The file was not found"
    if "\0" in query_name:
        raise ToolError("query-name не должен содержать NUL.")
    sources = source if isinstance(source, list) else [source]
    deadline = monotonic() + 10
    for item in sources:
        validate_source(item, timeout=_remaining(deadline))
    if not query_name.strip():
        return missing
    query = query_name.replace("\\", "/")
    if not case_sensitive:
        query = query.lower()
    files = []
    for item in sources:
        output = run_git(["ls-tree", "-r", "--name-only", "--end-of-options", item.target],
                         cwd=Path(item.repo_path), timeout=_remaining(deadline), local_only=True)
        for path in output.decode("utf-8", errors="replace").rstrip("\n").split("\n"):
            base = path.rsplit("/", 1)[-1]
            if path and ("." in base or base in {"Makefile", "Dockerfile", "LICENSE", "Vagrantfile", "Containerfile"}):
                files.append((item, path))
    _remaining(deadline)
    for basename_only in (True, False):
        matches = []
        for item, path in files:
            compared = path.rsplit("/", 1)[-1] if basename_only else path
            if not case_sensitive:
                compared = compared.lower()
            if query in compared:
                matches.append((item, path))
                if len(matches) == 100:
                    break
        if matches:
            lines = []
            previous = None
            for item, path in matches:
                if len(sources) > 1 and item.id != previous:
                    lines.append(f"Repository: {source_label(item, sources)}")
                previous = item.id
                lines.append(path)
            return "\n".join(lines)
    return missing


def file_read(source: RepositorySource | list[RepositorySource], file_path: str, *,
              start_line: int | None = None, end_line: int | None = None) -> str:
    path = normalize_path(file_path)
    start = max(start_line or 1, 1)
    end = max(end_line or 0, 0)
    if end and start > end:
        raise ToolError(f"Неверный диапазон строк: start-line {start} больше end-line {end}.")
    sources = source if isinstance(source, list) else [source]
    deadline = monotonic() + 30
    matches = []
    for item in sources:
        validate_source(item, timeout=_remaining(deadline))
        object_name = f"{item.target}:{path}"
        object_type = run_git(["cat-file", "--batch-check=%(objecttype)", "-z"],
                             input_bytes=object_name.encode("utf-8") + b"\0",
                             cwd=Path(item.repo_path), timeout=_remaining(deadline), local_only=True)
        if object_type.strip() == b"blob":
            matches.append(item)
    if not matches:
        raise ToolError(f"Файл {path} не найден в выбранных ревизиях или не обозначает blob.")
    if len(matches) > 1:
        available = "; ".join(source_label(item, sources) for item in matches)
        raise ToolError(f"Путь {path} неоднозначен. Укажите --repository <ID>. Совпадения: {available}")
    selected_source = matches[0]
    limit = min(500, end - start + 1) if end else 500
    selected: list[str] = []
    total = 0

    def consume(stdout: BinaryIO) -> None:
        nonlocal total
        trailing_lf = False
        for line in stdout:
            total += 1
            trailing_lf = line.endswith(b"\n")
            if total >= start and len(selected) < limit:
                selected.append(line.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8", errors="replace"))
        if trailing_lf:
            total += 1
            if total >= start and len(selected) < limit:
                selected.append("")

    stream_git(["show", "--end-of-options", f"{selected_source.target}:{path}"], consume,
               cwd=Path(selected_source.repo_path), timeout=_remaining(deadline), local_only=True)
    _remaining(deadline)
    if total > 0 and start > total:
        raise ToolError(f"В файле {path} только {total} строк; запрошено начало {start}.")
    effective_end = min(end, total) if end else total
    truncated = effective_end - start + 1 > 500
    output = [f"File: {path} (Total lines: {total})\n",
              f"IS_TRUNCATED: {str(truncated).lower()}\n",
              f"LINE_RANGE: {start}-{start - 1 + len(selected)}\n"]
    output.extend(f"{start + index}|{line}\n" for index, line in enumerate(selected))
    if truncated:
        output.append("\nNote: Results truncated to 500 lines. Please narrow your line range.\n")
    if len(sources) > 1:
        output.insert(0, f"Repository: {source_label(selected_source, sources)}\n")
    return "".join(output)
