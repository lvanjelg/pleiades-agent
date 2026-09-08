---
name: kanban
description: Manage the user's tasks and projects on an Obsidian Kanban board (the Kanban plugin, v2.0.51, is installed in the Obsidian vault). Use when the user asks to track work, add/complete/move/remove a task, plan or break down a project, "put it on the board", "what's left / what should I do next", or wants a kanban/agile-style view of their work. Turn the request into precise reads and edits of a board file; never improvise the file format.
---

# Kanban

The user tracks work on **Obsidian Kanban boards**. The Kanban plugin (v2.0.51) is installed in the Obsidian vault at `pleiades-vault/`, and boards are ordinary markdown files the plugin renders as a board. Boards live under `pleiades-vault/kanban/`. Your job: convert what the user says into correct, minimal reads and writes of board files, and report status back clearly.

File paths in this repo are relative to the repo root, so a board is at `pleiades-vault/kanban/<board-name>.md`.

## The board file format (exact — do not improvise)

A board file has three parts the plugin recognizes:

1. **YAML frontmatter** marking it as a board (this is what makes Obsidian render it as a kanban board instead of plain markdown):
```
---
kanban-plugin: board
---
```
(`kanban-plugin: basic` is also recognized; prefer `board` to match what the plugin writes.)

2. **Lanes and cards.** Each lane is a `## heading`. Each card is a task-list item (`- [ ]`) listed directly under its lane's heading:
```
## Backlog

- [ ] Wire up the auth flow
- [ ] Landing page copy

## In Progress

- [ ] API rate limiting

## Done

- [x] Project scaffold
```

3. A **settings block** at the end that the plugin maintains — a JSON code fence wrapped in comment markers. In the file it literally appears as:

    %% kanban:settings
    ```
    {"kanban-plugin":"board"}
    ```
    %%

The plugin rewrites this block itself. You do NOT need to create it for a new board (the plugin adds it on first save), but if it already exists you MUST preserve it byte-for-byte.

Card extras the plugin understands (only use when the user asks): a `#tag` appended to the title, a date like `⏳ 2026-09-10` appended to the title. Keep cards single-line unless the user explicitly wants more; extra detail belongs in a linked note, not a card.

## Operating rules (always)

1. **Never assume a board exists.** Before any write, list the boards under `pleiades-vault/kanban/` (use `search_files` or `run_shell`/`bash` with `ls`) to find the relevant one, or ask the user which board/project they mean. If none exists and the user wants one, create it (see below) — don't silently pick a different board.
2. **Read the board before editing it.** You must see its exact current lanes and cards to make a correct edit. Never blind-write a board with `overwrite=true`.
3. **Preserve the frontmatter and the `%% kanban:settings ... %%` block exactly.** Only edit lane headings and card lines.
4. **Map intent to the smallest correct edit:**
   - **Add a task** → append one `- [ ] <title>` line under the correct lane. New work goes in the backlog-style lane (the leftmost open lane); if the user says "I'm doing X next", put it in `In Progress` (or whatever lane the user names).
   - **Start a task** → move its line from the backlog lane to the `In Progress` lane (delete the line from the source lane, add it under the target lane heading).
   - **Complete a task** → if the board has a `Done` lane, move the card there; otherwise toggle `- [ ]` → `- [x]` in place and tell the user where it is.
   - **Reopen** → reverse the above.
   - **Rename / edit details** → replace the card's title text only.
   - **Delete** → remove the exact card line. If the user means "archive" (done but keepable), move to `Done` instead of deleting.
5. **One card = one unit of work.** If the user describes something big ("set up the whole backend"), break it into 2–5 actionable cards and confirm before writing them all.
6. **After any change, report status compactly** — don't dump the file. A short per-lane summary is enough:
   - `Backlog`: 3 · `In Progress`: 1 · `Done`: 2
   List the current `In Progress` card(s) by name. If the user asked a question ("what should I do next?"), answer it: name the single highest-value card in the backlog/`In Progress` and why. End with a wikilink to the board (`[[<board-name>]]`, or embed `![[<board-name>.md]]`) so the user can jump straight to it in Obsidian — the board file IS the Obsidian note, so all board work already lives in the vault.

## Creating a new board

When the user wants a new board (or none exists for their project), create `pleiades-vault/kanban/<name>.md` with this default structure and tell them the path:

```
---
kanban-plugin: board
---

## Backlog

## In Progress

## Done
```

Use exactly the lanes the user asks for if they name them; otherwise use `Backlog`, `In Progress`, `Done`. (A `Blocked` lane is fine if the user's workflow needs it.) After creating, add the tasks they mentioned. Don't add a `%% kanban:settings` block — the plugin creates it when it first saves the board.

## When NOT to use the board

Not everything belongs on a board. A single conversational request ("can you explain X", a one-off question) is not a board task — don't create cards unprompted. Put something on the board when the user signals tracking intent ("track this", "add this", "add to the board", "this is part of the project") or when it's clearly ongoing project work. If unsure whether something should be tracked, ask in one line rather than silently boarding it.

## Agile hygiene (offer, don't lecture)

When the user is planning a project, apply lightweight kanban discipline: cards are written as actions ("Fix login redirect", not "Login problem"), each card is small enough to finish in one sitting, and the `In Progress` lane should hold at most a couple of cards at a time (finish before starting more). Flag an overloaded `In Progress` lane when you see one — it's the highest-signal agile observation you can make.
