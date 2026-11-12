"""Your weak areas.

    py diffquiz/report.py              last 7 days, this repository
    py diffquiz/report.py --days 30
    py diffquiz/report.py --all-repos

Concepts are ranked worst-first by average grade. That ranking is the point
of the whole tool: a list of the ideas you have been leaning on the assistant
for, in the order worth reading up on.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store  # noqa: E402

BAR = "─" * 72


def meter(avg: float, width: int = 10) -> str:
    """A 0-5 average as a bar, so the ranking is scannable at a glance."""
    filled = int(round((avg / 5) * width))
    return "█" * filled + "·" * (width - filled)


def main() -> int:
    ap = argparse.ArgumentParser(description="Diff Quiz report.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--all-repos", action="store_true")
    args = ap.parse_args()

    repo = None if args.all_repos else store.current_repo()
    conn = store.connect()

    act = store.activity(conn, args.days, repo)
    p, d = store.counts(conn, repo)

    scope = "all repositories" if args.all_repos else Path(repo).name if repo else "this folder"
    print(f"\n{BAR}")
    print(f"  DIFF QUIZ — last {args.days} days — {scope}")
    print(BAR)
    print(f"  diffs quizzed      {act['rounds']}")
    print(f"  questions answered {act['answered']}")
    print(f"  average grade      {act['avg_grade'] if act['avg_grade'] is not None else '—'}/5")
    print(f"  waiting            {p} pending · {d} due")

    weak = store.weak_concepts(conn, args.days, repo)
    if not weak:
        print(f"\n  No answers in this window yet.")
        if p or d:
            print(f"  Start with:  py diffquiz/quiz.py")
        print()
        return 0

    print(f"\n  WEAKEST CONCEPTS (worst first)")
    print(f"  {'concept':<30} {'avg':>5}  {'':<10} {'tried':>5} {'missed':>6}")
    for row in weak:
        avg = row["avg_grade"] or 0
        print(f"  {row['concept'][:30]:<30} {avg:>5.2f}  {meter(avg)} "
              f"{row['attempts']:>5} {row['misses']:>6}")

    worst = [r for r in weak if (r["avg_grade"] or 0) < 3.5]
    if worst:
        print(f"\n  Worth revisiting: {', '.join(r['concept'] for r in worst[:4])}")
    strong = [r for r in weak if (r["avg_grade"] or 0) >= 4.5]
    if strong:
        print(f"  Solid: {', '.join(r['concept'] for r in strong[:4])}")

    upcoming = conn.execute(
        "SELECT concept, due_at FROM questions q JOIN rounds r ON r.id = q.round_id"
        " WHERE q.due_at > ?" + (" AND r.repo = ?" if repo else "") +
        " ORDER BY q.due_at LIMIT 5",
        [store.now_iso()] + ([repo] if repo else []),
    ).fetchall()
    if upcoming:
        print(f"\n  COMING UP")
        now = datetime.now(timezone.utc)
        for row in upcoming:
            when = datetime.fromisoformat(row["due_at"])
            days = (when - now).days
            print(f"  {row['concept'][:30]:<30} in {max(0, days)} day(s)")

    print()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(main())
