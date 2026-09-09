"""PLEIADES agent TUI — a decoupled frontend for the agent harness (README step 13).

The harness (core) never renders: it emits :class:`ux.Event` objects into an
:class:`ux.EventBus`, and this frontend reacts purely from event kind. It never
needs to know *why* the model is doing something.

Display model (live preview -> full scrollback):

- While a turn runs, a **live panel** shows the turn streaming in. The live
  preview is **bottom-anchored**: it never grows taller than the terminal, and
  the newest lines (the streaming tail, the active tool, the status line)
  always sit at the bottom edge, so you are never "stuck" above the action.
  Scrolling is limited *during* output, which is expected.
- When the turn finishes, the live preview is dismissed and the **full turn is
  printed to the terminal's normal scrollback** as one box. From that moment
  you can scroll and read every line of every finished turn. Nothing is hidden
  or trimmed.

Architecture
------------
- The harness runs on a **worker thread** per user turn; it emits events into
  the EventBus.
- This UI owns the terminal on the **main thread**: it drains queued events and
  redraws the live panel.
- Transport is pluggable: today it is the in-process EventBus; the same events
  could arrive over SSE/WebSocket later without touching the core loop.

Run with ``python main.py --tui`` (requires ``rich``).
"""
from __future__ import annotations

import json
import queue
import threading
import time
from typing import Optional

try:  # enables arrow-key history for input()
    import readline  # noqa: F401
except Exception:  # pragma: no cover - not available everywhere
    pass

from rich import box
from rich.console import Console, ConsoleOptions, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.segment import Segment
from rich.spinner import Spinner
from rich.text import Text

from ux import Event, EventBus, EventKind

PREVIEW_MAX = 240   # tool result preview
ARGS_MAX = 120      # tool call args summary
TURN_BORDER = "green"


def _preview(text: str, limit: int = PREVIEW_MAX) -> str:
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _args_summary(args) -> str:
    if args is None:
        return ""
    try:
        return _preview(json.dumps(args, ensure_ascii=False), ARGS_MAX)
    except Exception:
        return str(args)[:ARGS_MAX]


class _LineGroup:
    """Renders pre-measured segment-lines (the live preview's tail)."""

    def __init__(self, lines):
        self.lines = lines

    def __rich_console__(self, console, options):
        n = len(self.lines)
        for i, segs in enumerate(self.lines):
            yield from segs
            if i < n - 1:
                yield Segment.line()


class Tui:
    """Rich, event-driven terminal frontend (bottom-anchored live -> scrollback)."""

    def __init__(self, harness, bus: EventBus, console: Optional[Console] = None,
                 logo: str = ""):
        self.harness = harness
        self.bus = bus
        self.console = console or Console()
        self.logo = logo
        self.model = str(getattr(harness, "model", ""))
        self.session = str(getattr(harness, "session_id", ""))[:8]
        self.context = int(getattr(harness, "context", 0) or 0)
        self.usage_in = int(getattr(harness, "input", 0) or 0)
        self.usage_out = int(getattr(harness, "output", 0) or 0)
        self._q: "queue.Queue[Event]" = queue.Queue()
        self._turn_no = 0
        self._user = ""
        self._reply = ""
        self._reasoning = ""
        self._tools: list[dict] = []
        self._tool_index: dict[str, int] = {}
        self._tools_done = False
        self._error = ""
        self.busy = False
        self.status = "ready"
        self._live_cache: Optional[Panel] = None
        bus.subscribe(self._on_event)

    # ------------------------------------------------------------- events
    def _on_event(self, event: Event) -> None:
        self._q.put(event)

    def _drain(self) -> bool:
        changed = False
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                break
            changed = True
            self._apply(ev)
        return changed

    # ------------------------------------------------------- turn state
    def _reset_turn(self, user_text: str) -> None:
        self._turn_no += 1
        self._user = user_text
        self._reply = ""
        self._reasoning = ""
        self._tools = []
        self._tool_index = {}
        self._tools_done = False
        self._error = ""
        self.busy = True
        self.status = "thinking"
        self._invalidate()

    def _invalidate(self) -> None:
        self._live_cache = None

    # --------------------------------------------------- event -> state
    def _apply(self, ev: Event) -> None:
        kind, data = ev.kind, ev.data
        if kind is EventKind.USER:
            self.status = "working…"
            self._invalidate()
        elif kind is EventKind.THINKING:
            self.busy = True
            self.status = f"thinking · step {data.get('iteration', '')}"
            self._invalidate()
        elif kind is EventKind.REASONING:
            self.busy = True
            self.status = "reasoning…"
            self._reasoning += str(data.get("text", ""))
            self._invalidate()
        elif kind is EventKind.TOKEN:
            self.busy = True
            self.status = "streaming…"
            if self._tools_done and self._reply and not self._reply[-1].isspace():
                self._reply += " "
            self._reply += str(data.get("text", ""))
            self._invalidate()
        elif kind is EventKind.MESSAGE:
            self._reply += str(data.get("content", ""))
            self._invalidate()
        elif kind is EventKind.TOOL_CALL:
            cid = str(data.get("call_id", ""))
            block = {"id": cid, "name": str(data.get("name", "")),
                     "args": data.get("args", {}), "state": "run",
                     "ms": 0, "preview": ""}
            self._tools.append(block)
            self._tool_index[cid] = len(self._tools) - 1
            self.busy = True
            self.status = f"tool · {block['name']}"
            self._invalidate()
        elif kind is EventKind.TOOL_RESULT:
            cid = str(data.get("call_id", ""))
            idx = self._tool_index.get(cid)
            if idx is None:
                idx = len(self._tools)
                self._tools.append({"id": cid, "name": str(data.get("name", "")),
                                    "args": {}, "state": "err", "ms": 0,
                                    "preview": ""})
                self._tool_index[cid] = idx
            block = self._tools[idx]
            block["state"] = "ok" if data.get("success", True) else "err"
            block["ms"] = int(data.get("duration_ms", 0) or 0)
            block["preview"] = _preview(str(data.get("preview", "")))
            self._tools_done = True
            self.status = f"tool · {block['name']} done"
            self._invalidate()
        elif kind is EventKind.USAGE:
            self.usage_in = int(data.get("input_tokens", self.usage_in) or 0)
            self.usage_out = int(data.get("output_tokens", self.usage_out) or 0)
        elif kind is EventKind.DONE:
            self.busy = False
            self.status = "ready"
            self.usage_in = int(getattr(self.harness, "input", self.usage_in) or 0)
            self.usage_out = int(getattr(self.harness, "output", self.usage_out) or 0)
            self._invalidate()
        elif kind is EventKind.ERROR:
            self.busy = False
            self.status = "error"
            self._error = str(data.get("message", "error"))
            self._invalidate()
        elif kind is EventKind.SYSTEM:
            msg = str(data.get("content", data.get("message", "")))
            if msg:
                self.status = msg
                self._invalidate()

    # --------------------------------------------------------- content
    def _content_parts(self) -> list:
        parts: list = []
        parts.append(Text(f"❯ {self._user}", style="bold cyan"))
        if self._reasoning:
            parts.append(Text(self._reasoning, style="dim italic"))
        if self._reply:
            if self._reasoning:
                parts.append(Text("···", style="dim"))
            parts.append(Text(self._reply))
        for tool in self._tools:
            state = tool["state"]
            name = tool["name"]
            if state == "run":
                parts.append(Text(f"● {name} {_args_summary(tool['args'])}",
                                  style="magenta"))
            else:
                glyph, style = ("✓", "green") if state == "ok" else ("✗", "red")
                parts.append(Text(f"{glyph} {name}   {tool['ms']} ms", style=style))
                if tool["preview"]:
                    parts.append(Text("    " + tool["preview"], style="dim"))
        if self._error:
            parts.append(Text("✗ " + self._error, style="red bold"))
        return parts

    def _status_row(self):
        if self.busy:
            return Spinner("dots", text=(self.status or "working…")[:60], style="cyan")
        foot = Text()
        foot.append(self.status or "ready", style="dim")
        foot.append(f"   in {self.usage_in} · out {self.usage_out} tok", style="dim")
        return foot

    def _measure(self, renderable, width: int) -> list:
        opts = ConsoleOptions(
            size=(width, self.console.height or 24),
            legacy_windows=False,
            min_width=1,
            max_width=width,
            is_terminal=self.console.is_terminal,
            encoding=self.console.encoding,
            max_height=None,
        )
        return self.console.render_lines(renderable, opts, pad=False)

    # ------------------------------------------------------ live preview
    def _live_renderable(self) -> Panel:
        """Bottom-anchored live panel: never taller than the terminal, newest
        lines pinned to the bottom edge. Cached between content changes so the
        spinner can animate without re-measuring on every frame."""
        if self._live_cache is not None:
            return self._live_cache
        width = max(20, int(self.console.width or 80))
        height = max(5, int(self.console.height or 24))
        inner_width = width - 2          # panel border (padding 0)
        inner_rows = height - 2          # top + bottom border
        content_cap = max(1, inner_rows - 1)   # one row reserved for status
        content = self._content_parts()
        lines = self._measure(Group(*content), inner_width) if content else []
        if len(lines) > content_cap:
            tail = lines[-content_cap:]
        else:
            tail = lines
        inner = Group(_LineGroup(tail), self._status_row())
        self._live_cache = Panel(inner, title=f"pleiades · turn {self._turn_no}",
                                 border_style=TURN_BORDER, box=box.ROUNDED, padding=0)
        return self._live_cache

    def _full_renderable(self) -> Panel:
        """Unbounded panel of the finished turn (printed to scrollback)."""
        inner = Group(*self._content_parts(), self._status_row())
        return Panel(inner, title=f"pleiades · turn {self._turn_no}",
                     border_style=TURN_BORDER, box=box.ROUNDED, padding=0)

    # -------------------------------------------------------------- turns
    def _run_turn(self, text: str) -> None:
        self._reset_turn(text)
        done = threading.Event()

        def work() -> None:
            try:
                self.harness.run(text)
            except Exception as e:  # surface worker failures to the UI
                try:
                    self.bus.emit_kind(EventKind.ERROR,
                                       message=f"{type(e).__name__}: {e}")
                except Exception:
                    pass
            finally:
                done.set()

        threading.Thread(target=work, daemon=True).start()
        with Live(self._live_renderable(), console=self.console, screen=False,
                  refresh_per_second=15, auto_refresh=True, transient=True) as live:
            while True:
                if self._drain():
                    self._invalidate()
                    live.update(self._live_renderable())
                if done.is_set() and self._q.empty():
                    break
                time.sleep(0.02)
            while self._drain():
                self._invalidate()
                live.update(self._live_renderable())
        # live preview dismissed (transient); print the finished turn in full
        self.console.print(self._full_renderable())
        self.console.print()

    # -------------------------------------------------------------- driver
    def _header_text(self) -> Text:
        t = Text()
        t.append("PLEIADES", style="bold cyan")
        t.append(f"  ·  {self.model}", style="dim")
        t.append(f"  ·  {self.session}", style="dim")
        if self.context:
            used = self.usage_in + self.usage_out
            t.append(f"  ·  ctx {used * 100.0 / self.context:.0f}%", style="dim")
        return t

    def _print_banner(self) -> None:
        if self.logo:
            self.console.print(self.logo)
        self.console.print()
        self.console.print(self._header_text())
        self.console.print(Rule(style="dim"))
        self.console.print(Text("  PLEIADES agent TUI  ·  /help for commands  ·  /stop to quit",
                                style="dim italic"))

    def _prompt(self) -> Optional[str]:
        try:
            self.console.print(Text("pleiades › ", style="bold cyan"), end="")
            self.console.file.flush()
            return input()
        except (EOFError, KeyboardInterrupt):
            return None

    def _print_help(self) -> None:
        self.console.print(Text(
            "\n".join([
                "  commands",
                "    /stop /s /quit /q   quit",
                "    /clear /c           add breathing room",
                "    /usage /u           show token usage",
                "    /help /h            this help",
                "",
                "  output streams live at the bottom of the window while the agent",
                "  works; the finished turn is then printed to the terminal",
                "  scrollback in full, so you can scroll and read it all.",
            ]), style="dim"))

    def run(self) -> None:
        self._print_banner()
        while True:
            raw = self._prompt()
            if raw is None:
                break
            text = raw.strip()
            if not text:
                continue
            low = text.lower()
            if low in ("/stop", "/s", "/quit", "/q", "/exit"):
                break
            if low in ("/help", "/h", "/?"):
                self._print_help()
                continue
            if low in ("/clear", "/c"):
                self.console.print("\n" * 3)
                continue
            if low in ("/usage", "/u"):
                self.console.print(Text(f"in {self.usage_in} · out {self.usage_out} tok",
                                        style="dim"))
                continue
            self._run_turn(text)
        self.console.print(Text("bye", style="dim"))


def run_tui(console: Optional[Console] = None) -> None:
    """Entry point used by ``python main.py --tui``."""
    console = console or Console()
    bus = EventBus()
    try:
        import main  # noqa: PLC0415  (lazy: avoids importing the harness in tests)
        harness = main._build_harness(event_bus=bus)
    except Exception as e:  # e.g. LLM server unreachable
        console.print(Text(f"could not start the agent: {e}", style="red bold"))
        return
    logo = getattr(main, "logo", "")
    Tui(harness=harness, bus=bus, console=console, logo=logo).run()
