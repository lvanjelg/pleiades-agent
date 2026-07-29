import json
import tiktoken
import requests
import os
import logging
from dotenv import load_dotenv, dotenv_values 
from typing import Callable, Any
from dataclasses import dataclass, field
import time
import sqlite3
from datetime import datetime, UTC
from enum import Enum
load_dotenv() 

LOG_DIR = "logs/"

logo = r"""
██████╗ ██╗     ███████╗██╗ █████╗ ██████╗ ███████╗███████╗
██╔══██╗██║     ██╔════╝██║██╔══██╗██╔══██╗██╔════╝██╔════╝
██████╔╝██║     █████╗  ██║███████║██║  ██║█████╗  ███████╗
██╔═══╝ ██║     ██╔══╝  ██║██╔══██║██║  ██║██╔══╝  ╚════██║
██║     ███████╗███████╗██║██║  ██║██████╔╝███████╗███████║
╚═╝     ╚══════╝╚══════╝╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝╚══════╝
                                                           """





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
            session_id TEXT, turn_number INTEGER,
            tool_name TEXT, arguments TEXT, result TEXT,
            success INTEGER, duration_ms INTEGER, timestamp TEXT)""")
        self.db.commit()

    def create_session(self, session_id: str, user_id: str):
        self.db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (session_id, datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(), user_id))
        self.db.commit()

    def record_tool_invocation(self, session_id: str, turn: int,
                                tool: str, args: dict, result: str,
                                success: bool, duration_ms: int):
        self.db.execute(
            "INSERT INTO tool_invocations VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, turn, tool, json.dumps(args), result,
             int(success), duration_ms, datetime.now(UTC).isoformat()))
        self.db.commit()

    def get_analytics(self, session_id: str) -> dict:
        total = self.db.execute("SELECT COUNT(*) FROM tool_invocations WHERE session_id = ?", (session_id,)).fetchone()[0]
        rate = self.db.execute("SELECT AVG(success) FROM tool_invocations WHERE session_id = ?", (session_id,)).fetchone()[0] or 0
        return {"total_invocations": total, "success_rate": round(rate * 100, 1)}

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
    
class AgentHarness:
    def __init__(self, model, system_prompt: str = ""):
        self.model = model
        self.wrapper = Wrapper(model)
        self.system_prompt = system_prompt
        self.tools: dict[str, Tool] = {}
        self.max_iterations = 10

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
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]
        for i in range(self.max_iterations):
            response = self.wrapper.chat(
                messages=messages, tools=self.tool_list() if self.tools else None,
            )
            if not response.tool_calls:
                return response.message
            messages.append(response.message)
            for call in response.tool_calls:
                tool = self.tools.get(call.function.name)
                if not tool:
                    result = f"Error: Unknown tool '{call.function.name}'"
                else:
                    try:
                        args = json.loads(call.function.arguments)
                        result = tool.fn(**args)
                    except Exception as e:
                        result = f"Error: {type(e).__name__}: {e}"
                messages.append({"role": "tool", "content": str(result), "tool_call_id": call.id})
        return "Max iterations reached."

'''
Wrapper.chat(messages, tools) → HTTP POST → local LLM → parse response → return object with .content / .tool_calls / .message
'''

class Wrapper:
    def __init__(self, model):
        self.model = model

    def chat(self, messages: list[dict], tools: list[dict] = None) -> LLMResponse:
        base_prompt = "You are a helpful assistant. Answer the user's question based on your knowlddge in a concise manner. End the conversation with <end_of_conversation> token."
        r = requests.post("http://localhost:1234/api/v1/chat", 
            headers={"authorization" : "Bearer " + os.getenv("API_KEY"),},
            json={
                "model": self.model,
                "system_prompt": messages[0].get('content'),
                "input": messages[1].get('content'),
                "integrations": ["mcp/duck-duck-go-search"],
            })
        return self.parse_response(r)
    def parse_response(self, response: requests.Response) -> dict:
        if response.status_code != 200:
            return {"error": f"HTTP {response.status_code}: {response.text}"}
        data = response.json()
        output = data.get("output")
        tools = []
        reason = ""
        msg = ""
        for i in range(len(output)):
            if output[i].get("type") == "reasoning":
                reason += output[i].get("content")
            elif output[i].get("type") == "message":
                msg += output[i].get("content")
            elif output[i].get("type") == "tool_call":
                tools.append([output[i].get("tool",""),output[i].get("arguments",""),output[i].get("output",""),output[i].get("provider_info","")])
        return LLMResponse(
            content=reason,
            tool_calls=tools,
            message=msg,
            response_id=data.get("response_id", ""),
            stats=data.get("stats", ""),
            output=output,
        )


if __name__ == "__main__":
    print(logo)
    while True:
        print("-"*50)
        user_in = input("> ")
        a = AgentHarness("qwen/qwen3.5-9b")
        print(a.run(user_in))