"""SessionStart hook: announce waiting questions, in one line.

This exists because the Stop hook runs with `async: true` -- in the
background, so its own systemMessage may never reach the UI. Questions
generated silently that nobody is told about are questions nobody answers.
So the *next* session start reports the backlog instead.

Prints nothing when there is nothing waiting. A nag that fires every session
regardless gets tuned out, and then ignored even when it matters.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store  # noqa: E402

QUIZ = Path(__file__).resolve().parent / "quiz.py"


def quiz_hint(cwd: str) -> str:
    """The command that actually works from *this* directory.

    The hint used to be the literal "py diffquiz/quiz.py", which is only right
    when your cwd happens to be the project's parent. Move the install, or run
    a session anywhere else, and the hook confidently prints a command that
    fails. Derive it from __file__ instead: relative when that reads nicely,
    absolute when it has to.
    """
    try:
        return f"py {QUIZ.relative_to(Path(cwd).resolve()).as_posix()}"
    except (ValueError, OSError):
        return f"py {QUIZ.as_posix()}"


def main() -> int:
    payload = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        pass

    cwd = payload.get("cwd") or os.getcwd()
    repo = store.current_repo(cwd)

    pending, due = store.counts(store.connect(), repo)
    if not (pending or due):
        return 0

    bits = []
    if pending:
        bits.append(f"{pending} new question(s)")
    if due:
        bits.append(f"{due} due for review")
    print(json.dumps({
        "systemMessage": f"Diff Quiz: {' · '.join(bits)} — {quiz_hint(cwd)}",
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # never block a session from starting
