"""Regression tests for the path guard and lazy session rows.

Run with no test runner and no plugins::

    .venv/bin/python tests/test_paths_and_sessions.py

1. ``resolve_safe_path`` confines to a single root (``WORKING_ROOT``). The old
   ``vault_root`` branch compared the resolved candidate against *another copy
   of itself*, so ``is_relative_to`` was always true and that branch accepted
   every path -- including ``../`` escapes out of the vault. The branch is now
   deleted (the vault lives inside the repo, so its paths are repo-relative),
   and the escape/symlink/normalisation cases below pin the same invariants on
   the surviving root. They pass on the old code too: they are characterisation
   tests for a guard that was never broken, not a fix.

2. ``AgentHarness.__init__`` wrote the ``sessions`` row and the graph's
   ``conversation`` node at construction time. A process that started and exited
   without a turn therefore looked like a prior session, so
   ``graph_query(kind="sessions")`` listed empty runs ahead of real work.

Nothing here touches the network: the provider is a synthetic ``Provider`` with
``models_endpoint=False`` (so ``resolve_context_length`` is a static lookup) and
the model call is a scripted stand-in for ``Wrapper``.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import providers                                                    # noqa: E402
# Imported as `pleiades`: this module's own runner is also called main().
import main as pleiades                                             # noqa: E402
from main import AgentHarness, resolve_safe_path                    # noqa: E402


# --------------------------------------------------------------- path guard
class _working_root:
    """Swap the module's WORKING_ROOT for a temp dir, then restore it.

    The guard reads WORKING_ROOT at call time, so patching the module attribute
    is how these cases get a root small enough to escape from. Without this the
    only testable root is the repo itself, and every escape case would have to
    reach for /etc/passwd.
    """

    def __init__(self, path: str) -> None:
        # A root nested one level down, so the temp dir itself can hold the
        # "outside" files these tests try to reach.
        self.path = Path(path).resolve() / "root"
        self.path.mkdir()
        self.prev = None

    def __enter__(self):
        self.prev = pleiades.WORKING_ROOT
        pleiades.WORKING_ROOT = self.path
        return self.path

    def __exit__(self, *exc):
        pleiades.WORKING_ROOT = self.prev
        return False


def test_root_escape_rejected() -> None:
    """`../` out of the root must raise, not return an absolute system path."""
    with tempfile.TemporaryDirectory() as tmp, _working_root(tmp) as root:
        try:
            resolve_safe_path("../../etc/passwd")
        except ValueError:
            return
        raise AssertionError(f"escape out of {root} was accepted")


def test_root_escape_to_sibling_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp, _working_root(tmp) as root:
        (root.parent / "outside.txt").write_text("secret", encoding="utf-8")
        try:
            resolve_safe_path("../outside.txt")
        except ValueError:
            return
        raise AssertionError("sibling of the root was accepted")


def test_root_dotdot_that_stays_inside_allowed() -> None:
    """Rejecting escapes must not break legitimate normalised paths."""
    with tempfile.TemporaryDirectory() as tmp, _working_root(tmp) as root:
        (root / "notes").mkdir(parents=True)
        got = resolve_safe_path("notes/../notes/a.md")
        assert got == (root / "notes" / "a.md").resolve(), got


def test_root_plain_path_allowed() -> None:
    with tempfile.TemporaryDirectory() as tmp, _working_root(tmp) as root:
        got = resolve_safe_path("index.md")
        assert got == (root / "index.md").resolve(), got


def test_root_symlink_escape_rejected() -> None:
    """resolve() follows links, so an in-root link cannot tunnel out."""
    with tempfile.TemporaryDirectory() as tmp, _working_root(tmp) as root:
        outside = root.parent / "outside"
        outside.mkdir()
        (outside / "keys.txt").write_text("secret", encoding="utf-8")
        os.symlink(outside, root / "link")
        try:
            resolve_safe_path("link/keys.txt")
        except ValueError:
            return
        raise AssertionError("symlink out of the root was accepted")


def test_vault_paths_resolve_repo_relative() -> None:
    """The vault is an ordinary subtree: `pleiades-vault/...` needs no special root."""
    got = resolve_safe_path("pleiades-vault/kanban")
    assert got == (pleiades.WORKING_ROOT / "pleiades-vault" / "kanban").resolve(), got
    assert "pleiades-vault/pleiades-vault" not in str(got), got


def test_absolute_path_rejected() -> None:
    try:
        resolve_safe_path("/etc/passwd")
    except ValueError:
        return
    raise AssertionError("absolute path was accepted")


def test_working_root_escape_rejected() -> None:
    try:
        resolve_safe_path("../../../../etc/passwd")
    except ValueError:
        return
    raise AssertionError("escape out of the working directory was accepted")


def test_working_root_inside_allowed() -> None:
    got = resolve_safe_path("main.py")
    assert got == (pleiades.WORKING_ROOT / "main.py").resolve(), got


# ---------------------------------------------------------- session lifecycle
FAKE_PROVIDER = providers.Provider(
    name="fake", label="Fake Backend", base_url="http://127.0.0.1:1/v1",
    api_key_env="PLEIADES_TEST_KEY", default_model="fake-1",
    context_length=8192, model_env="PLEIADES_TEST_MODEL",
)


class ScriptedWrapper:
    """One tool-less assistant reply, without a socket."""

    def __init__(self, reply: str = "done") -> None:
        self.reply = reply
        self.model = "fake-1"
        self.provider = FAKE_PROVIDER
        self.calls = 0

    def chat(self, messages, tools=None) -> pleiades.LLMResponse:
        self.calls += 1
        return pleiades.LLMResponse(
            content=self.reply, tool_calls=[],
            message={"role": "assistant", "content": self.reply},
            stats={"prompt_tokens": 5, "completion_tokens": 2})


def _session_rows(harness) -> int:
    return harness.state.db.execute(
        "SELECT COUNT(*) FROM sessions WHERE session_id = ?",
        (harness.session_id,)).fetchone()[0]


def _build() -> AgentHarness:
    harness = AgentHarness("fake-1", "system prompt", provider=FAKE_PROVIDER)
    harness.wrapper = ScriptedWrapper()
    return harness


def test_no_session_row_before_first_turn() -> None:
    """Construction alone must not claim a session took place."""
    with tempfile.TemporaryDirectory() as tmp, _cwd(tmp):
        harness = _build()
        assert harness._session_started is False
        assert harness.conversation_node is None
        assert _session_rows(harness) == 0
        assert harness.graph.get_node("conversation", harness.session_id) is None
        assert "No sessions recorded." in harness.graph.query("sessions")


def test_backend_switch_before_first_turn_writes_nothing() -> None:
    """`/model` and `/provider` must not resurrect the empty rows."""
    with tempfile.TemporaryDirectory() as tmp, _cwd(tmp):
        harness = _build()
        harness.switch_model("fake-2")
        # A real registry entry: get_provider() validates names, and no request
        # is issued here, so no API key is needed.
        harness.switch_provider("deepseek")
        assert harness.conversation_node is None
        assert _session_rows(harness) == 0
        assert harness.graph.get_node("conversation", harness.session_id) is None


def test_first_turn_creates_row_node_and_edges() -> None:
    with tempfile.TemporaryDirectory() as tmp, _cwd(tmp):
        harness = _build()
        out = harness.run("hello")
        assert out == "done", out
        assert harness._session_started is True
        assert _session_rows(harness) == 1

        node = harness.graph.get_node("conversation", harness.session_id)
        assert node is not None, "no conversation node after a turn"
        task = harness.graph.get_node("task", f"{harness.session_id}:1")
        assert task is not None, "no task node after a turn"

        attached = harness.graph.neighbors(node["id"], relation="created_in")
        assert any(hit["node"]["type"] == "task" for hit in attached), attached
        assert harness.session_id[:8] in harness.graph.query("sessions")


def test_second_turn_does_not_duplicate_the_session() -> None:
    """sessions.session_id is a PRIMARY KEY; the flag is what prevents the crash."""
    with tempfile.TemporaryDirectory() as tmp, _cwd(tmp):
        harness = _build()
        harness.run("one")
        harness.run("two")
        assert _session_rows(harness) == 1
        assert len(harness.graph.query("sessions").splitlines()) == 1


class _cwd:
    """Temporary working directory, so agent_state.db never lands in the repo."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.prev = None

    def __enter__(self):
        self.prev = os.getcwd()
        os.chdir(self.path)
        return self.path

    def __exit__(self, *exc):
        os.chdir(self.prev)
        return False


def main() -> None:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} checks passed")


if __name__ == "__main__":
    main()
