"""OpenCode generation contracts and visible interactive discussion launch."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable


REVIEW_PROMPT = "Вызови task-local skill review-start-task и выполни ревью задачи."
CLI_TIMEOUT = 15


def format_log_event(event: dict) -> str:
    """Render a JSONL event without exposing its transport envelope or IDs."""
    if not isinstance(event, dict):
        return ""
    stamp = "--:--:--"
    timestamp = event.get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        try:
            stamp = datetime.fromtimestamp(timestamp / 1000).astimezone().strftime("%H:%M:%S")
        except (ValueError, OverflowError, OSError):
            pass
    kind = event.get("type")
    if not isinstance(kind, str):
        return ""
    part = event.get("part")
    part = part if isinstance(part, dict) else {}
    if kind in {"text", "reasoning"}:
        text = part.get("text")
        if not isinstance(text, str) or not text.strip():
            return ""
        label = "Размышление" if kind == "reasoning" else "Ответ"
        return f"[{stamp}] {label}\n{text.strip()}"
    if kind == "step_start":
        return f"[{stamp}] Начало шага"
    if kind == "step_finish":
        reason = part.get("reason")
        suffix = f": {reason}" if isinstance(reason, str) and reason else ""
        return f"[{stamp}] Шаг завершён{suffix}"
    if kind == "error":
        return f"[{stamp}] Ошибка\n{_log_value(event.get('error'))}"
    if kind != "tool_use":
        return ""
    name = part.get("tool")
    name = name if isinstance(name, str) else ""
    state = part.get("state")
    state = state if isinstance(state, dict) else {}
    lines = [f"[{stamp}] Инструмент" + (f": {name}" if name else "")]
    for key, label in (("input", "Ввод"), ("output", "Результат"), ("error", "Ошибка")):
        if key in state:
            lines.extend([f"{label}:", _log_value(state[key])])
    return "\n".join(lines)


def _log_value(value: object) -> str:
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, dict) and isinstance(value.get("message"), str):
        return value["message"]
    return json.dumps(value, ensure_ascii=False, indent=2)


def _generation(cli_version: str) -> str:
    if cli_version not in {"v1", "v2"}:
        raise ValueError("OpenCode CLI generation must be v1 or v2.")
    return cli_version


def validate_version(output: str, cli_version: str = "v2") -> str:
    """Validate the configured generation; version detection never selects it."""
    _generation(cli_version)
    match = re.search(r"(?im)^\s*(?:opencode\s+)?v?(\d+)\.\d+\.\d+(?:[-+][\w.-]+)?\s*$", output)
    if not match:
        raise ValueError("OpenCode: не удалось распознать версию CLI.")
    actual = match.group(0).strip()
    if int(match.group(1)) != int(cli_version[1:]):
        raise ValueError(f"OpenCode {actual} не соответствует режиму {cli_version}. "
                         "Укажите соответствующий executable или измените opencodeCliVersion и перезапустите сервис.")
    return actual


def required_run_flags(cli_version: str = "v2", model: str = "") -> tuple[str, ...]:
    flags = ("--standalone", "--auto", "--format") if _generation(cli_version) == "v2" else ("--command", "--dir")
    return flags + (("--model",) if model else ())


def headless_argv(executable: str, task_dir: Path, model: str = "", *,
                  cli_version: str = "v2", auto_flag: str = "--auto") -> list[str]:
    if _generation(cli_version) == "v2":
        argv = [executable, "run", "--standalone", "--auto", "--format", "json"]
    else:
        argv = [executable, "run", "--dir", str(task_dir), "--command", "review-start-task", auto_flag]
    if model:
        argv.extend(["--model", model])
    if cli_version == "v2":
        argv.append(REVIEW_PROMPT)
    return argv


def auth_argv(executable: str, cli_version: str = "v2") -> list[str]:
    argv = [executable, "auth", "list"]
    return argv + (["--standalone", "--format", "json"] if _generation(cli_version) == "v2" else [])


def has_auth(output: str, cli_version: str = "v2") -> bool:
    if _generation(cli_version) == "v1":
        counts = re.findall(r"(?i)(\d+)\s+credentials", output)
        return bool(counts and int(counts[-1]) > 0)
    try:
        integrations = json.loads(output)
    except (ValueError, TypeError) as exc:
        raise ValueError("OpenCode auth list вернул некорректный JSON.") from exc
    if not isinstance(integrations, list):
        raise ValueError("OpenCode auth list: ожидался массив integrations.")
    found = False
    for integration in integrations:
        if (not isinstance(integration, dict) or not isinstance(integration.get("id"), str)
                or not integration["id"] or not isinstance(integration.get("connections"), list)):
            raise ValueError("OpenCode auth list: некорректная integration.")
        for connection in integration["connections"]:
            if not isinstance(connection, dict):
                raise ValueError("OpenCode auth list: некорректное подключение.")
            kind = connection.get("type")
            fields = ("id", "label", "method") if kind == "credential" else ("name",) if kind == "env" else ()
            if not fields or any(not isinstance(connection.get(key), str) for key in fields):
                raise ValueError("OpenCode auth list: неизвестный формат подключения.")
            if kind == "credential" and connection["method"] not in {"key", "oauth", "external"}:
                raise ValueError("OpenCode auth list: неизвестный credential method.")
            if not connection["id" if kind == "credential" else "name"]:
                raise ValueError("OpenCode auth list: отсутствует ID подключения.")
            status = connection.get("status")
            if "status" in connection and (not isinstance(status, dict) or status.get("status") != "needs_auth"
                                           or not isinstance(status.get("message"), str)):
                raise ValueError("OpenCode auth list: неизвестный статус подключения.")
            if "status" not in connection:
                found = True
    return found


class OpenCodeLaunchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def discussion_argv(executable: str, task_dir: Path, model: str, context_path: Path, *,
                    cli_version: str = "v2", session_id: str = "") -> list[str]:
    relative = context_path.resolve().relative_to(task_dir.resolve()).as_posix()
    argv = [executable, str(task_dir.resolve()), "--prompt",
            f"Вызови skill review-discuss. Контекст: {relative}."]
    if _generation(cli_version) == "v2":
        if not session_id:
            raise OpenCodeLaunchError("opencode_open_failed", "OpenCode не вернул ID новой сессии.")
        argv.extend(["--session", session_id])
    elif model:
        argv.extend(["--model", model])
    return argv


def launch_discussion(executable: str, task_dir: Path, model: str, context_path: Path, *,
                      cli_version: str = "v2",
                      process_factory: Callable = subprocess.Popen,
                      platform: str = sys.platform) -> None:
    if platform != "win32":
        raise OpenCodeLaunchError("opencode_unavailable",
                                 "Автоматическое открытие терминала поддерживается только в Windows.")
    if not Path(executable).is_file():
        raise OpenCodeLaunchError("opencode_unavailable", "OpenCode executable недоступен.")
    task_dir = task_dir.resolve()
    if not task_dir.is_dir() or not context_path.is_file():
        raise OpenCodeLaunchError("opencode_unavailable", "Каталог задачи или контекст недоступен.")
    interpreter = Path(sys.executable)
    if interpreter.name.lower() == "pythonw.exe":
        interpreter = interpreter.with_name("python.exe")
    argv = [str(interpreter), str(Path(__file__).with_name("opencode_bootstrap.py")),
            executable, str(task_dir), model, str(context_path.resolve()), _generation(cli_version)]
    try:
        process_factory(argv, cwd=task_dir, creationflags=0x00000010)  # CREATE_NEW_CONSOLE
    except OSError as exc:
        raise OpenCodeLaunchError("opencode_open_failed",
                                 f"Не удалось открыть терминал OpenCode: {exc}") from exc


_CMD_META = re.compile(r'([()\[\]%!^"`<>&|;, *?])')


def _batch_argument(value: str) -> str:
    # A batch shim expands %* once more before its native target parses argv.
    # Preserve native backslash/quote rules through both cmd parsing passes.
    value = re.sub(r'(\\*)"', r'\1\1\\"', value)
    value = re.sub(r'(\\*)$', r'\1\1', value)
    value = '"' + value + '"'
    value = _CMD_META.sub(r'^\1', value)
    return _CMD_META.sub(r'^\1', value)


def _process_argv(argv: list[str]) -> list[str] | str:
    if sys.platform == "win32" and Path(argv[0]).suffix.lower() in {".cmd", ".bat"}:
        command = _CMD_META.sub(r'^\1', argv[0])
        command += " " + " ".join(_batch_argument(arg) for arg in argv[1:])
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        return f'{subprocess.list2cmdline([comspec])} /d /s /c "{command}"'
    return argv


def _interactive_run(argv: list[str], task_dir: Path) -> int:
    return subprocess.call(_process_argv(argv), cwd=task_dir)


def _bounded_run(argv: list[str], *, task_dir: Path | None = None,
                 timeout: float = CLI_TIMEOUT) -> subprocess.CompletedProcess:
    # Files avoid an inherited pipe keeping communicate() alive after a shim
    # exits. This also bounds failures of the best-effort process cleanup.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(_process_argv(argv), cwd=task_dir, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr)
        try:
            process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_bounded_process(process)
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
            raise
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(argv, process.returncode,
                                           stdout.read().decode("utf-8", errors="replace"),
                                           stderr.read().decode("utf-8", errors="replace"))


def _stop_bounded_process(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        # A newly discovered shared service is detached but still retains its
        # Win32 parent PID. Exclude that branch rather than taskkill /T it.
        script = f"""
$processes = @(Get-CimInstance Win32_Process)
$owned = [System.Collections.Generic.List[int]]::new()
$owned.Add({process.pid})
for ($index = 0; $index -lt $owned.Count; $index++) {{
    foreach ($child in $processes) {{
        if ($child.ParentProcessId -ne $owned[$index]) {{ continue }}
        if ($child.CommandLine -match '(?i)\\bserve\\s+--service(?:\\s|$)') {{ continue }}
        $owned.Add([int]$child.ProcessId)
    }}
}}
for ($index = $owned.Count - 1; $index -ge 0; $index--) {{
    taskkill.exe /PID $owned[$index] /F *> $null
}}
"""
        try:
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.kill()
    except OSError:
        pass


def check_version(executable: str, cli_version: str = "v2", *, timeout: float = CLI_TIMEOUT) -> str:
    try:
        result = _bounded_run([executable, "--version"], timeout=timeout)
        if result.returncode:
            raise ValueError("OpenCode: команда --version завершилась ошибкой.")
        return validate_version(result.stdout, cli_version)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise OpenCodeLaunchError("opencode_unavailable", str(exc)) from exc


def session_model(executable: str, session_id: str) -> str:
    """Read the actual assistant model without a prompt or default lookup."""
    try:
        result = _bounded_run([executable, "api", "session.message.list", "--param", f"sessionID={session_id}",
                               "--param", "type=assistant", "--param", "limit=1"],
                              timeout=2)
        model = json.loads(result.stdout)["data"][0]["model"]
        return f"{model['providerID']}/{model['id']}" + (f"#{model['variant']}" if model.get("variant") else "")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
        return "не определена"


def _api(executable: str, operation: str, task_dir: Path, *, data: dict | None = None) -> dict:
    argv = [executable, "api", operation]
    if data is None:
        argv.extend(["--param", f"location[directory]={task_dir}"])
    else:
        argv.extend(["--data", json.dumps(data, ensure_ascii=False, separators=(",", ":"))])
    try:
        result = _bounded_run(argv, task_dir=task_dir)
        if result.returncode:
            raise ValueError(f"OpenCode {operation}: {result.stderr.strip()[:1000] or 'CLI returned an error'}")
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError("Expected a JSON object")
        return response
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise OpenCodeLaunchError("opencode_open_failed", f"OpenCode {operation}: {exc}") from exc


def _model_ref(model: str) -> dict[str, str]:
    provider, sep, tail = model.partition("/")
    model_id, variant_sep, variant = tail.partition("#")
    if not sep or not provider or "#" in provider or not model_id or (variant_sep and (not variant or "#" in variant)):
        raise OpenCodeLaunchError("opencode_open_failed", f"Некорректная модель OpenCode: {model}")
    ref = {"providerID": provider, "id": model_id}
    if variant_sep:
        ref["variant"] = variant
    return ref


def _same_directory(location: object, task_dir: Path) -> bool:
    return (isinstance(location, dict) and isinstance(location.get("directory"), str)
            and Path(location["directory"]).resolve() == task_dir)


def _catalog_match(item: object, ref: dict[str, str]) -> bool:
    if (not isinstance(item, dict) or item.get("providerID") != ref["providerID"]
            or item.get("id") != ref["id"] or item.get("enabled") is not True):
        return False
    if "variant" not in ref:
        return True
    variants = item.get("variants")
    return isinstance(variants, list) and any(isinstance(v, dict) and v.get("id") == ref["variant"] for v in variants)


def prepare_discussion_argv(executable: str, task_dir: Path, model: str, context_path: Path, *,
                            cli_version: str = "v2") -> list[str]:
    """Prepare a fresh v2 session before its first prompt; v1 launches directly."""
    task_dir = task_dir.resolve()
    if _generation(cli_version) == "v1":
        return discussion_argv(executable, task_dir, model, context_path, cli_version=cli_version)
    payload: dict = {"location": {"directory": str(task_dir)}}
    if model:
        ref = _model_ref(model)
        catalog = _api(executable, "model.list", task_dir)
        available = catalog.get("data")
        if not _same_directory(catalog.get("location"), task_dir) or not isinstance(available, list):
            raise OpenCodeLaunchError("opencode_open_failed", "OpenCode model.list вернул некорректный каталог.")
        if not any(_catalog_match(item, ref) for item in available):
            raise OpenCodeLaunchError("opencode_open_failed", f"Выбранная модель OpenCode недоступна: {model}")
        payload["model"] = ref
    session = _api(executable, "session.create", task_dir, data=payload).get("data")
    if (not isinstance(session, dict) or not isinstance(session.get("id"), str) or not session["id"].strip()
            or session.get("parentID") is not None or not _same_directory(session.get("location"), task_dir)
            or (model and (not isinstance(session.get("model"), dict)
                           or any(session["model"].get(key) != value for key, value in payload["model"].items())))):
        raise OpenCodeLaunchError("opencode_open_failed", "OpenCode session.create вернул несовместимую сессию.")
    return discussion_argv(executable, task_dir, model, context_path, cli_version=cli_version, session_id=session["id"])
