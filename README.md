1. Message history + SQL store √

Ordered message log per conversation (role, content, tool calls, timestamp, token count). The foundation everything else reads from and writes to. ConversationStore interface so this can later be swapped/extended without touching the core loop.

2. Tool registry (static) √

Name → JSON schema → callable, with register/list/invoke. Hand-written tools only at this stage — no dynamic creation yet. Establishes the pattern (registry + schema validation) that skills, SOPs, and self-created artifacts will all reuse.

3. Budgeting system √

Tokenizer-accurate running count per conversation, plus an eviction policy (drop oldest, summarize-and-evict, or retrieve-relevant-only) for when approaching the context limit. Built early because every subsystem after this consumes budget, and it later doubles as the burn-rate signal for spiral detection.

4. Global traits √

The small, always-on rule set that isn't task-specific — Socratic style, no-unsolicited-code, verbosity defaults. Implemented as a static system-prompt fragment, sitting beneath everything else. Built now, before skills/personas exist, so it's a stable baseline you can check later additions against ("did this skill/persona override a global trait").

5. Skill registry (static) √

Triggered procedures: instructions + example few-shots + which tools they use, selected by task relevance rather than invoked directly. Same registry pattern as tools (register/list/search-by-trigger/load-into-context). Skill metadata (name, description, trigger criteria) kept clean and machine-readable from the start, since persona and SOPs will both build on top of it.

6. Persona layer (static, manually selected) √

Standing identity — career coach, project architect, coding agent — implemented as a system-prompt fragment plus policy knobs (preferred/excluded skills, verbosity, code-vs-no-code) layered on top of global traits. Manually selected at first (e.g. a /persona command). Comes after skills because its job is partly to narrow/modulate skill selection and delivery, which can't be tested without skills existing.

7. Graph store √

Nodes for messages, tools, skills, SOPs, conversations, tasks; edges for composed_of, created_in, used_in, succeeded/failed, references. This is where relationship-queries live that SQL can't answer well: skill/tool lineage, persona-scoped memory, which self-created artifacts never got promoted. Built once there's enough SQL history to mine — mining relationships from an empty store isn't useful.

Implemented in `graph_store.py` — tables `graph_nodes` / `graph_edges` in `agent_state.db`, sharing the harness's SQLite file so cross-session history accumulates in one place. Node types: `conversation, task, message, tool, skill, sop, artifact`. Edge types: `created_in, used_in, succeeded, failed, composed_of, references`.

Edges aggregate instead of duplicating: one edge per `(src, dst, relation)` whose `properties` carry running `count` / `last_ms`, so N tool calls are one edge with a total, not N rows. `tool_stats()` then answers "which tools do I actually use, how often, and with what success rate" directly off the graph, and `tools_used_together()` finds co-occurrence across sessions.

The harness records a `conversation` node at startup, a `task` + user/assistant `message` nodes per turn (`created_in`), and per tool call a `used_in` plus a `succeeded`/`failed` edge. `GraphStore.digest()` is injected each turn as a `CROSS-SESSION MEMORY (graph store)` system message, so what was learned in a previous session is present in the next one.

Query it via the `graph_query` tool (`summary`, `tools`, `skills`, `sessions`, `tasks`, `lineage`, `cooccurrence`, `search`) or the `/graph` command in both the REPL and the TUI.

8. Creation pipeline v1: tools √

Self-creation as its own pipeline (trigger → draft → sandbox-validate → provisional register → promote/prune), applied first to tools since they're the smallest blast radius. Establishes the draft/validate/provisional/promote pattern that skills and SOPs will reuse. Needs the sandbox/execution boundary built here too, since self-created tools shouldn't run with the same trust as hand-written ones.

Implemented in `creation.py`: a `CreationStore` (table `created_artifacts` in `agent_state.db`) plus a `CreationPipeline` that the harness exposes as two tools — `create_artifact` (draft → validate → provisional register) and `artifact_admin` (`list` / `show` / `promote` / `prune`). Validation is behavioural: the draft's own `tests` run before it is registered, and every later call re-execs the tool's source in a **fresh subprocess** (separate interpreter, cwd confined to the repo root, wall-clock timeout, capped output) instead of importing it — so a created tool cannot reach harness internals or patch the loop. That is a boundary, not an OS sandbox: the subprocess still has the user's privileges, so promotion is a trust decision. Provisional artifacts live under `created/`, outside every directory the harness loads at startup, so drafting one cannot change the running agent.

9. Creation pipeline v2: skills √

Same pipeline applied to skills — validation here is behavioral (does invoking it produce expected tool calls on a held-out example) rather than just schema-checking. Self-created skills can reference self-created tools, so tool-tier needs to be stable first. Skill lineage (which conversation birthed it) logged to the graph store.

`kind='skill'` reuses the same lifecycle, but validation is behavioural at a different level: **one bounded model call** runs the draft body against the held-out `example_task` and checks that `expected_tools` actually fire; the draft is rejected otherwise. A provisional skill lives at `created/skills/<name>/SKILL.md` and is listed in the trigger index (projected as `provisional`); promotion copies the file to `skills/<name>/SKILL.md` and needs the documented restart to load from disk, and pruning removes that copy — but only when its content is byte-identical to ours, so a hand-written skill can never be deleted. The artifact node records `created_in` (lineage: which conversation birthed it) and `references` edges to the tools its body names.

10. Creation pipeline v3: SOPs √

Fixed, ordered playbooks for recurring task classes, composed of skills (and sometimes raw tool calls) rather than adapted per-context like skills are. Built last among the creation tiers because an SOP is only trustworthy if the skills it chains are already validated — gate SOP drafting to only use promoted, not provisional, skills, since a rigid workflow built on shaky components is worse than a skill failing on its own.

`kind='sop'` stores an ordered `steps` list. Both drafting *and* promotion are gated on every chained skill being **promoted**, re-checked at promotion because a skill may have been pruned in the meantime. The SOP node carries `composed_of` edges to its skills, and once promoted it is listed in the index as a read-only playbook — deciding *when* to apply one is the router's job (step 11), so an SOP deliberately has no automatic trigger yet.

Review what the agent has built with `artifact_admin` (`list` shows tier and use/failure counts), `/artifacts` in either frontend, or `graph_query(kind="artifacts")`.

11. Router / "brain"

Sits in front of the main loop and makes three kinds of decisions per request: which model handles it (local small model vs. Claude vs. other provider), which persona is active, and whether an existing SOP/skill fits or the model should improvise. Start fully rule-based (keyword/embedding-similarity routing) — an ML or RL-based router is only worth the complexity once you have enough logged (state, decision, outcome) tuples from the rule-based version to make it meaningful.

12. Spiral detection + recovery

A monitor the router runs alongside execution, reading the same event stream. Detects tool-call loops, reasoning drift, context thrashing, error-retry loops, budget burn without progress, and task substitution — using cheap heuristics (repetition hashing, output-diffing, budget-vs-progress ratio) for most cases, and a periodic model-based goal-drift check for the subtler ones. Recovery escalates in tiers: nudge → interrupt-and-replan → rollback to last-good state → escalate to a stronger model → escalate to you. Built after the router because you need a stable notion of "the plan" to detect drift from, and the router's first version is what establishes that.

13. UX: thinking animation + general animation √

Fully decoupled presentation layer. The core loop emits state events (thinking, tool_call, tool_result, token, done) over whatever transport (SSE/WebSocket), and the frontend animates based on event type alone — it shouldn't need to know why the model is thinking. Parallelizable with everything above; build order doesn't matter here.

## Providers

The model backend is pluggable (`providers.py`). Each provider owns its endpoint,
auth env var, and thinking-mode dialect, so the harness loop stays provider-blind.

| Provider | Endpoint | Key env | Default model | Thinking |
|----------|----------|---------|---------------|----------|
| `local` (default) | `http://192.168.1.92:8080/v1` | `API_KEY` | mlx-serve hash id | `enable_thinking` |
| `deepseek` | `https://api.deepseek.com` | `DEEPSEEK_API` | `deepseek-flash` | `thinking` + `reasoning_effort` |

```bash
python main.py                          # local mlx-serve (default)
python main.py --provider deepseek      # DeepSeek API
python main.py --tui --provider deepseek
```

Selection order: `--provider` flag → `PLEIADES_PROVIDER` → `local`. `--model`
overrides, as do `LLM_MODEL` (local) and `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL`
(hosted). `DEEPSEEK_REASONING_EFFORT` sets `low` / `high` / `max` (`none`
disables thinking); `MLX_ENABLE_THINKING=0` does the same for mlx-serve.

### Switching while running

Both the REPL and the TUI take `/provider` and `/model` at the prompt, alongside
`/stop`:

```
/provider                show the active backend + what's available
/provider deepseek       switch backend (endpoint, auth, thinking dialect)
/model                   show the active model + what the provider advertises
/model deepseek-v4-pro   switch model within the current backend
```

Switching is in place: the tool registry, session id, message history, budget
counters and graph all survive, and the very next turn uses the new backend's
dialect. The graph's `conversation` node is re-stamped with the new
provider/model, so `/graph` reflects what actually ran. `--provider`/`--model`
set the starting point; these commands change it afterwards.

Two backend behaviours the wrapper has to respect, both encoded on the provider:

- **Reasoning replay.** When a request carries `tools`, DeepSeek requires every
  earlier turn's `reasoning_content` to be echoed back or it returns HTTP 400.
  Assistant reasoning is therefore persisted in `messages.reasoning` and replayed
  for providers with `reasoning_replay`. mlx-serve doesn't want it, so there it is
  dropped rather than burning context.
- **Streamed usage.** DeepSeek only reports token usage mid-stream when
  `stream_options.include_usage` is set (`stream_usage`); mlx-serve is asked
  without it. Both fall back to a char/4 estimate if usage is absent.

Switching to `deepseek` also removes the LAN server dependency at startup: the
local provider probes `GET /v1/models` for its context window, while hosted
providers use a known value, so no local server needs to be running.

## TUI

`python main.py --tui` runs a full-screen [Textual](https://textual.textualize.io)
app — the frontend the harness was designed around. Both frontends are built on
the same three pieces, so they can't drift apart visually:

| module | role |
|--------|------|
| `ux.py` | `EventSink` / `EventBus`: the loop emits state events and never renders |
| `presentation.py` | the pi palette, tool-state glyphs/tints, token formatting, preview trimming |
| `textual_tui/` | Textual frontend: `model.py` (transcript, no UI imports), `events.py` (sink → messages), `widgets.py`, `app.py` |
| `tui.py` | the legacy hand-rolled rich frontend, kept behind `--rich` |

```bash
python main.py --tui                    # Textual
python main.py --tui --rich             # legacy rich frontend
python main.py --tui --provider deepseek
```

Keys: `enter` send · `/help` commands · `ctrl+p` command palette · `pageup` /
`pagedown` scroll · `ctrl+home` jump to the newest output · `ctrl+l` clear ·
`ctrl+q` quit. The mouse wheel scrolls the transcript wherever the pointer is —
over the reply, the prompt or the status row.

The transcript follows the tail while a turn streams. Scrolling up releases it
immediately, even by a single notch, and the view then stays where you put it for
the rest of the turn; returning to the bottom (or `ctrl+home`) resumes following.

Text is selectable and copyable; a streaming reply is appended to the rendered
document rather than re-rendered.

Slash commands work in both frontends and are also in the palette. `/provider`
and `/model` with no argument open a picker rather than printing a list.

Layout is deliberate: a turn reads top to bottom in event order — prompt,
thinking, one collapsible per tool call (collapsed once finished, click to
expand its output), then the reply — so the newest output is where your eye
already is.

> One workaround is carried: `guard_detached_hit_test()` in `textual_tui/app.py`.
> Textual's selection code assumes the widget its mouse hit test returns still has
> a parent, but the hit-test map is only rebuilt on refresh — so clicking a
> markdown block that had just been rewritten (i.e. clicking a streaming answer)
> could kill the app with `AttributeError: 'NoneType' object has no attribute
> 'region'`. The shim turns such a click into a no-op. Delete it if Textual ever
> guards the `None` itself; the reproduction in `tests/test_textual_tui.py` will
> tell you whether the bug is still there.

### Tests

```bash
.venv/bin/python tests/test_textual_tui.py
```

Headless — `App.run_test()` renders into an in-memory terminal, and the harness is
faked — so it runs in CI with no tty and no network. It covers the transcript
model, the event sink, the whole app (turn rendering, order and visible height of
every band, status line, resize, commands, palette, error path), follow-the-tail,
and one construction pass against the real harness to catch frontend/harness API
drift.