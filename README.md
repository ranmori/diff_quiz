# Diff Quiz

A Claude Code hook that quizzes you on your own changes.

Code written with an assistant can work perfectly while you'd be unable to
defend or modify it alone. Diff Quiz closes that gap: every time Claude
finishes a turn, it reads the diff, asks a small model for three questions
about the *decisions* in it, and stores them. You answer later, in your own
words, and get graded 0–5. Fumbled answers come back tomorrow; solid ones come
back in a week, then a month.

```
you + Claude edit files
        │
        ▼
hook.py        Stop hook, async — git diff → claude -p → 3 questions → SQLite
        │
        ▼
status.py      SessionStart hook — "2 new questions · 1 due for review"
        │
        ▼
py diffquiz/quiz.py     you answer; claude -p grades, schedules the next review
        │
        ▼
py diffquiz/report.py   weak concepts, ranked worst-first
```

## Requirements

- Python 3.11+ (no third-party packages — stdlib and `sqlite3` only)
- The `claude` CLI on your PATH
- `git` (the quiz is driven by `git diff HEAD`)

## Setup

Clone it anywhere, then register the two hooks in your Claude Code settings
(`.claude/settings.local.json` in the project you want quizzed, or your user
settings to quiz every project). The paths must be absolute:

```json
{
  "env": { "PYTHONUTF8": "1" },
  "hooks": {
    "Stop": [
      { "hooks": [ {
          "type": "command",
          "command": "py",
          "args": ["C:/diff_quiz/diffquiz/hook.py"],
          "async": true,
          "timeout": 240,
          "statusMessage": "Diff Quiz: writing questions..."
      } ] }
    ],
    "SessionStart": [
      { "hooks": [ {
          "type": "command",
          "command": "py",
          "args": ["C:/diff_quiz/diffquiz/status.py"],
          "timeout": 20
      } ] }
    ]
  }
}
```

`async: true` on the `Stop` hook matters — question generation takes ~25 s and
would otherwise stall your prompt after every edit.

Questions are keyed by the repo you were working in, so one installation
quizzes you across all your projects.

## Commands

```sh
py diffquiz/quiz.py              # answer what's waiting
py diffquiz/quiz.py --review     # only reviews that have come due
py diffquiz/quiz.py --limit 3    # just three
py diffquiz/report.py            # weak areas, last 7 days
py diffquiz/report.py --days 30
py diffquiz/store.py             # smoke-test the DB + show the spacing curve
```

Your answers live in `diffquiz/data/diffquiz.db` and are gitignored. When
something seems broken, read `diffquiz/data/hook.log` first.

## How it actually works

See **[diffquiz/NOTES.md](diffquiz/NOTES.md)** — the platform constraints that
forced the design, the recursion trap and its two guards, the spaced-repetition
maths, the five bugs worth remembering, the tuning knobs, and the known
limitations.

One worth repeating here: **answer honestly rather than well.** A grade of 2
that exposes a real gap is worth more than a 4 you bluffed — the schedule only
helps if it reflects what you actually know.
