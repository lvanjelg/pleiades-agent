"""Turn widgets: the visual language, expressed as Textual components.

The layout is the same flat, borderless one the legacy TUI drew by hand, but
each element is now a real widget with a background and a style, so the
framework owns wrapping, clipping and scroll position:

    UserBand          prompt on pi's userMessageBg block
    ThinkingBlock     italic thinkingText markdown, inside a Collapsible
    ToolBand          one per tool call, tinted by outcome (pi's per-tool Box)
    ReplyBlock        the answer, as markdown
    StatusLine        cumulative usage + context % left, model • level right

Everything that decides *what* to show comes from :mod:`presentation` and
:mod:`textual_tui.model`, so this file is only about presentation.
"""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Collapsible, Markdown, Static

from presentation import PI, TOOL_STATE, args_summary, format_tokens, preview
from textual_tui.model import RUN, ToolRecord, Turn


class UserBand(Vertical):
    """The user's prompt, on the tinted block pi uses for user messages."""

    def __init__(self, text: str) -> None:
        super().__init__(classes="user-band")
        self._text = text

    def compose(self) -> ComposeResult:
        yield Markdown(f"❯ {self._text}")


class _StreamingMarkdown(Markdown):
    """Markdown that tolerates being handed text before it is mounted, and that
    streams by appending instead of re-rendering.

    Three Textual behaviours make this necessary:

    * ``update()`` on a detached Markdown raises ``MountError`` (the document's
      blocks are mounted lazily).
    * Mount handlers are dispatched for **every class in the MRO**, so
      ``Markdown._on_mount`` always runs — after a subclass's own hook — and
      calls ``update(initial_markdown or "")``, wiping anything pushed earlier.
      Text is therefore buffered and flushed from ``call_next``, which Textual
      processes after the whole ``Mount`` dispatch.
    * ``update()`` removes every block and mounts replacements. A click that
      lands between that removal and the next refresh hit-tests a widget with no
      parent, and Textual's selection path dereferences it
      (``Screen._forward_event`` → ``container.region``), crashing the app with
      ``AttributeError: 'NoneType' object has no attribute 'region'``. ``append``
      reuses the existing blocks, so streaming never removes anything — it is
      also much cheaper than re-parsing the whole document on every flush.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pending: str | None = None
        self._ready = False

    def set_text(self, text: str) -> None:
        self._pending = text or ""
        if self._ready:
            self._flush_pending()

    def on_mount(self) -> None:
        self.call_next(self._become_ready)

    def _become_ready(self) -> None:
        self._ready = True
        self._flush_pending()

    def _flush_pending(self) -> None:
        if self._pending is None:
            return
        text, self._pending = self._pending, None
        if text:
            self._apply(text)

    def _apply(self, text: str) -> None:
        """Append when the document only grew, re-render otherwise."""
        current = self._markdown or ""
        if current and text.startswith(current):
            delta = text[len(current):]
            if delta:
                self.append(delta)
            return
        self.update(text)


class ThinkingBlock(_StreamingMarkdown):
    """Thinking: italic, in pi's ``thinkingText`` grey, rendered as markdown."""

    def __init__(self) -> None:
        super().__init__("", classes="thinking")


class ToolCall(Collapsible):
    """One tool call, inline in the turn — the same shape as the thinking block.

    The title carries the live state (glyph, name, duration or args); the body
    carries the output preview once the call has finished. Collapsed by default
    once done, so a long tool trace does not bury the answer.

    The body Static is created with ``markup=False``: tool output is arbitrary
    text, and ``Static.update`` parses console markup by default, so a preview
    containing ``[...]`` (a repr with a list, a bash ``[ -f ... ]``, log lines)
    crashed the app with ``MarkupError``.
    """

    def __init__(self, record: ToolRecord) -> None:
        self.record = record
        super().__init__(Static("", classes="tool-output", markup=False),
                         title=self._heading(), collapsed=record.state == RUN,
                         classes=f"tool-call -{record.state}")

    def update_record(self, record: ToolRecord) -> None:
        self.record = record
        self.set_classes(f"tool-call -{record.state}")
        self.title = self._heading()
        self.collapsed = record.state != RUN
        # The body Static is composed lazily (the Collapsible mounts its
        # children when it mounts), so only touch it when it exists.
        body = self.query_one_optional(".tool-output")
        if body is not None:
            body.update(self._body())

    def _heading(self) -> str:
        state = TOOL_STATE.get(self.record.state, TOOL_STATE[RUN])
        text = f"{state['glyph']} {self.record.name}"
        if self.record.state == RUN:
            summary = args_summary(self.record.args)
            if summary:
                text += f"  {summary}"
        else:
            text += f"  {self.record.ms} ms"
        return text

    def _body(self) -> str:
        if self.record.state == RUN or not self.record.preview:
            return ""
        return preview(self.record.preview)


class ReplyBlock(_StreamingMarkdown):
    """The answer, rendered as markdown (HTML-free, framework-owned)."""

    def __init__(self) -> None:
        super().__init__("", classes="reply")


class StatusLine(Horizontal):
    """pi's footer: usage and context on the left, model and level on the right."""

    def __init__(self) -> None:
        super().__init__(id="status-row")
        self._left = Static("", id="status-left")
        self._right = Static("", id="status-right")

    def compose(self) -> ComposeResult:
        yield self._left
        yield self._right

    def update_state(self, *, busy: bool, status: str, usage_in: int, usage_out: int,
                     ctx_used: int, context: int, provider: str, model: str,
                     thinking: str) -> None:
        if busy:
            self._left.update(Text(status or "working…", style=PI["muted"]))
            self._right.update(Text(self._model_text(provider, model, thinking),
                                    style=PI["muted"]))
            return
        parts = Text()
        parts.append(f"↑{format_tokens(usage_in)} ↓{format_tokens(usage_out)}",
                     style=PI["muted"])
        if context:
            pct = (ctx_used * 100.0 / context) if ctx_used else 0.0
            colour = (PI["error"] if pct > 90 else
                      PI["warning"] if pct > 70 else PI["muted"])
            parts.append(f"  {pct:.1f}%/{format_tokens(context)}", style=colour)
        self._left.update(parts)
        self._right.update(Text(self._model_text(provider, model, thinking),
                                style=PI["muted"]))

    @staticmethod
    def _model_text(provider: str, model: str, thinking: str) -> str:
        text = f"{provider} · {model}" if provider else model
        return f"{text} • {thinking}" if thinking else text


class Transcript(VerticalScroll):
    """Scrolling history of turns, pinned to the bottom by Textual's anchor.

    The stick-to-bottom behaviour is Textual's own: ``anchor(True)`` keeps the
    view at the bottom as content is added, and any user scroll releases it
    (``scroll_to`` calls ``release_anchor``) until the reader returns to the
    bottom, where ``_check_anchor`` re-engages it. The compositor applies the
    anchor during layout, so late-arriving markdown blocks cannot leave the
    answer below the fold — the hand-rolled ``watch_virtual_size``/``scroll_end``
    version this replaces could, because it scrolled to where the bottom *was*.

    ``follow`` remains as a read-only view of the anchor state, for the status
    line and the app's wheel fallback.
    """

    def __init__(self) -> None:
        super().__init__(id="transcript")
        self._anchored = True

    @property
    def follow(self) -> bool:
        """True while the view is pinned to the newest content."""
        return self._anchored and not self._anchor_released

    def watch_virtual_size(self) -> None:
        # The compositor pins an anchored widget during layout, but only when it
        # repaints; a growth spurt with no repaint would leave the tail off
        # screen, so nudge it here as well.
        if self.follow:
            self.scroll_end(animate=False, immediate=True)

    def scroll_wheel(self, direction: int, step: float) -> None:
        """One wheel notch, for when the pointer is not over the transcript.

        ``_check_anchor`` only re-engages the anchor from ``watch_scroll_y``,
        which does not fire once the position is clamped at the bottom — so a
        run of notches that all land on the same clamped value would leave the
        anchor released even though the reader is back at the tail. Re-checking
        here closes that gap.
        """
        self.scroll_relative(y=direction * step, animate=False, immediate=True)
        self._check_anchor()

    def follow_tail(self) -> None:
        """Resume following and jump to the newest content."""
        self.anchor(True)

    def clear_turns(self) -> None:
        for child in list(self.children):
            if isinstance(child, TurnWidget):
                child.remove()


class TurnWidget(Vertical):
    """One turn: prompt, then the activity stream in event order — thinking,
    tool calls, more thinking as the model reacts to results, then the answer.

    The order comes from ``Turn.segments`` (the chronological record). A
    consecutive run of reasoning renders as one thinking block; a tool call
    interrupts it, so the next reasoning chunk starts a *new* block below the
    tool — the interleaved shape pi shows. Blocks are mounted on demand, each
    placed explicitly after the previous one; otherwise the lazily mounted
    blocks would land after the user band and reorder the turn.
    """

    def __init__(self, turn: Turn) -> None:
        super().__init__(classes="turn")
        self.turn = turn
        self._thinking: ThinkingBlock | None = None
        self._thinking_wrap: Collapsible | None = None
        self._thinking_by_segment: dict[int, ThinkingBlock] = {}
        self._open_reasoning: int | None = None
        self._tool_calls: dict[str, ToolCall] = {}
        self._reply: ReplyBlock | None = None
        self._error: Static | None = None
        self._queue: list = []
        self._tail = None

    def compose(self) -> ComposeResult:
        yield UserBand(self.turn.user)

    def on_mount(self) -> None:
        self._drain()

    def _mount_block(self, widget) -> None:
        """Queue ``widget`` behind the user band, in creation order.

        Events can arrive before this widget has been composed (reasoning and
        tool calls often do), so mounting is queued until the user band exists.
        """
        self._queue.append(widget)
        self._drain()

    def _drain(self) -> None:
        user = self.query_one_optional(UserBand)
        if user is None:
            return
        tail = self._tail or user
        while self._queue:
            widget = self._queue.pop(0)
            self.mount(widget, after=tail)
            tail = widget
        self._tail = tail

    # Blocks are mounted on demand so an empty turn costs nothing.
    def thinking_block(self) -> ThinkingBlock:
        """The thinking block for a *new* reasoning run.

        Called when reasoning (re)starts: consecutive reasoning events reuse
        the open block, but a tool call closes it — see ``refresh_from_model``
        — so the next chunk after the tool gets a fresh block below it.
        """
        if self._thinking is None:
            self._thinking = ThinkingBlock()
            self._thinking_wrap = Collapsible(self._thinking, title="thinking",
                                              collapsed=False,
                                              classes="thinking-wrap")
            self._mount_block(self._thinking_wrap)
        return self._thinking

    def tool_call(self, record: ToolRecord) -> ToolCall:
        """The inline block for one tool call, created on first sight."""
        block = self._tool_calls.get(record.call_id)
        if block is None:
            block = ToolCall(record)
            self._tool_calls[record.call_id] = block
            self._mount_block(block)
        return block

    def reply_block(self) -> ReplyBlock:
        if self._reply is None:
            self._reply = ReplyBlock()
            self._mount_block(self._reply)
        return self._reply

    def show_error(self, message: str) -> None:
        if self._error is None:
            self._error = Static(classes="turn-error")
            self._mount_block(self._error)
        self._error.update(f"✗ {message}")

    def refresh_from_model(self) -> None:
        """Pull the turn's current state into the widgets.

        Walks the chronological segment list instead of the flat tool registry,
        so the display order matches the order events actually arrived. A tool
        call closes the open reasoning run: the next reasoning segment mounts a
        fresh thinking block *below* the tool call — the interleaved pi layout.
        ``refresh_from_model`` re-runs on every event, so each segment maps to
        its block by index and reasoning runs stream into the block they
        opened; nothing is created twice.
        """
        turn = self.turn
        for index, segment in enumerate(turn.segments):
            if segment.kind == "reasoning":
                block = self._thinking_by_segment.get(index)
                if block is None:
                    # A tool call between reasoning chunks closes the open run.
                    self._thinking = None
                    self._thinking_wrap = None
                    block = self.thinking_block()
                    self._thinking_by_segment[index] = block
                    self._open_reasoning = index
                block.set_text(segment.text)
            else:
                record = segment.tool
                assert record is not None
                if self._open_reasoning is not None:
                    # Reasoning resumes after this call in a new block below.
                    self._open_reasoning = None
                self.tool_call(record).update_record(record)
        if turn.reply:
            self.reply_block().set_text(turn.reply)
        if turn.error:
            self.show_error(turn.error)

    def set_reply_markdown(self, text: str) -> None:
        self.reply_block().set_text(text)
