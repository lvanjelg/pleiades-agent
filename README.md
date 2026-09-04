1. Message history + SQL store

Ordered message log per conversation (role, content, tool calls, timestamp, token count). The foundation everything else reads from and writes to. ConversationStore interface so this can later be swapped/extended without touching the core loop.

2. Tool registry (static)

Name → JSON schema → callable, with register/list/invoke. Hand-written tools only at this stage — no dynamic creation yet. Establishes the pattern (registry + schema validation) that skills, SOPs, and self-created artifacts will all reuse.

3. Budgeting system

Tokenizer-accurate running count per conversation, plus an eviction policy (drop oldest, summarize-and-evict, or retrieve-relevant-only) for when approaching the context limit. Built early because every subsystem after this consumes budget, and it later doubles as the burn-rate signal for spiral detection.

4. Global traits

The small, always-on rule set that isn't task-specific — Socratic style, no-unsolicited-code, verbosity defaults. Implemented as a static system-prompt fragment, sitting beneath everything else. Built now, before skills/personas exist, so it's a stable baseline you can check later additions against ("did this skill/persona override a global trait").

5. Skill registry (static)

Triggered procedures: instructions + example few-shots + which tools they use, selected by task relevance rather than invoked directly. Same registry pattern as tools (register/list/search-by-trigger/load-into-context). Skill metadata (name, description, trigger criteria) kept clean and machine-readable from the start, since persona and SOPs will both build on top of it.

6. Persona layer (static, manually selected)

Standing identity — career coach, project architect, coding agent — implemented as a system-prompt fragment plus policy knobs (preferred/excluded skills, verbosity, code-vs-no-code) layered on top of global traits. Manually selected at first (e.g. a /persona command). Comes after skills because its job is partly to narrow/modulate skill selection and delivery, which can't be tested without skills existing.

7. Graph store

Nodes for messages, tools, skills, SOPs, conversations, tasks; edges for composed_of, created_in, used_in, succeeded/failed, references. This is where relationship-queries live that SQL can't answer well: skill/tool lineage, persona-scoped memory, which self-created artifacts never got promoted. Built once there's enough SQL history to mine — mining relationships from an empty store isn't useful.

8. Creation pipeline v1: tools

Self-creation as its own pipeline (trigger → draft → sandbox-validate → provisional register → promote/prune), applied first to tools since they're the smallest blast radius. Establishes the draft/validate/provisional/promote pattern that skills and SOPs will reuse. Needs the sandbox/execution boundary built here too, since self-created tools shouldn't run with the same trust as hand-written ones.

9. Creation pipeline v2: skills

Same pipeline applied to skills — validation here is behavioral (does invoking it produce expected tool calls on a held-out example) rather than just schema-checking. Self-created skills can reference self-created tools, so tool-tier needs to be stable first. Skill lineage (which conversation birthed it) logged to the graph store.

10. Creation pipeline v3: SOPs

Fixed, ordered playbooks for recurring task classes, composed of skills (and sometimes raw tool calls) rather than adapted per-context like skills are. Built last among the creation tiers because an SOP is only trustworthy if the skills it chains are already validated — gate SOP drafting to only use promoted, not provisional, skills, since a rigid workflow built on shaky components is worse than a skill failing on its own.

11. Router / "brain"

Sits in front of the main loop and makes three kinds of decisions per request: which model handles it (local small model vs. Claude vs. other provider), which persona is active, and whether an existing SOP/skill fits or the model should improvise. Start fully rule-based (keyword/embedding-similarity routing) — an ML or RL-based router is only worth the complexity once you have enough logged (state, decision, outcome) tuples from the rule-based version to make it meaningful.

12. Spiral detection + recovery

A monitor the router runs alongside execution, reading the same event stream. Detects tool-call loops, reasoning drift, context thrashing, error-retry loops, budget burn without progress, and task substitution — using cheap heuristics (repetition hashing, output-diffing, budget-vs-progress ratio) for most cases, and a periodic model-based goal-drift check for the subtler ones. Recovery escalates in tiers: nudge → interrupt-and-replan → rollback to last-good state → escalate to a stronger model → escalate to you. Built after the router because you need a stable notion of "the plan" to detect drift from, and the router's first version is what establishes that.

13. UX: thinking animation + general animation

Fully decoupled presentation layer. The core loop emits state events (thinking, tool_call, tool_result, token, done) over whatever transport (SSE/WebSocket), and the frontend animates based on event type alone — it shouldn't need to know why the model is thinking. Parallelizable with everything above; build order doesn't matter here.