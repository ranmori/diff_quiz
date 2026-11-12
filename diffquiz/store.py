"""SQLite storage for Diff Quiz.

Three tables, and the shape of them is the whole design:

  rounds     one per batch of changes we quizzed on. Holds the diff itself,
             because grading your answer later needs the code in front of it.
  questions  one per question. ALSO holds the spaced-repetition state
             (due_at, interval, ease) -- see the note below on why.
  answers    one per attempt. Append-only: we never overwrite an old answer,
             so the report can show whether you are improving.

Why spaced-repetition state lives on `questions` and not on a separate
`concepts` table: the thing that gets re-asked is a specific question. A
concept like "xml-mixed-content" is a *label* we group by for the weekly
report. Keeping schedule state per question keeps scheduling simple and
still lets the report aggregate by concept.
"""
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "diffquiz.db"


def repo_key(path) -> str | None:
    """Canonical string for a repo path, used as the DB key.

    This function exists because of a real bug: the hook stored
    `str(Path(root))` -> "C:\\AIML" while the quiz looked up git's raw output
    -> "C:/AIML". Same directory, different string, so `WHERE repo = ?`
    matched nothing and every question looked like it belonged to another
    project. Whenever a value is both written and queried, normalize it in
    ONE place and call that everywhere.
    """
    if not path:
        return None
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def current_repo(cwd=None) -> str | None:
    """The git repo containing `cwd`, canonicalized, or None if not a repo."""
    try:
        out = subprocess.run(
            ["git", "--no-pager", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        )
        return repo_key(out.stdout.strip()) if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id         INTEGER PRIMARY KEY,
    repo       TEXT NOT NULL,
    branch     TEXT,
    created_at TEXT NOT NULL,
    diff_hash  TEXT NOT NULL,
    diff_stat  TEXT,
    diff_text  TEXT NOT NULL
);

-- The dedup guard. The Stop hook fires on EVERY turn end, including turns
-- that changed nothing. Rather than tracking "did files change", we hash the
-- diff and let the database refuse a repeat. Simple and stateless.
CREATE UNIQUE INDEX IF NOT EXISTS rounds_repo_hash
    ON rounds(repo, diff_hash);

CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY,
    round_id      INTEGER NOT NULL REFERENCES rounds(id),
    concept       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    question      TEXT NOT NULL,
    reference     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    -- spaced repetition (SM-2 style); due_at NULL == never answered
    due_at        TEXT,
    interval_days REAL NOT NULL DEFAULT 0,
    ease          REAL NOT NULL DEFAULT 2.5,
    reps          INTEGER NOT NULL DEFAULT 0,
    lapses        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS questions_due ON questions(due_at);

CREATE TABLE IF NOT EXISTS answers (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    answered_at TEXT NOT NULL,
    answer      TEXT NOT NULL,
    grade       INTEGER NOT NULL,
    feedback    TEXT
);
"""


def now_iso() -> str:
    """UTC, ISO 8601. Always store UTC; render local only when displaying."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout: the Stop hook may write while an interactive quiz reads.
    # Without it, the second writer fails instantly with "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- writing


def add_round(conn, repo, branch, diff_hash, diff_stat, diff_text):
    """Insert a round. Returns its id, or None if we've seen this diff before.

    The None case is normal and expected, not an error: it is how the hook
    stays quiet on turns that changed nothing new.
    """
    try:
        cur = conn.execute(
            "INSERT INTO rounds (repo, branch, created_at, diff_hash, diff_stat, diff_text)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (repo, branch, now_iso(), diff_hash, diff_stat, diff_text),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # unique index on (repo, diff_hash) refused the duplicate


def minutes_since_last_round(conn, repo) -> float:
    """Age of the newest round for this repo, in minutes (inf if none).

    Used for the cooldown. The hook diffs the whole uncommitted working
    tree, so every small edit yields a *different* hash covering mostly the
    same code -- the hash dedup catches exact repeats but not near-repeats.
    A cooldown is the cheap fix: it batches a burst of edits into one round.
    """
    row = conn.execute(
        "SELECT created_at FROM rounds WHERE repo = ? ORDER BY id DESC LIMIT 1",
        (repo,),
    ).fetchone()
    if not row:
        return float("inf")
    last = datetime.fromisoformat(row["created_at"])
    return (datetime.now(timezone.utc) - last).total_seconds() / 60


def add_questions(conn, round_id, questions) -> int:
    ts = now_iso()
    conn.executemany(
        "INSERT INTO questions (round_id, concept, kind, question, reference, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                round_id,
                q.get("concept", "general"),
                q.get("kind", "why"),
                q["question"],
                q.get("reference", ""),
                ts,
            )
            for q in questions
        ],
    )
    conn.commit()
    return len(questions)


# --------------------------------------------------- spaced repetition


def schedule_next(ease: float, reps: int, interval: float, grade: int):
    """SM-2, simplified. Returns (interval_days, ease, reps, lapsed).

    The idea: a concept you answer well gets pushed further into the future
    (you don't need to see it tomorrow); one you fumble comes back at once.
    `ease` is a per-question difficulty multiplier that drifts with your
    performance, so questions you repeatedly struggle with keep returning
    sooner even once you start passing them.

    Grade scale (what the grader is told to use):
      0-2 = fail, you did not have the concept
      3   = correct but shaky
      4   = correct
      5   = correct and you explained the tradeoff
    """
    if grade < 3:
        # Lapse: back to tomorrow, and make this question harder to retire.
        return 1.0, max(1.3, ease - 0.20), 0, True

    reps += 1
    if reps == 1:
        interval = 1.0
    elif reps == 2:
        interval = 3.0
    else:
        interval = round(interval * ease, 1)

    # Reward a confident answer, nudge down a shaky one.
    ease = min(2.8, max(1.3, ease + (0.1 - (5 - grade) * 0.08)))
    return interval, ease, reps, False


def record_answer(conn, question_id: int, answer: str, grade: int, feedback: str):
    """Save the attempt and move the question's next-review date."""
    row = conn.execute(
        "SELECT ease, reps, interval_days, lapses FROM questions WHERE id = ?",
        (question_id,),
    ).fetchone()

    interval, ease, reps, lapsed = schedule_next(
        row["ease"], row["reps"], row["interval_days"], grade
    )
    due = datetime.now(timezone.utc) + timedelta(days=interval)

    conn.execute(
        "INSERT INTO answers (question_id, answered_at, answer, grade, feedback)"
        " VALUES (?, ?, ?, ?, ?)",
        (question_id, now_iso(), answer, grade, feedback),
    )
    conn.execute(
        "UPDATE questions SET due_at = ?, interval_days = ?, ease = ?, reps = ?, lapses = ?"
        " WHERE id = ?",
        (
            due.isoformat(timespec="seconds"),
            interval,
            ease,
            reps,
            row["lapses"] + (1 if lapsed else 0),
            question_id,
        ),
    )
    conn.commit()
    return interval


# ---------------------------------------------------------------- reading


def pending(conn, repo=None, limit=20):
    """Questions never answered yet."""
    sql = (
        "SELECT q.*, r.repo, r.diff_text, r.diff_stat FROM questions q"
        " JOIN rounds r ON r.id = q.round_id"
        " WHERE q.due_at IS NULL"
    )
    args = []
    if repo:
        sql += " AND r.repo = ?"
        args.append(repo)
    sql += " ORDER BY q.id LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def due(conn, repo=None, limit=20):
    """Questions answered before whose next review has come around."""
    sql = (
        "SELECT q.*, r.repo, r.diff_text, r.diff_stat FROM questions q"
        " JOIN rounds r ON r.id = q.round_id"
        " WHERE q.due_at IS NOT NULL AND q.due_at <= ?"
    )
    args = [now_iso()]
    if repo:
        sql += " AND r.repo = ?"
        args.append(repo)
    sql += " ORDER BY q.due_at LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def counts(conn, repo=None):
    """(pending, due) -- what SessionStart announces."""
    where, args = ("AND r.repo = ?", [repo]) if repo else ("", [])
    p = conn.execute(
        f"SELECT COUNT(*) c FROM questions q JOIN rounds r ON r.id = q.round_id"
        f" WHERE q.due_at IS NULL {where}",
        args,
    ).fetchone()["c"]
    d = conn.execute(
        f"SELECT COUNT(*) c FROM questions q JOIN rounds r ON r.id = q.round_id"
        f" WHERE q.due_at IS NOT NULL AND q.due_at <= ? {where}",
        [now_iso()] + args,
    ).fetchone()["c"]
    return p, d


def weak_concepts(conn, days=7, repo=None):
    """Concepts ranked worst-first, over answers in the last `days`."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    where, args = ("AND r.repo = ?", [repo]) if repo else ("", [])
    return conn.execute(
        f"""
        SELECT q.concept,
               COUNT(a.id)        AS attempts,
               ROUND(AVG(a.grade), 2) AS avg_grade,
               SUM(CASE WHEN a.grade < 3 THEN 1 ELSE 0 END) AS misses
        FROM answers a
        JOIN questions q ON q.id = a.question_id
        JOIN rounds r    ON r.id = q.round_id
        WHERE a.answered_at >= ? {where}
        GROUP BY q.concept
        ORDER BY avg_grade ASC, misses DESC
        """,
        [since] + args,
    ).fetchall()


def activity(conn, days=7, repo=None):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    where, args = ("AND r.repo = ?", [repo]) if repo else ("", [])
    rounds = conn.execute(
        f"SELECT COUNT(*) c FROM rounds r WHERE r.created_at >= ? {where}",
        [since] + args,
    ).fetchone()["c"]
    row = conn.execute(
        f"""SELECT COUNT(a.id) n, ROUND(AVG(a.grade), 2) avg FROM answers a
            JOIN questions q ON q.id = a.question_id
            JOIN rounds r    ON r.id = q.round_id
            WHERE a.answered_at >= ? {where}""",
        [since] + args,
    ).fetchone()
    return {"rounds": rounds, "answered": row["n"], "avg_grade": row["avg"]}


if __name__ == "__main__":
    # `py diffquiz/store.py` -- smoke test the schema and the scheduler.
    conn = connect()
    print(f"db ok: {DB_PATH}")
    print("tables:", [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")])
    print("\nscheduler walk-through (always passing, grade 4):")
    ease, reps, iv = 2.5, 0, 0.0
    for _ in range(6):
        iv, ease, reps, _ = schedule_next(ease, reps, iv, 4)
        print(f"  rep {reps}: next in {iv:>6} days (ease {ease:.2f})")
    print("\nnow fail one (grade 1):")
    iv, ease, reps, lapsed = schedule_next(ease, reps, iv, 1)
    print(f"  reset: next in {iv} days (ease {ease:.2f}, reps {reps}, lapsed {lapsed})")
