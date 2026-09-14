"""Bridge from the harness event stream into a Textual app.

``AgentHarness`` only ever calls ``bus.emit_kind(kind, **data)``, so a sink
implementing that method is a drop-in replacement for :class:`ux.EventBus` — no
harness changes are needed to move the frontend onto Textual.

Threading: the harness runs on a worker thread, and ``MessagePump.post_message``
is the documented thread-safe way to hand work to the app's own thread. Every
other Textual API is main-thread only, so nothing else in here touches widgets.
"""
from __future__ import annotations

from textual.message import Message

from ux import Event, EventKind


class HarnessEvent(Message):
    """One harness event, delivered on the app's thread."""

    def __init__(self, event: Event) -> None:
        self.event = event
        super().__init__()


class TextualSink:
    """EventSink that forwards harness events onto a Textual app's queue."""

    def __init__(self, app) -> None:
        self._app = app
        self._closed = False
        self._seq = 0

    def emit(self, event: Event) -> None:
        if self._closed:
            return
        self._seq += 1
        event.seq = self._seq
        try:
            self._app.post_message(HarnessEvent(event))
        except Exception:
            # Presentation must never be able to break the agent loop.
            pass

    def emit_kind(self, kind: EventKind | str, **data) -> Event:
        event = Event(kind=kind, data=data)
        self.emit(event)
        return event

    def close(self) -> None:
        self._closed = True

    # EventBus surface, so code written against the bus keeps working.
    def subscribe(self, fn):
        return fn

    def unsubscribe(self, fn) -> None:
        pass


class DeferredSink:
    """A sink installed before the app exists, wired up once it does.

    ``AgentHarness`` needs a sink at construction, but the sink needs the app,
    and the app needs the harness. This breaks the cycle: build the harness
    against this object, construct the app, then call :meth:`attach`.
    """

    def __init__(self) -> None:
        self._target = None
        self._pending: list[Event] = []

    def attach(self, target) -> None:
        self._target = target
        pending, self._pending = self._pending, []
        for event in pending:
            target.emit(event)

    def emit(self, event: Event) -> None:
        if self._target is None:
            self._pending.append(event)
        else:
            self._target.emit(event)

    def emit_kind(self, kind: EventKind | str, **data) -> Event:
        event = Event(kind=kind, data=data)
        self.emit(event)
        return event

    def close(self) -> None:
        if self._target is not None:
            self._target.close()

    def subscribe(self, fn):
        return fn

    def unsubscribe(self, fn) -> None:
        pass
