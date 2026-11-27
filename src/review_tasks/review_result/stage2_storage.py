"""Task-local, atomic storage and mutations for the Stage 2 result."""

from __future__ import annotations

import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from review_tasks.sgr_schema import ReviewYamlError
from review_tasks.review_result.stage1_storage import STAGE1_NAME, STAGE2_NAME, review_info_from_cwd


_CANDIDATE_RE = re.compile(r"^candidate-[1-9][0-9]*$")
_TOP_LEVEL_KEYS = ("findings", "missing_context")


class _LiteralString(str):
    pass


class _Stage2Dumper(yaml.SafeDumper):
    pass


def _represent_literal(dumper: yaml.SafeDumper, value: _LiteralString) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")


_Stage2Dumper.add_representer(_LiteralString, _represent_literal)


def _error(message: str) -> ReviewYamlError:
    return ReviewYamlError(message)


def stage2_path(cwd: Path | None = None) -> Path:
    return review_info_from_cwd(cwd) / STAGE2_NAME


def _empty_draft() -> dict[str, Any]:
    return {
        "findings": [],
        "missing_context": [],
    }


def _validate_top_level(raw: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _error(f"{source}: Stage 2 YAML должен быть объектом верхнего уровня.")
    if "questions_to_author" in raw:
        raise _error(
            f"{source}: несовместимый прежний формат Stage 2 с `questions_to_author`. "
            "Повторно подготовьте каталог и выполните ревью с новыми шаблонами."
        )
    if set(raw) != set(_TOP_LEVEL_KEYS):
        raise _error(
            f"{source}: Stage 2 должен содержать только поля: {', '.join(_TOP_LEVEL_KEYS)}."
        )
    if not isinstance(raw["findings"], list):
        raise _error(f"{source}: поле `findings` должно быть списком.")
    if not isinstance(raw["missing_context"], list):
        raise _error(f"{source}: поле `missing_context` должно быть списком.")
    return raw


def load_stage2(*, cwd: Path | None = None) -> dict[str, Any]:
    path = stage2_path(cwd)
    if not path.exists():
        return _empty_draft()
    if not path.is_file():
        raise _error(f"{path}: Stage 2 должен быть обычным файлом.")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _error(f"{path}: не удалось прочитать Stage 2: {exc}") from exc
    # A suggestion may legitimately contain a fenced BSL block.  Only a fence
    # that wraps the whole persisted payload makes the file non-bare YAML.
    if text.lstrip().startswith("```"):
        raise _error(f"{path}: Stage 2 должен быть bare YAML без Markdown-обёртки.")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _error(f"{path}: YAML не разбирается: {exc}") from exc
    return _validate_top_level(raw, source=str(path))


def _literal_suggestions(data: dict[str, Any]) -> dict[str, Any]:
    rendered = copy.deepcopy(data)
    for finding in rendered["findings"]:
        if isinstance(finding, dict):
            suggestion = finding.get("suggestion")
            if isinstance(suggestion, str) and "\n" in suggestion:
                finding["suggestion"] = _LiteralString(suggestion)
    return rendered


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    ordered = {key: data[key] for key in _TOP_LEVEL_KEYS}
    payload = yaml.dump(
        _literal_suggestions(ordered),
        Dumper=_Stage2Dumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    ).replace("\r\n", "\n").replace("\r", "\n")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _save_if_changed(before: dict[str, Any], after: dict[str, Any], *, cwd: Path | None) -> bool:
    path = stage2_path(cwd)
    changed = before != after or not path.exists()
    if not changed:
        return False
    if not path.parent.is_dir():
        raise _error(f"Не найден каталог _review_info: {path.parent}")
    try:
        _write_atomic(path, after)
    except OSError as exc:
        raise _error(f"{path}: не удалось атомарно сохранить Stage 2: {exc}") from exc
    return True


def _stage1_candidate(candidate: str, *, cwd: Path | None) -> dict[str, str]:
    if _CANDIDATE_RE.fullmatch(candidate) is None:
        raise _error("`--candidate` должен иметь формат candidate-N.")
    path = review_info_from_cwd(cwd) / STAGE1_NAME
    if not path.is_file():
        raise _error(f"Не найден обязательный Stage 1 результат: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _error(f"{path}: не удалось прочитать Stage 1: {exc}") from exc
    try:
        items = raw["risk_candidates"]["items"]
    except (KeyError, TypeError):
        raise _error(f"{path}: неверная структура Stage 1 candidates.")
    if not isinstance(items, list):
        raise _error(f"{path}: `risk_candidates.items` должен быть списком.")
    matches = [item for item in items if isinstance(item, dict) and item.get("name") == candidate]
    if len(matches) != 1:
        raise _error(f"Кандидат `{candidate}` не найден однозначно в Stage 1.")
    item = matches[0]
    identity = {key: item.get(key) for key in ("name", "area", "file")}
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise _error(f"Кандидат `{candidate}` содержит неверную identity в Stage 1.")
    return identity  # type: ignore[return-value]


def _finding_index(data: dict[str, Any], candidate: str, *, required: bool) -> int | None:
    matches: list[int] = []
    for index, finding in enumerate(data["findings"]):
        if not isinstance(finding, dict):
            raise _error(f"Stage 2: `findings[{index}]` должен быть объектом.")
        if finding.get("name") == candidate:
            matches.append(index)
    if len(matches) > 1:
        raise _error(f"Stage 2 содержит несколько findings для `{candidate}`.")
    if not matches:
        if required:
            raise _error(f"Finding для `{candidate}` не найден.")
        return None
    return matches[0]


def _details(finding: dict[str, Any], *, candidate: str) -> tuple[dict[str, Any], list[Any]]:
    verification = finding.get("verification")
    traces = finding.get("traces")
    if not isinstance(verification, dict):
        raise _error(f"Finding `{candidate}`: поле `verification` должно быть объектом.")
    if not isinstance(verification.get("channels"), list):
        raise _error(f"Finding `{candidate}`: `verification.channels` должен быть списком.")
    if not isinstance(traces, list):
        raise _error(f"Finding `{candidate}`: поле `traces` должно быть списком.")
    return verification, traces


def _read_suggestion_code(value: str, *, candidate: str, cwd: Path | None) -> str:
    root = Path.cwd() if cwd is None else cwd
    allowed = (root / "_review_info" / "tmp").resolve()
    expected = allowed / f"{candidate}-suggestion.bsl"
    supplied = Path(value)
    path = supplied if supplied.is_absolute() else root / supplied
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise _error(f"Не найден suggestion code file `{value}`.") from exc
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        raise _error("Suggestion code file должен находиться внутри `_review_info/tmp`.") from exc
    if resolved != expected:
        expected_relative = f"_review_info/tmp/{candidate}-suggestion.bsl"
        raise _error(
            f"`--suggestion-code-file`: для `{candidate}` допускается только "
            f"`{expected_relative}`; передан `{value}`."
        )
    if not resolved.is_file():
        raise _error(f"Suggestion code file `{value}` должен быть обычным файлом.")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise _error(f"Не удалось прочитать suggestion code file `{value}`: {exc}") from exc
    if payload.startswith(b"\xef\xbb\xbf"):
        raise _error("Suggestion code file должен быть UTF-8 без BOM.")
    try:
        return payload.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise _error("Suggestion code file должен иметь корректную кодировку UTF-8.") from exc


def _suggestion(
    text: str | None,
    code_file: str | None,
    *,
    candidate: str,
    cwd: Path | None,
) -> str | None:
    if code_file is not None and (text is None or not text.strip()):
        raise _error("`--suggestion-code-file` требует непустой `--suggestion-text`.")
    if text is None:
        return None
    if code_file is None:
        return text
    code = _read_suggestion_code(code_file, candidate=candidate, cwd=cwd)
    longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{text}\n\n{fence}bsl\n{code.rstrip(chr(10))}\n{fence}"


def upsert_finding(
    *, candidate: str, start_line: int, end_line: int, observation: str,
    source: str, confidence: str, false_positive_risk: str, decision: str,
    alternative_interpretation: str | None = None, drop_reason: str | None = None,
    severity: str | None = None, suggestion_text: str | None = None,
    suggestion_code_file: str | None = None, reset_details: bool = False,
    question_to_author: str | None = None,
    cwd: Path | None = None,
) -> bool:
    identity = _stage1_candidate(candidate, cwd=cwd)
    if question_to_author is not None and (
        not isinstance(question_to_author, str) or not question_to_author.strip()
    ):
        raise _error("`--question-to-author` должен быть непустой строкой.")
    if isinstance(start_line, bool) or isinstance(end_line, bool) or start_line <= 0 or end_line <= 0:
        raise _error("start_line и end_line должны быть положительными целыми числами.")
    if start_line > end_line:
        raise _error("start_line не может быть больше end_line.")
    if decision == "keep":
        if severity is None:
            raise _error("`--severity` обязателен при `--decision keep`.")
        if drop_reason is not None:
            raise _error("`--drop-reason` запрещён при `--decision keep`.")
        if suggestion_text is None or not suggestion_text.strip():
            raise _error("`--suggestion-text` обязателен при `--decision keep`.")
    elif decision == "drop":
        if drop_reason is None or not drop_reason.strip():
            raise _error("`--drop-reason` обязателен при `--decision drop`.")
        if severity is not None:
            raise _error("`--severity` запрещён при `--decision drop`.")
        if suggestion_text is not None or suggestion_code_file is not None:
            raise _error("Suggestion запрещён при `--decision drop`.")
    suggestion = (
        None
        if decision == "drop"
        else _suggestion(suggestion_text, suggestion_code_file, candidate=candidate, cwd=cwd)
    )
    before = load_stage2(cwd=cwd)
    after = copy.deepcopy(before)
    index = _finding_index(after, candidate, required=False)
    verification: dict[str, Any] = {
        "needed": False, "channels": [], "verification_summary": "self_evident"
    }
    traces: list[Any] = []
    if index is not None and not reset_details:
        verification, traces = _details(after["findings"][index], candidate=candidate)
    finding = {
        **identity,
        "start_line": start_line,
        "end_line": end_line,
        "observation": observation,
        "question_to_author": question_to_author,
        "source": source,
        "confidence": confidence,
        "self_check": {
            "false_positive_risk": false_positive_risk,
            "alternative_interpretation": alternative_interpretation,
            "decision": decision,
            "drop_reason": drop_reason,
        },
        "severity": severity,
        "suggestion": suggestion,
        "verification": verification,
        "traces": traces,
    }
    if index is None:
        after["findings"].append(finding)
    else:
        after["findings"][index] = finding
    return _save_if_changed(before, after, cwd=cwd)


def upsert_verification_channel(
    *, candidate: str, capability: str, hypothesis: str, status: str,
    result_note: str, cwd: Path | None = None,
) -> bool:
    if not capability:
        raise _error("`--capability` не может быть пустым.")
    before = load_stage2(cwd=cwd)
    after = copy.deepcopy(before)
    index = _finding_index(after, candidate, required=True)
    assert index is not None
    verification, _ = _details(after["findings"][index], candidate=candidate)
    channels = verification["channels"]
    if any(not isinstance(channel, dict) for channel in channels):
        raise _error(f"Finding `{candidate}`: каждый verification channel должен быть объектом.")
    for pos, channel in enumerate(channels):
        assert isinstance(channel, dict)
        if channel.get("capability") == capability:
            channels[pos] = {
                "capability": capability, "hypothesis": hypothesis,
                "status": status, "result_note": result_note,
            }
            break
    else:
        channels.append({
            "capability": capability, "hypothesis": hypothesis,
            "status": status, "result_note": result_note,
        })
    verification["needed"] = bool(channels)
    verification["verification_summary"] = (
        "self_evident" if not channels
        else "verified" if all(channel.get("status") == "applied" for channel in channels)
        else "limited_unverified"
    )
    return _save_if_changed(before, after, cwd=cwd)


def upsert_trace(
    *, candidate: str, established_at: int, obligation: str, status: str,
    satisfied_at: int | None = None, cwd: Path | None = None,
) -> bool:
    if isinstance(established_at, bool) or established_at <= 0:
        raise _error("`--established-at` должен быть положительным целым числом.")
    if satisfied_at is not None and (isinstance(satisfied_at, bool) or satisfied_at <= 0):
        raise _error("`--satisfied-at` должен быть положительным целым числом.")
    if not obligation:
        raise _error("`--obligation` не может быть пустым.")
    before = load_stage2(cwd=cwd)
    after = copy.deepcopy(before)
    index = _finding_index(after, candidate, required=True)
    assert index is not None
    _, traces = _details(after["findings"][index], candidate=candidate)
    if any(not isinstance(current, dict) for current in traces):
        raise _error(f"Finding `{candidate}`: каждый trace должен быть объектом.")
    trace = {
        "established_at": established_at, "obligation": obligation,
        "satisfied_at": satisfied_at, "status": status,
    }
    for pos, current in enumerate(traces):
        assert isinstance(current, dict)
        if current.get("established_at") == established_at and current.get("obligation") == obligation:
            traces[pos] = trace
            break
    else:
        traces.append(trace)
    return _save_if_changed(before, after, cwd=cwd)


def set_result(
    *, missing_context: list[str] | None = None, cwd: Path | None = None,
) -> bool:
    before = load_stage2(cwd=cwd)
    after = copy.deepcopy(before)
    after["missing_context"] = list(missing_context or [])
    return _save_if_changed(before, after, cwd=cwd)


__all__ = [
    "load_stage2", "set_result", "stage2_path", "upsert_finding",
    "upsert_trace", "upsert_verification_channel",
]
