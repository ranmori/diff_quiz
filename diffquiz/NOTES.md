# Diff Quiz — notes

A Claude Code hook that quizzes you on your own changes. The problem it
addresses: code written with an assistant can work perfectly while you'd be
unable to defend or modify it alone. This forces you to articulate the *why*
within minutes of the change, while it's still fresh, and brings back
whatever you fumbled.

---

## 1. How it runs

```
   you + Claude edit files
            │
            ▼
   ┌──────────────────────┐   Stop hook, async (doesn't block you)
   │ hook.py              │   git diff → claude -p → 3 questions → SQLite
   └──────────────────────┘
            │
            ▼
   ┌──────────────────────┐   SessionStart hook
   │ status.py            │   "2 new questions · 1 due for review"
   └──────────────────────┘
            │
            ▼
   py diffquiz/quiz.py        you answer in your own words
                              claude -p grades 0-5, schedules next review
            │
            ▼
   py diffquiz/report.py      weak concepts, ranked worst-first
```

### Commands

```sh
py diffquiz/quiz.py              # answer what's waiting
py diffquiz/quiz.py --review     # only reviews that have come due
py diffquiz/quiz.py --limit 3    # just three
py diffquiz/report.py            # weak areas, last 7 days
py diffquiz/report.py --days 30
py diffquiz/store.py             # smoke-test the DB + show the spacing curve
```

### Files

| File | Does |
|------|------|
| `hook.py` | Stop hook: capture diff, generate questions |
| `status.py` | SessionStart hook: announce the backlog |
| `llm.py` | `claude -p` wrapper + the two prompts (generate, grade) |
| `store.py` | SQLite schema, spaced-repetition maths, queries |
| `quiz.py` | the interactive loop you actually use |
| `report.py` | weak-concept ranking |
| `data/diffquiz.db` | your answers (gitignored) |
| `data/hook.log` | **look here first when something seems broken** |

Registered in `.claude/settings.local.json` (gitignored — the hook paths are
absolute and machine-specific). Review or disable the hooks with `/hooks`.

---

## 2. The three constraints that determined the design

Not preferences — things the platform forced.

### A hook has no terminal, so it cannot quiz you
A hook runs non-interactively: its stdin is the hook payload JSON, not a
keyboard. So the hook can only *generate and store* questions, and you answer
them later in a separate command. That split looks like a design choice and
isn't one.

### Stop fires on every turn, so the hook decides for itself if anything happened
`Stop` fires whenever Claude finishes — including turns that touched no files,
plus `/clear`, `/resume` and compaction. Rather than tracking "were files
edited", the hook hashes the diff and lets a `UNIQUE` index refuse repeats:

```sql
CREATE UNIQUE INDEX rounds_repo_hash ON rounds(repo, diff_hash);
```

No state to keep in sync, and `add_round()` returning `None` is the normal
"nothing new" path rather than an error.

### Asking an LLM is slow, so the hook must be async
Generating questions takes ~25 s on a 58 KB diff. Synchronously that would
stall your prompt after every edit, and you'd switch it off within a day.
Hence `"async": true`.

But async has a cost: the hook's own `systemMessage` runs in the background
and may never reach the UI. Questions generated silently that nobody is told
about are questions nobody answers. That's the entire reason `status.py` and
the `SessionStart` hook exist — the *next* session reports the backlog.

---

## 3. The recursion trap

The one that bites hardest:

```
Stop hook runs  →  calls `claude -p`  →  that Claude finishes
       ↑                                        │
       └──────────── fires Stop hook ───────────┘
```

An unbounded fork loop on your own machine. Two independent guards:

1. **`--settings '{"disableAllHooks":true}'` on the child.** The child runs no
   hooks at all. This is the real fix — recursion is impossible at the source.
2. **`DIFFQUIZ_RUNNING=1`** in the child's environment, checked on entry by
   `hook.py`. Only helps *after* a child is spawned, so it's a backstop.

Two guards for one problem is usually over-engineering. Justified here by
asymmetric cost: a renamed CLI flag on an older `claude` would fail silently
into a runaway loop, while the redundant check costs one dict lookup.

---

## 4. Spaced repetition, concretely

SM-2, simplified (`store.schedule_next`). Pass a question and it's pushed
further out; fumble it and it's back tomorrow.

```
grade ≥ 3 (pass):   rep 1 → 1 day,  rep 2 → 3 days,
                    thereafter  interval × ease
grade < 3 (fail):   straight back to 1 day, ease reduced,
                    rep count reset to 0
```

Run `py diffquiz/store.py` to watch the curve: **1 → 3 → 7.6 → 19.5 → 50 →
131 days** while you keep passing.

`ease` is a per-question difficulty multiplier (starts 2.5, clamped 1.3–2.8)
that drifts with your performance, so questions you *repeatedly* struggle with
keep coming back sooner even once you start passing them.

The grade scale the grader is given:

| Grade | Means |
|-------|-------|
| 5 | correct, and names the tradeoff or the reason behind the reason |
| 4 | correct and complete |
| 3 | essentially correct but vague |
| 2 | partly right, real misunderstanding |
| 1 | mostly wrong but engaged |
| 0 | wrong, empty, or evasive |

**Answer honestly rather than well.** A 2 that exposes a real gap is worth
more than a 4 you bluffed — the schedule only helps if it reflects what you
actually know.

Schedule state lives on `questions`, not on a separate `concepts` table: what
gets re-asked is a specific question. A `concept` tag like
`xml-mixed-content` is a *label* the report groups by.

---

## 5. Bugs we hit building it

Worth keeping, because four of the five were invisible until tested.

**1. `claude` isn't on PATH — except it is.**
`subprocess` with `shell=False` couldn't find it. On Windows npm installs
`claude.CMD`, and the bare name `claude` only resolves via `PATHEXT` — which
a shell applies and `CreateProcess` does not. Fix: `shutil.which("claude")`.
Note the hook *degraded correctly* here: logged it, rolled the round back,
broke nothing.

**2. The prompt had to move from argv to stdin.**
A diff is full of `&`, `|`, `%`, `^` and quotes, and `claude` is a `.CMD`
shim, so argv passes through `cmd.exe`. The binding reason though is a hard
limit: Windows caps a command line near 32,000 characters and our prompts
carry up to 60 KB of diff. Via argv it would have failed on exactly the large
changes most worth being quizzed about. Prompt now goes in on stdin.

**3. `C:\AIML` ≠ `C:/AIML`.**
The hook stored `str(Path(root))` → `C:\AIML`; the quiz looked up git's raw
output → `C:/AIML`. Same directory, different string, so `WHERE repo = ?`
matched nothing and every question looked like it belonged to another project.
Fix: `store.repo_key()` — **when a value is both written and queried,
normalize it in exactly one place and call that everywhere.**

**4. Data files aren't changes.**
The first run swallowed `Rag/data/chunks.jsonl` — 103 KB of German law — as
"a code change". Now any path under a `data/` directory, and the data
extensions, are skipped. A data file isn't a judgment call, and including it
just dilutes the diff the model has to find a question in.

**5. The tool found a real flaw in itself.**
Its own second question pointed out that the diff is taken against `HEAD`, so
before a commit *every* small edit re-hashes the whole cumulative change set
— a new hash over mostly the same code, meaning near-duplicate questions
every turn. Fixed with `COOLDOWN_MINUTES = 20`: at most one round per repo per
20 minutes, batching a burst of edits into one. Checked *before* the model
call, so a cooled-down turn is free.

---

## 6. Tuning

Top of `hook.py`:

| Knob | Default | Meaning |
|------|---------|---------|
| `COOLDOWN_MINUTES` | 20 | min gap between rounds per repo |
| `MIN_CHANGED_LINES` | 4 | below this, too small to quiz |
| `MAX_DIFF_CHARS` | 60,000 | prompt cap; huge diffs give vague questions |
| `MAX_UNTRACKED_BYTES` | 40,000 | per new file, so one giant file can't crowd the rest |
| `SKIP_DIRS` / `SKIP_SUFFIXES` | — | never quizzed on |

The question style is set by the `GENERATE` prompt in `llm.py`, and the
grading strictness by `GRADE`. Those prompts are the real product — the
Python is plumbing.

---

## 7. Known limitations

- **New files are synthesized, not real diffs.** `git diff HEAD` ignores
  untracked files, so `untracked_diff()` hand-builds the `diff --git` text.
  `git add -N` would give proper hunks, rename detection and textconv for
  free, but a hook that mutates your git index as a side effect of ending a
  turn is a nasty surprise. Since the diff only ever becomes prompt text,
  fidelity matters less than not touching the index.
- **Hunk splitting is naive.** `collect_diff` splits on `\ndiff --git `, so a
  tracked file whose own *contents* contain that string could be
  mis-attributed. It happened while building this (`hook.py` contains the
  literal). Harmless for prompt text; would matter if anything parsed it.
- **Revert hides a round.** Edit a file and revert it, and the hash matches a
  stored one, so that turn is silently skipped. Likewise `git stash` or a
  branch switch can reproduce an old hash and suppress a round you'd want.
- **No cost tracking.** Each round is one `claude -p` call (~25 s), each
  graded answer another. Worth a counter if it ever feels heavy.
- **Questions are only as good as the diff.** A 200-line refactor with no
  single decision in it produces mediocre questions. That's a reasonable
  signal in itself.

## 8. If it seems broken

1. `cat diffquiz/data/hook.log` — every failure path writes there.
2. Pipe-test the hook directly, exactly as Claude Code invokes it:
   `echo '{"cwd":"C:/AIML"}' | py diffquiz/hook.py`
3. Remember the cooldown: silence within 20 minutes of the last round is
   correct behaviour, not a failure.
4. `/hooks` shows whether the hooks are registered at all. The UI only
   surfaces hook activity when one errors or runs slowly — **silent success
   is invisible by design**, so "I saw nothing" is not evidence it didn't run.
