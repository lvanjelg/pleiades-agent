import json
import tiktoken
import requests
import os
import logging
import uuid
from dotenv import load_dotenv, dotenv_values 
from typing import Callable, Any
from dataclasses import dataclass, field
import time
import sqlite3
from datetime import datetime as dt, UTC
from enum import Enum
load_dotenv() 
logger = logging.getLogger(__name__)
LOG_DIR = "logs/"

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

@dataclass
class MemoryConfig:
    max_context_tokens: int = 64_000
    keep_recent_messages: int = 8
    always_preserve_system: bool = True

@dataclass
class BudgetConfig:
    max_tokens: int = 30_000
    max_tool_calls: int = 25
    max_time_seconds: float = 300.0
    max_per_tool_calls: int = 5

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
    def __init__(self, config: BudgetConfig):
        self.config = config
        self.tokens_used = 0
        self.tool_calls_total = 0
        self.tool_calls_per_tool: dict[str, int] = {}
        self.start_time = time.time()

    def record_tokens(self, input_tokens: int, output_tokens: int):
        self.tokens_used += input_tokens + output_tokens

    def record_tool_call(self, tool_name: str):
        self.tool_calls_total += 1
        self.tool_calls_per_tool[tool_name] = self.tool_calls_per_tool.get(tool_name, 0) + 1

    def check(self) -> str | None:
        if self.tokens_used >= self.config.max_tokens:
            return f"Token budget exceeded: {self.tokens_used} (limit {self.config.max_tokens})"
        if self.tool_calls_total >= self.config.max_tool_calls:
            return f"Tool call budget exceeded: {self.tool_calls_total}"
        if time.time() - self.start_time >= self.config.max_time_seconds:
            return "Time budget exceeded"
        for tool, count in self.tool_calls_per_tool.items():
            if count >= self.config.max_per_tool_calls:
                return f"Per-tool limit: '{tool}' called {count} times"
        return None


class AgentMemory:
    def __init__(self, config: MemoryConfig):
        self.config = config
        self.messages: list[dict] = []
        self.encoder = tiktoken.encoding_for_model("gpt-5-")

    def add(self, role: str, content: str, **kwargs):
        self.messages.append({"role": role, "content": content, **kwargs})

    def get_messages(self) -> list[dict]:
        total = sum(len(self.encoder.encode(m.get("content", ""))) + 4 for m in self.messages)
        if total <= self.config.max_context_tokens:
            return self.messages
        return self._compress()

    def _compress(self) -> list[dict]:
        keep = self.config.keep_recent_messages
        system_msg = None
        if self.config.always_preserve_system:
            system_msgs = [m for m in self.messages if m["role"] == "system"]
            if system_msgs:
                system_msg = system_msgs[0]
        recent = self.messages[-keep:]
        old = self.messages[:-keep]
        if not old:
            return [system_msg] + recent if system_msg else recent
        # Summarize old messages (in production, call a cheap model like Haiku)
        old_text = "\n".join(f"[{m['role']}]: {m.get('content', '')[:200]}" for m in old)
        summary = " | ".join([line[:100] for line in old_text.split("\n") if any(kw in line.lower() for kw in ["tool:", "result:", "error:"])][:10])
        compressed = [{"role": "system", "content": f"[EARLIER CONTEXT: {summary}]"}]
        if system_msg:
            compressed = [system_msg] + compressed
        compressed.extend(recent)
        return compressed

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


    
'''
Creates agent DB with SQLite to track sessions and tool invocations and provide analytics
'''
class AgentState:
    def __init__(self, db_path: str = "agent_state.db"):
        self.db = sqlite3.connect(db_path)
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
            tool_call_id TEXT, tool_calls TEXT,
            timestamp TEXT)""")
        self.db.commit()
        
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
                        tool_calls: list | None = None):
        self.db.execute(
            "INSERT INTO messages VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, turn, tool_name, role, content,
             tool_call_id, json.dumps(tool_calls) if tool_calls else None,
             dt.now(UTC).isoformat()))
        self.db.commit()

    def get_messages(self, session_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT session_id, turn_number, tool_name, role, content, "
            "tool_call_id, tool_calls FROM messages "
            "WHERE session_id = ? ORDER BY turn_number, message_id",
            (session_id,)).fetchall()
        messages = []
        for _session_id, _turn_number, tool_name, role, content, tool_call_id, tool_calls in rows:
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
            messages.append(msg)
        return messages

    def get_analytics(self, session_id: str) -> dict:
        total = self.db.execute("SELECT COUNT(*) FROM tool_invocations WHERE session_id = ?", (session_id,)).fetchone()[0]
        rate = self.db.execute("SELECT AVG(success) FROM tool_invocations WHERE session_id = ?", (session_id,)).fetchone()[0] or 0
        return {"total_invocations": total, "success_rate": round(rate * 100, 1)}

    
class AgentHarness:
    def __init__(self, model, system_prompt: str = ""):
        self.model = model
        self.wrapper = Wrapper(model)
        self.system_prompt = system_prompt
        self.tools: dict[str, Tool] = {}
        self.max_iterations = 100
        self.state = AgentState()
        self.session_id = str(uuid.uuid4())
        self.user_id = "local"
        self.state.create_session(self.session_id, self.user_id)
        self.turn = 0

    def register_tool(self, tool: Tool):
        self.tools[tool.name] = tool

    def tool_list(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": t.name, "description": t.description,
                "parameters": t.parameters,
            }}
            for t in self.tools.values()
        ]

    def run(self, user_input: str) -> str:
        self.turn += 1
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]
        self.state.record_message(self.session_id, self.turn, "user", user_input)
        for i in range(self.max_iterations):
            response = self.wrapper.chat(
                messages=messages, tools=self.tool_list() if self.tools else None,
            )
            logger.info(response)
            reasoning = response.message.get("reasoning_content", "") if isinstance(response.message, dict) else ""
            logger.info("[reasoning]" + reasoning)
            logger.info("[response]" + response.content)
            self.state.record_message(
                self.session_id, self.turn, "assistant", response.content or "",
                tool_calls=response.message.get("tool_calls") if isinstance(response.message, dict) else None,
            )
            if not response.tool_calls:
                return response.content
            messages.append(response.message)
            for call in response.tool_calls:
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
                self.state.record_message(
                    self.session_id, self.turn, "tool", str(result),
                    tool_name=call.name, tool_call_id=str(call.call_id))
                messages.append({"role": "tool", "content": str(result), "tool_call_id": call.call_id})
        return "Max iterations reached."

'''
Wrapper.chat(messages, tools) → HTTP POST → local LLM → parse response → return object with .content / .tool_calls / .message
'''

class Wrapper:
    def __init__(self, model):
        self.model = model
        self.tool_id = 1

    def chat(self, messages: list[dict], tools: list[dict] = None) -> LLMResponse:
        try:
            r = requests.post("http://192.168.1.92:1234/v1/chat/completions", 
                headers={"authorization" : "Bearer " + os.getenv("API_KEY"),},
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                },
                timeout=300)
        except requests.exceptions.RequestException as e:
            return self._error_response(f"Request to LLM server failed: {e}")
        return self.parse_response(r)

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


if __name__ == "__main__":
    print(logo)
    iso_time = dt.now().isoformat()
    a = AgentHarness("qwen/qwen3.8-27b", SYS_PROMPT)
    for tool in load_tools("tools.json", {"websearch": websearch}):
        a.register_tool(tool)
    logging.basicConfig(level=logging.INFO,handlers=[logging.FileHandler(LOG_DIR + "/" + iso_time + "_agent_run.log", mode="w")],)
    while True:
        print("-"*50)
        user_in = input("> ")
        logger.info(user_in)
        if user_in == '/stop' or user_in == '/s':
            break
        response = a.run(user_in)
        print(response)