---
name: diff-quiz
description: Run the Diff Quiz review loop in conversation — ask the user the questions the Stop hook generated about their own recent diffs, grade their answers 0-5 against the stored rubric, and write the spaced-repetition schedule back to SQLite. Use this whenever the user wants to answer, review, or go through their diff quiz; says "quiz me", "quiz me on my changes", "let's do my questions", "what am I weak on"; asks about pending/due questions or their backlog; mentions diffquiz, diff quiz, or the quiz hook; or responds to the SessionStart notice that says "N new question(s) · N due for review". Also use it when they ask what their weak concepts are or want their review stats. Prefer this over telling them to run `py diffquiz/quiz.py` themselves — the point of this skill is that they no longer need a separate terminal.
---

# Diff Quiz — in-conversation review

The Diff Quiz hook watches the user's repos: when a turn ends, it diffs the
working tree, asks a model for three questions about the *decisions* in that
diff, and stores them in SQLite. Answering happens later, separately.

That split exists because a hook has no terminal — its stdin is the hook
payload JSON, not a keyboard — so `quiz.py` was written as a standalone
interactive session. **In a conversation that constraint is gone.** You can ask
a question, read the reply as an ordinary message, grade it, and persist the
schedule. That is this skill's entire reason to exist: same database, same
rubric, no second terminal.

## The one rule that matters

**Never answer a question for the user, and never hint before they answer.**

The tool exists because AI-assisted code can work while the author's
understanding of it quietly lags. A quiz where the assistant supplies the
answer measures nothing and silently corrupts the schedule — a question marked
passed recedes to 50+ days, so a gap gets buried rather than found.

So when they say "I don't know", that is a real and useful answer: record it
honestly (grade 0-1) and *then* explain the concept properly. Teaching after
grading is the whole point. Teaching before it is cheating, however helpfully
it is meant.

If they ask you to answer for them, say plainly that a borrowed answer would
schedule the question away for weeks, offer to record it as "don't know" and
teach it instead, or to skip it so it stays in the queue.

## Setup

All commands go through `scripts/dq.py`. It locates the project via
`DIFFQUIZ_HOME`, falling back to `C:/diff_quiz`. Run with `py -X utf8` so
`§`, `·` and box-drawing characters survive the Windows console.

```sh
py -X utf8 <skill>/scripts/dq.py list --repo <the repo they're working in>
```

## Workflow

### 1. See what is waiting

```sh
py -X utf8 <skill>/scripts/dq.py list --limit 20
```

Returns JSON: `pending` (never answered), `due` (answered before, review has
come around), and the questions themselves. Questions are keyed per repo, so
pass `--repo` when the session's cwd is not the repo being quizzed.

Note `list` deliberately does **not** return the reference answer. Nothing can
leak an answer into the conversation before it is asked.

If nothing is waiting, say so and stop — don't invent questions. New ones
arrive when the hook next sees a real diff.

### 2. Decide how many

Ask the user, or infer: "quiz me" with no number suggests 3, which is enough
to be useful and short enough to finish. Take `due` reviews before `new`
questions when both exist — a review that has come due is the one with a
schedule riding on it.

State up front how many you'll ask, so they know the shape of it.

### 3. Ask one question, then stop

Ask **one** question per message and wait for the reply. A batch of three
invites three shallow answers, and you cannot grade answer one without seeing
it before question two is asked.

Present it plainly:

```
**1 of 3** · `concept-tag` · (why)

<the question text, verbatim>
```

Keep the question's own wording. It was written against the actual diff, and
paraphrasing drifts away from what the reference answer expects. The `kind`
tag (`why`, `breaks`, `alternative`) tells them what shape of answer is wanted
and is worth showing.

Then say nothing more. No preamble about what the question is driving at, no
"hint: think about...", no restating the diff.

### 4. Grade what they wrote

Once they answer, fetch the yardstick:

```sh
py -X utf8 <skill>/scripts/dq.py show <id>
```

This returns `reference` (the expected answer, which may itself be incomplete)
and `diff` (the change it came from). Grade with **the rubric below** — it is
copied from `GRADE` in `diffquiz/llm.py` on purpose. The CLI grader and this
skill must use one scale, or the user's history becomes two incompatible
records and the weak-concept report turns into noise.

```
5 - correct, and names the tradeoff or the reason behind the reason
4 - correct and complete
3 - essentially correct but vague, or missing an important qualifier
2 - partly right, with a real misunderstanding
1 - mostly wrong, but engaged with the question
0 - wrong, empty, or evasive ("I don't know", "because it's better")
```

Judge the substance. Terse correct answers score well; long hedging ones do
not. If they are right for a reason the reference missed, score it correct and
say so.

**Grade the words they wrote, not the meaning you can reconstruct.** This is
the failure mode specific to doing it in conversation: you may have *written*
the code being asked about, so you can fill in a vague answer from your own
memory of the diff and read competence into "because of the hash thing". The
CLI grader sees only their text. Hold to that — a 3 that reflects a vague
answer is worth more to them than a generous 4.

Lenient grading is not kindness here. The schedule is the product: an inflated
grade pushes a real gap out to 50 days.

### 5. Record it

Write the answer and your feedback to files first — prose contains quotes and
newlines, and on Windows argv passes through `cmd.exe`:

```sh
py -X utf8 <skill>/scripts/dq.py record <id> --grade 3 \
    --answer-file <tmp>/answer.txt --feedback-file <tmp>/feedback.txt
```

Feedback is 1-3 sentences addressed to them as "you". If they lost points, say
specifically what was missing. If they scored 5, say what was good — do not
invent a criticism to look rigorous.

`record` returns the schedule movement. Show it, because it is the feedback
loop made visible:

```
**3/5** — you named the dedup but not why a UNIQUE index beats tracking state.

reps 0 → 1 · ease 2.50 → 2.36 · next review in 1 day
```

Then, if they scored 3 or below, teach the concept — now that grading is done,
properly and concretely. Use the `reference` and the real `diff`.

### 6. Next question, or close out

Move to the next question with no commentary in between beyond the grade
block. At the end, summarise: grades given, where the weak spots are, what
remains in the backlog.

## Other requests this skill covers

**"What am I weak on?"** / stats:

```sh
py -X utf8 <skill>/scripts/dq.py report --days 7
```

Prints the dashboard: diffs quizzed, average grade, and weak concepts ranked
worst-first. Read it out and point at the worst concept rather than just
dumping the table.

**"Why do I keep getting asked this?"** Because they failed it: a grade below
3 resets `reps` to 0, drops `ease`, and sets the next review to 1 day. `ease`
is a per-question difficulty multiplier (0.1.3-2.8) that drifts with
performance, so a question they *repeatedly* struggle with keeps returning
sooner even once they start passing it.

**"Generate questions now."** You can't usefully — that is the `Stop` hook's
job and it runs on its own, async, with a 20-minute per-repo cooldown so a
burst of edits becomes one round. If they want to check it is alive, the log
is the receipt: `diffquiz/data/hook.log`. Silence within 20 minutes of the
last round is correct behaviour, not a failure.

## When something looks broken

1. `diffquiz/data/hook.log` — every failure path writes there.
2. `list` returning nothing while the SessionStart notice claims a backlog
   almost always means a repo-key mismatch: pass `--repo` explicitly with the
   path the questions were stored under.
3. If `dq.py` cannot find the project, set `DIFFQUIZ_HOME` to the folder
   containing `diffquiz/store.py`.
