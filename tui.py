"""PLEIADES agent TUI — a decoupled frontend for the agent harness (README step 13).

The harness (core) never renders: it emits :class:`ux.Event` objects into an
:class:`ux.EventBus`, and this frontend reacts purely from event kind. It never
needs to know *why* the model is doing something.

Display model (live preview -> full scrollback):

- **No borders.** The layout is flat, following the pi agent TUI: a tinted block
  for your prompt, italic thinking, a tinted block of tool activity, then the
  answer as markdown, separated by blank lines rather than borders or titles.
- **pi's colours.** The palette in :data:`PI` is copied from pi's dark theme
  (``userMessageBg`` #343541, ``toolPendingBg`` #282832, ``toolSuccessBg``
  #283228, ``toolErrorBg`` #3c2828, ``thinkingText`` #808080, and the markdown
  heading/code/quote colours). ``get_theme()`` installs the markdown half as a
  rich theme.
- While a turn runs, a **live preview** shows it streaming in. The preview is
  **bottom-anchored**: it never grows taller than the terminal, and the newest
  lines (the streaming tail, the active tool, the status line) always sit at the
  bottom edge, so you are never "stuck" above the action. Scrolling is limited
  *during* output, which is expected.
- When the turn finishes, the live preview is dismissed and the **full turn is
  printed to the terminal's normal scrollback**. From that moment you can scroll
  and read every line of every finished turn. Nothing is hidden or trimmed.

Turn layout, top to bottom (bands are tinted, no borders)::

    ▛▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ userMessageBg #343541 ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▜
    ❯ what changed in the repo
    ▙▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▟

    thinking… (italic thinkingText #808080, markdown)

    ▛▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ toolSuccessBg #283228 ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▜
    ✓ bash  29 ms
        ?? providers.py
    ▙▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▟

    ## Findings
    …markdown…

    ↑1.2k ↓345  0.4%/1.0M            deepseek-flash · DeepSeek API

The model's answer is always the bottom-most content: tool activity is printed
*above* it so a long tool trace can never bury the end of the turn.

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

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from rich.theme import Theme

from ux import Event, EventBus, EventKind
# Shared presentation layer (palette, token and tool-row formatting) so the
# Textual frontend and this legacy one cannot drift apart.
from presentation import (ARGS_MAX, PI, PREVIEW_MAX, THINKING_STYLE,  # noqa: F401
                          TOOL_ROWS_MAX, args_summary as _args_summary,
                          format_tokens, preview as _preview,
                          rich_theme as get_theme, thinking_label)

# The live loop can redraw ~50x/sec while tokens stream. Parsing markdown costs
# ~3x a plain text render (measured), so the live preview re-parses at most this
# often (plus a term that grows with the reply) instead of on every frame.
LIVE_MARKDOWN_INTERVAL = 0.15


def _is_blank(line) -> bool:
    """True when a pre-measured segment line carries no visible text.

    A tinted row is never blank: it is visible even though every cell is a
    space, and treating it as blank would let blank-collapsing eat the padding
    row of a background band.
    """
    if _has_background(line):
        return False
    return not any(segment.text.strip() for segment in line)


def _has_background(line) -> bool:
    """True when any segment in the line paints a background."""
    return any(segment.style is not None and segment.style.bgcolor is not None
               for segment in line if not segment.control)


def _rstrip_lines(lines: list) -> list:
    """Trim trailing whitespace from measured lines.

    Markdown pads its blocks out to the render width, so every paragraph and
    table would drag invisible trailing spaces into the scrollback — and into
    anything you copy out of it. Tinted lines are left alone: their trailing
    spaces are the band. Control segments are never dropped.
    """
    out: list = []
    for segs in lines:
        if _has_background(segs):
            out.append(list(segs))
            continue
        segs = list(segs)
        while segs:
            seg = segs[-1]
            if seg.control is not None:
                break                       # never drop control codes
            if seg.text.strip() == "":
                segs.pop()                  # whitespace-only tail (or empty) segment
                continue
            trimmed = seg.text.rstrip()
            if trimmed != seg.text:
                segs[-1] = Segment(trimmed, seg.style)
            break
        out.append(segs)
    return out


def _collapse_blanks(lines: list, keep: int = 1) -> list:
    """Cap consecutive blank lines and normalise them to truly empty ones.

    Markdown ends every block with its own blank line, so a source that already
    separates blocks with a blank line renders as two — three-line gaps between
    every paragraph. It also leaves whitespace-only filler rows behind; those are
    replaced with empty lines so the scrollback stays clean to copy out.
    """
    out: list = []
    blanks = 0
    for line in lines:
        if _is_blank(line):
            blanks += 1
            if blanks <= keep:
                out.append([])
        else:
            blanks = 0
            out.append(line)
    return out


class _Band:
    """A full-width background band around a renderable.

    This is pi's ``Box(padY, padX, bgFn)``: the block is tinted edge to edge,
    with padding rows top and bottom, and no border. Every segment inside keeps
    its own colours and gains the band's background, so the tint stays unbroken
    through headings and inline code (a later style wins when both set a
    background, and the band's is applied last).
    """

    def __init__(self, renderable, bg: str, pad_x: int = 1, pad_y: int = 1):
        self.renderable = renderable
        self.bg = bg
        self.pad_x = pad_x
        self.pad_y = pad_y

    def __rich_console__(self, console, options):
        width = max(1, options.max_width)
        inner = max(1, width - 2 * self.pad_x)
        lines = console.render_lines(
            self.renderable, options.update(width=inner, height=None), pad=False)
        style = Style(bgcolor=self.bg)
        for _ in range(self.pad_y):
            yield Segment(" " * width, style)
            yield Segment.line()
        for segs in lines:
            used = sum(seg.cell_length for seg in segs if not seg.control)
            yield Segment(" " * self.pad_x, style)
            for seg in segs:
                if seg.control:
                    yield seg
                else:
                    yield Segment(seg.text, (seg.style or Style()) + style)
            yield Segment(" " * max(0, inner - used + self.pad_x), style)
            yield Segment.line()
        for _ in range(self.pad_y):
            yield Segment(" " * width, style)
            yield Segment.line()


class _LineGroup:
    """Renders pre-measured segment-lines (the live preview's tail)."""

    def __init__(self, lines):
        self.lines = lines

    def __rich_console__(self, console, options):
        # A line break after *every* line, including the last — that is what
        # rich's own renderables do. Without it the renderable that follows this
        # one (the status row) continues on the final content line instead of
        # starting its own row.
        for segs in self.lines:
            yield from segs
            yield Segment.line()


class Tui:
    """Rich, event-driven terminal frontend (bottom-anchored live -> scrollback)."""

    def __init__(self, harness, bus: EventBus, console: Optional[Console] = None,
                 logo: str = ""):
        self.harness = harness
        self.bus = bus
        self.console = console or Console(theme=get_theme())
        self.logo = logo
        self.model = str(getattr(harness, "model", ""))
        self.provider = str(getattr(getattr(harness, "provider", None), "label", ""))
        self.session = str(getattr(harness, "session_id", ""))[:8]
        self.context = int(getattr(harness, "context", 0) or 0)
        self.usage_in = int(getattr(harness, "input", 0) or 0)
        self.usage_out = int(getattr(harness, "output", 0) or 0)
        # pi's footer reports current context occupancy, not cumulative tokens
        self.ctx_used = int(getattr(harness, "usage", 0) or 0)
        self.thinking = thinking_label(getattr(harness, "provider", None))
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
        self._live_cache = None
        # live-preview markdown caches, keyed by slot (see _throttled_markdown)
        self._md_cache: dict = {}
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
        self._md_cache = {}
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
            # Carry a terminal note (`Stopped: …`, `Max iterations reached.`,
            # an incomplete turn) into the reply when nothing else was rendered,
            # so the agent never appears to just stop with a blank transcript.
            note = str(data.get("content") or "").strip()
            if note and not (self._reply or "").strip():
                self._reply = note
            self.busy = False
            self.status = "ready"
            self.usage_in = int(getattr(self.harness, "input", self.usage_in) or 0)
            self.usage_out = int(getattr(self.harness, "output", self.usage_out) or 0)
            self.ctx_used = int(getattr(self.harness, "usage", self.ctx_used) or 0)
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
    def _throttled_markdown(self, text: str, slot: str = "reply",
                            style: str = ""):
        """Markdown for the live preview, re-parsed on a budget.

        Streaming redraws far more often than a human can read, so re-parsing the
        whole answer every frame buys nothing and costs real time on long replies.
        The cadence scales with the document: small answers update almost every
        frame, big ones settle to a few parses a second.
        """
        now = time.monotonic()
        key = len(text)
        cached = self._md_cache.get(slot)
        if cached is not None and cached[0] == key:
            return cached[2]                      # nothing changed
        interval = LIVE_MARKDOWN_INTERVAL + key / 50_000
        if cached is not None and (now - cached[1]) < interval:
            return cached[2]                      # too soon: keep last parse
        try:
            renderable = Markdown(text, style=style) if style else Markdown(text)
        except Exception:
            renderable = Text(text, style=style)
        self._md_cache[slot] = (key, now, renderable)
        return renderable

    def _reply_renderable(self, markdown: bool, throttle: bool = False):
        """The agent's reply, formatted.

        The agent writes markdown (headings, bold, `code`, lists, tables), so it
        is rendered as markdown rather than dumped as source — raw `**` and
        backticks make a correct answer look broken. The finished turn always
        renders fresh; the live preview passes ``throttle`` so streaming stays
        smooth.
        """
        if not markdown:
            return Text(self._reply)
        if throttle:
            return self._throttled_markdown(self._reply, "reply")
        try:
            return Markdown(self._reply)
        except Exception:
            # Never let a rendering nicety kill a finished turn.
            return Text(self._reply)

    def _thinking_renderable(self, markdown: bool, throttle: bool):
        """Thinking, as pi draws it: italic in the ``thinkingText`` grey, no box."""
        style = THINKING_STYLE
        if throttle:
            return self._throttled_markdown(self._reasoning, "thinking", style)
        try:
            return Markdown(self._reasoning, style=style)
        except Exception:
            return Text(self._reasoning, style=style)

    def _tool_bands(self) -> Group:
        """One tinted band per tool call, the way pi wraps each tool execution
        in its own ``Box(1, 1, bg)`` coloured by outcome: pending while it runs,
        red if it failed, green once it succeeded."""
        hidden = max(0, len(self._tools) - TOOL_ROWS_MAX)
        blocks: list = []
        if hidden:
            blocks.append(Text(f"… +{hidden} earlier tool call"
                               f"{'s' if hidden != 1 else ''}", style=PI["muted"]))
        for tool in self._tools[hidden:]:
            state = tool["state"]
            name = tool["name"]
            if state == "run":
                bg, glyph, color = (PI["toolPendingBg"], "●", PI["muted"])
            elif state == "ok":
                bg, glyph, color = (PI["toolSuccessBg"], "✓", PI["success"])
            else:
                bg, glyph, color = (PI["toolErrorBg"], "✗", PI["error"])
            rows: list = [Text.assemble((f"{glyph} ", color),
                                        (name, f"bold {PI['text']}"),
                                        (f"  {tool['ms']} ms", PI["muted"]))]
            if state == "run" and tool["args"]:
                rows.append(Text("    " + _args_summary(tool["args"]), style=PI["muted"]))
            elif tool["preview"]:
                rows.append(Text("    " + tool["preview"], style=PI["muted"]))
            blocks.append(_Band(Group(*rows), bg))
        return Group(*blocks)

    def _content_parts(self, markdown: bool = False, throttle: bool = False) -> list:
        """Build the turn body in reading order, pi-style: no borders, no
        titles, blank lines between blocks.

            ❯ the user's prompt

            reasoning (dim italic), if the model produced any

            tool activity, most recent last

            the model's answer  <-- always the bottom-most content

        Tool activity sits ABOVE the reply on purpose. The answer is the thing
        you need to see, and a wall of tool lines below it both pushed it up
        out of the live preview's tail and buried the end of the turn. Up here
        the log still reads as a trace of what happened, and it scrolls away
        above the answer instead of crowding it.
        """
        parts: list = []
        marker = Text.assemble(("❯ ", f"bold {PI['accent']}"), (self._user, PI["text"]))
        parts.append(_Band(marker, PI["userMessageBg"]))
        if self._reasoning:
            parts.append(Text(""))
            parts.append(self._thinking_renderable(markdown, throttle))

        if self._tools:
            parts.append(Text(""))
            parts.append(self._tool_bands())

        if self._reply:
            parts.append(Text(""))
            parts.append(self._reply_renderable(markdown, throttle))
        if self._error:
            parts.append(Text(""))
            parts.append(Text("✗ " + self._error, style=f"bold {PI['error']}"))
        return parts

    def _status_row(self):
        """The line under a turn, laid out like pi's footer: cumulative usage and
        the current context occupancy on the left, the model and its thinking
        level right-aligned, all dim. While the agent works it is a spinner."""
        if self.busy:
            return Spinner("dots", text=(self.status or "working…")[:60], style="dim")
        left = f"↑{format_tokens(self.usage_in)} ↓{format_tokens(self.usage_out)}"
        pct_text = ""
        pct_style = PI["muted"]
        if self.context:
            # pi reports how full the *current* context is, not cumulative tokens.
            pct = (self.ctx_used * 100.0 / self.context) if self.ctx_used else 0.0
            pct_text = f"  {pct:.1f}%/{format_tokens(self.context)}"
            if pct > 90:
                pct_style = PI["error"]
            elif pct > 70:
                pct_style = PI["warning"]
        right = f"{self.provider} · {self.model}" if self.provider else self.model
        if self.thinking:
            right += f" • {self.thinking}"
        width = self._content_width()
        pad = width - cell_len(left) - cell_len(pct_text) - cell_len(right)
        foot = Text()
        foot.append(left, style="dim")
        foot.append(pct_text, style=pct_style)
        if pad >= 2:
            foot.append(" " * pad, style="dim")
            foot.append(right, style="dim")
        return foot

    def _content_width(self) -> int:
        """Line budget shared by the footer and the measured turn body.

        One column short of the terminal: a line that exactly fills the width
        makes terminals wrap to a phantom next row.
        """
        return max(20, int(self.console.width or 80) - 1)

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
    def _live_renderable(self):
        """Bottom-anchored live preview at a **constant** height.

        The frame is always exactly as tall as the terminal, with the newest
        lines pinned to the bottom and unused rows left blank above them. rich
        erases a live frame by moving the cursor up one row per line it drew, so
        a frame that grows and shrinks between refreshes can leave fragments of
        an earlier, taller frame behind — which is what "spacing issues" in the
        scrollback turn out to be. A fixed height keeps that arithmetic constant.
        """
        if self._live_cache is not None:
            return self._live_cache
        width = self._content_width()
        height = max(5, int(self.console.height or 24))
        content_cap = max(1, height - 1)       # one row for the status line
        content = self._content_parts(markdown=True, throttle=True)
        lines = _rstrip_lines(self._measure(Group(*content), width)) if content else []
        tail = lines[-content_cap:] if len(lines) > content_cap else lines
        rows = [[] for _ in range(max(0, content_cap - len(tail)))] + list(tail)
        self._live_cache = Group(_LineGroup(rows), self._status_row())
        return self._live_cache

    def _full_renderable(self):
        """The finished turn, printed to scrollback: same flat layout, markdown
        rendered fresh and blank-line runs tightened."""
        content = Group(*self._content_parts(markdown=True), self._status_row())
        lines = self._measure(content, self._content_width())
        return _LineGroup(_collapse_blanks(_rstrip_lines(lines)))

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
        t.append("PLEIADES", style=f"bold {PI['accent']}")
        if self.provider:
            t.append(f"  ·  {self.provider}", style=PI["muted"])
        t.append(f"  ·  {self.model}", style=PI["muted"])
        t.append(f"  ·  {self.session}", style=PI["muted"])
        if self.context:
            used_pct = (self.ctx_used * 100.0 / self.context) if self.ctx_used else 0.0
            color = PI["error"] if used_pct > 90 else PI["warning"] if used_pct > 70 else PI["muted"]
            t.append(f"  ·  ctx {used_pct:.0f}%", style=color)
        return t

    def _print_banner(self) -> None:
        if self.logo:
            self.console.print(self.logo)
        self.console.print()
        self.console.print(self._header_text())
        self.console.print(Text("  /help for commands  ·  /stop to quit",
                                style=f"italic {PI['dim']}"))
        self.console.print()

    def _prompt(self) -> Optional[str]:
        try:
            self.console.print(Text("pleiades › ", style=f"bold {PI['accent']}"), end="")
            self.console.file.flush()
            return input()
        except (EOFError, KeyboardInterrupt):
            return None

    def _print_help(self) -> None:
        self.console.print(Text(
            "\n".join([
                "  commands",
                "    /stop /s /quit /q   quit",
                "    /provider /p [name] show or switch backend (local, deepseek)",
                "    /model /m [name]    show or switch model",
                "    /clear /c           add breathing room",
                "    /usage /u           show token usage",
                "    /graph /g           cross-session graph summary",
                "    /help /h            this help",
                "",
                "  output streams live at the bottom of the window while the agent",
                "  works; the finished turn is then printed to the terminal",
                "  scrollback in full, so you can scroll and read it all.",
            ]), style="dim"))

    def _sync_from_harness(self) -> None:
        """Re-read the mutable harness state the header reflects (after a switch)."""
        self.model = str(getattr(self.harness, "model", ""))
        self.provider = str(getattr(getattr(self.harness, "provider", None), "label", ""))
        self.context = int(getattr(self.harness, "context", 0) or 0)
        self.ctx_used = int(getattr(self.harness, "usage", 0) or 0)
        self.thinking = thinking_label(getattr(self.harness, "provider", None))

    def _show_provider(self, name: str) -> None:
        if not name:
            self.console.print(Text(self.harness.status_line(), style="dim"))
            self.console.print(Text("available: " + ", ".join(self.harness.available_providers()),
                                    style="dim"))
            return
        try:
            line = self.harness.switch_provider(name)
        except ValueError as e:
            self.console.print(Text(f"error: {e}", style="red"))
            return
        self._sync_from_harness()
        self.console.print(Text(f"provider -> {line}", style="dim"))

    def _show_model(self, name: str) -> None:
        if not name:
            self.console.print(Text(f"model: {self.harness.model}", style="dim"))
            models = self.harness.available_models()
            if models:
                self.console.print(Text("available: " + ", ".join(models), style="dim"))
            return
        try:
            line = self.harness.switch_model(name)
        except ValueError as e:
            self.console.print(Text(f"error: {e}", style="red"))
            return
        self._sync_from_harness()
        self.console.print(Text(f"model -> {line}", style="dim"))

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
            cmd, _, arg = text.partition(" ")      # keep arg case (model ids)
            cmd, arg = cmd.lower(), arg.strip()
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
                                        style=PI["muted"]))
                continue
            if low in ("/graph", "/g"):
                self.console.print(Text(self.harness.graph.query("summary"),
                                        style=PI["muted"]))
                continue
            if cmd in ("/provider", "/p"):
                self._show_provider(arg)
                continue
            if cmd in ("/model", "/m"):
                self._show_model(arg)
                continue
            self._run_turn(text)
            self._sync_from_harness()
        self.console.print(Text("bye", style="dim"))


def run_tui(console: Optional[Console] = None, provider=None,
            model: Optional[str] = None) -> None:
    """Entry point used by ``python main.py --tui``."""
    console = console or Console(theme=get_theme())
    bus = EventBus()
    try:
        import main  # noqa: PLC0415  (lazy: avoids importing the harness in tests)
        harness = main._build_harness(event_bus=bus, provider=provider, model=model)
    except Exception as e:  # e.g. LLM server unreachable
        console.print(Text(f"could not start the agent: {e}", style="red bold"))
        return
    logo = getattr(main, "logo", "")
    Tui(harness=harness, bus=bus, console=console, logo=logo).run()
