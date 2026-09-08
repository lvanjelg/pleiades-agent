---
name: visualize
description: Add a correct, minimal visual to a lesson — a diagram or geometric picture — that renders inline in the Obsidian log. Use when an idea is genuinely clearer as a picture: a dependency graph, system/flow, sequence, state machine, tree, comparison, or a spatial/geometric thing (coordinate geometry, number line, vectors, a plot, a physical layout). Outsources authoring+rendering to a maker subagent that renders the image and iterates until it is correct, then you embed the returned file.
---

# Visualize

A picture earns its place only when it shows something words can't — shape, structure, direction, relationship, geometry. This skill produces ONE such picture, gets it rendered and checked before returning, and drops it into the lesson so it renders inline in the Obsidian log.

You are the **creative director**. You decide the exact idea and distill it to its fewest carrying elements. A **maker subagent** does the authoring, rendering, visual verification, and saving, then returns a filename. You embed that filename in your reply.

## When to visualize (and when not to)

This teaching system builds a **dependency graph in the learner's head** — axioms at the root, derived facts hanging off them. A visual is powerful exactly when it makes that structure (or a geometry) visible. Reach for one when:

- The idea is a **structure or relationship**: dependencies, a system with parts and arrows, a flow/pipeline, a sequence of exchanges, a state machine, a tree/hierarchy, a comparison, a containment (what's inside vs outside).
- The idea is **spatial or geometric**: coordinate geometry, a number line, vectors, a function's shape, a physical arrangement.

Do NOT visualize when prose or a single equation already carries it. A decorative diagram that just restates the sentence next to it adds noise and a chance to be wrong. When in doubt, don't — a missing visual is cheaper than a false one.

## Choose the maker

Two makers, defined as subagents in this repo's `agents/` folder:

- **`mermaid-maker`** — structural/relational visuals: dependency graphs, flowcharts, sequence/state/ER/class diagrams, trees, mindmaps, timelines. This is the default and fits the dependency-graph pedagogy directly.
- **`svg-maker`** — spatial/geometric visuals Mermaid can't lay out: exact coordinates, geometry figures, number lines, vectors, plots, custom shapes.

Rule of thumb: if it's *nodes-and-edges / relationships*, use mermaid-maker. If it's *positions-and-shapes / geometry*, use svg-maker.

## Brief the maker well: one idea, fewest elements

The most common failure is **cramming** — every extra label makes the picture harder to read AND harder to lay out correctly. Before briefing, prune to the fewest elements that carry the idea, and for each ask: *"if I delete this, is the idea still clear?"* If yes, delete it.

Give the maker the concept AND the concrete elements you want — not a vague topic, and not a long checklist.

- BAD: "make a diagram about how TCP works"
- GOOD: "graph TD: a node 'packet' at the top; arrows down to 'ordering' and 'retransmit on loss'; both arrows down into 'reliable stream'. No title. Show that reliability is built FROM packets, not alongside them."

Keep the idea intact but trust the maker to compose; if your brief lists more than ~5–7 elements, cut it first.

## Invoke

Dispatch the maker with the `subagent` tool:

```
subagent(agent="mermaid-maker", task="<your minimal, concrete brief>")
```
```
subagent(agent="svg-maker", task="<your minimal, concrete brief>")
```

The maker authors its source with this repo's file tools (`write_file`/`edit_file`) and renders it to a PNG via `run_shell`, using a locally installed renderer (Mermaid CLI for mermaid-maker; `rsvg-convert`/ImageMagick for svg-maker). It iterates until the render is clean, saves the PNG into the project's `viz/` folder with a unique filename, and returns:

```
RESULT:
filename: viz-<slug>-<timestamp>.png
path: viz/viz-<slug>-<timestamp>.png
```

> **Harness note:** this agent has no image-viewer tool, so the maker cannot literally **look at** its own PNG — the render-and-verify loop is the design goal, but the maker verifies by clean render + source re-reading. Final visual confirmation may fall to you (open the returned file once).

If it returns `RESULT: NONE`, it couldn't make a correct picture of the brief — simplify or rethink, or decide the visual isn't worth it. Never hand-author or fake a diagram yourself; correctness depends on the maker's render-and-inspect loop.

## Embed it in the lesson

Put the embed directly in your teaching reply, using Obsidian's wikilink embed with the returned **filename** (not the full path) and a display width:

```
![[viz-<slug>-<timestamp>.png|500]]
```

That's all. Obsidian resolves the embed by filename anywhere in the vault (the maker saves into the project's `viz` folder, which is inside the vault) — so it renders inline in the lesson automatically. Width `|500` is a good default; use larger for dense diagrams. Introduce the visual in a sentence, then let it carry the idea — don't narrate every element back in prose.

## Why this is reliable

- The maker only publishes a PNG after a clean render (exit 0, file produced), so "didn't render" or "syntax error" is caught before it reaches the learner — though without an image viewer the maker cannot catch a false-but-valid picture itself, so spot-check the returned file when the visual's correctness matters.
- PNG embed means **what the learner sees is pixel-identical to the file the maker produced** — no re-render drift.
- Unique filenames keep Obsidian's by-filename embed resolution unambiguous.

> The makers render by running locally installed tools via `run_shell` (Mermaid CLI + installed Chrome for mermaid-maker; `rsvg-convert`, fallback ImageMagick for svg-maker). You don't render anything yourself — you only brief the maker and embed the filename it returns.