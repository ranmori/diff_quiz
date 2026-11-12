"""Answer your pending Diff Quiz questions.

    py diffquiz/quiz.py              pending questions first, then anything due
    py diffquiz/quiz.py --review     only reviews that have come due
    py diffquiz/quiz.py --new        only questions never answered
    py diffquiz/quiz.py --limit 3    stop after 3
    py diffquiz/quiz.py --all-repos  not just this repository

You type the answer in your own words; Claude grades it 0-5 against the diff
and its own reference answer, then schedules when you see it again.

Answer honestly rather than well. A 2 that reveals a real gap is worth more
than a 4 you bluffed -- the schedule is only useful if it reflects what you
actually know. Type `skip` to pass without recording, or `quit` to stop.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm  # noqa: E402
import store  # noqa: E402

KIND_LABEL = {
    "why": "WHY THIS APPROACH",
    "breaks": "WHAT BREAKS",
    "alternative": "THE ALTERNATIVE",
}
BAR = "─" * 72


def read_answer() -> str:
    """Multi-line input, terminated by a blank line.

    Multi-line on purpose: these questions want 2-4 sentences, and a
    single-line prompt quietly pressures you into a one-liner.
    """
    print("  Your answer (blank line to submit, 'skip', or 'quit'):")
    lines = []
    while True:
        try:
            line = input("  > " if not lines else "    ")
        except EOFError:
            break
        if not line.strip():
            if lines:
                break
            continue  # ignore a leading blank line
        if not lines and line.strip().lower() in {"skip", "quit", "q"}:
            return line.strip().lower()
        lines.append(line)
    return "\n".join(lines).strip()


def show(row, index, total):
    print(f"\n{BAR}")
    print(f"  Question {index}/{total}   [{KIND_LABEL.get(row['kind'], row['kind'].upper())}]")
    print(f"  concept: {row['concept']}   ·   changes: {row['diff_stat'] or 'n/a'}")
    if row["reps"]:
        print(f"  review #{row['reps'] + 1} of this question"
              + (f"  ({row['lapses']} earlier miss(es))" if row["lapses"] else ""))
    print(BAR)
    print(f"\n  {row['question']}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Answer Diff Quiz questions.")
    ap.add_argument("--review", action="store_true", help="only questions that are due")
    ap.add_argument("--new", action="store_true", help="only never-answered questions")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--all-repos", action="store_true")
    args = ap.parse_args()

    repo = None if args.all_repos else store.current_repo()
    conn = store.connect()

    rows = []
    if not args.review:
        rows += list(store.pending(conn, repo, args.limit))
    if not args.new:
        rows += list(store.due(conn, repo, max(0, args.limit - len(rows))))
    rows = rows[: args.limit]

    if not rows:
        p, d = store.counts(conn, repo)
        print("Nothing to answer right now.")
        print(f"  {p} pending · {d} due for review")
        if p or d:
            if args.review or args.new:
                print("  (filtered out by --review/--new; try without flags)")
            else:
                print(f"  (held back by --limit {args.limit})")
        return 0

    print(f"\n{len(rows)} question(s). Answer in your own words -- 2-4 sentences is plenty.")

    done = graded_sum = 0
    for i, row in enumerate(rows, 1):
        show(row, i, len(rows))
        answer = read_answer()

        if answer in {"quit", "q"}:
            print("\n  Stopping. Nothing lost -- unanswered questions stay pending.")
            break
        if answer == "skip" or not answer:
            print("  skipped\n")
            continue

        print("\n  grading...", flush=True)
        try:
            grade, feedback = llm.grade_answer(row, answer, row["diff_text"])
        except llm.LLMError as exc:
            print(f"  Could not grade: {exc}")
            print("  Your answer was NOT recorded, so the question stays pending.")
            continue

        interval = store.record_answer(conn, row["id"], answer, grade, feedback)
        done += 1
        graded_sum += grade

        verdict = "correct" if grade >= 4 else "shaky" if grade == 3 else "missed"
        print(f"\n  grade {grade}/5 — {verdict}")
        print(f"  {feedback}")
        if grade < 3:
            print(f"\n  REFERENCE ANSWER:\n  {row['reference']}")
        print(f"\n  next review in {interval:g} day(s)")

    p, d = store.counts(conn, repo)
    print(f"\n{BAR}")
    if done:
        print(f"  answered {done}  ·  average {graded_sum / done:.1f}/5")
    print(f"  {p} pending · {d} due for review")
    print(f"  weak areas:  py diffquiz/report.py")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")  # German/umlauts, box chars
        except (AttributeError, OSError):
            pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
