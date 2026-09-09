"""Decoupled UX event layer (README step 13).

The core loop never renders anything itself — it emits *state events* (thinking,
token, message, tool_call, tool_result, done, ...) and the presentation layer
animates based on event kind alone. It does not need to know why the model is
thinking or what a tool does; it just reflects each event.

Transport is abstracted behind :class:`EventSink`. This module ships the
in-process :class:`EventBus` (pub/sub) used by the TUI. A future SSE/WebSocket
transport implements the same EventSink contract, so the core loop keeps calling
``bus.emit(event)`` unchanged while the frontend moves off-box.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class EventKind(str, Enum):
    """State events the core loop can emit (step 13's vocabulary, plus extras).

    - thinking    model is about to / currently generating (before a turn step)
    - token       a chunk of assistant text arrived (streaming)
    - message     a complete assistant message arrived (non-streamed path)
    - tool_call   the model requested a tool; execution is about to start
    - tool_result a tool finished (success or failure, with duration/preview)
    - done        the whole user turn finished
    - usage       token counters snapshot
    - user / system / error   informational, echoed by the frontend
    """

    SYSTEM = "system"
    USER = "user"
    THINKING = "thinking"
    REASONING = "reasoning"
    TOKEN = "token"
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    DONE = "done"
    USAGE = "usage"
    ERROR = "error"


@dataclass
class Event:
    kind: EventKind
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.kind, str):
            try:
                self.kind = EventKind(self.kind)
            except ValueError:
                self.kind = EventKind.SYSTEM


class EventSink:
    """Contract every transport satisfies. Core code depends on this, not on a
    concrete bus, so an SSE/WebSocket transport can be swapped in later."""

    def emit(self, event: Event) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class EventBus(EventSink):
    """In-process, thread-safe pub/sub transport.

    Subscribers run synchronously on the emitter's thread, so they must be fast
    (the TUI subscriber only pushes onto a queue). A failing subscriber is
    swallowed — it must never break the core loop.
    """

    def __init__(self) -> None:
        self._subscribers: list[Callable[[Event], None]] = []
        self._lock = threading.Lock()
        self._seq = 0

    def subscribe(self, fn: Callable[[Event], None]) -> Callable[[Event], None]:
        with self._lock:
            self._subscribers.append(fn)
        return fn

    def unsubscribe(self, fn: Callable[[Event], None]) -> None:
        with self._lock:
            if fn in self._subscribers:
                self._subscribers.remove(fn)

    def emit(self, event: Event) -> None:
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn(event)
            except Exception:
                # A broken subscriber must not take down the agent loop.
                pass

    def emit_kind(self, kind: EventKind | str, **data: Any) -> Event:
        event = Event(kind=kind, data=data)
        self.emit(event)
        return event

    def close(self) -> None:
        with self._lock:
            self._subscribers.clear()
