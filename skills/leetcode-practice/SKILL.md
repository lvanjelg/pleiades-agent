---
name: leetcode-practice
description: Give the user LeetCode-style practice problems to strengthen their problem-solving, chosen to fit their current learning goals. Use when the user asks for practice problems, algorithm/data-structure practice, "give me a problem", interview prep reps, or to keep a practice streak going. Problem selection defaults to the user's stated learning goals; an explicit user preference (a specific problem, difficulty, topic, or company) overrides that default whenever given.
---

# LeetCode Practice

The user practices coding problems the way athletes run drills: targeted reps on the patterns they're currently learning. Your job is to select, present, coach, and track those reps. The default driver of selection is the user's **current learning goals**; but per the user's standing rule, **if they ask for something specific — a particular problem, difficulty, topic, company set, or time limit — that explicit preference overrides the goal-based default.** When in doubt, confirm which one applies in a single line.

## Step 1 — Establish the learning context

Keep it light; ask only what's missing:

- **Learning goal / pattern(s) in focus** (e.g. "dynamic programming", "sliding window", "graphs", "binary search"). If they don't name one, ask — a problem not aimed at a goal is a random rep, and random reps are how practice stalls.
- **Language** (default to what they've been using).
- **Difficulty** and **time budget** for this session (one quick warmup vs a hard 45-minute problem).
- How many problems they want this session.
- Whether hints are allowed by default.

## Step 2 — Select the problem

Map the learning goal to the pattern it isolates, then pick a problem that exercises **one** target pattern — not a grab-bag that tests five things. Calibrate to their edge (teach-skill style): slightly above comfortable is the zone; if the last few went smoothly, escalate; if they keep stalling, back off and rebuild the prerequisite.

**Original problems only.** Do not reproduce LeetCode problem statements verbatim — author an original problem that exercises the same pattern (same shape, different story/numbers). This keeps practice honest (you can't accidentally have memorized it) and avoids copyright issues. You can say which real LeetCode-style pattern it maps to so they can find more reps later.

Avoid problems they've already solved unless the session is explicitly a review — check the progress log (Step 5) if one exists.

## Step 3 — Present the problem

Give a clean, Obsidian-friendly block:

- **Title**, difficulty, and the pattern(s) it trains.
- The **problem statement** with constraints.
- **2–3 worked examples** (input → output, with a one-line note where the trick is).
- The **function signature** in their language.
- A one-line "what to watch for" — optional, and only if it doesn't give the trick away.

Don't include the solution or even hints in the initial presentation. Ask them to attempt it and report back (or paste their code). Offer a time-box if they want one.

## Step 4 — Coach the solve

- **They're stuck:** don't dump the solution. Escalate hints one at a time, least-revealing first (re-read the problem → name the pattern that fits → point at the specific bottleneck). Reveal the full solution only after hints are exhausted or they explicitly ask.
- **They solved it:** don't just say "nice." Require the **time & space complexity** analysis — naming it is where the learning locks in. Then offer a variant that twists the same pattern for a second rep.
- **They got it wrong:** figure out *where* — a wrong approach vs a small bug vs a misunderstood constraint are three different lessons. Diagnose before teaching, mirroring the teach skill's probe-before-teach rule.

## Step 5 — Track progress

Keep a lightweight log at `pleiades-vault/leetcode-practice/log.md` (create it if missing). One line per problem:

`2026-09-08 | Two-pointer · container | Easy | solved clean | 20 min`

Fields: date, pattern(s), difficulty, outcome (solved clean / solved with hints / stuck / wrong), time. This log is what makes future selection intelligent — it tells you what patterns are over- and under-repped and which problems to schedule for spaced review. A problem they missed gets a **re-review in a few days** (say so and note it) rather than being forgotten. Update the log after each session; don't pad it with editorializing.

## Vault & Obsidian

Problems and solutions are Obsidian markdown. For a persistent session copy, save the problem (statement, examples, hint ladder) as a note under `pleiades-vault/leetcode-practice/` and link it from your reply (`[[<problem-slug>]]`); otherwise keep problems in chat and let only the log persist. Write examples as tables (input → output → note), use LaTeX `$…$` for complexity math when it aids clarity, and add a mermaid diagram only when the *data structure* is the obstacle (a tree, a linked-list rearrangement, a graph) — don't diagram code that prose carries. The progress log at `pleiades-vault/leetcode-practice/log.md` is itself an Obsidian note the user can open. Rendered images are rarely needed; if one genuinely helps, use the `visualize` skill (dispatch `mermaid-maker`/`svg-maker` via the `subagent` tool) and embed the returned PNG.

## Boundaries

- Don't default to their preference when none was given (goals drive selection) — and don't ignore an explicit preference when one was given (it overrides). Both directions of the rule matter.
- Don't give solutions pre-emptively; the problem's value is in the attempt.
- Don't assign problems far outside their stated level without checking — a demoralizing wall is a failed rep, not practice.
