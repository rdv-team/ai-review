"""Источники контекста подготовленной task capsule."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..infrastructure.git import ToolError, run_git
from ..infrastructure.text_files import write_text_lf


class RepositorySource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    repo_path: str
    target: str = Field(pattern=r"^[0-9a-fA-F]{40}$")

    @field_validator("repo_path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if not value.strip() or "\0" in value or not Path(value).is_absolute():
            raise ValueError("Требуется абсолютный путь локального репозитория")
        return str(Path(value).resolve())


class RepositoryContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    repositories: list[RepositorySource] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_sources(self) -> RepositoryContext:
        if len({item.id for item in self.repositories}) != len(self.repositories):
            raise ValueError("Повторяющиеся ID репозиториев")
        if len({os.path.normcase(item.repo_path) for item in self.repositories}) != len(self.repositories):
            raise ValueError("Повторяющиеся пути репозиториев")
        return self


def write_sources(task_dir: Path, sources: list[tuple[Path, str]]) -> Path:
    context = RepositoryContext(schema_version=1, repositories=[
        RepositorySource(id=f"repo-{index}", repo_path=str(root.resolve()), target=target)
        for index, (root, target) in enumerate(sources, 1)
    ])
    path = task_dir / "_review_info" / "repository_context.json"
    write_text_lf(path, context.model_dump_json(indent=2) + "\n")
    return path


def prepare_sources(repositories: list[tuple[Path, str]],
                    material_targets: dict[Path, str]) -> list[tuple[Path, str]]:
    """Зафиксировать настроенные ref для источников без материалов ревью."""
    sources = []
    for root, ref in repositories:
        target = material_targets.get(root)
        if target is None:
            target = run_git(["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
                             cwd=root, timeout=10, local_only=True).decode("ascii").strip()
        sources.append((root, target))
    return sources


def source_label(source: RepositorySource, sources: list[RepositorySource]) -> str:
    name = Path(source.repo_path).name
    if sum(Path(item.repo_path).name == name for item in sources) > 1:
        name = source.repo_path
    return f"{source.id} ({name})"


def select_sources(task_dir: Path, repository: str | None = None) -> list[RepositorySource]:
    path = task_dir / "_review_info" / "repository_context.json"
    try:
        context = RepositoryContext.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(f"Не удалось прочитать описатель {path}. Повторите подготовку задачи через init, при необходимости с --force.\n{exc}") from exc
    sources = context.repositories
    selected = sources if repository is None else [item for item in sources if item.id == repository]
    if not selected:
        available = "; ".join(source_label(item, sources) for item in sources)
        raise ToolError(f"Неизвестный ID репозитория: {repository}. Доступны: {available}")
    return selected


def validate_source(source: RepositorySource, *, timeout: float = 10) -> None:
    root = Path(source.repo_path)
    # Не разрешаем Git найти чужой репозиторий в родительском каталоге.
    if not root.is_dir() or not ((root / ".git").exists() or ((root / "HEAD").is_file() and (root / "objects").is_dir())):
        raise ToolError(f"Недоступен локальный репозиторий: {root}. Повторите init.")
    run_git(["rev-parse", "--verify", "--end-of-options", source.target + "^{commit}"], cwd=root, timeout=timeout, local_only=True)


def select_source(task_dir: Path, repository: str | None = None) -> RepositorySource:
    sources = select_sources(task_dir, repository)
    if len(sources) != 1:
        available = "; ".join(source_label(item, sources) for item in sources)
        raise ToolError(f"Укажите --repository <ID>. Доступны: {available}")
    validate_source(sources[0])
    return sources[0]


def normalize_path(value: str, *, pattern: bool = False) -> str:
    normalized = value.replace("\\", "/")
    # Magic pathspec сохраняется; проверяем также первый компонент после magic.
    components = normalized
    if pattern and components.startswith(":(") and ")" in components:
        components = components.split(")", 1)[1]
    if "\0" in normalized or ".." in components.split("/"):
        raise ToolError("Путь не должен содержать NUL или компонент '..'.")
    if not pattern and (not normalized.strip() or normalized.startswith("/") or PureWindowsPath(normalized).drive):
        raise ToolError("Укажите относительный file-path внутри дерева TARGET.")
    return normalized
