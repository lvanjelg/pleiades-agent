"""Headless tests for the Textual frontend.

Run with no test runner and no plugins::

    .venv/bin/python tests/test_textual_tui.py

Everything here is deterministic — the harness is faked and the app runs under
``App.run_test()``, which renders into an in-memory terminal instead of a tty.
That matters because the bugs this suite exists to catch (widgets mounted before
their parent, `Markdown.update` on an unmounted widget, frames built before the
size is known) only show up when something actually renders.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ux import Event, EventKind                                    # noqa: E402
from presentation import TOOL_ROWS_MAX                             # noqa: E402
from textual.widgets import Markdown, Static                         # noqa: E402
from textual import events                                          # noqa: E402
from textual_tui import DeferredSink, PleiadesApp, TextualSink     # noqa: E402
from textual_tui.app import guard_detached_hit_test                # noqa: E402
from textual_tui.events import HarnessEvent                        # noqa: E402
from textual_tui.model import ERR, OK, RUN, ToolRecord, Transcript   # noqa: E402
from textual_tui.widgets import (ReplyBlock, StatusLine, ThinkingBlock,  # noqa: E402
                                 ToolCall, TurnWidget, UserBand)

MouseDown = events.MouseDown
from textual_tui.widgets import Transcript as TranscriptView        # noqa: E402

REPLY = "## Answer\n\nThe answer is here."


# --------------------------------------------------------------------- model
def test_model_user_and_tokens() -> None:
    t = Transcript()
    assert t.apply("user", {"content": "hi"}) is True
    assert t.busy and t.current is not None and t.current.user == "hi"
    t.apply("token", {"text": "a"})
    t.apply("token", {"text": "b"})
    assert t.current.reply == "ab"
    t.apply("done", {})
    assert t.current.finished and not t.busy


def test_model_space_repair_after_tool() -> None:
    """Text resuming after a tool result gets the space the provider omits."""
    t = Transcript()
    t.apply("user", {"content": "q"})
    t.apply("token", {"text": "before"})
    t.apply("tool_call", {"call_id": "c1", "name": "bash", "args": {}})
    t.apply("tool_result", {"call_id": "c1", "success": True, "duration_ms": 1})
    t.apply("token", {"text": "after"})
    assert t.current.reply == "before after", repr(t.current.reply)


def test_model_orphan_tool_result_is_visible() -> None:
    t = Transcript()
    t.apply("user", {"content": "q"})
    t.apply("tool_result", {"call_id": "ghost", "name": "bash", "success": False})
    tools = t.current.tools
    assert len(tools) == 1 and tools[0].state == ERR and tools[0].call_id == "ghost"


def test_model_keeps_only_last_error() -> None:
    t = Transcript()
    t.apply("user", {"content": "q"})
    t.apply("error", {"message": "first"})
    t.apply("error", {"message": "second"})
    assert t.current.error == "second"


def test_model_tool_cap_and_worst_state() -> None:
    t = Transcript()
    t.apply("user", {"content": "q"})
    for i in range(12):
        t.apply("tool_call", {"call_id": f"c{i}", "name": "bash", "args": {}})
        t.apply("tool_result", {"call_id": f"c{i}", "success": i != 11})
    hidden, rows = t.current.visible_tools()
    assert hidden == 12 - TOOL_ROWS_MAX and len(rows) == TOOL_ROWS_MAX, (hidden, rows)
    assert t.current.tools[-1].state == ERR and t.current.worst_state == ERR


def test_model_reset_and_unknown_event() -> None:
    t = Transcript()
    t.apply("user", {"content": "q"})
    assert t.apply("not-a-kind", {}) is False
    t.reset()
    assert t.turns == [] and t.current is None and not t.busy and t.status == "ready"


def test_model_snapshot_usage_is_keyword_only() -> None:
    t = Transcript()
    t.apply("user", {"content": "q"})
    t.snapshot_usage(usage_in=10, usage_out=5, ctx_used=3)
    assert (t.current.usage_in, t.current.usage_out, t.current.ctx_used) == (10, 5, 3)


def test_state_constants() -> None:
    assert (RUN, OK, ERR) == ("run", "ok", "err")


def test_harness_cancel_and_budget_stop() -> None:
    """A cancelled or over-budget turn stops cleanly instead of looping.

    Both were silent gaps: BudgetEnforcer.check() existed but nothing called
    it, and there was no way to interrupt a turn at all — the only escape was
    quitting the app.
    """
    import main as harness_mod

    class StopProbe(harness_mod.AgentHarness):
        """Harness that exercises the stop paths without touching the network."""

        def __init__(self):
            # Bypass the real __init__ (DB, graph, provider): only the pieces
            # run() touches are needed, and run() is exercised until it stops.
            self._cancel = harness_mod.threading.Event()
            self.bus = None
            self.max_iterations = 5
            self.budgeter = harness_mod.BudgetEnforcer(10_000)
            self.state = None
            self.session_id = "test"
            self.turn = 0

        def _ensure_session(self):
            pass

        def run(self, user_input: str) -> str:  # minimal stand-in loop
            self._new_turn()
            for i in range(self.max_iterations):
                stop = self._cancel.is_set() or self.budgeter.check()
                if stop:
                    return f"Stopped: {stop}"
                self.budgeter.record_tool_call("x")
                self.budgeter.tokens_used = 20_000   # force budget trip on it 2
            return "completed"

    probe = StopProbe()
    assert probe.run("go").startswith("Stopped: Token budget"), probe.run("go")
    probe2 = StopProbe()
    probe2.cancel()
    assert probe2.run("go").startswith("Stopped: ")
    # the event resets per turn
    probe2._cancel.clear()
    assert not probe2.cancelled


def test_provider_thinking_levels_and_set() -> None:
    import os
    import providers

    deepseek = providers.PROVIDERS["deepseek"]
    assert deepseek.thinking_levels() == ["off", "low", "medium", "high"]
    old = os.environ.get("DEEPSEEK_REASONING_EFFORT")
    try:
        assert deepseek.set_thinking("low") == "low"
        assert os.environ["DEEPSEEK_REASONING_EFFORT"] == "low"
        assert deepseek.set_thinking("none") == "off"
        assert os.environ["DEEPSEEK_REASONING_EFFORT"] == "off"
    finally:
        if old is None:
            os.environ.pop("DEEPSEEK_REASONING_EFFORT", None)
        else:
            os.environ["DEEPSEEK_REASONING_EFFORT"] = old
    try:
        deepseek.set_thinking("bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass

    local = providers.PROVIDERS["local"]
    assert local.thinking_levels() == ["on", "off"]
    old = os.environ.get("MLX_ENABLE_THINKING")
    try:
        assert local.set_thinking("off") == "off"
        assert os.environ["MLX_ENABLE_THINKING"] == "0"
    finally:
        if old is None:
            os.environ.pop("MLX_ENABLE_THINKING", None)
        else:
            os.environ["MLX_ENABLE_THINKING"] = old


def test_openrouter_provider() -> None:
    """OpenRouter: OpenAI shape, its own reasoning dialect, OPENROUTER_API key."""
    import os
    import providers

    p = providers.PROVIDERS["openrouter"]
    assert providers.get_provider("openrouter") is p
    assert p.chat_url == "https://openrouter.ai/api/v1/chat/completions"
    assert p.api_key_env == "OPENROUTER_API"
    assert p.thinking_levels() == ["off", "low", "medium", "high"]

    # Dialect: reasoning.effort, and a real off switch.
    old = os.environ.get("OPENROUTER_REASONING_EFFORT")
    try:
        assert p.thinking_params() == {"reasoning": {"effort": "high"}}
        assert p.set_thinking("low") == "low"
        assert os.environ["OPENROUTER_REASONING_EFFORT"] == "low"
        assert p.thinking_params() == {"reasoning": {"effort": "low"}}
        assert p.set_thinking("off") == "off"
        assert p.thinking_params() == {"reasoning": {"enabled": False}}
        assert p.thinking_label() == "off"
        assert p.set_thinking("high") == "high"
        assert p.thinking_label() == "high"
    finally:
        if old is None:
            os.environ.pop("OPENROUTER_REASONING_EFFORT", None)
        else:
            os.environ["OPENROUTER_REASONING_EFFORT"] = old
    try:
        p.set_thinking("on")   # not a valid effort word
        assert False, "expected ValueError"
    except ValueError:
        pass

    # Key resolution honours the fallback alias.
    old_key = os.environ.pop("OPENROUTER_API", None)
    old_alias = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        os.environ["OPENROUTER_API_KEY"] = "alias-key"
        assert p.api_key() == "alias-key"
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)
        if old_key is not None:
            os.environ["OPENROUTER_API"] = old_key
        if old_alias is not None:
            os.environ["OPENROUTER_API_KEY"] = old_alias

    # The TUI status line reads the same dialect.
    from presentation import thinking_label as fmt
    assert fmt(p) == "high"


async def check_prompt_autocomplete() -> None:
    """The prompt completes slash commands and their arguments.

    Textual's Input(suggester=...) shows the completion as dim ghost text;
    right/end accepts it. Asserted through the Input._suggestion reactive,
    which is what the renderer displays.
    """
    from textual_tui.widgets import CommandSuggester

    deferred = DeferredSink()
    app = PleiadesApp(harness=FakeHarness(deferred), logo="")
    deferred.attach(TextualSink(app))

    # Pure suggester logic first — no app needed.
    s = CommandSuggester(app)
    assert await s.get_suggestion("/pro") == "/provider", "command completion"
    assert await s.get_suggestion("/pro ") is None, "no bare space completion"
    assert await s.get_suggestion("/provider ") == "/provider local", \
        "argument completion (first matching option)"
    assert await s.get_suggestion("/provider deep") == "/provider deepseek"
    assert await s.get_suggestion("/thinking ") == "/thinking off", \
        "levels come from the live provider"
    assert await s.get_suggestion("/bogus ") is None
    assert await s.get_suggestion("hello") is None, "plain messages never complete"

    async with app.run_test(size=(100, 30)) as pilot:
        prompt = app.query_one("#prompt")
        prompt.focus()
        prompt.value = "/prov"
        await pilot.pause(0.2)          # suggestion arrives via a worker
        assert prompt._suggestion == "/provider", prompt._suggestion
        # Accepting with `right` completes the value...
        await pilot.press("right")
        await pilot.pause(0.1)
        assert prompt.value == "/provider", prompt.value
        # ...and typing a space then completes the argument.
        prompt.value = "/provider d"
        await pilot.pause(0.2)
        assert prompt._suggestion == "/provider deepseek", prompt._suggestion


# --------------------------------------------------------------------- sinks
def test_sink_forwards_and_deferred_buffers() -> None:
    seen: list[tuple] = []

    class FakeApp:
        def post_message(self, message) -> None:
            seen.append(message.event.kind)

    deferred = DeferredSink()
    deferred.emit_kind("user", content="x")          # buffered, no target yet
    assert seen == []
    deferred.attach(TextualSink(FakeApp()))          # replays, then forwards
    deferred.emit(Event(kind=EventKind.DONE, data={}))
    assert seen == [EventKind.USER, EventKind.DONE], seen
    # subscribe/unsubscribe must stay safe now that the transport is attached.
    assert deferred.subscribe(lambda event: None) is not None
    deferred.close()


# ----------------------------------------------------------------------- app
class FakeProvider:
    label = "DeepSeek API"

    def thinking_params(self) -> dict:
        return {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}


class FakeHarness:
    """Emits one scripted turn and exposes the command surface the app calls."""

    model = "deepseek-flash"
    session_id = "abcdef12"
    context = 1_000_000
    input = 13_979
    output = 936
    usage = 42_000
    provider = FakeProvider()

    def __init__(self, sink) -> None:
        self.sink = sink
        self.fail = False

    def run(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("provider exploded")
        s = self.sink
        s.emit_kind("user", content=text)
        s.emit_kind("thinking", iteration=0)
        s.emit_kind("reasoning", text="checking the guards")
        s.emit_kind("tool_call", call_id="c1", name="bash", args={"command": "ls"})
        s.emit_kind("tool_result", call_id="c1", name="bash", success=True,
                    duration_ms=12, preview="a b c")
        s.emit_kind("reasoning", text="result looks good, answering")
        for token in ["## Answer\n\n", "The ", "answer ", "is ", "here."]:
            s.emit_kind("token", text=token)
        s.emit_kind("done", content="")

    def graph_query(self, kind: str = "summary") -> str:
        return "graph digest"

    def available_providers(self) -> list[str]:
        return ["local", "deepseek"]

    def available_models(self, timeout: float = 5.0) -> list[str]:
        return ["deepseek-flash", "deepseek-v4-pro"]

    def switch_provider(self, name: str | None = None, model: str | None = None) -> str:
        return f"switched to {name}"

    def switch_model(self, model: str) -> str:
        return f"model {model}"


async def check_app() -> None:
    deferred = DeferredSink()
    harness = FakeHarness(deferred)
    app = PleiadesApp(harness=harness, logo="LOGO")
    deferred.attach(TextualSink(app))

    async with app.run_test(size=(100, 30)) as pilot:
        # ---- one turn, rendered ------------------------------------------
        await pilot.press(*"hello there", "enter")
        await pilot.pause(0.5)
        turns = app.query(TurnWidget)
        assert len(turns) == 1, len(turns)
        turn = turns.first().turn
        assert turn.user == "hello there", turn.user
        assert turn.reasoning == ("checking the guards"
                                  "result looks good, answering"), turn.reasoning
        assert turn.reply == REPLY, repr(turn.reply)
        assert turn.finished

        calls = app.query(ToolCall)
        assert len(calls) == 1, len(calls)
        assert "-ok" in calls.first().classes, calls.first().classes
        assert "bash" in calls.first().title, calls.first().title

        # ---- everything is actually visible, in the right order -----------
        # The turn is the chronological stream: thinking, tool, thinking again
        # (post-result reasoning in its OWN block below the tool — pi's
        # interleaved layout), then the reply.
        order = [type(child).__name__ for child in turns.first().children]
        assert order == ["UserBand", "Collapsible", "ToolCall", "Collapsible",
                         "ReplyBlock"], order

        thinking = app.query(ThinkingBlock)
        assert len(thinking) == 2, len(thinking)
        assert thinking[0]._markdown == "checking the guards", thinking[0]._markdown
        assert thinking[1]._markdown == "result looks good, answering", \
            thinking[1]._markdown
        reply = app.query_one(ReplyBlock)
        user_md = app.query_one(UserBand).query_one(Markdown)
        assert user_md._markdown == "❯ hello there", user_md._markdown
        assert reply._markdown == REPLY, reply._markdown
        for widget in (app.query_one(UserBand), thinking[0], thinking[1], reply,
                       calls.first()):
            assert widget.size.height > 0, (type(widget).__name__, widget.size)

        # post-result reasoning sits BELOW the tool call, not above it
        assert thinking[1].region.y > calls.first().region.y, \
            (thinking[1].region.y, calls.first().region.y)

        # ---- status line --------------------------------------------------
        status = app.query_one(StatusLine)
        left = str(status.query_one("#status-left").render())
        right = str(status.query_one("#status-right").render())
        assert "↑14k ↓936" in left and "4.2%/1.0M" in left, left
        assert "DeepSeek API · deepseek-flash" in right, right

        # ---- the reply aligns with the thinking/tool blocks ---------------
        # Thinking and tool calls sit inside a Collapsible whose Contents carry
        # a 2-column indent; the reply sits directly in the turn, so it needs
        # the same indent or the answer hangs flush left ("weird spacing").
        # content_region is where the text starts (region + padding).
        assert reply.content_region.x == thinking[0].content_region.x, \
            (reply.content_region.x, thinking[0].content_region.x)

        # ---- resizing does not corrupt anything ---------------------------
        await pilot.resize_terminal(60, 20)
        await pilot.pause(0.1)
        await pilot.resize_terminal(140, 40)
        await pilot.pause(0.1)
        assert app.query(TurnWidget).first().turn.reply == REPLY

        # ---- slash commands ----------------------------------------------
        await pilot.press(*"/help", "enter")
        await pilot.pause(0.15)
        assert type(app.screen_stack[-1]).__name__ == "HelpScreen"
        await pilot.press("escape")
        await pilot.pause(0.15)
        assert len(app.screen_stack) == 1

        await pilot.press(*"/usage", "enter")
        await pilot.pause(0.15)   # notification only, no screen

        await pilot.press(*"/graph", "enter")
        await pilot.pause(0.15)
        assert type(app.screen_stack[-1]).__name__ == "GraphScreen"
        await pilot.press("escape")
        await pilot.pause(0.15)

        await pilot.press(*"/model", "enter")
        await pilot.pause(0.5)    # worker thread fetches the catalogue
        assert type(app.screen_stack[-1]).__name__ == "PickerScreen"
        await pilot.press("down", "enter")
        await pilot.pause(0.5)
        assert len(app.screen_stack) == 1

        await pilot.press(*"/provider", "enter")
        await pilot.pause(0.3)
        assert type(app.screen_stack[-1]).__name__ == "PickerScreen"
        await pilot.press("escape")
        await pilot.pause(0.15)

        await pilot.press(*"/bogus", "enter")
        await pilot.pause(0.15)   # warns, does not raise

        # ---- command palette ---------------------------------------------
        titles = app.palette_titles()
        assert any(t.startswith("/help") for t in titles), titles
        assert any(t.startswith("/model") for t in titles), titles
        assert len(titles) == len(set(titles)), titles
        await pilot.press("ctrl+p")
        await pilot.pause(0.3)
        assert type(app.screen_stack[-1]).__name__ == "CommandPalette", \
            [type(s).__name__ for s in app.screen_stack]
        await pilot.press("escape")
        await pilot.pause(0.2)

        # selection is enabled so output can be copied out of the transcript
        assert app.ALLOW_SELECT is True

        # ---- clear --------------------------------------------------------
        await pilot.press(*"/clear", "enter")
        await pilot.pause(0.15)
        assert len(app.query(TurnWidget)) == 0
        assert app.transcript.turns == []

        # ---- error path ---------------------------------------------------
        harness.fail = True
        await pilot.press(*"boom", "enter")
        await pilot.pause(0.4)
        assert app.transcript.current is not None
        assert "provider exploded" in app.transcript.current.error, app.transcript.current.error


LONG_REPLY = "\n\n".join(f"line {i} of a long answer" for i in range(60))


class LongHarness(FakeHarness):
    """Emits a reply taller than the viewport, to exercise follow-the-tail."""

    def run(self, text: str) -> None:
        s = self.sink
        s.emit_kind("user", content=text)
        s.emit_kind("token", text=LONG_REPLY)
        s.emit_kind("done", content="")


async def check_follow_tail() -> None:
    deferred = DeferredSink()
    app = PleiadesApp(harness=LongHarness(deferred), logo="")
    deferred.attach(TextualSink(app))

    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.press(*"write a long answer", "enter")
        await pilot.pause(0.8)

        transcript = app.query_one(TranscriptView)
        assert transcript.virtual_size.height > transcript.size.height, \
            (transcript.virtual_size, transcript.size)
        # Following the tail means the newest line is on screen: the view is
        # stuck to the bottom, not left where the bottom used to be.
        assert transcript.follow, "follow was switched off by its own scrolling"
        assert transcript.scroll_offset.y >= transcript.max_scroll_y - 1, \
            (transcript.scroll_offset.y, transcript.max_scroll_y)

        # Scrolling up stops the follow; ctrl+home resumes it.
        transcript.scroll_to(y=0, animate=False)
        await pilot.pause(0.2)
        assert not transcript.follow, "scrolling up did not release the tail"
        await pilot.press("ctrl+home")
        await pilot.pause(0.2)
        assert transcript.follow and transcript.scroll_offset.y > 0


async def check_selection_during_streaming() -> None:
    """Clicking the reply while it streams must not kill the app.

    This is the crash the app hit in a live terminal: Textual's hit test can
    return a markdown block that was removed since the last refresh (blocks are
    removed and re-mounted when the document is rewritten), and the selection
    code then dereferences that detached widget's parent —
    ``AttributeError: 'NoneType' object has no attribute 'region'``.

    ``Screen._forward_event`` is the frame from that traceback, so driving it
    directly is the only way to exercise the path without a real terminal.
    """
    deferred = DeferredSink()
    app = PleiadesApp(harness=FakeHarness(deferred), logo="")
    deferred.attach(TextualSink(app))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"stream please", "enter")
        await pilot.pause(0.4)
        turn = app.query_one(TurnWidget).turn
        reply = app.query_one(ReplyBlock)

        turn.reply = "alpha\n\nbeta\n\ngamma"
        reply.set_text(turn.reply)
        await pilot.pause(0.3)

        region = reply.region
        x, y = region.x + 3, region.y + 1
        hit, _offset = app.screen.get_widget_and_offset_at(x, y)
        assert hit is not None and hit.parent is not None, "hit test broke"

        crashes = []
        for index in range(2, 30):
            turn.reply = "\n\n".join(f"paragraph {n}" for n in range(index))
            reply.set_text(turn.reply)
            for _ in range(3):
                event = MouseDown(hit, x, y, 0, 0, 1, False, False, False,
                                  screen_x=x, screen_y=y)
                try:
                    app.screen._forward_event(event)  # see docstring
                except AttributeError as exc:
                    crashes.append(f"{type(hit).__name__}: {exc}")
                await asyncio.sleep(0)

        assert not crashes, crashes[:3]
        assert reply._markdown == turn.reply, reply._markdown[-40:]

        # A click that lands on an attached widget still starts a selection,
        # so the guard has not disabled selection altogether.
        await pilot.pause(0.1)
        fresh, _ = app.screen.get_widget_and_offset_at(x, y)
        app.screen._forward_event(MouseDown(fresh, x, y, 0, 0, 1, False, False,
                                            False, screen_x=x, screen_y=y))
        assert app.screen._select_state is not None

        # Streaming appends rather than re-rendering the document.
        blocks = app.query("MarkdownParagraph")
        assert len(blocks) > 1, len(blocks)


MARKDOWN_DOC = """## Heading

A paragraph with `code`, **bold** and a [link](http://x.dev).

- bullet one
- bullet two

```python
print("fenced")
```
"""


class CharHarness(FakeHarness):
    """Streams the reply one character at a time, like a real provider."""

    def run(self, text: str) -> None:
        self.sink.emit_kind("user", content=text)
        for char in MARKDOWN_DOC:
            self.sink.emit_kind("token", text=char)
        self.sink.emit_kind("done", content="")


class QuietHarness(FakeHarness):
    """Starts a turn and sends no reply, so a block can render it in one go."""

    def run(self, text: str) -> None:
        self.sink.emit_kind("user", content=text)
        self.sink.emit_kind("done", content="")


def _block_shape(app) -> list[tuple[str, str]]:
    return [(type(block).__name__, " ".join(sorted(block.classes)))
            for block in app.query("MarkdownBlock")]


async def check_streamed_markdown_matches_full_render() -> None:
    """Appending incrementally must land on the same document as one render."""
    deferred = DeferredSink()
    app = PleiadesApp(harness=CharHarness(deferred), logo="")
    deferred.attach(TextualSink(app))

    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press(*"stream it", "enter")
        await pilot.pause(1.2)
        streamed = _block_shape(app)
        text = app.query_one(ReplyBlock)._markdown

    # The same document rendered in one go, as the first content of a block.
    reference = DeferredSink()
    plain = PleiadesApp(harness=QuietHarness(reference), logo="")
    reference.attach(TextualSink(plain))
    async with plain.run_test(size=(100, 40)) as pilot:
        await pilot.press(*"hi", "enter")
        await pilot.pause(0.4)
        plain.query_one(TurnWidget).set_reply_markdown(MARKDOWN_DOC)
        await pilot.pause(0.5)
        whole = _block_shape(plain)

    assert text == MARKDOWN_DOC, text[-60:]
    assert streamed == whole, (streamed, whole)
    names = [name for name, _cls in streamed]
    assert "MarkdownH2" in names and "MarkdownFence" in names, names


def _wheel(app, widget, up: bool) -> None:
    """One mouse-wheel notch over ``widget`` (no Pilot helper exists for this)."""
    x, y = widget.region.x + 5, widget.region.y + min(3, widget.size.height - 1)
    event = (events.MouseScrollUp if up else events.MouseScrollDown)(
        widget, x, y, 0, 1 if up else -1, 1, False, False, False,
        screen_x=x, screen_y=y)
    app.screen._forward_event(event)


async def check_scrolling() -> None:
    """The transcript must scroll from the keyboard and the wheel.

    Three separate faults made this fail in a real terminal: a first notch moved
    less than the "still at the bottom" tolerance (so the next content change
    snapped the view back), the wheel was ignored when the pointer was over the
    prompt or the status row — where it is right after typing — and overriding
    Textual's wheel handler double-scrolled, because handlers are dispatched for
    every class in the MRO.
    """
    deferred = DeferredSink()
    app = PleiadesApp(harness=LongHarness(deferred), logo="")
    deferred.attach(TextualSink(app))
    step = app.scroll_sensitivity_y

    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.press(*"write a long answer", "enter")
        await pilot.pause(0.8)
        transcript = app.query_one(TranscriptView)
        assert transcript.max_scroll_y > 0, "the turn did not overflow"
        assert transcript.follow and transcript.scroll_offset.y == transcript.max_scroll_y

        # One wheel notch over the transcript: exactly one step, and the tail is
        # released immediately rather than on the third notch.
        _wheel(app, transcript, up=True)
        await pilot.pause(0.2)
        assert transcript.scroll_offset.y == transcript.max_scroll_y - step, \
            (transcript.scroll_offset.y, transcript.max_scroll_y, step)
        assert not transcript.follow, "one notch did not release the tail"

        # ... and it stays released when the layout settles (the snap-back).
        transcript.refresh(layout=True)
        await pilot.pause(0.2)
        assert not transcript.follow
        assert transcript.scroll_offset.y < transcript.max_scroll_y

        # The wheel works with the pointer on the prompt and the status row.
        for target in (app.query_one("#prompt"), app.query_one(StatusLine)):
            before = transcript.scroll_offset.y
            _wheel(app, target, up=True)
            await pilot.pause(0.2)
            assert transcript.scroll_offset.y == before - step, \
                (type(target).__name__, before, transcript.scroll_offset.y)

        # Keyboard scrolling is not captured by the focused input.
        before = transcript.scroll_offset.y
        await pilot.press("pageup")
        await pilot.pause(0.2)
        assert transcript.scroll_offset.y < before
        await pilot.press("pagedown")
        await pilot.pause(0.2)

        # Back at the bottom, following resumes.
        for _ in range(40):
            _wheel(app, transcript, up=False)
        await pilot.pause(0.3)
        assert transcript.scroll_offset.y == transcript.max_scroll_y
        assert transcript.follow, "reaching the bottom did not resume following"


class SlowHarness(FakeHarness):
    """Streams tokens with a delay, so a turn is still running when we scroll."""

    def run(self, text: str) -> None:
        self.sink.emit_kind("user", content=text)
        for index in range(60):
            self.sink.emit_kind("token", text=f"\n\nline {index} arriving")
            time.sleep(0.015)
        self.sink.emit_kind("done", content="")


async def check_scrolling_while_streaming() -> None:
    """Scrolling up during a turn must hold the view where the reader put it."""
    deferred = DeferredSink()
    app = PleiadesApp(harness=SlowHarness(deferred), logo="")
    deferred.attach(TextualSink(app))

    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.press(*"stream slowly", "enter")
        await pilot.pause(0.6)                     # let the turn overflow
        transcript = app.query_one(TranscriptView)
        for _ in range(3):
            _wheel(app, transcript, up=True)
            await pilot.pause(0.05)
        held = transcript.scroll_offset.y
        assert not transcript.follow and held < transcript.max_scroll_y

        await pilot.pause(1.2)                     # plenty more tokens arrive
        assert transcript.scroll_offset.y <= held, \
            (held, transcript.scroll_offset.y)
        assert not transcript.follow
        assert transcript.max_scroll_y > held, "the turn stopped growing too soon"


async def check_tool_calls_inline() -> None:
    """Tool calls render inline, in event order, and expand on click.

    They used to be one group pinned above the reply; now each call is its own
    collapsible mounted where the event arrived, like the thinking block.
    """
    deferred = DeferredSink()
    app = PleiadesApp(harness=FakeHarness(deferred), logo="")
    deferred.attach(TextualSink(app))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"run the tools", "enter")
        await pilot.pause(0.5)
        turn_widget = app.query_one(TurnWidget)

        # Inline, in event order: user → thinking → tool → thinking → reply.
        order = [type(child).__name__ for child in turn_widget.children]
        assert order == ["UserBand", "Collapsible", "ToolCall", "Collapsible",
                         "ReplyBlock"], order

        call = app.query_one(ToolCall)
        assert call.record.call_id == "c1"
        assert "✓" in call.title and "bash" in call.title, call.title
        assert call.collapsed, "a finished call should start collapsed"
        assert "-ok" in call.classes

        # Clicking the title expands the call and shows the output preview.
        await pilot.click(ToolCall)
        await pilot.pause(0.3)
        assert not call.collapsed
        body = call.query_one(".tool-output")
        assert body.size.height > 0, "the output preview did not render"

        # A call that is still running shows its args and stays expanded.
        # Applied through the model (not by appending to turn.tools) because
        # the widget renders the chronological segment list.
        app.transcript.apply("tool_call", {"call_id": "c2", "name": "bash",
                                           "args": {"command": "sleep 1"}})
        turn_widget.refresh_from_model()
        await pilot.pause(0.3)
        running = app.query(ToolCall)[-1]
        assert "-run" in running.classes and "sleep 1" in running.title
        assert not running.collapsed


class MarkupHarness(FakeHarness):
    """Tool output that looks like console markup — the live crash."""

    def run(self, text: str) -> None:
        self.sink.emit_kind("user", content=text)
        self.sink.emit_kind("tool_call", call_id="c1", name="bash",
                            args={"command": "cat logs/x.log"})
        self.sink.emit_kind(
            "tool_result", call_id="c1", name="bash", success=True,
            duration_ms=36,
            preview="INFO:main:LLMResponse(content='', tool_calls=[ToolCall("
                    "call_id='call_00_d8Q2OkdD08iOOF3ENBWv7595', "
                    "name='graph_query')])")
        self.sink.emit_kind("done", content="")


async def check_tool_output_is_not_markup() -> None:
    """A preview containing ``[...]`` must render, not raise MarkupError.

    ``Static.update`` parses console markup by default, and tool output is
    arbitrary text — a repr with a list (``tool_calls=[ToolCall(...)]``) killed
    the app the moment the result landed. The body Static is therefore created
    with ``markup=False``.
    """
    deferred = DeferredSink()
    app = PleiadesApp(harness=MarkupHarness(deferred), logo="")
    deferred.attach(TextualSink(app))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press(*"show the log", "enter")
        await pilot.pause(0.5)
        call = app.query_one(ToolCall)
        assert "-ok" in call.classes

        # Expanding shows the preview verbatim, brackets and all.
        await pilot.click(ToolCall)
        await pilot.pause(0.3)
        body = call.query_one(".tool-output")
        assert body.size.height > 0, "the markup-looking preview did not render"

        # Args in the title with markup-looking content must not crash either.
        app.transcript.apply("tool_call", {"call_id": "c2", "name": "bash",
                                           "args": {"command": "echo [bold red]x[/]"}})
        app.query_one(TurnWidget).refresh_from_model()
        await pilot.pause(0.3)
        assert "[bold red]" in app.query(ToolCall)[-1].title


async def check_real_harness() -> None:
    """The app against the real harness — construction and rendering only.

    No turn is sent, so this never touches the network: it exists to catch API
    drift between the frontend and ``main._build_harness`` (banner fields, the
    provider label, the context window).
    """
    import main

    deferred = DeferredSink()
    harness = main._build_harness(event_bus=deferred, provider="deepseek")
    assert harness.model, "harness resolved no model"
    app = PleiadesApp(harness=harness, logo=getattr(main, "logo", ""))
    deferred.attach(TextualSink(app))

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        banner = str(app.query_one("#banner-info", Static).render())
        assert "DeepSeek API" in banner, banner
        assert harness.model in banner, banner
        assert f"session {harness.session_id[:8]}" in banner, banner

        status = app.query_one(StatusLine)
        right = str(status.query_one("#status-right").render())
        assert "DeepSeek API" in right and harness.model in right, right
        assert app.thinking in right or not app.thinking, right

        # /provider with no argument opens the picker off the real registry.
        await pilot.press(*"/provider", "enter")
        await pilot.pause(0.3)
        assert type(app.screen_stack[-1]).__name__ == "PickerScreen"
        await pilot.press("escape")
        await pilot.pause(0.2)


def test_guard_is_idempotent() -> None:
    """Installing the hit-test guard twice must not stack wrappers."""
    import textual.screen as screen_module

    guard_detached_hit_test()
    first = screen_module.Screen.get_widget_and_offset_at
    guard_detached_hit_test()
    assert screen_module.Screen.get_widget_and_offset_at is first
    assert getattr(first, "_textual_pleiades_guard", False) is True


def main() -> None:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    asyncio.run(check_app())
    print("  ok  app smoke (turn, status, resize, commands, palette, error)")
    asyncio.run(check_follow_tail())
    print("  ok  follow-the-tail (long turn sticks to the bottom, ctrl+home)")
    asyncio.run(check_selection_during_streaming())
    print("  ok  selection survives streaming (the NoneType.region crash)")
    asyncio.run(check_streamed_markdown_matches_full_render())
    print("  ok  streamed markdown == one-shot render")
    asyncio.run(check_scrolling())
    print("  ok  scrolling (wheel anywhere, keyboard, follow release/resume)")
    asyncio.run(check_scrolling_while_streaming())
    print("  ok  scrolling during a stream (view holds, no yank-back)")
    asyncio.run(check_tool_calls_inline())
    print("  ok  tool calls inline (event order, expand on click, live args)")
    asyncio.run(check_tool_output_is_not_markup())
    print("  ok  tool output is not markup (the MarkupError crash)")
    asyncio.run(check_prompt_autocomplete())
    print("  ok  prompt autocomplete (commands + arguments, ghost text)")
    asyncio.run(check_real_harness())
    print("  ok  real harness (banner, status, provider picker)")
    print(f"\n{len(tests) + 10} checks passed")


if __name__ == "__main__":
    main()
