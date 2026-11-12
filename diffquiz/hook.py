"""Stop hook: capture the diff, ask Claude for 3 questions, store them.

Registered as a Claude Code `Stop` hook, so it runs when Claude finishes a
turn. Four things about that environment drive the code below:

1. It runs on EVERY turn end, including turns that touched no files, plus
   /clear, /resume and compaction. So the hook decides for itself whether
   anything happened: it hashes the diff and lets the unique index refuse
   repeats. No state to keep in sync.

2. There is no terminal. Nothing here can prompt you -- stdin is the hook
   payload, not a keyboard. Hence the split: this stores questions, and
   quiz.py (which you run yourself) asks them.

3. It must never break the session. Every failure path exits 0 and writes to
   data/hook.log. A quiz tool that blocks your work gets switched off, and a
   learning tool only helps if it survives contact with a bad day.

4. It must not recurse -- see the long note in llm.py.
"""
import hashlib
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm  # noqa: E402
import store  # noqa: E402

LOG = Path(__file__).resolve().parent / "data" / "hook.log"

# Tuning. These are the knobs worth touching.
MIN_CHANGED_LINES = 4      # below this, the change is too small to be worth a question
COOLDOWN_MINUTES = 20      # batch a burst of edits into one round (see store.py)
MAX_DIFF_CHARS = 60_000    # cap the prompt; huge diffs produce vague questions
MAX_UNTRACKED_BYTES = 40_000   # one huge new file would crowd out the rest

# Never quiz on these: generated, vendored, data, or our own bookkeeping.
SKIP_PREFIXES = ("diffquiz/data/",)
SKIP_SUFFIXES = (
    ".lock", ".db", ".sqlite3", ".log", ".min.js", ".map",
    ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".xml",
    ".jsonl", ".csv", ".tsv", ".parquet",
)
# Directories whose contents are inputs/outputs rather than decisions. A
# 100 KB data file is not a change you made a judgment call about, and
# including it only dilutes the diff the model has to find a question in.
SKIP_DIRS = {"data", "node_modules", ".venv", "venv", "__pycache__",
             "dist", "build", ".git", "target"}


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp}  {msg}\n")


def git(args, cwd):
    """Run git and return stdout, or '' on any failure.

    --no-pager matters: without it git may try to page output and hang,
    and a hung hook is worse than a broken one.
    """
    try:
        proc = subprocess.run(
            ["git", "--no-pager", *args],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def skip(path: str) -> bool:
    p = path.replace("\\", "/")
    if p.startswith(SKIP_PREFIXES) or p.lower().endswith(SKIP_SUFFIXES):
        return True
    return any(seg in SKIP_DIRS for seg in p.split("/")[:-1])


def untracked_diff(repo: Path):
    """Synthesize a diff for brand-new files.

    `git diff HEAD` does not show untracked files at all -- a whole new file
    would be invisible to the quiz, which is exactly when questions matter
    most. We could `git add -N` them, but a hook that mutates your index is
    a nasty surprise, so we build the diff text ourselves instead.
    """
    listing = git(["ls-files", "--others", "--exclude-standard"], repo)
    chunks = []
    for rel in listing.splitlines():
        rel = rel.strip()
        if not rel or skip(rel):
            continue
        f = repo / rel
        try:
            if not f.is_file() or f.stat().st_size > MAX_UNTRACKED_BYTES:
                continue
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable: nothing useful to quiz on
        lines = text.splitlines()
        body = "\n".join("+" + ln for ln in lines)
        chunks.append(
            f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n"
            f"--- /dev/null\n+++ b/{rel}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
        )
    return "\n".join(chunks)


def collect_diff(repo: Path):
    """Everything changed since the last commit. Returns (diff, stat)."""
    tracked = git(["diff", "HEAD"], repo)
    if not tracked:
        # No commits yet? Then HEAD does not resolve and everything is new.
        tracked = git(["diff"], repo)

    # Drop hunks for files we never quiz on.
    kept = []
    for block in tracked.split("\ndiff --git "):
        if not block.strip():
            continue
        block = block if block.startswith("diff --git ") else "diff --git " + block
        first = block.splitlines()[0]
        path = first.split(" b/")[-1].strip() if " b/" in first else ""
        if not skip(path):
            kept.append(block)
    diff = "\n".join(kept)

    new_files = untracked_diff(repo)
    if new_files:
        diff = (diff + "\n" + new_files) if diff else new_files

    stat = git(["diff", "--stat", "HEAD"], repo).strip().splitlines()
    stat_line = stat[-1].strip() if stat else ""
    n_new = new_files.count("new file mode")
    if n_new:
        stat_line = (stat_line + f", {n_new} new file(s)").lstrip(", ")

    return diff, stat_line


def changed_line_count(diff: str) -> int:
    return sum(
        1 for ln in diff.splitlines()
        if (ln.startswith("+") or ln.startswith("-"))
        and not ln.startswith(("+++", "---"))
    )


def main() -> int:
    # Guard 1 of 2 against recursion (llm.py holds the explanation).
    if os.environ.get("DIFFQUIZ_RUNNING"):
        return 0

    payload = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            payload = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        pass  # the payload is a nicety; cwd below is the only thing we want

    cwd = payload.get("cwd") or os.getcwd()
    root = git(["rev-parse", "--show-toplevel"], cwd).strip()
    if not root:
        return 0  # not a git repo: nothing to diff, nothing to do
    repo = Path(root)
    # One canonical form for the DB key -- store.repo_key explains why.
    repo_id = store.repo_key(root)

    diff, stat = collect_diff(repo)
    n = changed_line_count(diff)
    if n < MIN_CHANGED_LINES:
        return 0
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n[... diff truncated ...]"

    # Dedup: the same working tree must not be quizzed twice. Hash before
    # calling the model, so a no-op turn costs nothing.
    diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], repo).strip()

    conn = store.connect()

    # Cooldown. Without it, every small edit to an uncommitted change set
    # produces a new hash over mostly the same code, and you get three
    # near-duplicate questions per turn. Checked before the model call so a
    # cooled-down turn is free.
    age = store.minutes_since_last_round(conn, repo_id)
    if age < COOLDOWN_MINUTES:
        return 0

    round_id = store.add_round(conn, repo_id, branch, diff_hash, stat, diff)
    if round_id is None:
        return 0  # already quizzed this exact state

    try:
        questions = llm.generate_questions(diff, stat or f"{n} changed lines")
    except llm.LLMError as exc:
        # Roll the empty round back, or the hash blocks a retry forever.
        conn.execute("DELETE FROM rounds WHERE id = ?", (round_id,))
        conn.commit()
        log(f"generate failed ({stat}): {exc}")
        return 0

    store.add_questions(conn, round_id, questions)
    p, d = store.counts(conn, repo_id)
    log(f"+{len(questions)} questions from [{stat}] -> {p} pending, {d} due")

    # Shown in the UI when the hook runs synchronously. Harmless when async.
    print(json.dumps({
        "systemMessage": f"Diff Quiz: {len(questions)} new questions "
                         f"({p} pending). Run: py diffquiz/quiz.py",
        "suppressOutput": True,
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Last resort: a crashing hook must still not break the session.
        log("CRASH\n" + traceback.format_exc())
        sys.exit(0)
