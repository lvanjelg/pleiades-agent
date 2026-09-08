"""PLEIADES agent TUI — a decoupled frontend for the agent harness (README step 13).

The harness (core) never renders: it emits :class:`ux.Event` objects into an
:class:`ux.EventBus` and this frontend animates *purely from event kind* —
thinking, token, message, tool_call, tool_result, done. It never needs to know
why the model is doing something.

Architecture
------------
- The harness runs on a **worker thread** per user turn.
- This UI owns the terminal on the **main thread**, running a ``rich.live``
  refresh loop that drains events from a queue and redraws a single viewport.
- Transport is pluggable: today it's the in-process EventBus; the same events
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
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.text import Text

from ux import Event, EventBus, EventKind

BLOCKS_MAX = 80          # how many message blocks to render (rolling window)
TEXT_MAX = 4000          # cap per user/assistant block
PREVIEW_MAX = 160        # tool result preview
ARGS_MAX = 120           # tool call args summary


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


class Tui:
    """Rich, event-driven terminal frontend."""

    def __init__(self, harness, bus: EventBus, console: Optional[Console] = None,
                 logo: str = ""):
        self.harness = harness
        self.bus = bus
        self.console = console or Console()
        self.logo = logo
        self.model = str(getattr(harness, "model", ""))
        self.session = str(getattr(harness, "session_id", ""))[:8]
        self.context = int(getattr(harness, "context", 0) or 0)
        self._q: "queue.Queue[Event]" = queue.Queue()
        self.blocks: list[dict] = []
        self.busy = False
        self.status = "ready"
        self.usage_in = int(getattr(harness, "input", 0) or 0)
        self.usage_out = int(getattr(harness, "output", 0) or 0)
        self._active: Optional[dict] = None   # assistant block being streamed into
        self._tools: dict[str, dict] = {}      # call_id -> tool block
        bus.subscribe(self._on_event)

    # ------------------------------------------------------------- events
    def _on_event(self, event: Event) -> None:
        """Runs on the emitter's thread (the worker). Just queue; never render."""
        self._q.put(event)

    def _apply(self, ev: Event) -> None:
        data = ev.data
        kind = ev.kind
        if kind is EventKind.USER:
            self._new_block("user", text=str(data.get("content", ""))[:TEXT_MAX])
            self.status = "working…"
        elif kind is EventKind.THINKING:
            self.busy = True
            self.status = f"thinking · step {data.get('iteration', '')}"
        elif kind is EventKind.TOKEN:
            if self._active is None or self._active["kind"] != "assistant":
                self._active = self._new_block("assistant", text="", streaming=True)
            self._active["text"] += str(data.get("text", ""))
        elif kind is EventKind.MESSAGE:
            text = str(data.get("content", ""))[:TEXT_MAX]
            if self._active is not None and self._active["kind"] == "assistant":
                self._active["text"] = text
                self._active["streaming"] = False
            else:
                self._new_block("assistant", text=text)
        elif kind is EventKind.TOOL_CALL:
            cid = str(data.get("call_id", ""))
            block = self._new_block("tool", call_id=cid, name=str(data.get("name", "")),
                                    args=data.get("args", {}), state="running",
                                    duration=0, preview="")
            self._tools[cid] = block
            self.busy = True
            self.status = f"tool · {block['name']}"
        elif kind is EventKind.TOOL_RESULT:
            cid = str(data.get("call_id", ""))
            block = self._tools.get(cid)
            if block is None:
                block = self._new_block("tool", call_id=cid, name=str(data.get("name", "")),
                                        args={}, state="err", duration=0, preview="")
                self._tools[cid] = block
            block["state"] = "ok" if data.get("success", True) else "err"
            block["duration"] = int(data.get("duration_ms", 0) or 0)
            block["preview"] = _preview(str(data.get("preview", "")))
            self.status = f"tool · {block['name']} done"
        elif kind is EventKind.USAGE:
            self.usage_in = int(data.get("input_tokens", self.usage_in) or 0)
            self.usage_out = int(data.get("output_tokens", self.usage_out) or 0)
        elif kind is EventKind.DONE:
            self.busy = False
            self.status = "ready"
            if self._active is not None and self._active["kind"] == "assistant":
                self._active["streaming"] = False
            self._active = None
            self.usage_in = int(getattr(self.harness, "input", self.usage_in) or 0)
            self.usage_out = int(getattr(self.harness, "output", self.usage_out) or 0)
        elif kind is EventKind.ERROR:
            self._new_block("error", text=str(data.get("message", "error")))
            self.status = "error"
            self.busy = False
        elif kind is EventKind.SYSTEM:
            msg = str(data.get("content", data.get("message", "")))
            if msg:
                self._new_block("system", text=msg)

    def _new_block(self, kind: str, **fields) -> dict:
        block = {"kind": kind, "text": "", **fields}
        self.blocks.append(block)
        if len(self.blocks) > BLOCKS_MAX * 4:
            del self.blocks[: len(self.blocks) - BLOCKS_MAX * 4]
        return block

    # ------------------------------------------------------------ rendering
    def _render_block(self, block: dict) -> Text:
        kind = block["kind"]
        out = Text()
        if kind == "user":
            out.append("❯ ", style="bold cyan")
            out.append((block.get("text") or "").rstrip(), style="bold")
        elif kind == "assistant":
            text = block.get("text") or ""
            out.append(text if text else "…")
            if block.get("streaming") and text:
                out.append("▍", style="cyan")
        elif kind == "tool":
            state = block.get("state", "running")
            if state == "running":
                glyph, style = "●", "magenta"
            elif state == "ok":
                glyph, style = "✓", "green"
            else:
                glyph, style = "✗", "red"
            line = Text(f"{glyph} {block.get('name', 'tool')}", style=style)
            summary = _args_summary(block.get("args"))
            if summary:
                line.append(" " + summary, style="dim")
            out.append_text(line)
            if state in ("ok", "err"):
                out.append(f"   {int(block.get('duration', 0) or 0)} ms", style="dim")
                preview = block.get("preview")
                if preview:
                    out.append("\n    " + preview, style="dim")
            else:
                out.append("   running…", style="dim")
        elif kind == "error":
            out.append("✗ " + (block.get("text") or ""), style="red bold")
        else:  # system
            out.append((block.get("text") or ""), style="dim italic")
        return out

    def _header(self) -> Text:
        t = Text()
        t.append("PLEIADES", style="bold cyan")
        t.append(f"  ·  {self.model}", style="dim")
        t.append(f"  ·  {self.session}", style="dim")
        if self.context:
            used = self.usage_in + self.usage_out
            t.append(f"  ·  ctx {used * 100.0 / self.context:.0f}%", style="dim")
        return t

    def _footer_line(self) -> Text:
        t = Text()
        t.append(f"in {self.usage_in} · out {self.usage_out} tok", style="dim")
        t.append("   ", style="dim")
        t.append("type /help · /clear · /stop", style="dim")
        return t

    def _viewport(self) -> Panel:
        parts: list = [self._header(), Rule(style="dim")]
        if len(self.blocks) > BLOCKS_MAX:
            parts.append(Text("  … earlier messages omitted …", style="dim italic"))
        elif not self.blocks:
            parts.append(Text("  (no messages yet — ask something below)", style="dim italic"))
        for block in self.blocks[-BLOCKS_MAX:]:
            parts.append(self._render_block(block))
        if self.busy:
            parts.append(Spinner("dots", text=self.status or "working…", style="cyan"))
        parts.append(Rule(style="dim"))
        parts.append(self._footer_line())
        return Panel(Group(*parts), box=box.ROUNDED, border_style="dim", padding=(0, 1))

    # -------------------------------------------------------------- turns
    def _drain(self) -> bool:
        changed = False
        while True:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                return changed
            self._apply(ev)
            changed = True

    def _run_turn(self, text: str) -> None:
        self.busy = True
        worker_done = threading.Event()

        def work() -> None:
            try:
                self.harness.run(text)
            except Exception as e:  # surface worker failures to the UI
                try:
                    self.bus.emit_kind(EventKind.ERROR, message=f"{type(e).__name__}: {e}")
                except Exception:
                    pass
            finally:
                worker_done.set()

        threading.Thread(target=work, daemon=True).start()
        with Live(self._viewport(), console=self.console, screen=False,
                  refresh_per_second=15, auto_refresh=True) as live:
            while not worker_done.is_set() or not self._q.empty():
                if self._drain():
                    live.update(self._viewport())
                time.sleep(0.02)
            while self._drain():  # flush anything emitted just before finish
                live.update(self._viewport())
        self.busy = False
        self.console.print()

    # -------------------------------------------------------------- driver
    def _prompt(self) -> Optional[str]:
        try:
            self.console.print(Text("pleiades › ", style="bold cyan"), end="")
            return input()
        except (EOFError, KeyboardInterrupt):
            return None

    def _print_help(self) -> None:
        self.console.print(Text(
            "\n".join([
                "  commands",
                "    /stop /s /quit /q   quit",
                "    /clear /c           clear the transcript",
                "    /usage /u           show token usage",
                "    /help /h            this help",
                "",
                "  anything else is sent to the agent.",
            ]), style="dim"))

    def _print_banner(self) -> None:
        if self.logo:
            self.console.print(self.logo)
        self.console.print()
        self.console.print(self._header())
        self.console.print(Rule(style="dim"))
        self.console.print(Text("  PLEIADES agent TUI  ·  /help for commands  ·  /stop to quit",
                                style="dim italic"))

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
                self.blocks.clear()
                self._tools.clear()
                self._active = None
                continue
            if low in ("/usage", "/u"):
                self.console.print(
                    Text(f"in {self.usage_in} · out {self.usage_out} tok", style="dim"))
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
