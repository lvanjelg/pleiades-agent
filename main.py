import json
import re
import shlex
import subprocess
import sys
import requests
import os
import logging
from pathlib import Path
import uuid
from pathlib import Path
from dotenv import load_dotenv, dotenv_values 
from typing import Callable, Any
from dataclasses import dataclass, field
import time
import sqlite3
from datetime import datetime as dt, UTC
from enum import Enum
from graph_store import GraphStore
import providers
load_dotenv() 
logger = logging.getLogger(__name__)
LOG_DIR = "logs/"
WORKING_ROOT = Path(__file__).resolve().parent
MAX_TOOL_OUTPUT = 8000
MAX_SEARCH_MATCHES = 100
CONTEXT_COMPRESS_THRESHOLD = 0.85
ALLOWED_SHELL_COMMANDS = {"git", "pytest", "pip", "python", "python3",
                          "ls", "cat", "echo", "pwd", "wc", "grep", "find"}
SKILLS_DIR = "skills/"
SUBAGENT_MAX_DEPTH = 3
AGENTS_DIR = "agents/"
GRAPH_DB_PATH = "agent_state.db"  # graph store shares the harness SQLite file
with open("prompt.md", encoding="utf-8") as _prompt_file:
    SYS_PROMPT = _prompt_file.read().replace(
        "{current_date}", dt.now().date().isoformat())
logo = r"""
██████╗ ██╗     ███████╗██╗ █████╗ ██████╗ ███████╗███████╗
██╔══██╗██║     ██╔════╝██║██╔══██╗██╔══██╗██╔════╝██╔════╝
██████╔╝██║     █████╗  ██║███████║██║  ██║█████╗  ███████╗
██╔═══╝ ██║     ██╔══╝  ██║██╔══██║██║  ██║██╔══╝  ╚════██║
██║     ███████╗███████╗██║██║  ██║██████╔╝███████╗███████║
╚═╝     ╚══════╝╚══════╝╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝╚══════╝
                                                           """

@dataclass
class ToolCall:
    call_id: int | str
    name: str
    args: dict
    output: str = ""
    error: str = ""
    provider_info: dict = field(default_factory=dict)

@dataclass
class LLMResponse:
    content: str
    tool_calls: list
    message: dict
    response_id: str
    stats: dict
    output: list

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema
    fn: Callable

class ErrorType(Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    UNAVAILABLE = "unavailable"

@dataclass
class ToolError:
    error_type: ErrorType
    message: str
    suggestion: str

def format_tool_error(error: ToolError) -> str:
    parts = [f"[TOOL ERROR: {error.error_type.value.upper()}]"]
    parts.append(error.message)
    if error.suggestion:
        parts.append(f"Suggested action: {error.suggestion}")
    return "\n".join(parts)

'''
Tracks token usage and tool calls, checks for budget limits and prevents excessive usage
'''
class BudgetEnforcer:
    def __init__(self, budget: int):
        self.budget = budget
        self.tokens_used = 0
        self.max_tool_calls = 50
        self.max_time = 360
        self.tool_calls_total = 0
        self.tool_calls_per_tool: dict[str, int] = {}
        self.start_time = time.time()

    def record_tokens(self, input_tokens: int, output_tokens: int):
        self.tokens_used += input_tokens + output_tokens

    def record_tool_call(self, tool_name: str):
        self.tool_calls_total += 1
        self.tool_calls_per_tool[tool_name] = self.tool_calls_per_tool.get(tool_name, 0) + 1

    def check(self) -> str | None:
        if self.tokens_used >= self.budget:
            return f"Token budget exceeded: {self.tokens_used} (limit {self.budget})"
        if self.tool_calls_total >= self.max_tool_calls:
            return f"Tool call budget exceeded: {self.tool_calls_total}"
        if time.time() - self.start_time >= self.max_time:
            return "Time budget exceeded"
        return None


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}
        self.call_counts: dict[str, int] = {}

    def register(self, tool: Tool):
        self.tools[tool.name] = tool
        self.call_counts[tool.name] = 0

    def validate_call(self, tool_name: str, arguments: dict) -> tuple[bool, str]:
        if tool_name not in self.tools:
            return False, f"Unknown tool: {tool_name}"
        schema = self.tools[tool_name].parameters
        for field in schema.get("required", []):
            if field not in arguments:
                return False, f"Missing required parameter: {field}"
        for arg_name, arg_value in arguments.items():
            if arg_name not in schema.get("properties", {}):
                return False, f"Unexpected parameter: {arg_name}"
        return True, "OK"

    def execute(self, tool_name: str, arguments: dict) -> Any:
        self.call_counts[tool_name] += 1
        return self.tools[tool_name].fn(**arguments)


def websearch(query: str, search_depth: str = "basic", freshness: str | None = None) -> str:
    payload = {
        "query": query,
        "search_depth": search_depth,
        "max_results": 5,
        "include_answer": False,
    }
    if freshness is not None:
        payload["time_range"] = freshness

    api_key = os.getenv("TAVILY_KEY")
    if not api_key:
        raise ValueError("TAVILY_KEY not set")

    r = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=10,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Tavily search failed: HTTP {r.status_code}: {r.text}")

    data = r.json()
    results = data.get("results", [])
    trimmed = [
        {key: item.get(key) for key in ("title", "url", "content", "score")}
        for item in results
    ]
    return json.dumps(trimmed)


def resolve_safe_path(relative_path: str, vault_root: str | None = None) -> Path:
    """Resolve a path under WORKING_ROOT and reject anything that escapes it.
    If vault_root is provided and relative path is Obsidian-style (relative to vault),
    resolve to {vault_root}/{relative_path} instead of working dir.
    """
    # If it looks like an absolute path (starts with / or /), reject it
    if Path(relative_path).is_absolute():
        raise ValueError(f"Absolute paths not allowed: {relative_path}")
    
    if vault_root:
        resolved = (Path(vault_root) / relative_path).resolve()
        # Verify it's under the vault_root
        expected_resolved = (Path(vault_root) / relative_path).resolve()
        if resolved.is_relative_to(expected_resolved):
            return resolved
    
    # Fall back to working directory resolution
    resolved = (WORKING_ROOT / relative_path).resolve()
    if not resolved.is_relative_to(WORKING_ROOT.resolve()):
        raise ValueError(f"Path escapes working directory: {relative_path}")
    return resolved


def _cap(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def read_file(path: str, line_range: list[int] | None = None) -> str:
    resolved = resolve_safe_path(path)
    with open(resolved, encoding="utf-8") as f:
        lines = f.readlines()
    if line_range is not None:
        if len(line_range) != 2:
            raise ValueError("line_range must be [start_line, end_line]")
        start, end = line_range
        lines = lines[start - 1:end]
        first = start
    else:
        first = 1
    return "\n".join(f"{i:4d} | {line.rstrip()}" for i, line in enumerate(lines, start=first))


def write_file(path: str, content: str, overwrite: bool = False) -> str:
    resolved = resolve_safe_path(path)
    if resolved.exists() and not overwrite:
        raise FileExistsError(
            f"File already exists: {path} (use edit_file or set overwrite=True)")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"Wrote {len(content.encode('utf-8'))} bytes to {path}"


def edit_file(path: str, old_str: str, new_str: str = "") -> str:
    resolved = resolve_safe_path(path)
    content = resolved.read_text(encoding="utf-8")
    count = content.count(old_str)
    if count == 0:
        raise ValueError(f"old_str not found in {path}")
    if count > 1:
        raise ValueError(f"old_str appears {count} times in {path}; provide more context")
    content = content.replace(old_str, new_str, 1)
    resolved.write_text(content, encoding="utf-8")
    return f"Edited {path}: replaced 1 occurrence ({len(old_str)} chars -> {len(new_str)} chars)"


def run_python(code: str, timeout: int | None = None) -> str:
    timeout = timeout or 30
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=timeout)
    out = proc.stdout or ""
    if proc.stderr:
        out += f"\n[stderr]\n{proc.stderr}"
    if proc.returncode != 0:
        out += f"\n[exit code: {proc.returncode}]"
    return _cap(out.strip()) if out.strip() else "(no output)"


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = (html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"[ \t]+", " ", html).strip()


def fetch_url(url: str, max_length: int | None = None) -> str:
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} fetching {url}")
    text = _strip_html(r.text)
    return _cap(text, max_length or 4000)


def run_shell(command: str, cwd: str | None = None) -> str:
    args = shlex.split(command)
    if not args:
        raise ValueError("Empty command")
    # Working directory. A caller-supplied cwd goes through the path guard, which
    # rejects absolute paths. The fallbacks are already-rooted, trusted paths, so
    # they must NOT go through resolve_safe_path — it would reject them for being
    # absolute, and every default call would fail.
    vault_env = os.environ.get("VAULT_ROOT", "").strip()
    if cwd:
        work_dir = resolve_safe_path(cwd)
    elif vault_env:
        work_dir = Path(vault_env).resolve()
    else:
        work_dir = WORKING_ROOT

    if args[0] not in ALLOWED_SHELL_COMMANDS:
        return (f"Denied: '{args[0]}' is not in the allow list "
                f"({', '.join(sorted(ALLOWED_SHELL_COMMANDS))}). "
                f"Use 'bash' if you genuinely need it — that tool has no allow list.")
    proc = subprocess.run(args, cwd=work_dir, capture_output=True, text=True, timeout=60)
    out = proc.stdout or ""
    if proc.stderr:
        out += f"\n[stderr]\n{proc.stderr}"
    if proc.returncode != 0:
        out += f"\n[exit code: {proc.returncode}]"
    return _cap(out.strip()) if out.strip() else "(no output)"


def run_bash(command: str, cwd: str | None = None) -> str:
    """Run a real bash command (shell=True): pipelines, &&, redirects, globs, and
    compound commands all work. Unlike run_shell there is no allow-list gate or
    blocking prompt — the harness runs the command as-is. Use this for anything
    that needs actual shell features or falls outside run_shell's allow list."""
    if not command or not command.strip():
        raise ValueError("Empty command")
    work_dir = resolve_safe_path(cwd) if cwd else WORKING_ROOT
    proc = subprocess.run(command, shell=True, cwd=work_dir,
                          capture_output=True, text=True, timeout=120)
    out = proc.stdout or ""
    if proc.stderr:
        out += f"\n[stderr]\n{proc.stderr}"
    if proc.returncode != 0:
        out += f"\n[exit code: {proc.returncode}]"
    return _cap(out.strip()) if out.strip() else "(no output)"


def _format_matches(matches: list[tuple[str, int, str]], truncated: bool = False) -> str:
    lines = [f"{path}:{lineno}: {line}" for path, lineno, line in matches]
    if truncated:
        lines.append(f"...truncated at {MAX_SEARCH_MATCHES} matches")
    return "\n".join(lines) if lines else "(no matches)"


def search_files(pattern: str, path: str | None = None, file_glob: str | None = None, vault_root: str | None = None) -> str:
    root = resolve_safe_path(path, vault_root) if path else resolve_safe_path(".", vault_root)
    if not root.is_dir():
        raise ValueError(f"Not a directory: {path or WORKING_ROOT}")
    try:
        compiled = re.compile(pattern)
        use_regex = True
    except re.error:
        compiled = None
        use_regex = False
    matches = []
    for p in (root.rglob(file_glob) if file_glob else root.rglob("")):
        if not p.is_file():
            continue
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                for lineno, line in enumerate(f, 1):
                    hit = compiled.search(line) if use_regex else pattern in line
                    if hit:
                        # Path should be relative to vault_root for Obsidian-style paths
                        if vault_root:
                            rel = p.relative_to(Path(vault_root))
                        else:
                            rel = p.relative_to(WORKING_ROOT)
                        matches.append((str(rel), lineno, line.rstrip()))
                        if len(matches) >= MAX_SEARCH_MATCHES:
                            return _format_matches(matches, truncated=True)
        except (OSError, UnicodeDecodeError):
            continue
    return _format_matches(matches)


def memory_note(mode: str, key: str | None = None, content: str | None = None, vault_root: str | None = None) -> str:
    # Notes default to <working root>/memory. A caller-supplied vault_root still
    # goes through the path guard like every other path.
    notes_dir = resolve_safe_path("memory", vault_root) if vault_root else (WORKING_ROOT / "memory")
    if mode == "save":
        if not key or content is None:
            raise ValueError("save requires both 'key' and 'content'")
        notes_dir.mkdir(parents=True, exist_ok=True)
        (notes_dir / f"{key}.md").write_text(content, encoding="utf-8")
        return f"Saved note '{key}' to {notes_dir / f'{key}.md'}"
    if mode == "recall":
        if not key:
            raise ValueError("recall requires 'key'")
        note_path = notes_dir / f"{key}.md"
        if not note_path.exists():
            raise FileNotFoundError(f"No note found for key '{key}'")
        return note_path.read_text(encoding="utf-8")
    if mode == "list":
        if not notes_dir.exists():
            return "(no notes)"
        notes = sorted(p.stem for p in notes_dir.iterdir() if p.suffix == ".md")
        return "\n".join(notes) if notes else "(no notes)"
    raise ValueError(f"Unknown mode: {mode}")


def load_tools(tools_path: str = "tools.json",
               handlers: dict[str, Callable] | None = None) -> list[Tool]:
    handlers = handlers or {}
    with open(tools_path, encoding="utf-8") as f:
        data = json.load(f)
    tools = []
    for entry in data.get("tools", []):
        fn = entry.get("function", {})
        name = fn.get("name")
        handler = handlers.get(name)
        if handler is None:
            raise ValueError(f"No handler registered for tool '{name}'")
        tools.append(Tool(
            name=name,
            description=fn.get("description", ""),
            parameters=fn.get("parameters", {}),
            fn=handler,
        ))
    return tools




def load_subagents(agents_dir: str = AGENTS_DIR) -> dict:
    """Load sub-agent configs from agents/*.md files (frontmatter + body).

    Each file opens with YAML-style frontmatter between --- fences:
        name, description, tools (comma list), spawn (comma list, optional)
    The markdown body becomes the agent's system_prompt, so editing these
    files is how you tune sub-agent instructions.
    """
    agents: dict[str, dict] = {}
    agents_path = Path(agents_dir)
    if not agents_path.is_dir():
        return agents
    for md_file in sorted(agents_path.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        parts = text.split("\n---", 1)
        if len(parts) != 2:
            continue
        frontmatter, body = parts
        fields = {}
        for line in frontmatter.splitlines()[1:]:
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        name = fields.get("name")
        if not name:
            continue
        agents[name] = {
            "description": fields.get("description", ""),
            "system_prompt": body.strip(),
            "tools": [t.strip() for t in fields.get("tools", "").split(",") if t.strip()],
            "spawn": [s.strip() for s in fields.get("spawn", "").split(",") if s.strip()],
        }
    return agents


SUBAGENTS = load_subagents()


def load_skill_meta(skills_dir: str = SKILLS_DIR) -> list[dict]:
    """Parse skills/*/SKILL.md frontmatter into [{name, description, path}]."""
    meta: list[dict] = []
    skills_path = Path(skills_dir)
    if not skills_path.is_dir():
        return meta
    for skill in os.scandir(skills_path):
        for md_file in Path(skill).glob('**/*.*'):
            text = md_file.read_text(encoding="utf-8")
            if not text.startswith("---"):
                continue
            parts = text.split("\n---", 1)
            if len(parts) != 2:
                continue
            frontmatter, _body = parts
            fields = {}
            for line in frontmatter.splitlines()[1:]:
                if ":" in line:
                    key, _, value = line.partition(":")
                    fields[key.strip()] = value.strip()
            if fields.get("name") and fields.get("description"):
                meta.append({"name": fields["name"],
                             "description": fields["description"],
                             "path": str(md_file)})
    return meta


def load_skills(skills_dir: str = SKILLS_DIR) -> str:
    """The trigger index injected into the system prompt: one 'name: description' per skill."""
    return "\n".join(f"{s['name']}: {s['description']}"
                     for s in load_skill_meta(skills_dir)).strip()

SKILLS = load_skills()

'''
Creates agent DB with SQLite to track sessions and tool invocations and provide analytics
'''
class AgentState:
    def __init__(self, db_path: str = "agent_state.db", vault_root: str | None = None):
        self.vault_root = vault_root
        # The TUI runs harness.run() on a worker thread while AgentState is created
        # on the main thread. Only one turn runs at a time, so relaxing the
        # same-thread check is safe here.
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, created_at TEXT,
            last_active TEXT, user_id TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS tool_invocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_call_id TEXT,
            session_id TEXT, turn_number INTEGER,
            tool_name TEXT, arguments TEXT, result TEXT,
            success INTEGER, duration_ms INTEGER, timestamp TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, turn_number INTEGER,
            tool_name TEXT, role TEXT, content TEXT,
            tool_call_id TEXT, tool_calls TEXT, reasoning TEXT,
            timestamp TEXT)""")
        # Additive migration for DBs created before `reasoning` existed; the
        # INSERT below names its columns, so column order doesn't matter.
        self._ensure_column("messages", "reasoning", "TEXT")
        self.db.commit()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        
    def create_session(self, session_id: str, user_id: str):
        self.db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (session_id, dt.now(UTC).isoformat(), dt.now(UTC).isoformat(), user_id))
        self.db.commit()

    def record_tool_invocation(self, tool_call_id: str, session_id: str, turn: int,
                                tool: str, args: dict, result: str,
                                success: bool, duration_ms: int):
        self.db.execute(
            "INSERT INTO tool_invocations "
            "(tool_call_id, session_id, turn_number, tool_name, arguments, "
            "result, success, duration_ms, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tool_call_id, session_id, turn, tool, json.dumps(args), result,
             int(success), duration_ms, dt.now(UTC).isoformat()))
        self.db.commit()

    def record_message(self, session_id: str, turn: int,
                        role: str, content: str,
                        tool_name: str = "",
                        tool_call_id: str = "",
                        tool_calls: list | None = None,
                        reasoning: str = ""):
        self.db.execute(
            "INSERT INTO messages "
            "(session_id, turn_number, tool_name, role, content, "
            "tool_call_id, tool_calls, reasoning, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, turn, tool_name, role, content,
             tool_call_id, json.dumps(tool_calls) if tool_calls else None,
             reasoning or None, dt.now(UTC).isoformat()))
        self.db.commit()

    def get_messages(self, session_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT session_id, turn_number, tool_name, role, content, "
            "tool_call_id, tool_calls, reasoning FROM messages "
            "WHERE session_id = ? ORDER BY turn_number, message_id",
            (session_id,)).fetchall()
        messages = []
        for (_session_id, _turn_number, tool_name, role, content,
             tool_call_id, tool_calls, reasoning) in rows:
            msg = {"role": role, "content": content}
            if tool_calls:
                try:
                    msg["tool_calls"] = json.loads(tool_calls)
                except (json.JSONDecodeError, TypeError):
                    msg["tool_calls"] = []
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            if tool_name:
                msg["name"] = tool_name
            if reasoning:
                # DeepSeek rejects tool-carrying requests whose earlier turns
                # omit reasoning_content; kept so history can be replayed.
                msg["reasoning_content"] = reasoning
            messages.append(msg)
        return messages

    def get_analytics(self, session_id: str) -> dict:
        total = self.db.execute("SELECT COUNT(*) FROM tool_invocations WHERE session_id = ?", (session_id,)).fetchone()[0]
        rate = self.db.execute("SELECT AVG(success) FROM tool_invocations WHERE session_id = ?", (session_id,)).fetchone()[0] or 0
        return {"total_invocations": total, "success_rate": round(rate * 100, 1)}

    
class AgentHarness:
    def __init__(self, model: str | None = None, system_prompt: str = "",
                 event_bus=None, provider=None, wrapper=None):
        self.bus = event_bus
        self.provider = (provider if isinstance(provider, providers.Provider)
                         else providers.get_provider(provider))
        self.model = self.provider.resolve_model(model)
        self.wrapper = wrapper or Wrapper(self.model, self.provider)
        self.system_prompt = system_prompt
        self.tools: dict[str, Tool] = {}
        self.skills: str = SKILLS
        self.max_iterations = 100
        self.state = AgentState()
        self.session_id = str(uuid.uuid4())
        self.user_id = "local"
        self.state.create_session(self.session_id, self.user_id)
        # Graph store (README step 7): persistent nodes/edges across sessions.
        self.graph = GraphStore(GRAPH_DB_PATH)
        self.conversation_node = self.graph.upsert_node(
            "conversation", self.session_id,
            label=f"session {self.session_id[:8]}",
            properties={"user_id": self.user_id, "model": self.model,
                        "provider": self.provider.name})
        for skill in load_skill_meta():
            self.graph.upsert_node("skill", skill["name"], label=skill["name"],
                                   properties={"description": skill["description"]})
        self.memory_digest = self.graph.digest()
        self.turn = 0
        self.context = self.provider.resolve_context_length()
        self.usage = 0
        self.input = 0
        self.output = 0
        self.budgeter = BudgetEnforcer(self.context)
        self.messages: list[dict] = []
        self.keep_msg_count = 8

    # -- decoupled UX events (README step 13) ---------------------------
    def _emit(self, kind: str, **data) -> None:
        """Emit a state event to the attached EventBus (no-op without one)."""
        if self.bus is not None:
            try:
                self.bus.emit_kind(kind, **data)
            except Exception:
                pass

    def register_tool(self, tool: Tool):
        self.tools[tool.name] = tool
        try:
            self.graph.upsert_node("tool", tool.name, label=tool.name,
                                   properties={"description": (tool.description or "")[:400]})
        except Exception:
            pass

    # -- graph store (README step 7) ------------------------------------
    def _record_turn_graph(self, user_input: str):
        """Create the turn's task + user-message nodes, created_in this conversation."""
        try:
            task = self.graph.upsert_node(
                "task", f"{self.session_id}:{self.turn}",
                label=(user_input or "").strip()[:120] or "(empty)",
                properties={"turn": self.turn}, session_id=self.session_id)
            self.graph.touch_edge(task, self.conversation_node, "created_in",
                                  session_id=self.session_id)
            self._record_message_graph("user", user_input, 0)
            return task
        except Exception:
            return None

    def _record_message_graph(self, role: str, content: str, index: int) -> None:
        try:
            node = self.graph.upsert_node(
                "message", f"{self.session_id}:{self.turn}:{role}:{index}",
                label=role, properties={"role": role, "preview": (content or "")[:200]},
                session_id=self.session_id)
            self.graph.touch_edge(node, self.conversation_node, "created_in",
                                  session_id=self.session_id)
        except Exception:
            pass

    def _record_tool_graph(self, name: str, duration_ms: int, success: bool,
                           task_node=None) -> None:
        try:
            node = self.graph.upsert_node("tool", name, label=name)
            inc = {"count": 1, "last_ms": duration_ms}
            self.graph.touch_edge(node, self.conversation_node, "used_in",
                                  session_id=self.session_id, increments=inc)
            self.graph.touch_edge(node, self.conversation_node,
                                  "succeeded" if success else "failed",
                                  session_id=self.session_id, increments=inc)
            if task_node is not None:
                self.graph.touch_edge(task_node, node, "used_in",
                                      session_id=self.session_id, increments=inc)
        except Exception:
            pass

    def graph_query(self, kind: str = "summary", name: str = "", limit: int = 10) -> str:
        """Query the cross-session knowledge graph (see graph_store.GraphStore.query)."""
        try:
            return self.graph.query(kind=kind, name=name, limit=int(limit or 10))
        except Exception as e:
            return f"graph query failed: {type(e).__name__}: {e}"

    # -- runtime switching (the /provider and /model commands) -----------
    def status_line(self) -> str:
        """One line describing the backend the next turn will talk to."""
        return (f"{self.provider.label} · {self.model} · "
                f"{self.context:,} ctx · auth={self.provider.api_key_env}")

    def available_providers(self) -> list[str]:
        return list(providers.PROVIDER_NAMES)

    def available_models(self, timeout: float = 5.0) -> list[str]:
        """Model ids the current provider advertises (best-effort)."""
        return self.provider.list_models(timeout)

    def _sync_conversation_node(self) -> None:
        """Keep the graph's conversation node describing the current backend."""
        try:
            self.graph.upsert_node(
                "conversation", self.session_id,
                label=f"session {self.session_id[:8]}",
                properties={"user_id": self.user_id, "model": self.model,
                            "provider": self.provider.name})
        except Exception:
            pass

    def switch_provider(self, name: str | None = None, model: str | None = None) -> str:
        """Point the harness at another backend, in place.

        Only the endpoint, auth and thinking dialect change: the tool registry,
        session id, message history, budget counters and graph all survive, so
        the next turn simply talks to the new provider. Switching mid-session is
        safe — the graph replay rules differ per provider (providers.py), and the
        wrapper applies the new provider's dialect on the very next request.
        """
        provider = providers.get_provider(name)
        self.provider = provider
        self.model = provider.resolve_model(model)
        self.wrapper.provider = provider
        self.wrapper.model = self.model
        self.context = provider.resolve_context_length()
        self.budgeter.budget = self.context
        self._sync_conversation_node()
        return self.status_line()

    def switch_model(self, model: str) -> str:
        """Switch model within the current provider (endpoint/auth unchanged)."""
        model = (model or "").strip()
        if not model:
            raise ValueError("no model given")
        self.model = model
        self.wrapper.model = model
        self._sync_conversation_node()
        return self.status_line()

    def tool_list(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": t.name, "description": t.description,
                "parameters": t.parameters,
            }}
            for t in self.tools.values()
        ]

    def add(self, role: str, content: str, **kwargs):
        self.messages.append({"role": role, "content": content, **kwargs})

    def get_messages(self) -> list[dict]:
        # budgeter.tokens_used = the last context size the API reported (usage.prompt_tokens)
        if self.budgeter.tokens_used >= int(self.context * CONTEXT_COMPRESS_THRESHOLD):
            return self._compress()
        return list(self.messages)

    def _compress(self) -> list[dict]:
        keep = self.keep_msg_count
        sys_msgs = [m for m in self.messages if m.get("role") == "system"]
        system_msg = sys_msgs[0] if sys_msgs else None
        others = [m for m in self.messages if m.get("role") != "system"]
        recent = others[-keep:]
        old = others[:-keep]
        if not old:
            return [system_msg] + recent if system_msg else list(recent)
        lines = []
        for m in old:
            content = (m.get("content") or "").replace("\n", " ").strip()
            label = f"[{m.get('role')}]"
            if m.get("role") == "tool":
                label += f":{m.get('name', 'tool')}"
            lines.append(f"{label}: {content[:200]}")
        summary_text = "EARLIER CONTEXT: " + " | ".join(lines)[:1500]
        if system_msg:
            merged = dict(system_msg)
            merged["content"] = f"{system_msg.get('content', '')}\n\n{summary_text}"
            return [merged] + recent
        return [{"role": "system", "content": summary_text}] + recent

    def run(self, user_input: str, root_dir: str | None = None) -> str:
        # Use root_dir if provided, otherwise fallback to vault_root from AgentState
        effective_vault_root = root_dir or getattr(self.state, "vault_root", "pleiades")
        self.vault_root = effective_vault_root
        
        self.turn += 1
        self._emit("user", content=user_input)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "system", "content": self.skills},
        ]
        if getattr(self, "memory_digest", ""):
            messages.append({"role": "system",
                             "content": "CROSS-SESSION MEMORY (graph store)\n" + self.memory_digest})
        history = self.state.get_messages(self.session_id)
        if not self.provider.reasoning_replay:
            # mlx-serve doesn't want prior chain-of-thought echoed back into the
            # prompt (it would only burn context); DeepSeek requires it.
            for past in history:
                past.pop("reasoning_content", None)
        messages += [
            *history,
            {"role": "user", "content": user_input},
        ]
        self.state.record_message(self.session_id, self.turn, "user", user_input)
        task_node = self._record_turn_graph(user_input)
        for i in range(self.max_iterations):
            self._emit("thinking", iteration=i)
            streamed = {"tokens": 0}
            streamed_reasoning = {"tokens": 0}
            if self.bus is not None:
                def _on_token(text, _s=streamed):
                    _s["tokens"] += 1
                    self._emit("token", text=text)
                def _on_reasoning(text, _s=streamed_reasoning):
                    _s["tokens"] += 1
                    self._emit("reasoning", text=text)
                response = self.wrapper.chat_streamed(
                    messages=messages,
                    tools=self.tool_list() if self.tools else None,
                    on_token=_on_token,
                    on_reasoning=_on_reasoning,
                )
            else:
                response = self.wrapper.chat(
                    messages=messages, tools=self.tool_list() if self.tools else None,
                )
            logger.info(response)
            reasoning = response.message.get("reasoning_content", "") if isinstance(response.message, dict) else ""
            logger.info("[reasoning]" + reasoning)
            logger.info("[response]" + response.content)
            if self.bus is not None and reasoning and streamed_reasoning["tokens"] == 0:
                # Non-stream fallback: surface reasoning that never streamed.
                self._emit("reasoning", text=reasoning)
            self.state.record_message(
                self.session_id, self.turn, "assistant", response.content or "",
                tool_calls=response.message.get("tool_calls") if isinstance(response.message, dict) else None,
                reasoning=reasoning,
            )
            if response.content:
                self._record_message_graph("assistant", response.content, i)
            self.input += response.stats.get("prompt_tokens", 0)
            self.output += response.stats.get("completion_tokens", 0)
            self.budgeter.tokens_used = response.stats.get("prompt_tokens", 0)
            self.usage = response.stats.get("prompt_tokens", 0)
            if self.bus is not None and response.content and streamed["tokens"] == 0:
                self._emit("message", content=response.content)
            if not response.tool_calls:
                self._emit("done", content=response.content or "")
                return response.content
            if not self.provider.reasoning_replay and isinstance(response.message, dict):
                response.message.pop("reasoning_content", None)
            messages.append(response.message)
            for call in response.tool_calls:
                self._emit("tool_call", call_id=str(call.call_id), name=call.name, args=call.args)
                tool = self.tools.get(call.name)
                start = time.monotonic()
                if not tool:
                    result = f"Error: Unknown tool '{call.name}'"
                    success = False
                else:
                    try:
                        result = tool.fn(**call.args)
                        success = True
                    except Exception as e:
                        result = f"Error: {type(e).__name__}: {e}"
                        success = False
                duration_ms = int((time.monotonic() - start) * 1000)
                self.state.record_tool_invocation(
                    str(call.call_id), self.session_id, self.turn, call.name, call.args,
                    str(result), success, duration_ms)
                self._record_tool_graph(call.name, duration_ms, success, task_node)
                self.state.record_message(
                    self.session_id, self.turn, "tool", str(result),
                    tool_name=call.name, tool_call_id=str(call.call_id))
                self._emit("tool_result", call_id=str(call.call_id), name=call.name,
                           success=success, duration_ms=duration_ms,
                           preview=str(result)[:600] if result else "")
                messages.append({"role": "tool", "content": str(result), "tool_call_id": call.call_id})
        self._emit("done", content="Max iterations reached.")
        return "Max iterations reached."

    def run_subagent(self, agent: str, task: str, _from: str | None = None, _depth: int = 0) -> str:
        if agent not in SUBAGENTS:
            return f"Error: Unknown subagent '{agent}'. Available: {', '.join(SUBAGENTS)}"
        if _from is not None and agent not in SUBAGENTS[_from]["spawn"]:
            return f"Error: '{_from}' may not spawn '{agent}'"
        if _depth >= SUBAGENT_MAX_DEPTH:
            return "Subagent depth limit reached."
        cfg = SUBAGENTS[agent]
        allowed = {name: t for name, t in self.tools.items() if name in cfg["tools"]}
        schemas = [
            {"type": "function", "function": {
                "name": t.name, "description": t.description,
                "parameters": t.parameters,
            }}
            for t in allowed.values()
        ]
        messages = [
            {"role": "system", "content": cfg["system_prompt"]},
            {"role": "user", "content": task},
        ]
        for _ in range(self.max_iterations):
            response = self.wrapper.chat(messages=messages, tools=schemas or None)
            logger.info(response)
            if not response.tool_calls:
                return response.content or "(no output)"
            messages.append(response.message)
            for call in response.tool_calls:
                if call.name == "subagent":
                    result = self.run_subagent(
                        agent=call.args.get("agent", ""), task=call.args.get("task", ""),
                        _from=agent, _depth=_depth + 1)
                else:
                    tool = allowed.get(call.name)
                    if not tool:
                        result = f"Error: tool '{call.name}' not available to '{agent}'"
                    else:
                        try:
                            result = tool.fn(**call.args)
                        except Exception as e:
                            result = f"Error: {type(e).__name__}: {e}"
                messages.append({"role": "tool", "content": str(result), "tool_call_id": call.call_id})
        return "Subagent max iterations reached."

def _iter_sse_json(lines):
    """Yield parsed JSON objects from an OpenAI-style SSE chat stream."""
    for raw in lines:
        text = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else (raw or "").strip()
        if not text.startswith("data:"):
            continue
        payload = text[len("data:"):].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def _apply_stream_event(acc: dict, event: dict) -> tuple[list[str], list[str]]:
    """Fold one chat.completion.chunk into acc; return the (content, reasoning)
    fragments it produced so callers can emit live 'token' events."""
    content_tokens: list[str] = []
    reasoning_tokens: list[str] = []
    choices = event.get("choices") or []
    choice0 = choices[0] if choices else {}
    delta = choice0.get("delta") or {}
    if delta.get("content"):
        acc["content"].append(delta["content"])
        content_tokens.append(delta["content"])
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    if reasoning:
        acc["reasoning"].append(reasoning)
        reasoning_tokens.append(reasoning)
    for tc in delta.get("tool_calls") or []:
        idx = tc.get("index", len(acc["tool_calls"]))
        while len(acc["tool_calls"]) <= idx:
            acc["tool_calls"].append({"id": None, "type": "function",
                                      "function": {"name": "", "arguments": ""}})
        slot = acc["tool_calls"][idx]
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]
    if choice0.get("finish_reason"):
        acc["finish"] = choice0["finish_reason"]
    if event.get("usage"):
        acc["usage"] = event["usage"]
    return content_tokens, reasoning_tokens


def _safe_json(text: str) -> dict:
    try:
        parsed = json.loads(text or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _estimate_tokens(*texts: str) -> int:
    """Rough char/4 token estimate, used only when a stream omits usage."""
    total = 0
    for text in texts:
        if text:
            total += max(1, len(text) // 4)
    return total


class Wrapper:
    """OpenAI-compatible chat client for the active :class:`providers.Provider`.

    The provider owns the endpoint, the auth env var, and the backend-specific
    thinking flags (see providers.py); this class only speaks the wire format.
    """

    def __init__(self, model, provider=None):
        self.model = model
        self.provider = provider or providers.get_provider()
        self.tool_id = 1

    def _headers(self) -> dict | None:
        """Auth headers, or None when the provider's API key is missing."""
        try:
            return self.provider.headers()
        except KeyError:
            return None

    def _missing_key(self) -> LLMResponse:
        return self._error_response(
            f"{self.provider.api_key_env} is not set — add it to .env to use the "
            f"'{self.provider.name}' provider ({self.provider.label}).")

    def chat(self, messages: list[dict], tools: list[dict] = None, skills: dict = None,
             _allow_thinking: bool = True) -> LLMResponse:
        headers = self._headers()
        if headers is None:
            return self._missing_key()
        thinking = self.provider.thinking_params() if _allow_thinking else {}
        payload = {"model": self.model, "messages": messages, "tools": tools}
        payload.update(thinking)
        try:
            r = requests.post(self.provider.chat_url, headers=headers,
                              json=payload, timeout=(15, 600))
        except requests.exceptions.RequestException as e:
            return self._error_response(f"Request to LLM server failed: {e}")
        if r.status_code == 400 and thinking:
            # Server rejects our thinking flags: retry once without them.
            return self.chat(messages, tools, skills, _allow_thinking=False)
        return self.parse_response(r)

    def chat_streamed(self, messages: list[dict], tools: list[dict] = None,
                      on_token=None, on_reasoning=None,
                      _allow_thinking: bool = True) -> LLMResponse:
        """SSE streaming chat. Delivers content tokens to on_token and reasoning
        tokens (``reasoning_content`` — mlx-serve and DeepSeek both use that
        field) to on_reasoning as they arrive, and returns the SAME LLMResponse
        shape as chat(). Thinking is requested with the active provider's dialect
        (``enable_thinking`` for mlx-serve, ``thinking``/``reasoning_effort`` for
        DeepSeek); if the server rejects those (HTTP 400) it retries once without
        them. Falls back to chat() when the server doesn't actually stream
        (non-SSE body), or when the request fails before any data arrives."""
        headers = self._headers()
        if headers is None:
            return self._missing_key()
        thinking = self.provider.thinking_params() if _allow_thinking else {}
        payload = {"model": self.model, "messages": messages, "tools": tools,
                   "stream": True}
        payload.update(self.provider.stream_usage_param())
        payload.update(thinking)
        try:
            r = requests.post(
                self.provider.chat_url,
                headers=headers,
                json=payload, stream=True, timeout=(15, 600),
            )
        except requests.exceptions.RequestException as e:
            return self._error_response(f"Request to LLM server failed: {e}")
        if r.status_code != 200:
            if r.status_code == 400 and thinking:
                return self.chat_streamed(messages, tools, on_token, on_reasoning,
                                          _allow_thinking=False)
            return self.parse_response(r)

        acc = {"content": [], "reasoning": [], "tool_calls": [], "usage": {}, "finish": None}
        saw = False
        try:
            for ev in _iter_sse_json(r.iter_lines()):
                saw = True
                content_tokens, reasoning_tokens = _apply_stream_event(acc, ev)
                if on_token is not None:
                    for t in content_tokens:
                        on_token(t)
                if on_reasoning is not None:
                    for t in reasoning_tokens:
                        on_reasoning(t)
        except requests.exceptions.RequestException as e:
            return self._error_response(f"Request to LLM server failed mid-stream: {e}")
        if not saw:
            # Server ignored stream=True: body is one JSON blob, not SSE.
            return self.chat(messages, tools)

        content = "".join(acc["content"])
        reasoning = "".join(acc["reasoning"])
        usage = dict(acc["usage"] or {})
        if not usage.get("prompt_tokens"):
            usage["prompt_tokens"] = _estimate_tokens(json.dumps(messages, default=str))
        if not usage.get("completion_tokens"):
            usage["completion_tokens"] = _estimate_tokens(content)
        stats = {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens",
                                      usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)),
        }
        calls = []
        for tc in acc["tool_calls"]:
            name = tc["function"].get("name", "")
            if not tc.get("id") or not name:
                continue
            call_id = tc.get("id")
            if not call_id:
                call_id = self.tool_id
                self.tool_id += 1
            calls.append(ToolCall(
                call_id=call_id, name=name,
                args=_safe_json(tc["function"].get("arguments", "")),
                output="", error="", provider_info={},
            ))
        message = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        if calls:
            message["content"] = content or None
            message["tool_calls"] = [
                {"id": c.call_id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                for c in calls
            ]
        return LLMResponse(
            content=content, tool_calls=calls, message=message,
            response_id="stream", stats=stats, output=[],
        )

    def _error_response(self, message: str) -> LLMResponse:
        return LLMResponse(
            content=message,
            tool_calls=[],
            message={},
            response_id="",
            stats={},
            output=[],
        )

    def parse_response(self, response: requests.Response) -> LLMResponse:
        if response.status_code != 200:
            return self._error_response(f"HTTP {response.status_code}: {response.text}")
        try:
            data = response.json()
        except ValueError:
            return self._error_response(f"Invalid JSON response from LLM server: {response.text[:300]}")
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        tools = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            call_id = tc.get("id")
            if not call_id:
                call_id = self.tool_id
                self.tool_id += 1
            tools.append(ToolCall(
                call_id=call_id,
                name=fn.get("name", ""),
                args=args,
                output="",
                error="",
                provider_info={},
            ))
        return LLMResponse(
            content=content,
            tool_calls=tools,
            message=message,
            response_id=data.get("id", ""),
            stats=data.get("usage", {}),
            output=[],
        )


def _build_harness(event_bus=None, provider=None, model=None):
    """Construct the harness with tools + logging (shared by REPL and TUI)."""
    iso_time = dt.now().isoformat()
    a = AgentHarness(model, SYS_PROMPT, event_bus=event_bus, provider=provider)
    handlers = {
        "websearch": websearch,
        "read_file": read_file,
        "write_file": write_file,
        "edit_file": edit_file,
        "run_python": run_python,
        "fetch_url": fetch_url,
        "run_shell": run_shell,
        "search_files": search_files,
        "bash": run_bash,
        "memory_note": memory_note,
        "graph_query": a.graph_query,
        "subagent": a.run_subagent,
    }
    for tool in load_tools("tools.json", handlers):
        a.register_tool(tool)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.FileHandler(LOG_DIR + "/" + iso_time + "_agent_run.log", mode="w")],
    )
    return a


def _run_repl(harness):
    """Original plain-text REPL (default)."""
    print(logo)
    print(f"  {harness.status_line()}")
    while True:
        print("-" * 50)
        try:
            user_in = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        logger.info(user_in)
        cmd, _, arg = user_in.strip().partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ('/stop', '/s'):
            break
        if cmd in ('/graph', '/g'):
            print(harness.graph.query("summary"))
            continue
        if cmd in ('/provider', '/p'):
            if not arg:
                print(harness.status_line())
                print("available: " + ", ".join(harness.available_providers()))
                continue
            try:
                print("switched -> " + harness.switch_provider(arg))
            except ValueError as e:
                print(f"error: {e}")
            continue
        if cmd in ('/model', '/m'):
            if not arg:
                print(f"model: {harness.model}")
                models = harness.available_models()
                if models:
                    print("available: " + ", ".join(models))
                continue
            try:
                print("switched -> " + harness.switch_model(arg))
            except ValueError as e:
                print(f"error: {e}")
            continue
        response = harness.run(user_in)
        print(response)
        print("-" * 50)
        print(f"In {harness.input} | Out {harness.output}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="pleiades",
        description="PLEIADES agent harness — plain REPL or animated TUI.",
    )
    parser.add_argument("--tui", action="store_true",
                        help="run the animated PLEIADES TUI instead of the plain REPL")
    parser.add_argument("--provider", choices=list(providers.PROVIDER_NAMES), default=None,
                        help="LLM backend: 'local' (mlx-serve on the LAN) or 'deepseek' "
                             "(api.deepseek.com). Defaults to $PLEIADES_PROVIDER, else 'local'.")
    parser.add_argument("--model", default=None,
                        help="Override the model name for the chosen provider "
                             "(defaults to $LLM_MODEL / $DEEPSEEK_MODEL, else the provider default).")
    args = parser.parse_args()

    try:
        if args.tui:
            try:
                from tui import run_tui
            except ImportError:
                print("The TUI needs 'rich'. Install it with:  pip install rich")
                raise SystemExit(1)
            try:
                run_tui(provider=args.provider, model=args.model)
            except KeyboardInterrupt:
                print()
            raise SystemExit(0)

        _run_repl(_build_harness(provider=args.provider, model=args.model))
    except ValueError as e:  # bad provider name, unknown model, etc.
        print(f"error: {e}")
        raise SystemExit(2)
