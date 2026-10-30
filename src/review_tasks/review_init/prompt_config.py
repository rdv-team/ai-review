from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from review_tasks.infrastructure.text_files import read_text_required


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewPromptConfig:
    model: str
    max_model_tokens: int
    output_soft_token_buffer: int
    output_hard_token_buffer: int
    patch_extra_lines_before: int
    patch_extra_lines_after: int
    dynamic_context_enabled: bool
    dynamic_context_max_lines_up: int
    ignore_consecutive_comment_lines_threshold: int

    @property
    def fixed_lines_before(self) -> int:
        return self.patch_extra_lines_before

    @property
    def fixed_lines_after(self) -> int:
        return self.patch_extra_lines_after

    @property
    def soft_input_budget(self) -> int:
        return self.max_model_tokens - self.output_soft_token_buffer

    @property
    def hard_input_budget(self) -> int:
        return self.max_model_tokens - self.output_hard_token_buffer


@dataclass(frozen=True)
class DiffViewerConfig:
    tool: Literal["kdiff3", "meld", "winmerge"] = "kdiff3"
    executable: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.tool, str) or self.tool not in {"kdiff3", "meld", "winmerge"}:
            raise ConfigError("Некорректная конфигурация diff_viewer: tool должен быть kdiff3, meld или winmerge.")
        if not isinstance(self.executable, str):
            raise ConfigError("Некорректная конфигурация diff_viewer: executable должен быть строкой.")
        if not self.executable:
            return
        path = self.executable
        if not (PureWindowsPath(path).is_absolute() or Path(path).is_absolute()):
            raise ConfigError("Некорректная конфигурация diff_viewer: executable должен быть абсолютным путём.")
        if any(ord(char) < 32 or char in '\"<>|?*' for char in path):
            raise ConfigError("Некорректная конфигурация diff_viewer: executable содержит недопустимые символы пути.")
        windows_path = PureWindowsPath(path)
        if windows_path.is_absolute():
            if windows_path.suffix.lower() != ".exe":
                raise ConfigError("Некорректная конфигурация diff_viewer: Windows executable должен указывать приложение .exe.")
            if any(part.endswith((" ", ".")) or ":" in part for part in windows_path.parts[1:]):
                raise ConfigError("Некорректная конфигурация diff_viewer: недопустимое имя компонента executable.")


@dataclass(frozen=True)
class EffectiveConfig:
    review_prompt: ReviewPromptConfig
    diff_viewer: DiffViewerConfig = field(default_factory=DiffViewerConfig)


def _load_toml_file(path: Path) -> dict[str, Any]:
    try:
        text = read_text_required(path)
    except FileNotFoundError as exc:
        raise ConfigError(f"Файл конфигурации не найден: {path}") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Некорректный TOML в файле конфигурации {path}: {exc}") from exc


def _packaged_default() -> dict[str, Any]:
    try:
        text = resources.files("review_tasks").joinpath("configuration.toml").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError("Packaged configuration.toml не найден.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Некорректный packaged configuration.toml: {exc}") from exc
    return tomllib.loads(text)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _expect_int(section: dict[str, Any], key: str) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Некорректная конфигурация review_prompt: `{key}` должен быть целым числом.")
    return value


def _expect_bool(section: dict[str, Any], key: str) -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"Некорректная конфигурация review_prompt: `{key}` должен быть true/false.")
    return value


def _expect_str(section: dict[str, Any], key: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Некорректная конфигурация review_prompt: `{key}` должен быть непустой строкой.")
    return value


def _review_prompt_from_dict(data: dict[str, Any]) -> ReviewPromptConfig:
    section = data.get("review_prompt")
    if not isinstance(section, dict):
        raise ConfigError("Некорректная конфигурация review_prompt: секция `[review_prompt]` отсутствует.")

    cfg = ReviewPromptConfig(
        model=_expect_str(section, "model"),
        max_model_tokens=_expect_int(section, "max_model_tokens"),
        output_soft_token_buffer=_expect_int(section, "output_soft_token_buffer"),
        output_hard_token_buffer=_expect_int(section, "output_hard_token_buffer"),
        patch_extra_lines_before=_expect_int(section, "patch_extra_lines_before"),
        patch_extra_lines_after=_expect_int(section, "patch_extra_lines_after"),
        dynamic_context_enabled=_expect_bool(section, "dynamic_context_enabled"),
        dynamic_context_max_lines_up=_expect_int(section, "dynamic_context_max_lines_up"),
        ignore_consecutive_comment_lines_threshold=_expect_int(section, "ignore_consecutive_comment_lines_threshold"),
    )
    _validate_review_prompt(cfg)
    return cfg


def _diff_viewer_from_dict(data: dict[str, Any]) -> DiffViewerConfig:
    section = data.get("diff_viewer")
    if not isinstance(section, dict):
        raise ConfigError("Некорректная конфигурация diff_viewer: секция [diff_viewer] должна быть таблицей.")
    if set(section) - {"tool", "executable"}:
        raise ConfigError("Некорректная конфигурация diff_viewer: поддерживаются только tool и executable.")
    tool = section.get("tool")
    if not isinstance(tool, str):
        raise ConfigError("Некорректная конфигурация diff_viewer: tool должен быть строкой.")
    executable = section.get("executable")
    if not isinstance(executable, str):
        raise ConfigError("Некорректная конфигурация diff_viewer: executable должен быть строкой.")
    return DiffViewerConfig(tool=tool, executable=executable)


def _validate_review_prompt(cfg: ReviewPromptConfig) -> None:
    invalid: list[str] = []
    if cfg.max_model_tokens <= 0:
        invalid.append("max_model_tokens должен быть > 0")
    for key in (
        "output_soft_token_buffer",
        "output_hard_token_buffer",
        "dynamic_context_max_lines_up",
        "ignore_consecutive_comment_lines_threshold",
    ):
        if getattr(cfg, key) < 0:
            invalid.append(f"{key} должен быть >= 0")
    # Контекстные строки блока ограничены диапазоном 0..10 (кламп удалён —
    # завышенное значение теперь честно отклоняется, а не молча урезается).
    for key in ("patch_extra_lines_before", "patch_extra_lines_after"):
        value = getattr(cfg, key)
        if value < 0 or value > 10:
            invalid.append(f"{key} должен быть в диапазоне 0..10")
    if cfg.output_soft_token_buffer < cfg.output_hard_token_buffer:
        invalid.append("output_soft_token_buffer должен быть >= output_hard_token_buffer")
    if cfg.soft_input_budget <= 0:
        invalid.append("max_model_tokens - output_soft_token_buffer должен быть > 0")
    if cfg.hard_input_budget <= 0:
        invalid.append("max_model_tokens - output_hard_token_buffer должен быть > 0")
    if invalid:
        raise ConfigError("Некорректная конфигурация review_prompt: " + "; ".join(invalid) + ".")


def load_effective_config(
    target_repo: str | Path | None,
    explicit_config: str | Path | None,
) -> EffectiveConfig:
    data = _packaged_default()
    if target_repo is not None:
        repo_config = Path(target_repo) / ".review-tasks.toml"
        if repo_config.exists():
            data = _deep_merge(data, _load_toml_file(repo_config))
    if explicit_config is not None:
        data = _deep_merge(data, _load_toml_file(Path(explicit_config)))
    return EffectiveConfig(
        review_prompt=_review_prompt_from_dict(data),
        diff_viewer=_diff_viewer_from_dict(data),
    )
