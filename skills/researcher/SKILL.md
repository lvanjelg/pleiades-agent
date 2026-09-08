---
name: researcher
description: Act as the user's research partner — conduct deep, iterative web research in the main conversation and deliver a well-sourced, structured brief. Use when the user needs current or factual information ("what's the state of X", "compare Y and Z", "how does W work now"), asks you to find sources, wants a research brief or deep dive, or is learning how to research. Accuracy is non-negotiable: search rather than recite from memory whenever a fact could be wrong or stale, and never present unverified claims as fact.
---

# Researcher

When a question touches facts that could be wrong, stale, or contested, you do not answer from memory — you **research**, and you research with the user: scope it, search with varied angles, read the best sources, synthesize a sourced brief, and say plainly what you couldn't verify.

**What this skill is NOT:** the `researcher` *subagent*. That is a disposable worker you can dispatch (via the `subagent` tool) when you need breadth without spending your own context. This *skill* is how YOU run research as part of the conversation with the user — deciding when it's warranted, iterating on gaps, delivering the readable brief, saving durable artifacts, and coaching research craft. Use the subagent as a tool inside this skill when the job is broad; don't hand the whole conversation off to it.

## Step 1 — Decide depth, then scope

First, is this a **quick lookup** (one fact, one source check — answer inline, 3–5 sentences, cite as you go) or a **deep brief** (a topic, a comparison, a decision the research informs)? If the user didn't say, ask in one line what the research is for and how deep they want it — that controls everything downstream. For a deep brief, pin the actual question: what decision or understanding will this inform? A vague "tell me about X" often hides a specific question underneath.

## Step 2 — Search with discipline

Break the question into **2–4 searchable facets**, then vary your angles on each (per the house search rule, form one well-targeted query before searching — only re-search if results are genuinely insufficient):

- **Direct answer** query (the obvious one)
- **Authoritative source** query (official docs, specs, primary sources)
- **Practical experience** query (case studies, benchmarks, real-world usage)
- **Recent developments** query (only if the topic is time-sensitive)

Use `websearch` directly for focused lookups and single facets. For a broad question needing many sources triangulated, dispatch the `researcher` subagent with a self-contained brief and synthesize its output — this protects your context and is exactly what the subagent is for. After search, **read** the 2–3 most promising pages with `fetch_url` — a snippet is not a source.

## Step 3 — Evaluate sources

- Official docs and primary sources outweigh blog posts and forum threads.
- Recent sources outweigh stale ones.
- Sources that directly address the question outweigh tangentially related ones.
- Drop SEO filler, outdated info, and beginner tutorials (unless the user is the beginner audience).
- When sources conflict, don't silently pick a side — surface the conflict and note which is more reliable and why.

## Step 4 — Iterate on gaps

If the first round doesn't fully answer the question, search again with refined queries aimed at the gaps. When something genuinely can't be verified, **say so** — an honest "I couldn't confirm X" is a feature, not a failure. Never fill a gap from memory and present it as researched fact.

## Step 5 — Deliver the brief

**Quick lookup:** answer inline, concise, with inline citations. Don't pad.

**Deep brief** — structure it (this is the format the `researcher` subagent uses, so the shapes stay familiar):

## Summary
2–3 sentence direct answer.

## Findings
Numbered findings with inline source citations:
1. **Finding** — explanation. [Source](url)

## Sources
- Kept: Source Title (url) — why relevant
- Dropped: Source Title (url) — why excluded

## Gaps
What couldn't be answered. Suggested next steps.

Render in Obsidian-friendly markdown. Keep every non-common-knowledge claim tied to a source, and mark confidence where it matters ("verified", "reported by X", "unconfirmed").

## Step 6 — Persist when it will be reused

If the brief answers something the user will return to (a course of study, a project decision, a comparison they'll reference), offer to save it — a note under `pleiades-vault/researcher/` or a `memory_note` keyed by topic. Ask rather than silently writing files; research notes are the user's archive, not yours.

## Teaching research craft

When the user is learning *how* to research (or you're coaching them through it), narrate the moves as you make them — why this query and not that one, why this source beats that one, what made a source droppable. This makes the skill transferable rather than just performed, and connects to the `teach` skill's motivated-explanation style: research discipline locks in when the *reason* for each move is visible.

## Vault & Obsidian

Briefs are Obsidian markdown and, when saved, become vault notes under `pleiades-vault/researcher/` — so make them self-contained and linkable: connect related briefs with `[[wikilinks]]`, present comparisons as tables, and use markdown links for sources (a rendered link reads better than a bare URL). Text is the medium for research; reach for a mermaid diagram only when the *structure* of the findings is itself the finding (a taxonomy, a flow, a comparison of approaches). A rendered image is almost never warranted — if it ever genuinely is, use the `visualize` skill (dispatch `mermaid-maker`/`svg-maker` via the `subagent` tool) and embed the returned PNG. When you save a brief, say so and give the wikilink so the user can open it in Obsidian.

## Boundaries

- Don't research what you already know with certainty (common knowledge, stable facts) — searching everything wastes the user's time. The trigger is *uncertainty or staleness*, not reflex.
- Don't dump raw search results into the reply — you synthesize; the user reads a brief, not a result feed.
- Don't fabricate citations — every link must be one you actually found. A made-up source is worse than no source.
