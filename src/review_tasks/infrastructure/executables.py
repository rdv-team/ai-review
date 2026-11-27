"""Resolve executable overrides, configured paths, and PATH commands."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_executable(command: str, env_var: str, *, configured: str | None = None) -> str | None:
    candidates = [os.environ.get(env_var), configured, command]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if (path.is_absolute() or path.parent != Path(".")) and path.exists():
            return str(path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None
