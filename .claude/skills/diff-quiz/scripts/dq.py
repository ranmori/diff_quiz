"""Helper CLI for the diff-quiz skill: read questions, write graded answers.

Why this exists rather than letting the model write SQL each time: the schedule
is real state. A hand-rolled UPDATE that forgets to move `due_at`, or that
writes a grade without an `answers` row, corrupts the review history silently
and you'd only notice weeks later when nothing came back for review. So every
write goes through `store.record_answer()`, the same function the interactive
`quiz.py` uses -- one code path, one set of invariants.

Commands:
  list   --repo <path>            questions waiting, as JSON
  show   <id>                     one question incl. reference answer + diff
  record <id> --grade N --feedback-file F --answer-file A
  report [--days N]               delegate to report.py

Answers and feedback come in via *files*, never argv. Prose contains quotes,
newlines, `&` and `%`, and on Windows argv passes through cmd.exe -- the same
bug that forced the prompt onto stdin in hook.py (NOTES.md section 5.2).
"""
import argparse
import json
import os
import sys
from pathlib import Path


def find_project() -> Path:
    """Locate the diffquiz package.

    Checked in order so an unusual install can be fixed without editing code:
    DIFFQUIZ_HOME, then the conventional location, then a repo-relative guess
    for the case where this skill lives inside the project itself.
    """
    candidates = []
    if os.environ.get("DIFFQUIZ_HOME"):
        candidates.append(Path(os.environ["DIFFQUIZ_HOME"]))
    candidates.append(Path("C:/diff_quiz"))
    # .claude/skills/diff-quiz/scripts/dq.py -> up 4 is the project root
    candidates.append(Path(__file__).resolve().parents[4])

    for base in candidates:
        if (base / "diffquiz" / "store.py").exists():
            return base
    raise SystemExit(
        "cannot find the diffquiz project. Set DIFFQUIZ_HOME to the folder "
        "that contains diffquiz/store.py, e.g. DIFFQUIZ_HOME=C:/diff_quiz"
    )


PROJECT = find_project()
sys.path.insert(0, str(PROJECT / "diffquiz"))

import store  # noqa: E402


def row_brief(row, state: str) -> dict:
    """The fields needed to *ask* a question -- deliberately not `reference`.

    The reference answer is withheld here so that listing the backlog cannot
    put the answer in front of whoever is about to be asked. `show` returns it,
    and is meant to be called only after an answer has been given.
    """
    return {
        "id": row["id"],
        "state": state,
        "concept": row["concept"],
        "kind": row["kind"],
        "question": row["question"],
        "reps": row["reps"],
        "lapses": row["lapses"],
        "due_at": row["due_at"],
        "diff_stat": row["diff_stat"],
    }


def cmd_list(args, conn) -> int:
    repo = store.repo_key(args.repo) if args.repo else store.current_repo()
    items = [row_brief(r, "new") for r in store.pending(conn, repo, args.limit)]
    items += [row_brief(r, "due") for r in store.due(conn, repo, args.limit)]
    pending, due = store.counts(conn, repo)
    print(json.dumps({
        "repo": repo,
        "pending": pending,
        "due": due,
        "questions": items[: args.limit],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_show(args, conn) -> int:
    row = conn.execute(
        "SELECT q.*, r.repo, r.diff_text, r.diff_stat FROM questions q"
        " JOIN rounds r ON r.id = q.round_id WHERE q.id = ?",
        (args.id,),
    ).fetchone()
    if row is None:
        print(json.dumps({"error": f"no question with id {args.id}"}))
        return 1

    diff = row["diff_text"] or ""
    if len(diff) > args.max_diff:
        diff = diff[: args.max_diff] + f"\n... [truncated at {args.max_diff} chars]"
    print(json.dumps({
        "id": row["id"],
        "concept": row["concept"],
        "kind": row["kind"],
        "question": row["question"],
        "reference": row["reference"],
        "reps": row["reps"],
        "ease": row["ease"],
        "diff_stat": row["diff_stat"],
        "diff": diff,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_record(args, conn) -> int:
    if not 0 <= args.grade <= 5:
        print(json.dumps({"error": "grade must be 0-5"}))
        return 1

    # utf-8-sig, not utf-8: PowerShell's `Set-Content -Encoding utf8` writes a
    # BOM, and a leading ﻿ is not whitespace, so `.strip()` leaves it. A
    # "blank" file then reads as one-character-long, sails past the emptiness
    # check below, and a BOM gets recorded as the user's answer -- which is
    # exactly what happened the first time this was tested. Strip it explicitly
    # too, in case a BOM arrives mid-file from a concatenated write.
    answer = Path(args.answer_file).read_text(encoding="utf-8-sig").strip().lstrip("﻿").strip()
    feedback = Path(args.feedback_file).read_text(encoding="utf-8-sig").strip().lstrip("﻿").strip()
    if not answer:
        print(json.dumps({"error": "refusing to record an empty answer"}))
        return 1

    before = conn.execute(
        "SELECT reps, ease, interval_days FROM questions WHERE id = ?",
        (args.id,),
    ).fetchone()
    if before is None:
        print(json.dumps({"error": f"no question with id {args.id}"}))
        return 1

    store.record_answer(conn, args.id, answer, args.grade, feedback)
    conn.commit()

    after = conn.execute(
        "SELECT reps, ease, interval_days, due_at, lapses FROM questions"
        " WHERE id = ?", (args.id,),
    ).fetchone()
    print(json.dumps({
        "recorded": args.id,
        "grade": args.grade,
        "reps": f"{before['reps']} -> {after['reps']}",
        "ease": f"{before['ease']:.2f} -> {after['ease']:.2f}",
        "next_review_days": round(after["interval_days"], 1),
        "due_at": after["due_at"],
        "lapses": after["lapses"],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_report(args, conn) -> int:
    import subprocess
    return subprocess.call(
        [sys.executable, str(PROJECT / "diffquiz" / "report.py"),
         "--days", str(args.days)],
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )


def main() -> int:
    ap = argparse.ArgumentParser(prog="dq")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="questions waiting, as JSON")
    p.add_argument("--repo", default=None)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="one question, with reference answer")
    p.add_argument("id", type=int)
    p.add_argument("--max-diff", type=int, default=6000)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("record", help="save a graded answer")
    p.add_argument("id", type=int)
    p.add_argument("--grade", type=int, required=True)
    p.add_argument("--answer-file", required=True)
    p.add_argument("--feedback-file", required=True)
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("report", help="weak concepts")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    return args.fn(args, store.connect())


if __name__ == "__main__":
    sys.exit(main())
