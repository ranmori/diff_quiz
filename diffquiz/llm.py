"""Talk to Claude from inside the hook, via the `claude -p` CLI.

Why the CLI instead of the Anthropic API: `claude -p` reuses the login you
already have, so there is no API key to manage and no per-call billing setup.

THE RECURSION TRAP (the one thing to understand in this file):

  Our Stop hook runs when Claude finishes.
  The hook calls `claude -p`.
  That child Claude finishes too -- firing the Stop hook again.
  Which calls `claude -p`. Forever.

Two independent guards, because this failure is expensive if it escapes:

  1. `--settings {"disableAllHooks": true}` on the child. The child runs no
     hooks at all, so it cannot re-trigger us. This is the real fix.
  2. The DIFFQUIZ_RUNNING environment variable, which the child inherits and
     hook.py checks on entry. A backstop in case guard 1 ever stops working
     (a flag rename, an older CLI) -- a belt alongside the braces, since the
     cost of being wrong here is a runaway loop on the user's machine.
"""
import json
import os
import re
import shutil
import subprocess

TIMEOUT = 180

# The child must not run hooks -- see the note above.
CHILD_SETTINGS = json.dumps({"disableAllHooks": True})


class LLMError(RuntimeError):
    pass


def claude_exe() -> str:
    """Absolute path to the claude CLI.

    On Windows, npm installs it as `claude.CMD`, not `claude`. With
    shell=False (which we want -- no shell means no shell injection from
    diff contents) the bare name "claude" does not resolve and you get a
    FileNotFoundError. shutil.which applies PATHEXT and finds it.
    """
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("the `claude` CLI is not on PATH")
    return exe


def ask(prompt: str, timeout: int = TIMEOUT) -> str:
    """Send a prompt to Claude headlessly and return the text reply.

    The prompt goes in on STDIN, not as an argument. Two reasons, and the
    second is the binding one:
      - a diff is full of quotes, &, |, %, ^; as an argv element on Windows
        it passes through cmd.exe (claude is a .CMD shim) and can be mangled;
      - Windows caps a command line near 32,000 characters, and our prompts
        carry up to 60 KB of diff. Via argv it would fail on exactly the
        large changes most worth being quizzed about.
    """
    env = dict(os.environ, DIFFQUIZ_RUNNING="1")
    try:
        proc = subprocess.run(
            [claude_exe(), "-p", "--settings", CHILD_SETTINGS],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            shell=False,
        )
    except FileNotFoundError:
        raise LLMError("the `claude` CLI is not on PATH")
    except subprocess.TimeoutExpired:
        raise LLMError(f"claude did not reply within {timeout}s")

    if proc.returncode != 0:
        raise LLMError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip()


def ask_json(prompt: str, timeout: int = TIMEOUT):
    """Same, but insist on JSON back.

    Models often wrap JSON in ```json fences even when told not to, so we
    strip fences and, failing that, grab the outermost {...} or [...] block.
    Being liberal here is cheaper than a retry.
    """
    raw = ask(prompt, timeout)

    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    match = re.search(r"(\[.*\]|\{.*\})", raw, re.S)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    raise LLMError(f"could not parse JSON from reply: {raw[:300]}")


# ------------------------------------------------------------ prompts

GENERATE = """\
You are quizzing a developer on a code change they just made with an AI \
assistant's help. The risk you are guarding against: code that works but \
that they could not defend or modify alone.

Write exactly 3 questions about the diff below, one of each kind:

  "why"         -- why this approach, rather than an obvious alternative?
  "breaks"      -- what breaks if a specific condition changes? Name the
                   condition concretely (an input, a scale, an assumption).
  "alternative" -- what is a different way to do this, and what would it
                   cost or gain?

Rules:
- Ask about judgment, not recall. Never ask what a line of code says; ask
  why it says that.
- Anchor each question in something actually present in this diff. No
  generic software-engineering questions.
- A question must be answerable in 2-4 sentences by someone who genuinely
  understood the change.
- `concept` is a short kebab-case tag naming the transferable idea, e.g.
  "xml-mixed-content", "cache-invalidation", "retry-backoff". Reuse obvious
  tags across diffs so weak areas accumulate; this tag is what the spaced
  repetition groups by.
- `reference` is YOUR best answer, 2-4 sentences. It is not shown to the
  learner before they answer -- it is the grader's yardstick.

Reply with ONLY a JSON array, no prose, no code fences:

[
  {{"concept": "...", "kind": "why",         "question": "...", "reference": "..."}},
  {{"concept": "...", "kind": "breaks",      "question": "...", "reference": "..."}},
  {{"concept": "...", "kind": "alternative", "question": "...", "reference": "..."}}
]

Changed files: {stat}

DIFF:
{diff}
"""

GRADE = """\
Grade a developer's answer about a change they made. Be a fair examiner: \
reward real understanding, do not reward fluent restatement of the question.

Scale:
  5 - correct, and names the tradeoff or the reason behind the reason
  4 - correct and complete
  3 - essentially correct but vague, or missing an important qualifier
  2 - partly right, with a real misunderstanding
  1 - mostly wrong, but engaged with the question
  0 - wrong, empty, or evasive ("I don't know", "because it's better")

Judge the substance. Terse, correct answers score well; long, hedging ones
do not. If their answer is right for a reason the reference answer missed,
score it correct and say so in the feedback.

Feedback: 1-3 sentences, addressed to them as "you". If they lost points,
say specifically what was missing. If they scored 5, say what was good --
do not invent a criticism.

Reply with ONLY this JSON, no prose, no code fences:
{{"grade": <0-5>, "feedback": "..."}}

QUESTION ({kind}, concept: {concept}):
{question}

REFERENCE ANSWER (the yardstick, may be incomplete):
{reference}

THEIR ANSWER:
{answer}

THE DIFF THIS IS ABOUT:
{diff}
"""


def generate_questions(diff: str, stat: str):
    """Returns a list of question dicts. Raises LLMError on a bad reply."""
    data = ask_json(GENERATE.format(diff=diff, stat=stat))
    if not isinstance(data, list):
        raise LLMError(f"expected a JSON array, got {type(data).__name__}")

    out = []
    for item in data:
        if isinstance(item, dict) and item.get("question"):
            out.append(item)
    if not out:
        raise LLMError("reply contained no usable questions")
    return out[:3]


def grade_answer(question_row, answer: str, diff: str):
    """Returns (grade, feedback). Clamps the grade so a bad reply can't
    corrupt the schedule -- a stray 99 would push the next review out years."""
    data = ask_json(
        GRADE.format(
            kind=question_row["kind"],
            concept=question_row["concept"],
            question=question_row["question"],
            reference=question_row["reference"],
            answer=answer,
            diff=diff,
        )
    )
    try:
        grade = int(round(float(data.get("grade", 0))))
    except (TypeError, ValueError):
        grade = 0
    return max(0, min(5, grade)), str(data.get("feedback", "")).strip()
