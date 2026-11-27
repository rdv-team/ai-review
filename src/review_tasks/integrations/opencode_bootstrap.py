"""Fixed console entrypoint; arguments are data, never Python or shell source."""
from __future__ import annotations

import sys
from pathlib import Path

# Direct execution keeps the visible Python console independent of PATH and
# the installed review-tasks console-script wrapper.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from review_tasks.integrations.opencode import prepare_discussion_argv, _interactive_run


def main() -> int:
    try:
        executable, directory, model, context, cli_version = sys.argv[1:]
        task_dir = Path(directory)
        argv = prepare_discussion_argv(executable, task_dir, model, Path(context), cli_version=cli_version)
        result = _interactive_run(argv, task_dir)
        if result == 0:
            return 0
        print(f"OpenCode exited with code {result}.", flush=True)
    except Exception as exc:
        print(f"OpenCode discussion could not start: {exc}", flush=True)
        result = 1
    try:
        input("Press Enter to close this window...")
    except EOFError:
        pass
    return result


if __name__ == "__main__":
    raise SystemExit(main())
