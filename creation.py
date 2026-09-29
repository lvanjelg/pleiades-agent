"""Creation pipeline — self-authored tools, skills and SOPs (README steps 8-10).

Three artifact kinds, one pipeline:

* **tool** (step 8) — a JSON-schema function with a Python body. Validation is
  behavioural: the draft is run against the author's own test cases in a
  subprocess.
* **skill** (step 9) — a ``SKILL.md`` procedure. Validation is behavioural too,
  but at a different level: one bounded model call checks that the draft
  produces the expected tool calls on a held-out example. Its lineage (which
  conversation birthed it) is written to the graph store.
* **sop** (step 10) — a fixed, ordered playbook. Drafting is gated on the skills
  it chains being **promoted**, because a rigid workflow built on provisional
  components is worse than a skill failing on its own.

The shared lifecycle is ``draft -> validate -> provisional -> promote | prune``.
Fixing the pipeline fixes all three tiers, which is the point of building it
once (step 8) and reusing it (steps 9-10).

Execution boundary
------------------
A created tool never runs in the harness process. Its source is written to
``created/tools/<name>.py`` and every call execs that file in a **fresh
subprocess**: separate interpreter, cwd confined to the repo root, wall-clock
timeout, capped output. The tool therefore cannot import harness internals,
patch the loop, or hold a reference to anything of ours.

This is a *boundary*, not an OS sandbox. The subprocess is an ordinary Python
process with the user's privileges and full filesystem access, so promotion is
a trust decision, not containment — the same honesty as ``refuse_destructive_git``
in main.py ("a speed bump, not a sandbox").

Provisional artifacts live outside every directory the harness loads at startup
(``created/``, not ``skills/``), so drafting one cannot change the running agent.
Promotion is the only transition that touches a loaded path.
"""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime as dt, UTC
from pathlib import Path
from typing import Callable, Optional

KINDS = ("tool", "skill", "sop")
TIERS = ("draft", "provisional", "promoted", "pruned")
#: provisional + promoted are *live* (registered callable / visible index).
LIVE_TIERS = ("provisional", "promoted")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
CREATED_DIR = "created"
MAX_OUTPUT = 8000
TOOL_TIMEOUT = 30
MIN_DESCRIPTION = 20
MAX_DESCRIPTION = 600

# Runs a created tool's source in a fresh interpreter and prints the result of
# its `run(**kwargs)`. argv: <source path> <args json>.
_SANDBOX_RUNNER = r'''
import json, sys

path, raw = sys.argv[1], sys.argv[2]
args = json.loads(raw)
namespace = {"__name__": "__created_tool__", "__file__": path}
with open(path, encoding="utf-8") as fh:
    source = fh.read()
exec(compile(source, path, "exec"), namespace)
fn = namespace.get("run")
if not callable(fn):
    raise SystemExit("created tool defines no callable 'run'")
result = fn(**args)
sys.stdout.write(result if isinstance(result, str) else json.dumps(result, default=str))
'''


def _now() -> str:
    return dt.now(UTC).isoformat()


def _cap(text: str, limit: int = MAX_OUTPUT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _json_loads(text, default):
    try:
        value = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, type(default)) else default


@dataclass
class Result:
    """Outcome of a pipeline operation, shaped for a tool result string."""

    ok: bool
    message: str
    kind: str = ""
    name: str = ""
    tier: str = ""
    path: str = ""
    checks: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        head = "OK" if self.ok else "REJECTED"
        lines = [f"{head}: {self.message}"]
        if self.name:
            lines.append(f"{self.kind} '{self.name}' · tier={self.tier} · path={self.path}")
        lines += [f"  - {c}" for c in self.checks]
        return "\n".join(lines)


class CreationStore:
    """Lifecycle + lineage rows for created artifacts, in the shared SQLite file."""

    def __init__(self, db_path: str = "agent_state.db"):
        # Same threading relaxation as AgentState/GraphStore: the TUI runs a turn
        # on a worker thread, but only one turn is ever in flight.
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS created_artifacts (
            kind TEXT NOT NULL, name TEXT NOT NULL,
            tier TEXT NOT NULL DEFAULT 'draft',
            description TEXT DEFAULT '',
            payload TEXT DEFAULT '{}',
            path TEXT DEFAULT '',
            uses INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            validation TEXT DEFAULT '{}',
            session_id TEXT DEFAULT '',
            created_at TEXT, updated_at TEXT,
            UNIQUE(kind, name))""")
        self.db.commit()

    def upsert(self, kind: str, name: str, *, tier: str = "draft",
               description: str = "", payload: Optional[dict] = None,
               path: str = "", validation: Optional[dict] = None,
               session_id: str = "") -> None:
        """Insert or refresh an artifact row.

        ``tier`` on an existing row is only overwritten when explicitly given, so
        re-drafting a promoted artifact cannot silently demote it.
        """
        now = _now()
        self.db.execute(
            "INSERT INTO created_artifacts "
            "(kind, name, tier, description, payload, path, validation, session_id, "
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(kind, name) DO UPDATE SET "
            "tier=CASE WHEN excluded.tier='draft' THEN created_artifacts.tier "
            "          ELSE excluded.tier END, "
            "description=excluded.description, "
            "payload=excluded.payload, path=excluded.path, "
            "validation=excluded.validation, updated_at=excluded.updated_at",
            (kind, name, tier, description, json.dumps(payload or {}), path,
             json.dumps(validation or {}), session_id, now, now))
        self.db.commit()

    def get(self, kind: str, name: str) -> Optional[dict]:
        row = self.db.execute(
            "SELECT kind, name, tier, description, payload, path, uses, failures, "
            "validation, session_id, created_at, updated_at "
            "FROM created_artifacts WHERE kind=? AND name=?",
            (kind, str(name))).fetchone()
        return self._row(row) if row else None

    def list(self, kind: Optional[str] = None,
             tiers: Optional[tuple[str, ...]] = None) -> list[dict]:
        clauses, params = [], []
        if kind:
            clauses.append("kind=?"); params.append(kind)
        if tiers:
            clauses.append(f"tier IN ({','.join('?' * len(tiers))})"); params += list(tiers)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.execute(
            f"SELECT kind, name, tier, description, payload, path, uses, failures, "
            f"validation, session_id, created_at, updated_at "
            f"FROM created_artifacts {where} ORDER BY updated_at DESC", params).fetchall()
        return [self._row(r) for r in rows]

    def set_tier(self, kind: str, name: str, tier: str) -> None:
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r} (one of {', '.join(TIERS)})")
        self.db.execute(
            "UPDATE created_artifacts SET tier=?, updated_at=? WHERE kind=? AND name=?",
            (tier, _now(), kind, str(name)))
        self.db.commit()

    def bump(self, kind: str, name: str, uses: int = 0, failures: int = 0) -> None:
        self.db.execute(
            "UPDATE created_artifacts SET uses=uses+?, failures=failures+?, updated_at=? "
            "WHERE kind=? AND name=?", (uses, failures, _now(), kind, str(name)))
        self.db.commit()

    @staticmethod
    def _row(row) -> dict:
        return {"kind": row[0], "name": row[1], "tier": row[2], "description": row[3],
                "payload": _json_loads(row[4], {}), "path": row[5],
                "uses": row[6], "failures": row[7],
                "validation": _json_loads(row[8], {}), "session_id": row[9],
                "created_at": row[10], "updated_at": row[11]}


# --------------------------------------------------------------------- validation

def _check_name(name: str) -> Optional[str]:
    if not NAME_RE.match(name or ""):
        return "name must be lowercase, start with a letter, and be 2-40 chars of [a-z0-9_-]"
    return None


def _check_description(description: str) -> Optional[str]:
    text = (description or "").strip()
    if "\n" in text:
        return "description must be a single line (it is re-sent in the trigger index every turn)"
    if not (MIN_DESCRIPTION <= len(text) <= MAX_DESCRIPTION):
        return f"description must be {MIN_DESCRIPTION}-{MAX_DESCRIPTION} characters"
    return None


def _check_tool_code(code: str) -> Optional[str]:
    """Parse (never execute) the source and require a top-level callable ``run``."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return f"code does not compile: line {exc.lineno}: {exc.msg}"
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
               for node in tree.body):
        return "code must define a top-level `def run(**kwargs) -> str`"
    return None


def _check_parameters(parameters: dict) -> Optional[str]:
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        return "spec.parameters must be a JSON-Schema object with type='object'"
    if not isinstance(parameters.get("properties", {}), dict):
        return "spec.parameters.properties must be an object"
    return None


def _check_tests(tests) -> Optional[str]:
    if not isinstance(tests, list) or not tests:
        return "tests must be a non-empty list — behavioural validation is required, not optional"
    for i, case in enumerate(tests):
        if not isinstance(case, dict):
            return f"tests[{i}] must be an object"
        if not isinstance(case.get("args", {}), dict):
            return f"tests[{i}].args must be an object"
        if "expect" not in case and "expect_exact" not in case:
            return f"tests[{i}] needs 'expect' (substring) or 'expect_exact'"
    return None


def validate_sop_steps(steps, promoted: set[str], known_tools: set[str]) -> list[str]:
    """Return a list of problems; empty means the playbook is well-formed.

    The gate that matters: every skill a SOP chains must be *promoted*. A SOP is
    fixed and rigid, so a provisional component underneath it fails worse than
    the component failing on its own would.
    """
    problems: list[str] = []
    if not isinstance(steps, list) or not steps:
        return ["spec.steps must be a non-empty ordered list"]
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            problems.append(f"step {i} must be an object")
            continue
        keys = {k for k in step if k in ("skill", "tool")}
        if len(keys) != 1:
            problems.append(f"step {i} must name exactly one of 'skill' or 'tool'")
            continue
        if "skill" in step:
            name = str(step["skill"])
            if name not in promoted:
                problems.append(f"step {i}: skill '{name}' is not promoted (SOPs may only chain promoted skills)")
        else:
            name = str(step["tool"])
            if name not in known_tools:
                problems.append(f"step {i}: no tool named '{name}'")
    return problems


# -------------------------------------------------------------------- the pipeline

class CreationPipeline:
    """Draft, validate, register, promote and prune created artifacts.

    Dependencies are injected so the pipeline has no import cycle with main.py and
    is testable without a harness: ``resolve`` is the repo's path guard,
    ``known_tools``/``known_skills`` report what already exists, and ``wrapper``
    supplies the one bounded model call that behavioural skill validation needs.
    """

    def __init__(self, store: CreationStore, resolve: Callable[[str], Path],
                 root: Path, *, known_tools: Callable[[], set[str]],
                 known_skills: Callable[[], set[str]],
                 wrapper: Callable[[], object] = lambda: None,
                 tool_schemas: Callable[[], list[dict]] = lambda: [],
                 session_id: Callable[[], str] = lambda: "",
                 timeout: int = TOOL_TIMEOUT):
        self.store = store
        self.resolve = resolve
        self.root = Path(root)
        self.known_tools = known_tools
        self.known_skills = known_skills
        self.wrapper = wrapper
        self.tool_schemas = tool_schemas
        self.session_id = session_id
        self.timeout = timeout

    # ---------------------------------------------------------------- create
    def create(self, kind: str, name: str, description: str,
               spec: Optional[dict] = None, tests: Optional[list] = None,
               *, verify_behavior: bool = True) -> Result:
        kind = (kind or "").strip().lower()
        name = (name or "").strip()
        spec = spec if isinstance(spec, dict) else {}
        if kind not in KINDS:
            return Result(False, f"kind must be one of {', '.join(KINDS)}", kind, name)
        for problem in (_check_name(name), _check_description(description)):
            if problem:
                return Result(False, problem, kind, name)
        if name in self.known_tools() and kind == "tool":
            return Result(False, f"a tool named '{name}' already exists", kind, name)
        if kind == "skill":
            if name in self.known_skills():
                return Result(False, f"a skill named '{name}' already exists", kind, name)
            existing = self.store.get("skill", name)
            if existing and existing["tier"] in LIVE_TIERS:
                return Result(False, f"skill '{name}' is already {existing['tier']}", kind, name)

        if kind == "tool":
            return self._create_tool(name, description, spec, tests)
        if kind == "skill":
            return self._create_skill(name, description, spec, verify_behavior=verify_behavior)
        return self._create_sop(name, description, spec)

    # -- tools -----------------------------------------------------------
    def _create_tool(self, name, description, spec, tests) -> Result:
        parameters = spec.get("parameters")
        code = spec.get("code") or ""
        problems = [p for p in (_check_parameters(parameters), _check_tool_code(code),
                                _check_tests(tests)) if p]
        if problems:
            return Result(False, problems[0], "tool", name, checks=problems)

        rel = f"{CREATED_DIR}/tools/{name}.py"
        target = self.resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code if code.endswith("\n") else code + "\n", encoding="utf-8")

        checks, failures = [], []
        for i, case in enumerate(tests, 1):
            ok, out = self._exec_tool(rel, case.get("args", {}))
            if ok and "expect_exact" in case:
                good = out == case["expect_exact"]
            elif ok:
                good = case.get("expect", "") in out
            else:
                good = False
            label = f"test {i}: {'pass' if good else 'FAIL'}"
            checks.append(f"{label} — output {out[:200]!r}")
            if not good:
                failures.append(f"test {i} failed (expected {case.get('expect_exact', case.get('expect'))!r}, got {out[:200]!r})")

        if failures:
            self._record("tool", name, "draft", description, spec, rel,
                         {"ok": False, "error": failures[0]}, "")
            return Result(False, "sandbox validation failed: " + failures[0],
                          "tool", name, "draft", rel, checks)

        payload = {"parameters": parameters, "code_path": rel}
        self._record("tool", name, "provisional", description, payload, rel,
                     {"ok": True, "checks": checks}, "")
        return Result(True, "tool created and registered provisionally "
                            "(callable now; promote with artifact_admin)", "tool", name,
                      "provisional", rel, checks)

    # -- skills ----------------------------------------------------------
    def _create_skill(self, name, description, spec, *, verify_behavior=True) -> Result:
        body = (spec.get("body") or "").strip()
        declared = spec.get("tools") or []
        example_task = (spec.get("example_task") or "").strip()
        expected = [str(t) for t in (spec.get("expected_tools") or [])]

        problems = []
        if len(body) < 80:
            problems.append("spec.body must be the operating manual (at least 80 characters)")
        if not isinstance(declared, list):
            problems.append("spec.tools must be a list of tool names the skill uses")
        unknown = [t for t in declared if t not in self.known_tools()]
        if unknown:
            problems.append(f"spec.tools names tools that do not exist: {', '.join(map(str, unknown))}")
        if not example_task:
            problems.append("spec.example_task is required — skill validation is behavioural")
        if not expected:
            problems.append("spec.expected_tools is required — the held-out example must say which tools should fire")
        if problems:
            return Result(False, problems[0], "skill", name, checks=problems)

        checks = [f"structure ok · tools {', '.join(map(str, declared)) or '(none)'}",
                  f"held-out example: {example_task[:120]}"]
        behavior_error = ""
        if verify_behavior:
            ok, detail = self.behavioral_check(body, example_task, expected)
            checks.append(("behaviour: PASS — " if ok else "behaviour: FAIL — ") + detail)
            if not ok:
                behavior_error = detail

        rel = f"{CREATED_DIR}/skills/{name}/SKILL.md"
        target = self.resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._skill_markdown(name, description, body), encoding="utf-8")

        payload = {"body": body, "tools": declared, "example_task": example_task,
                   "expected_tools": expected}
        if behavior_error:
            self._record("skill", name, "draft", description, payload, rel,
                         {"ok": False, "error": behavior_error, "checks": checks}, "")
            return Result(False, f"behavioural validation failed: {behavior_error}",
                          "skill", name, "draft", rel, checks)
        self._record("skill", name, "provisional", description, payload, rel,
                     {"ok": True, "checks": checks}, "")
        return Result(True, "skill created provisionally — it is in the trigger index, "
                            "but needs a restart to be loadable from disk on promotion",
                      "skill", name, "provisional", rel, checks)

    def behavioral_check(self, body: str, example_task: str, expected: list[str]) -> tuple[bool, str]:
        """One bounded model call: does the draft produce the expected tool calls?

        This is step 9's "validation is behavioural rather than schema-checking".
        It is deliberately one call, non-streamed, and never raises: a failed
        check is a rejected draft, not a crashed turn.
        """
        wrapper = self.wrapper()
        if wrapper is None:
            return True, "skipped (no model wrapper attached)"
        try:
            response = wrapper.chat(
                messages=[{"role": "system", "content": body},
                          {"role": "user", "content": example_task}],
                tools=self.tool_schemas() or None)
        except Exception as exc:  # a broken check must not break the turn
            return False, f"model call failed: {type(exc).__name__}: {exc}"
        got = [c.name for c in (getattr(response, "tool_calls", None) or [])]
        if not got:
            return False, f"no tool calls on the held-out example (expected {', '.join(expected)})"
        missing = [t for t in expected if t not in got]
        if missing:
            return False, f"called {', '.join(got)} but not {', '.join(missing)}"
        return True, f"called {', '.join(got)}"

    # -- SOPs ------------------------------------------------------------
    def _create_sop(self, name, description, spec) -> Result:
        steps = spec.get("steps")
        promoted = {s["name"] for s in self.store.list("skill", tiers=("promoted",))}
        problems = validate_sop_steps(steps, promoted, self.known_tools())
        if problems:
            return Result(False, problems[0], "sop", name, checks=problems)

        rel = f"{CREATED_DIR}/sops/{name}.md"
        target = self.resolve(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._sop_markdown(name, description, steps), encoding="utf-8")
        payload = {"steps": steps}
        self._record("sop", name, "provisional", description,
                     payload, rel, {"ok": True, "checks": ["all chained skills are promoted"]}, "")
        return Result(True, "SOP created provisionally (all chained skills are promoted)",
                      "sop", name, "provisional", rel)

    # ----------------------------------------------------------------- admin
    def promote(self, kind: str, name: str) -> Result:
        row = self.store.get(kind, name)
        if not row:
            return Result(False, f"no {kind} named '{name}'", kind, name)
        if row["tier"] == "draft":
            return Result(False, "cannot promote a draft that never passed validation",
                          kind, name, "draft", row["path"])
        if row["tier"] == "promoted":
            return Result(True, "already promoted", kind, name, "promoted", row["path"])
        if kind == "skill":
            src = self.resolve(row["path"])
            dst = self.resolve(f"skills/{name}/SKILL.md")
            if dst.exists() and name in self.known_skills():
                return Result(False, f"a hand-written skill already owns the name '{name}'",
                              kind, name, row["tier"], row["path"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        if kind == "sop":
            # Promotion re-checks the gate: a chained skill may have been pruned
            # since the SOP was drafted.
            promoted = {s["name"] for s in self.store.list("skill", tiers=("promoted",))}
            problems = validate_sop_steps(row["payload"].get("steps"), promoted, self.known_tools())
            if problems:
                return Result(False, "promotion blocked: " + problems[0], kind, name,
                              row["tier"], row["path"], problems)
        self.store.set_tier(kind, name, "promoted")
        note = " — restart the harness to load it from disk" if kind == "skill" else ""
        return Result(True, f"promoted{note}", kind, name, "promoted", row["path"])

    def prune(self, kind: str, name: str) -> Result:
        row = self.store.get(kind, name)
        if not row:
            return Result(False, f"no {kind} named '{name}'", kind, name)
        if kind == "skill" and row["tier"] == "promoted":
            self._retire_promoted_skill(name, row)
        self.store.set_tier(kind, name, "pruned")
        return Result(True, "pruned (row and lineage kept; the artifact is no longer live)",
                      kind, name, "pruned", row["path"])

    def _retire_promoted_skill(self, name: str, row: dict) -> None:
        """Remove the promoted copy from ``skills/`` if it is byte-identical to ours.

        Pruning a promoted skill has to take the file out of ``skills/`` or it
        would simply load again on the next start. The content check is what stops
        this from ever deleting a hand-written skill that happens to share a name.
        """
        try:
            src = self.resolve(row["path"])
            dst = self.resolve(f"skills/{name}/SKILL.md")
            if (src.is_file() and dst.is_file()
                    and src.read_text(encoding="utf-8") == dst.read_text(encoding="utf-8")):
                dst.unlink()
        except (OSError, ValueError):
            pass

    def show(self, kind: str, name: str) -> Result:
        row = self.store.get(kind, name)
        if not row:
            return Result(False, f"no {kind} named '{name}'", kind, name)
        validation = row["validation"]
        checks = list(validation.get("checks", []))
        if validation.get("error"):
            checks.append("error: " + validation["error"])
        return Result(True, row["description"], kind, name, row["tier"], row["path"], checks)

    def listing(self, kind: str = "") -> str:
        rows = self.store.list(kind or None)
        if not rows:
            return "No created artifacts yet."
        lines = []
        for row in rows:
            lines.append(f"- {row['kind']}:{row['name']} [{row['tier']}] "
                         f"uses={row['uses']} failures={row['failures']} — "
                         f"{(row['description'] or '')[:80]}")
        return "\n".join(lines)

    # ----------------------------------------------------------------- wiring
    def tool_callable(self, name: str) -> Callable:
        """A sandboxed callable for a created tool: every call is a subprocess."""
        row = self.store.get("tool", name)
        rel = row["path"] if row else ""

        def created_tool(**kwargs):
            try:
                ok, out = self._exec_tool(rel, kwargs)
            except Exception as exc:  # defensive: never let the boundary leak into the loop
                ok, out = False, f"{type(exc).__name__}: {exc}"
            self.store.bump("tool", name, uses=1 if ok else 0, failures=0 if ok else 1)
            return out if ok else f"Error: {out}"

        created_tool.__name__ = name
        return created_tool

    def registrable_tools(self) -> list[tuple[str, str, dict, Callable]]:
        """(name, description, parameters, callable) for every live created tool."""
        out = []
        for row in self.store.list("tool", tiers=LIVE_TIERS):
            parameters = row["payload"].get("parameters") or {"type": "object", "properties": {}}
            out.append((row["name"], row["description"], parameters, self.tool_callable(row["name"])))
        return out

    def skill_index(self, exclude: Optional[set[str]] = None) -> str:
        """Prompt lines for created skills/SOPs not already on disk.

        Provisional skills are visible here (that is what makes them usable before
        promotion); SOPs are listed only once promoted, per step 10's gating.
        """
        exclude = exclude or set()
        lines = []
        for row in self.store.list("skill", tiers=LIVE_TIERS):
            if row["name"] not in exclude:
                lines.append(f"{row['name']} [{row['tier']}]: {row['description']}")
        promoted_sops = self.store.list("sop", tiers=("promoted",))
        if promoted_sops:
            lines.append("promoted SOPs (fixed playbooks; read with artifact_admin show): "
                         + ", ".join(s["name"] for s in promoted_sops))
        return "\n".join(lines)

    # ---------------------------------------------------------------- internals
    def _exec_tool(self, rel_path: str, args: dict) -> tuple[bool, str]:
        target = self.resolve(rel_path)
        if not target.is_file():
            return False, f"created tool source is missing: {rel_path}"
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _SANDBOX_RUNNER, str(target), json.dumps(args)],
                cwd=self.root, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return False, f"timed out after {self.timeout}s"
        except OSError as exc:
            return False, f"could not start sandbox: {exc}"
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return False, _cap(err or out or f"exit code {proc.returncode}")
        return True, _cap(out) if out else "(no output)"

    def _record(self, kind: str, name: str, tier: str, description: str,
                payload: dict, path: str, validation: dict, session_id: str) -> None:
        self.store.upsert(kind, name, tier=tier, description=description, payload=payload,
                          path=path, validation=validation,
                          session_id=session_id or self.session_id())

    @staticmethod
    def _skill_markdown(name: str, description: str, body: str) -> str:
        return f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n"

    @staticmethod
    def _sop_markdown(name: str, description: str, steps: list) -> str:
        lines = [f"---", f"name: {name}", f"description: {description}", "kind: sop", "---",
                 "", f"# {name}", "", description.strip(), "", "## Steps", ""]
        for i, step in enumerate(steps, 1):
            target = step.get("skill") or step.get("tool")
            kind = "skill" if "skill" in step else "tool"
            note = f" — {step['note']}" if step.get("note") else ""
            lines.append(f"{i}. `{target}` ({kind}){note}")
        return "\n".join(lines) + "\n"
