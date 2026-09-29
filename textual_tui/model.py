"""Transcript model: the record of what happened, independent of any widget.

The legacy rich TUI kept no transcript at all — each finished turn was printed
into the terminal's scrollback and then discarded by ``_reset_turn``. A
screen-owning frontend has to own that record, so this module is the new source
of truth, and it deliberately has **no UI imports at all** so the behaviour can
be tested headlessly.

The event handling here mirrors the legacy ``Tui._apply`` exactly, including the
small oddities worth preserving:

* a space is inserted when assistant text resumes after a tool result
  (``_tools_done`` latch), because providers stream continuations without one;
* a ``tool_result`` for an unknown call id still produces a visible failed tool
  row rather than being dropped;
* only the most recent error is kept per turn.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from presentation import TOOL_ROWS_MAX
from ux import EventKind

RUN, OK, ERR = "run", "ok", "err"


@dataclass
class ToolRecord:
    """One tool call and its outcome."""

    call_id: str
    name: str
    args: dict = field(default_factory=dict)
    state: str = RUN
    ms: int = 0
    preview: str = ""


@dataclass
class Segment:
    """One entry in the turn's chronological activity stream.

    A turn is not "reasoning, then tools, then reply": the model reasons,
    calls a tool, reasons again about the result, calls another tool… The
    segments list preserves that order (pi renders the same stream inline),
    while ``Turn.tools`` keeps the flat registry the widgets index by call id.
    """

    kind: str                       # "reasoning" | "tool"
    text: str = ""                  # reasoning chunks accumulate here
    tool: ToolRecord | None = None


@dataclass
class Turn:
    """Everything one user turn produced."""

    user: str
    reply: str = ""
    error: str = ""
    segments: list[Segment] = field(default_factory=list)
    tools: list[ToolRecord] = field(default_factory=list)
    tools_done: bool = False
    """Set once a tool result has arrived; providers may stream a continuation
    without one."""
    pending_space: bool = False
    """A token is expected that may need a joining space (post-tool boundary)."""
    usage_in: int = 0
    usage_out: int = 0
    ctx_used: int = 0
    finished: bool = False

    def visible_tools(self) -> tuple[int, list[ToolRecord]]:
        """``(hidden_count, rows)`` — the tail cap shared with the legacy TUI."""
        hidden = max(0, len(self.tools) - TOOL_ROWS_MAX)
        return hidden, self.tools[hidden:]

    @property
    def reasoning(self) -> str:
        """All reasoning concatenated (compat; see ``segments`` for order)."""
        return "".join(s.text for s in self.segments if s.kind == "reasoning")

    @property
    def worst_state(self) -> str:
        """Band tint for the tool group: failure beats running beats success."""
        states = {tool.state for tool in self.tools}
        if ERR in states:
            return ERR
        if RUN in states:
            return RUN
        return OK


class Transcript:
    """Ordered turns plus the in-flight turn, driven purely by harness events."""

    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self.current: Turn | None = None
        self.busy = False
        self.status = "ready"

    # ------------------------------------------------------------ lifecycle
    def begin(self, user: str) -> Turn:
        """Start a new turn (the ``user`` event, or a submitted prompt)."""
        turn = Turn(user=user or "")
        self.turns.append(turn)
        self.current = turn
        self.busy = True
        self.status = "thinking"
        return turn

    def reset(self) -> None:
        """Forget every turn (the ``/clear`` command)."""
        self.turns.clear()
        self.current = None
        self.busy = False
        self.status = "ready"

    # --------------------------------------------------------------- events
    def apply(self, kind: EventKind | str, data: dict) -> bool:
        """Fold one harness event in. Returns True when something changed."""
        try:
            kind = EventKind(kind)
        except ValueError:
            return False
        data = data or {}

        if kind is EventKind.USER:
            self.begin(str(data.get("content", "")))
            return True

        if kind is EventKind.THINKING:
            self.busy = True
            self.status = f"thinking · step {data.get('iteration', '')}"
            return True

        if kind is EventKind.REASONING:
            turn = self._turn()
            if not turn.segments or turn.segments[-1].kind != "reasoning":
                turn.segments.append(Segment(kind="reasoning"))
            turn.segments[-1].text += str(data.get("text", ""))
            self.busy = True
            self.status = "reasoning…"
            return True

        if kind is EventKind.TOKEN:
            turn = self._turn()
            text = str(data.get("text", ""))
            # The first token after a tool result can resume mid-word without
            # leading whitespace, so join it to the reply with a space. This
            # must apply once, on the boundary itself: doing it on every token
            # shreds words between streamed chunks ("you 're", "s essio n").
            if turn.pending_space:
                if text and text[0].isalnum() and turn.reply \
                        and not turn.reply[-1].isspace():
                    turn.reply += " "
                turn.pending_space = False
            turn.reply += text
            self.busy = True
            self.status = "streaming…"
            return True

        if kind is EventKind.MESSAGE:
            turn = self._turn()
            turn.reply += str(data.get("content", ""))
            return True

        if kind is EventKind.TOOL_CALL:
            turn = self._turn()
            call_id = str(data.get("call_id", ""))
            record = ToolRecord(call_id=call_id,
                                name=str(data.get("name", "")),
                                args=data.get("args") or {})
            turn.tools.append(record)
            turn.segments.append(Segment(kind="tool", tool=record))
            self.busy = True
            self.status = f"tool · {record.name}"
            return True

        if kind is EventKind.TOOL_RESULT:
            turn = self._turn()
            call_id = str(data.get("call_id", ""))
            record = next((t for t in turn.tools if t.call_id == call_id), None)
            if record is None:
                # A result for a call we never saw still deserves a visible row.
                record = ToolRecord(call_id=call_id,
                                    name=str(data.get("name", "")),
                                    state=ERR)
                turn.tools.append(record)
                turn.segments.append(Segment(kind="tool", tool=record))
            record.state = OK if data.get("success", True) else ERR
            record.ms = int(data.get("duration_ms", 0) or 0)
            record.preview = str(data.get("preview", ""))
            turn.tools_done = True
            turn.pending_space = True
            self.status = f"tool · {record.name} done"
            return True

        if kind is EventKind.DONE:
            turn = self._turn()
            # The harness carries a terminal note on `done` whenever a turn ends
            # without a normal answer: `Stopped: …` (cancel / budget), `Max
            # iterations reached.`, or an empty model reply. Discarding it left
            # the transcript blank and the agent looked like it had simply
            # stopped. Only adopt it when nothing else was rendered, so a
            # streamed reply is not duplicated.
            note = str(data.get("content") or "").strip()
            if note and not turn.reply.strip():
                turn.reply = note
            turn.finished = True
            self.busy = False
            self.status = "ready"
            return True

        if kind is EventKind.ERROR:
            turn = self._turn()
            turn.error = str(data.get("message", "error"))
            self.busy = False
            self.status = "error"
            return True

        if kind is EventKind.USAGE:
            turn = self._turn()
            turn.usage_in = int(data.get("input_tokens", turn.usage_in) or 0)
            turn.usage_out = int(data.get("output_tokens", turn.usage_out) or 0)
            return True

        if kind is EventKind.SYSTEM:
            message = str(data.get("content", data.get("message", "")))
            if message:
                self.status = message
                return True
        return False

    # ------------------------------------------------------------- internals
    def _turn(self) -> Turn:
        if self.current is None:
            return self.begin("")
        return self.current

    def snapshot_usage(self, *, usage_in: int, usage_out: int, ctx_used: int) -> None:
        """Mirror the harness counters onto the in-flight turn (and the app)."""
        turn = self.current
        if turn is not None:
            turn.usage_in = int(usage_in or 0)
            turn.usage_out = int(usage_out or 0)
            turn.ctx_used = int(ctx_used or 0)
