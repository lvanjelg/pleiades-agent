"""The PLEIADES Textual application.

Replaces the hand-rolled rich ``Live`` frontend. The framework owns the screen,
so wrapping, clipping, scrolling and resize are its problem — the machinery in
the legacy ``tui.py`` (fixed-height frames, tail-keeping, measuring with
ConsoleOptions, cached renderables) exists only because ``Live`` counts lines to
erase and has no screen buffer.

Layout is the same flat, borderless one pi uses: tinted prompt block, italic
thinking, tinted tool bands, markdown answer, one dim footer line.
"""
from __future__ import annotations

from functools import partial
from typing import Iterable

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from presentation import MD_COLOR as MD, PI, format_tokens, thinking_label
from textual_tui.events import DeferredSink, HarnessEvent, TextualSink
from textual_tui.model import Transcript
from textual_tui.widgets import (CommandSuggester, StatusLine,
                                 Transcript as TranscriptView, TurnWidget)
from ux import EventKind

USER_BG = PI["userMessageBg"]

_GUARD_INSTALLED = False


def guard_detached_hit_test() -> None:
    """Make the mouse hit test ignore widgets that have been detached.

    Textual's ``Screen._forward_event`` turns a hit test into a selection with::

        container = content_widget.parent
        ... container.region.offset ...

    The compositor's hit-test map is only rebuilt when the screen refreshes, so a
    widget removed since the last paint (markdown blocks are removed and
    re-mounted on every content update, turns are removed by ``/clear``) can
    still be returned — with no parent. Textual then dereferences it and the app
    dies with ``AttributeError: 'NoneType' object has no attribute 'region'`` on a
    plain click, which is exactly what happened in a live terminal while an
    answer was streaming.

    Returning ``(None, None)`` is semantically right (a detached widget is not
    under the cursor) and takes the non-selecting branch in every caller —
    ``MouseDown``, ``MouseMove`` and ``MouseUp`` all treat ``None`` as "no widget
    here", and ``get_selected_text`` already skips detached widgets.

    Idempotent. Delete this once Textual guards the ``None`` itself; the
    reproduction is in ``tests/test_textual_tui.py``.
    """
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return

    original = Screen.get_widget_and_offset_at

    def guarded(self, x: int, y: int):
        widget, offset = original(self, x, y)
        if widget is not None and widget.parent is None:
            return None, None
        return widget, offset

    guarded.__doc__ = original.__doc__
    guarded._textual_pleiades_guard = True  # type: ignore[attr-defined]
    Screen.get_widget_and_offset_at = guarded
    _GUARD_INSTALLED = True

HELP_TEXT = """\
  commands

    /provider /p [name]   show or switch backend
    /model /m [name]      show or switch model
    /graph /g             cross-session graph summary
    /usage /u             token usage and context
    /clear /c             clear the transcript
    /thinking /t [level]  show or set thinking (off|low|medium|high or on|off)
    /help /h              this help
    /stop /s              stop the running turn (quit: ctrl+q)

  keys

    enter                 send
    esc                   focus the prompt / close a dialog
    ctrl+p                command palette
    pageup / pagedown     scroll the transcript
    ctrl+home             jump back to the newest output
    ctrl+c                quit

  Scrolling follows the tail while a turn streams. Scroll up and following
  stops until you return to the bottom or send another prompt.
"""

# Command palette entries: (command, label). The label is what gets matched and
# shown, the command is what runs — so the palette also teaches the shortcuts.
PALETTE = [
    ("/provider", "Switch provider"),
    ("/model", "Switch model"),
    ("/graph", "Show graph summary"),
    ("/usage", "Show usage"),
    ("/clear", "Clear transcript"),
    ("/thinking", "Set thinking level"),
    ("/help", "Help"),
    ("/stop", "Stop the running turn"),
]


class PickerScreen(ModalScreen[str]):
    """Generic chooser used by /provider and /model."""

    BINDINGS = [Binding("escape", "dismiss_picker", "close")]

    def __init__(self, title: str, options: Iterable[tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._options = list(options)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title")
            yield OptionList(*[Option(label, id=value)
                               for label, value in self._options])

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)


class GraphScreen(ModalScreen[None]):
    """Cross-session graph digest, scrollable and selectable."""

    BINDINGS = [Binding("escape", "close", "close"), Binding("q", "close", "close")]

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("cross-session graph", classes="dialog-title")
            with VerticalScroll():
                yield Static(self._text, id="graph-body")

    def action_close(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """The /help text as a screen, so it never pollutes the transcript."""

    BINDINGS = [Binding("escape", "close", "close"), Binding("q", "close", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("pleiades", classes="dialog-title")
            with VerticalScroll():
                yield Static(HELP_TEXT, id="help-body")

    def action_close(self) -> None:
        self.dismiss(None)


class PleiadesCommands(Provider):
    """Command-palette entries for the slash commands."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        app = self.screen.app
        for title in app.palette_titles():
            score = matcher.match(title)
            if score > 0:
                yield Hit(score, matcher.highlight(title),
                          partial(app.palette_invoke, title), text=title)


class PleiadesApp(App[None]):
    """Full-screen agent frontend."""

    CSS = f"""
    Screen {{ layout: vertical; }}
    #transcript {{ height: 1fr; padding: 0 1;
                   scrollbar-size-vertical: 1;
                   scrollbar-background: transparent;
                   scrollbar-color: {PI['borderMuted']};
                   scrollbar-color-hover: {PI['accent']};
                   scrollbar-color-active: {PI['accent']}; }}

    /* Vertical/Horizontal default to height: 1fr, which makes each band fight
       for the viewport and starve its siblings. Every transcript band sizes to
       its own content; only the transcript itself fills. */
    .turn, .user-band, .reply, .thinking, .tool-call, .tool-output,
    .thinking-wrap, Collapsible, Collapsible > Contents
        {{ height: auto; }}

    /* Textual renders markdown as widgets with its own blue heading default;
       restate the pi palette on the real selectors (MD_COLOR in presentation). */
    Markdown {{ padding: 0; margin: 0; background: transparent; }}
    MarkdownH1, MarkdownH2, MarkdownH3, MarkdownH4, MarkdownH5, MarkdownH6
        {{ color: {MD['heading']}; text-style: bold; padding: 0; margin: 0 0 1 0; }}
    MarkdownParagraph {{ margin: 0; }}
    MarkdownBlockQuote, .markdown-quote
        {{ color: {MD['quote']}; margin: 0; border: none; padding: 0 0 0 2; }}
    MarkdownBullet, MarkdownBulletList, MarkdownOrderedList
        {{ color: {MD['bullet']}; margin: 0; padding: 0; }}
    MarkdownBlock > .code_inline, MarkdownBlock > .code
        {{ color: {MD['code']}; background: transparent; }}
    MarkdownBlock > .strong {{ color: {MD['strong']}; text-style: bold; }}
    MarkdownBlock > .em {{ color: {MD['em']}; text-style: italic; }}
    MarkdownFence {{ margin: 0; padding: 0 0 0 2; background: transparent; }}
    MarkdownFence > Label {{ color: {MD['code_block']}; }}

    #logo {{ color: {PI['accent']}; }}
    #banner-info {{ color: {PI['muted']}; margin: 0 0 1 0; }}
    #banner-hint {{ color: {PI['dim']}; text-style: italic; margin: 0 0 1 0; }}

    .turn {{ margin: 0 0 1 0; }}
    .user-band {{ background: {USER_BG}; padding: 1 1; margin: 0 0 1 0; }}
    .thinking-wrap, .tool-call {{ border: none; padding: 0; margin: 0 0 1 0;
                                  background: transparent; }}
    .thinking-wrap > Contents, .tool-call > Contents {{ padding: 0 0 0 2; }}
    CollapsibleTitle {{ color: {PI['muted']}; padding: 0 1 0 0; }}
    .thinking {{ color: {PI['thinkingText']}; text-style: italic;
                 background: transparent; }}
    .tool-call.-run > CollapsibleTitle {{ color: {PI['muted']}; }}
    .tool-call.-ok > CollapsibleTitle {{ color: {PI['success']}; }}
    .tool-call.-err > CollapsibleTitle {{ color: {PI['error']}; }}
    .tool-output {{ color: {PI['toolOutput']}; }}
    /* The reply sits directly in the turn (no Collapsible), so it needs the
       same 2-column indent the Collapsible Contents give thinking and tool
       calls — otherwise the answer is flush left while everything above it is
       indented, which reads as broken spacing. */
    .reply {{ margin: 0; padding: 0 0 0 2; }}
    .turn-error {{ color: {PI['error']}; text-style: bold; }}

    #status-row {{ height: 1; padding: 0 1; }}
    #status-left {{ width: 1fr; }}
    #status-right {{ width: auto; text-align: right; }}
    #prompt-row {{ height: auto; padding: 0 1; }}
    #prompt-marker {{ width: auto; padding: 0 1 0 0; color: {PI['accent']};
                      text-style: bold; }}
    #prompt {{ border: none; height: 1; background: transparent; padding: 0; }}

    .dialog {{ width: 80%; max-width: 100; height: auto; max-height: 80%;
               background: $surface; border: round {PI['borderMuted']}; padding: 1 2; }}
    .dialog-title {{ color: {PI['accent']}; text-style: bold; padding: 0 0 1 0; }}
    #graph-body, #help-body {{ height: auto; color: {PI['muted']}; }}
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "quit"),
        Binding("ctrl+l", "clear_transcript", "clear"),
        Binding("ctrl+home", "follow_tail", "newest", show=False),
        Binding("escape", "focus_prompt", "prompt", show=False),
        Binding("pageup", "scroll_up", "scroll", show=False),
        Binding("pagedown", "scroll_down", "scroll", show=False),
    ]
    COMMANDS = {PleiadesCommands}
    ALLOW_SELECT = True

    def __init__(self, harness, logo: str = "") -> None:
        super().__init__()
        guard_detached_hit_test()
        # Three lines per wheel notch: Textual's default of two needs ~11 notches
        # to traverse a screenful, which feels like the wheel is not working.
        self.scroll_sensitivity_y = 3.0
        self.harness = harness
        self.logo = logo
        self.transcript = Transcript()
        self._stream_dirty = False
        self.model = str(getattr(harness, "model", ""))
        self.provider = str(getattr(getattr(harness, "provider", None), "label", ""))
        self.session = str(getattr(harness, "session_id", ""))[:8]
        self.context = int(getattr(harness, "context", 0) or 0)

    # ------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        with TranscriptView() as transcript:
            if self.logo:
                yield Static(self.logo, id="logo")
            yield Static(self._banner_info(), id="banner-info")
            yield Static("/help for commands  ·  /stop to quit", id="banner-hint")
        yield StatusLine()
        with Horizontal(id="prompt-row"):
            yield Label("pleiades ›", id="prompt-marker")
            yield Input(placeholder="message or /command", id="prompt",
                        suggester=CommandSuggester(self))

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        self.set_interval(0.1, self._tick)
        self._refresh_status()

    def _banner_info(self) -> str:
        parts = ["PLEIADES"]
        if self.provider:
            parts.append(self.provider)
        parts.append(self.model)
        parts.append(f"session {self.session}")
        return "  ·  ".join(part for part in parts if part)

    # ------------------------------------------------------------ harness
    def _sync_from_harness(self) -> None:
        self.model = str(getattr(self.harness, "model", ""))
        self.provider = str(getattr(getattr(self.harness, "provider", None), "label", ""))
        self.context = int(getattr(self.harness, "context", 0) or 0)
        self.query_one("#banner-info", Static).update(self._banner_info())

    @property
    def thinking(self) -> str:
        return thinking_label(getattr(self.harness, "provider", None))

    def _usage(self) -> tuple[int, int, int]:
        return (int(getattr(self.harness, "input", 0) or 0),
                int(getattr(self.harness, "output", 0) or 0),
                int(getattr(self.harness, "usage", 0) or 0))

    def _refresh_status(self) -> None:
        usage_in, usage_out, ctx_used = self._usage()
        self.query_one(StatusLine).update_state(
            busy=self.transcript.busy, status=self.transcript.status,
            usage_in=usage_in, usage_out=usage_out, ctx_used=ctx_used,
            context=self.context, provider=self.provider, model=self.model,
            thinking=self.thinking)

    def _current_widget(self) -> TurnWidget | None:
        turn = self.transcript.current
        if turn is None:
            return None
        for child in reversed(self.query_one(TranscriptView).children):
            if isinstance(child, TurnWidget) and child.turn is turn:
                return child
        return None

    def _turn_widget(self, turn) -> TurnWidget:
        widget = TurnWidget(turn)
        self.query_one(TranscriptView).mount(widget)
        return widget

    # -------------------------------------------------------------- events
    def on_harness_event(self, message: HarnessEvent) -> None:
        event = message.event
        turn_before = self.transcript.current
        self.transcript.apply(event.kind, event.data)

        if self.transcript.current is not turn_before or turn_before is None:
            widget = self._turn_widget(self.transcript.current)
        else:
            widget = self._current_widget()
        if widget is None:
            self._refresh_status()
            return

        if event.kind is EventKind.TOKEN:
            # Coalesce: markdown parsing on every token is what made the legacy
            # live preview cost ~19ms per frame; the timer flushes instead.
            self._stream_dirty = True
        elif event.kind in (EventKind.REASONING, EventKind.TOOL_CALL,
                            EventKind.TOOL_RESULT, EventKind.MESSAGE):
            widget.refresh_from_model()
            self._autoscroll()
        elif event.kind is EventKind.ERROR:
            widget.show_error(self.transcript.current.error)
            self.notify(self.transcript.current.error, title="error",
                        severity="error")
        elif event.kind is EventKind.DONE:
            self._flush_stream()
            widget.refresh_from_model()
            self.transcript.snapshot_usage(**self._to_snapshot())
            self._refresh_status()
            self._autoscroll()

        self._refresh_status()

    def _to_snapshot(self) -> dict:
        usage_in, usage_out, ctx_used = self._usage()
        return {"usage_in": usage_in, "usage_out": usage_out, "ctx_used": ctx_used}

    def _tick(self) -> None:
        if self._stream_dirty:
            self._flush_stream()

    def _flush_stream(self) -> None:
        self._stream_dirty = False
        widget = self._current_widget()
        turn = self.transcript.current
        if widget is None or turn is None:
            return
        widget.set_reply_markdown(turn.reply)
        self._autoscroll()

    def _autoscroll(self) -> None:
        transcript = self.query_one(TranscriptView)
        if transcript.follow:
            transcript.follow_tail()

    # ------------------------------------------------------------- scrolling
    # Mouse-scroll events bubble, so reaching the app means no widget under the
    # pointer wanted them. Before this, the wheel did nothing whenever the pointer
    # sat on the prompt or the status row — exactly where it is after typing —
    # which reads as "the app cannot scroll".
    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._wheel(-1)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._wheel(+1)

    def _wheel(self, direction: int) -> None:
        if len(self.screen_stack) > 1:
            return  # a dialog owns the wheel; leave the transcript alone
        self.query_one(TranscriptView).scroll_wheel(
            direction, self.scroll_sensitivity_y)

    # ---------------------------------------------------------------- turns
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        command, _, arg = text.partition(" ")
        if command.startswith("/"):
            self._dispatch(command.lower(), arg.strip())
            return
        self._start_turn(text)

    def _start_turn(self, text: str) -> None:
        # The turn widget is created by the harness's own `user` event, so the
        # frontend and the model agree on when a turn began.
        self.query_one(TranscriptView).follow_tail()
        self._run_turn(text)

    @work(thread=True, exclusive=True)
    def _run_turn(self, text: str) -> None:
        """Run one turn off the UI thread (the harness is blocking HTTP)."""
        try:
            self.harness.run(text)
        except Exception as exc:  # surfaced as an error line + toast
            from textual_tui.events import HarnessEvent as _HarnessEvent
            from ux import Event
            self.post_message(_HarnessEvent(Event(
                kind=EventKind.ERROR,
                data={"message": f"{type(exc).__name__}: {exc}"})))

    # ------------------------------------------------------------- commands
    def _dispatch(self, command: str, arg: str) -> None:
        if command in ("/stop", "/s"):
            self._stop_turn()
        elif command in ("/quit", "/q", "/exit"):
            self.exit()
        elif command in ("/help", "/h", "/?"):
            self.push_screen(HelpScreen())
        elif command in ("/clear", "/c"):
            self.action_clear_transcript()
        elif command in ("/usage", "/u"):
            self._show_usage()
        elif command in ("/graph", "/g"):
            self._show_graph()
        elif command in ("/provider", "/p"):
            self._provider_command(arg)
        elif command in ("/model", "/m"):
            self._model_command(arg)
        elif command in ("/thinking", "/t"):
            self._thinking_command(arg)
        else:
            self.notify(f"unknown command {command}", title="error",
                        severity="warning")

    def _show_usage(self) -> None:
        usage_in, usage_out, ctx_used = self._usage()
        parts = [f"↑{format_tokens(usage_in)} ↓{format_tokens(usage_out)} tokens"]
        if self.context:
            pct = (ctx_used * 100.0 / self.context) if ctx_used else 0.0
            parts.append(f"context {pct:.1f}% of {format_tokens(self.context)}")
        self.notify("  ·  ".join(parts), title="usage")

    def _show_graph(self) -> None:
        # Uses the guarded harness wrapper: the legacy frontend called
        # harness.graph.query directly and would crash on any store error.
        self.push_screen(GraphScreen(self.harness.graph_query("summary")))

    def _provider_command(self, arg: str) -> None:
        if arg:
            self._switch_provider(arg)
            return
        options = [(name, name) for name in self.harness.available_providers()]
        self.push_screen(PickerScreen("switch provider", options),
                         self._on_provider_picked)

    def _on_provider_picked(self, name: str | None) -> None:
        if name:
            self._switch_provider(name)

    @work(thread=True, exclusive=True)
    def _switch_provider(self, name: str) -> None:
        # resolve_context_length() can issue a network probe for the local
        # provider, so this must not run in a message handler.
        try:
            line = self.harness.switch_provider(name)
        except ValueError as exc:
            self.call_from_thread(self.notify, str(exc), title="error",
                                  severity="error")
            return
        self.call_from_thread(self._after_switch, line)

    def _model_command(self, arg: str) -> None:
        if arg:
            self._switch_model(arg)
            return
        self._fetch_models()

    @work(thread=True, exclusive=True)
    def _fetch_models(self) -> None:
        models = self.harness.available_models()
        self.call_from_thread(self._open_model_picker, list(models))

    def _open_model_picker(self, models: list[str]) -> None:
        if not models:
            self.notify("provider advertises no models", title="model",
                        severity="warning")
            return
        options = [(name, name) for name in models]
        self.push_screen(PickerScreen("switch model", options), self._on_model_picked)

    def _on_model_picked(self, name: str | None) -> None:
        if name:
            self._switch_model(name)

    @work(thread=True, exclusive=True)
    def _switch_model(self, name: str) -> None:
        try:
            line = self.harness.switch_model(name)
        except ValueError as exc:
            self.call_from_thread(self.notify, str(exc), title="error",
                                  severity="error")
            return
        self.call_from_thread(self._after_switch, line)

    def _after_switch(self, line: str) -> None:
        self._sync_from_harness()
        self._refresh_status()
        self.notify(line, title="switched")

    # ------------------------------------------------------------- stop
    def _stop_turn(self) -> None:
        """Cancel the running turn (cooperative — takes effect at the next
        check point in the harness loop) or quit if the app is idle."""
        if self.transcript.busy and hasattr(self.harness, "cancel"):
            self.harness.cancel()
            self.notify("stopping… (finishes at the next tool boundary)",
                        title="stop", severity="warning")
        else:
            self.notify("no turn running — ctrl+q quits", title="stop",
                        severity="warning")

    # ---------------------------------------------------------- thinking
    def _thinking_command(self, arg: str) -> None:
        provider = getattr(self.harness, "provider", None)
        levels = provider.thinking_levels() if hasattr(provider, "thinking_levels") else []
        if not arg:
            current = self.thinking or "n/a"
            self.notify(f"thinking: {current}"
                        + (f"  ·  levels: {' '.join(levels)}" if levels else ""),
                        title="thinking")
            return
        try:
            line = self.harness.set_thinking(arg)
        except (ValueError, AttributeError) as exc:
            self.notify(str(exc), title="thinking", severity="error")
            return
        self._refresh_status()
        self.notify(f"thinking → {line}", title="thinking")

    # ---------------------------------------------------------- palette glue
    def palette_titles(self) -> list[str]:
        return [f"{command}  {label}" for command, label in PALETTE]

    def palette_invoke(self, title: str) -> None:
        # Titles carry their command, so the palette teaches the shortcuts and
        # there is no second table to keep in sync.
        self._dispatch(title.split(" ", 1)[0], "")

    # ------------------------------------------------------------- actions
    def action_clear_transcript(self) -> None:
        self.query_one(TranscriptView).clear_turns()
        self.transcript.reset()
        self._refresh_status()

    def action_follow_tail(self) -> None:
        self.query_one(TranscriptView).follow_tail()

    def action_focus_prompt(self) -> None:
        self.query_one("#prompt", Input).focus()

    def action_scroll_up(self) -> None:
        self.query_one(TranscriptView).scroll_page_up(animate=False)

    def action_scroll_down(self) -> None:
        self.query_one(TranscriptView).scroll_page_down(animate=False)


def run_textual_tui(*, provider=None, model=None) -> None:
    """Entry point for ``python main.py --tui``."""
    import main  # local import: avoids a cycle at module import time

    sink = DeferredSink()
    harness = main._build_harness(event_bus=sink, provider=provider, model=model)
    app = PleiadesApp(harness=harness, logo=getattr(main, "logo", ""))
    sink.attach(TextualSink(app))
    app.run()
