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
import threading
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO


class ToolError(RuntimeError):
    """Ошибка, которую имеет смысл показать пользователю stderr-ом."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def git_env(*, local_only: bool = False) -> dict[str, str]:
    """Окружение для git-подпроцесса: неинтерактивная аутентификация."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if local_only:
        # Чтение контекста не должно инициировать fetch в partial clone.
        env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def _run(args: list[str], cwd: Path | None = None,
         input_bytes: bytes | None = None, check: bool = True,
         timeout: float | None = 120, local_only: bool = False) -> subprocess.CompletedProcess:
    """Единая точка вызова git. Возвращает CompletedProcess (bytes)."""
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            input=input_bytes,
            capture_output=True,
            env=git_env(local_only=local_only),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError('Превышено время ожидания Git-команды.') from exc
    if check and result.returncode != 0:
        cmd = " ".join(args)
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(f"Команда завершилась с ошибкой: {cmd}\n{err}")
    return result


def run_git(args: list[str], cwd: Path | None = None,
            input_bytes: bytes | None = None, check: bool = True,
            timeout: float | None = 120, *, local_only: bool = False) -> bytes:
    """Выполнить git с корректной UTF-8 обработкой путей и вернуть stdout-bytes."""
    full = ["git", "-c", "core.quotepath=false"] + args
    return _run(full, cwd=cwd, input_bytes=input_bytes, check=check, timeout=timeout, local_only=local_only).stdout


def git_text(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    return run_git(args, cwd=cwd, check=check).decode("utf-8", errors="replace")


def git_ok(args: list[str], cwd: Path | None = None) -> bool:
    result = _run(["git", "-c", "core.quotepath=false"] + args, cwd=cwd, check=False)
    return result.returncode == 0


def git_result(args: list[str], cwd: Path | None = None,
               input_bytes: bytes | None = None,
               timeout: float | None = 120, *, local_only: bool = False) -> subprocess.CompletedProcess:
    """Выполнить git без исключения, сохранив stdout/stderr для диагностики."""
    return _run(
        ["git", "-c", "core.quotepath=false"] + args,
        cwd=cwd,
        input_bytes=input_bytes,
        check=False,
        timeout=timeout,
        local_only=local_only,
    )


def stream_git(args: list[str], consume: Callable[[BinaryIO], None], *,
               cwd: Path, timeout: float = 30, local_only: bool = False) -> None:
    """Прочитать stdout Git с дедлайном на чтение и ожидание процесса."""
    with subprocess.Popen(
        ["git", "-c", "core.quotepath=false", *args], cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_env(local_only=local_only),
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        expired = threading.Event()
        stderr: list[bytes] = []

        def drain_stderr() -> None:
            stderr.append(process.stderr.read())

        def expire() -> None:
            expired.set()
            try:
                process.kill()
            except OSError:
                pass

        reader = threading.Thread(target=drain_stderr, daemon=True)
        timer = threading.Timer(timeout, expire)
        reader.start()
        timer.start()
        try:
            consume(process.stdout)
            process.wait()
            reader.join()
            if expired.is_set():
                raise ToolError("Превышено время ожидания Git-команды.")
            if process.returncode:
                error = b"".join(stderr).decode("utf-8", errors="replace").strip()
                raise ToolError(f"Git завершился с ошибкой: {error}")
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join()
            timer.join()


def decode_process_stderr(result: subprocess.CompletedProcess) -> str:
    return result.stderr.decode("utf-8", errors="replace").strip()
