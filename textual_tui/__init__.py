"""Textual frontend for the PLEIADES agent harness.

``python main.py --tui`` runs :func:`run_textual_tui`; the legacy rich frontend
is still available with ``--rich`` until the Textual app has proven itself.
"""
from textual_tui.app import PleiadesApp, run_textual_tui
from textual_tui.events import DeferredSink, HarnessEvent, TextualSink
from textual_tui.model import ToolRecord, Transcript, Turn
from textual_tui.widgets import StatusLine, ToolCall, TurnWidget

__all__ = [
    "PleiadesApp",
    "run_textual_tui",
    "DeferredSink",
    "HarnessEvent",
    "TextualSink",
    "Transcript",
    "Turn",
    "ToolRecord",
    "StatusLine",
    "ToolCall",
    "TurnWidget",
]
