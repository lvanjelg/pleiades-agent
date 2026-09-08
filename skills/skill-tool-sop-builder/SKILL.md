---
name: skill-tool-sop-builder
description: Build, extend, or fix the user's own agent capabilities — skills, tools, and SOPs. Use when the user asks to "add a skill", "create/make me a new skill/tool/SOP", "turn this into a skill", "register this as a reusable workflow", or wants to review/improve an existing skill in this repo. Guides the draft → validate → promote pipeline, with the correct wording so the resulting artifact is something the agent actually understands and performs as intended.
---

# Skill / Tool / SOP Builder

This is the **meta-skill**: it builds new capabilities for this agent repo. When the user asks to add a capability, you run the build pipeline. There are three artifact types in this system — know which one you're building before you write anything:

- **Skill** — a *triggered procedure*, adapted per context. One file: `skills/<name>/SKILL.md`. The harness auto-loads every skill's frontmatter (`name: description`) at startup and injects it into the system prompt as the trigger index; the body is the operating manual the agent follows when the skill fires.
- **Tool** — the smallest blast radius: a JSON-schema function. Registered in `tools.json` (schema) with a matching handler wired in `main.py`. Use for a discrete, repeatable operation, not a procedure.
- **SOP** — a *fixed, ordered playbook* for a recurring task class, chaining skills (and sometimes raw tool calls) in a fixed order. Only build an SOP from **promoted**, already-validated skills — a rigid workflow built on shaky components is worse than a skill failing on its own.

Read the user's request and state which type you're building (and why) before drafting — the wrong type is the most common failure here. When in doubt: skill (procedures) > tool (operations) > SOP (fixed sequences).

## The pipeline: interview → draft → validate → promote

### Step 1 — Interview (never draft blind)

You cannot write a good skill from a one-line wish. Ask the user, in one compact block, for whatever is missing:

- **Trigger**: what will the user *say* that should fire this? Collect 2–4 realistic example phrasings. These become the raw material for the description.
- **Inputs**: what must the agent gather before acting?
- **Output**: what should the result look like — exact format if it matters (tables, file writes, a fixed structure)? This is what makes output consistent instead of vibes-based.
- **Tools**: which existing tools/subagents does it use (read_file, write_file, edit_file, run_shell, bash, run_python, websearch, fetch_url, search_files, memory_note, subagent with scout/researcher/worker/mermaid-maker/svg-maker)? Use real names only.
- **When NOT to use it**: the cases that look similar but shouldn't fire it.
- **Success**: how will the user know it worked?

### Step 2 — Draft with correct wording

Write the artifact so an agent understands and performs it as intended. The house style is set by the existing skills — read `skills/teach/SKILL.md` as the exemplar before drafting if you need a refresher. The rules that matter:

**Frontmatter** (two fields, both single-line):
- `name`: one short lowercase label (`teach`, `visualize`, `hackathon`).
- `description`: the trigger index. This is the ONLY part auto-loaded into the agent's context, so it must carry the trigger. Write it as: what the capability is for, then explicit "Use when…" trigger phrases with realistic example phrasings, then what it produces and one "do not" boundary. If the capability has distinct sub-modes, say they're chosen by what the user says (see `skills/hackathon/SKILL.md` for the model).

**Body** — the operating manual. Follow the structural template:
- A one-paragraph purpose + how the capability decides it applies.
- When to use / when NOT to use, with concrete examples.
- What to gather before acting.
- A numbered process (or an SOP-style checklist where the task is recurring, not one-shot).
- Exact output format — include a ready-to-fill template or table when output consistency matters (see the hackathon judging rubric and the kanban file format for the level of precision expected).
- Pitfalls and edge cases, stated as rules ("never X", "always Y before Z").
- Reference tools by their exact names; if a tool doesn't exist yet, say "planned" explicitly rather than inventing one.
- Format for the user's Obsidian-rendered output: markdown headings/tables, LaTeX `$…$` for math, mermaid fenced blocks for graphs, wikilink embeds for files.

**Style rules:** second person, imperative, direct ("Do X", "Never Y"). Concrete beats generic — every instruction should be executable without the agent having to guess intent. No filler.

### Step 3 — Validate

Before you hand it over, verify the artifact will actually load and behave:

- **Location & parse**: the file is at `skills/<name>/SKILL.md`, starts with `---`, and has both frontmatter fields on single lines. If unsure, check it parses the way `load_skills()` in `main.py` expects (name + description extracted).
- **Trigger test**: read the description cold and ask — would an agent with only this description know to fire this skill for each example phrasing from Step 1? If not, sharpen the description; the description is a router.
- **Behavior test**: walk one example invocation through the body mentally (or, for a skill with file output, actually run its steps once on a scratch input). Does it produce the expected output format? If a skill writes files, run the write once to confirm the paths and structure are right.
- **No dead references**: every tool, subagent, path, and file it names actually exists in this repo.

### Step 4 — Promote

- New skills are loaded at **startup** by `load_skills()` in `main.py`, so a new skill takes effect on the next run. Tell the user to restart (`python main.py`) and say which skill will appear.
- Report what you created, the exact path, and a one-line summary of the trigger. Show the frontmatter description so the user can sanity-check the trigger wording.
- If the user maintains the vault note `pleiades-vault/skills.md` as a registry, offer to update it — don't edit it unprompted.

## When you're editing an existing skill

Same discipline, narrower scope: read the current file first, keep the frontmatter `name` stable (renaming breaks the trigger), and make the smallest edit that fixes the gap — usually the description (trigger too narrow/broad) or one section of the body (missing a case, wrong output format). Re-run the trigger test after any description change.

## Vault & Obsidian

The implemented artifact always lives where the harness reads it (`skills/<name>/SKILL.md`, `tools.json`, …) — but the **design work belongs in the user's Obsidian vault**, where they can read and iterate on it. For a non-trivial build, draft the design first as a note under `pleiades-vault/skill-tool-sop-builder/<name>/` — the interview answers, the draft description, the intended trigger phrases, the before/after — then implement from it, and link that note from your reply (`[[<name>-design]]`).

Present drafts and capability structure in Obsidian-native markdown: tables for a description/trigger test matrix, mermaid fenced blocks when showing how a skill triggers or how skills/SOPs chain (Obsidian renders them), and `![[file|width]]` embeds for any rendered image. If a picture (not a diagram) is genuinely clearer, use the `visualize` skill — dispatch `mermaid-maker`/`svg-maker` via the `subagent` tool and embed the returned PNG. Never hand-draw an image.

## Pitfalls

- **Vague description** — the #1 cause of "the agent never uses my skill". If you can't imagine the user's exact phrasing firing it, the description needs the phrase.
- **Body that reads like a topic, not a procedure** — the agent needs steps and formats, not a lecture.
- **Building the wrong artifact type** — a fixed workflow is an SOP, a discrete op is a tool, a judgment procedure is a skill.
- **Over-promising tools** — only reference tools that exist; flag anything as "planned" if it doesn't.
- **One file per skill** — keep the body in the SKILL.md; don't scatter a skill across files in its folder (the loader picks up any file, which causes duplicate entries).
