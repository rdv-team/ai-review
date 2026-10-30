"""Единый git-runner review-tasks (D6).

Все git-подпроцессы движка проходят через этот модуль с единой политикой:

- `-c core.quotepath=false` — стабильные UTF-8 пути (кириллица без экранирования);
- `env GIT_TERMINAL_PROMPT=0` — git не запрашивает креды интерактивно в терминале
  (`git fetch` без доступной неинтерактивной аутентификации падает честной ошибкой
  вместо зависания на вводе; credential helpers не затрагиваются);
- bytes-I/O с decode `errors="replace"`.

Потребители: `cli.py`, `prefix_config.py`, `index_json/config_xml.py`. Вне этого
модуля прямых subprocess-вызовов git в `src/review_tasks` не остаётся (кроме
`local_service`).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class ToolError(RuntimeError):
    """Ошибка, которую имеет смысл показать пользователю stderr-ом."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def git_env() -> dict[str, str]:
    """Окружение для git-подпроцесса: неинтерактивная аутентификация."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _run(args: list[str], cwd: Path | None = None,
         input_bytes: bytes | None = None, check: bool = True,
         timeout: float | None = None) -> subprocess.CompletedProcess:
    """Единая точка вызова git. Возвращает CompletedProcess (bytes)."""
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        input=input_bytes,
        capture_output=True,
        env=git_env(),
        timeout=timeout,
    )
    if check and result.returncode != 0:
        cmd = " ".join(args)
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(f"Команда завершилась с ошибкой: {cmd}\n{err}")
    return result


def run_git(args: list[str], cwd: Path | None = None,
            input_bytes: bytes | None = None, check: bool = True,
            timeout: float | None = None) -> bytes:
    """Выполнить git с корректной UTF-8 обработкой путей и вернуть stdout-bytes."""
    full = ["git", "-c", "core.quotepath=false"] + args
    return _run(full, cwd=cwd, input_bytes=input_bytes, check=check, timeout=timeout).stdout


def git_text(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    return run_git(args, cwd=cwd, check=check).decode("utf-8", errors="replace")


def git_ok(args: list[str], cwd: Path | None = None) -> bool:
    result = _run(["git", "-c", "core.quotepath=false"] + args, cwd=cwd, check=False)
    return result.returncode == 0


def git_result(args: list[str], cwd: Path | None = None,
               input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    """Выполнить git без исключения, сохранив stdout/stderr для диагностики."""
    return _run(
        ["git", "-c", "core.quotepath=false"] + args,
        cwd=cwd,
        input_bytes=input_bytes,
        check=False,
    )


def decode_process_stderr(result: subprocess.CompletedProcess) -> str:
    return result.stderr.decode("utf-8", errors="replace").strip()
