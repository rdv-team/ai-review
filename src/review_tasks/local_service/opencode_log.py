"""Incremental readable companion to the untouched OpenCode JSONL streams."""
from __future__ import annotations

import json
from pathlib import Path

from ..integrations.opencode import format_log_event, session_model


READABLE_LOG_FILENAME = "opencode.agent.log"


class OpenCodeReadableLog:
    def __init__(self, stdout: Path, stderr: Path, output: Path, *, executable: str | None = None) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.output = output
        self._offsets = [0, 0]
        self._pending = [b"", b""]
        self._initialized = False
        self._executable = executable
        self._model_pending = True

    def update(self, final: bool = False) -> None:
        """Append newly completed records; I/O failures never fail a review."""
        try:
            if not self._initialized:
                self.output.parent.mkdir(parents=True, exist_ok=True)
                self.output.write_bytes(b"")
                self._initialized = True
            offsets = self._offsets.copy()
            pending = self._pending.copy()
            rendered = []
            for index, source in enumerate((self.stdout, self.stderr)):
                try:
                    with source.open("rb") as stream:
                        stream.seek(offsets[index])
                        chunk = stream.read()
                        offsets[index] = stream.tell()
                except FileNotFoundError:
                    chunk = b""
                lines = (pending[index] + chunk).split(b"\n")
                pending[index] = lines.pop()
                if final and pending[index]:
                    lines.append(pending[index])
                    pending[index] = b""
                for line in lines:
                    block = self._line(line, is_stderr=index == 1)
                    if block:
                        rendered.append(block)
            if rendered:
                data = ("\n\n".join(rendered) + "\n\n").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
                with self.output.open("r+b") as output:
                    output.seek(0, 2)
                    previous_size = output.tell()
                    try:
                        output.write(data)
                        output.flush()
                    except OSError:
                        output.truncate(previous_size)
                        raise
            self._offsets = offsets
            self._pending = pending
        except OSError:
            # Retain offsets and buffers, so the next poll retries this batch.
            return

    def _line(self, raw: bytes, *, is_stderr: bool) -> str:
        if not raw.strip():
            return ""
        try:
            text = raw.decode("utf-8").rstrip("\r")
        except UnicodeDecodeError:
            return "[Неполная или повреждённая строка UTF-8.]"
        try:
            event = json.loads(text)
        except ValueError:
            if is_stderr and not text.lstrip().startswith(("{", "[")):
                return f"Ошибка: {text}"
            return "[Неполная или повреждённая строка JSONL.]"
        if not isinstance(event, dict):
            return "[Некорректная строка JSONL.]"
        block = format_log_event(event)
        if not block and is_stderr:
            block = "Ошибка:\n" + json.dumps(event, ensure_ascii=False, indent=2)
        if self._model_pending and self._executable and event.get("sessionID"):
            self._model_pending = False
            block = f"Фактическая модель: {session_model(self._executable, event['sessionID'])}\n\n{block}"
        return block
