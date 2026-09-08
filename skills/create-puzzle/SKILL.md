---
name: create-puzzle
description: Create coding puzzles for the user to practice on — chess-style debugging/completion challenges where the user is given a bugged or incomplete piece of software and must find the bug or complete the software from a spec. Use when the user asks for a coding puzzle, "something to debug", "broken code to fix", "an incomplete program to finish", "give me a challenge", or timed bug-hunt practice. Each puzzle targets one teachable misconception, ships with tests so it's gradable, and holds the solution back for after the attempt.
---

# Create-Puzzle (coding puzzles)

This skill produces **coding puzzles**: realistic pieces of software that are subtly bugged or deliberately incomplete, which the user must debug or complete from written instructions — chess-style, meaning the position is deliberately constructed so that finding the move (the bug / the missing piece) teaches one specific thing. The user solves in their editor; you author, verify, and later coach the solve.

**Correctness is non-negotiable.** A puzzle with an accidental bug of your own, or one that can't actually be solved from its spec, wastes the user's time and poisons trust. **Never hand the user a puzzle you have not run yourself.** Every puzzle gets verified end-to-end before you deliver it.

## Step 1 — Scope before you build

Ask (one compact block, only what's missing): what concept/area they're practicing (e.g. "Python async", "off-by-one loops", "SQL joins"), language, difficulty, puzzle **type** (below), time-box (5-min warmup vs 30-min deep dive), and whether they want files written to the vault or everything in chat.

### Puzzle types

- **find-the-bug** — working-looking code that is subtly wrong. The user reads it, finds the bug, and explains/predicts the failure. Best for sharpening detection of a specific recurring mistake.
- **complete-the-software** — a scaffold with a clear spec and one missing function/module. The user implements to spec. Best for building a specific skill from instructions.
- **make-it-pass** — code plus failing tests; the user fixes it until the tests go green. Best for practice with a clear grading oracle.

If they didn't name a type, pick based on the goal: detecting mistakes → find-the-bug; building a skill → complete-the-software; pure reps → make-it-pass.

## Step 2 — Author the puzzle

**One target per puzzle.** Decide the single misconception or skill this puzzle exists to teach (e.g. "mutating a list while iterating it", "off-by-one in slicing", "shadowing a variable name"). Everything else in the code is minimal scaffolding — no noise code that exists only to look busy. If you can delete a line and the lesson survives, delete it. A puzzle that accidentally tests three things tests nothing.

**Design the bug the way the teach skill designs wrong answers:** each planted bug must be a *specific, plausible mistake a real learner would actually make* — not a typo, not something absurd. The puzzle should separate people who understand the concept from people who don't. For find-the-bug, the bug should be the kind that passes a casual read and fails on one edge case or under one specific input.

**Give it a real shape:** realistic function/module names, real imports, authentic (but small) context — not toy pseudo-code. Include:

- The **instructions/spec**: what the software is supposed to do, written so the expected behavior is unambiguous.
- **2–3 examples** (input → expected output) for complete-the-software, or a description of the failure the user should hunt for.
- A **grading oracle**: for complete-the-software and make-it-pass, include runnable tests (or clear expected outputs) so "solved" is checkable, not vibes.
- A **difficulty rating** and an optional time-box.

**Hold the solution back.** Write the solution + a short explanation of the bug, why it bites, and the misconception it tests — but keep it OUT of the user's face. Options: a companion `-solution.md` saved alongside the puzzle, a spoiler block at the end, or simply held in your reply until asked. Reveal only after the user has attempted (or explicitly given up and asked).

## Step 3 — Verify before you deliver

Run the reference yourself before the user ever sees it:

- **find-the-bug / make-it-pass**: run the bugged code and confirm it actually misbehaves as intended (the bug bites), then apply the fix and confirm the corrected code passes the tests. Use `run_python`/`bash`.
- **complete-the-software**: write the reference implementation, run it against the provided examples/tests, confirm it passes. Confirm the spec is sufficient — that a competent solver could reach it without guessing.
- If verification surfaces an ambiguity, fix the spec/scaffold, don't ship it.

Only after it verifies do you deliver.

## Step 4 — Deliver

Write the puzzle files into the vault so they persist and render in Obsidian (unless the user wants it inline only): `pleiades-vault/coding-puzzle-creator/<slug>/` — the code file, an `instructions.md`, the tests, and (separately) the `solution.md`. Then give a **short chat brief**: what the file does, the type, difficulty, the task in 2–3 sentences, and the exact command to run the tests / observe the failure. Don't dump the whole scaffold into chat if it's saved to a file — point at the path.

## Step 5 — Coach the solve

When the user reports back (or asks for help):

- **If wrong or stuck:** don't reveal the answer. Work the teach-skill way: ask what they expected vs what happened, point at the specific behavior that's off, and let them find the cause. Offer hints in increasing specificity, one at a time. The hint ladder is part of the puzzle's value.
- **If they ask for the solution:** reveal it, but lead with the *misconception* (why the bug is plausible and what it teaches), not just the one-line fix.
- **After a successful solve:** a 2–3 sentence debrief naming the misconception and how it generalizes; offer one harder variant on the same concept if they want another round. Adjust difficulty on their success/failure pattern.

## Vault & Obsidian

Puzzle files persist in the vault under `pleiades-vault/coding-puzzle-creator/<slug>/` so they stay openable and render in Obsidian. Write `instructions.md` and `solution.md` as Obsidian-native markdown: the spec and examples as markdown (tables work well for input→output pairs), LaTeX `$…$` only if the logic is math-heavy, and a mermaid fenced block when a diagram genuinely clarifies the intended behavior or data flow. Keep the code itself in real code files (`.py`, `.js`, …) the user opens in their editor — don't paste whole scaffolds into notes. Link the puzzle from your reply (`[[<slug>/instructions]]` or `![[instructions.md]]`). A rendered image is rarely needed; if one truly helps, use the `visualize` skill — dispatch `mermaid-maker`/`svg-maker` via the `subagent` tool and embed the returned PNG. Never fake an image.
