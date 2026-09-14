"""Regression tests for the git denylist and the memory-note path guard.

Run with no test runner and no plugins::

    .venv/bin/python tests/test_git_guard_and_notes.py

Two holes, both closed here:

1. ``memory_note`` interpolated its ``key`` straight into a filename
   (``notes_dir / f"{key}.md"``), so ``key="../../pwned"`` wrote outside the
   working root. The key is model-supplied and therefore untrusted input.

2. Nothing stopped a shell tool from running ``git clean -xdf`` -- which deletes
   untracked *and ignored* files, i.e. `agent_state.db`, `logs/`, `.env`,
   `AGENTS.md`, and anything untracked in the worktree. ``git checkout`` /
   ``git restore`` likewise discard worktree edits with no confirmation.

The denylist is a speed bump, not a sandbox (see ``refuse_destructive_git``);
these tests pin the cases that matter, not the absence of a bypass.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as pleiades                                             # noqa: E402
from main import memory_note, refuse_destructive_git, run_bash, run_shell  # noqa: E402


class _working_root:
    """Swap WORKING_ROOT for a temp dir, then restore it."""

    def __init__(self, tmp: str) -> None:
        self.path = pathlib.Path(tmp).resolve() / "root"
        self.path.mkdir()
        self.prev = None

    def __enter__(self):
        self.prev = pleiades.WORKING_ROOT
        pleiades.WORKING_ROOT = self.path
        return self.path

    def __exit__(self, *exc):
        pleiades.WORKING_ROOT = self.prev
        return False


# ------------------------------------------------------------- memory notes
def test_note_round_trips() -> None:
    with tempfile.TemporaryDirectory() as tmp, _working_root(tmp) as root:
        out = memory_note("save", key="project-x", content="notes")
        assert str(root / "memory" / "project-x.md") in out, out
        assert memory_note("recall", key="project-x") == "notes"
        assert "project-x" in memory_note("list")


def test_note_key_traversal_rejected() -> None:
    """`..` in the key used to escape the root and write a file outside it."""
    for bad in ("../../pwned", "../outside", "a/../../evil"):
        with tempfile.TemporaryDirectory() as tmp, _working_root(tmp) as root:
            try:
                memory_note("save", key=bad, content="x")
            except ValueError:
                pass
            else:
                raise AssertionError(f"key {bad!r} was accepted")
            stray = list(root.parent.glob("*.md"))
            assert not stray, f"escaped the root: {stray}"


def test_percent_encoded_traversal_is_just_a_filename() -> None:
    """Nothing decodes %2f, so it stays an opaque name inside memory/."""
    with tempfile.TemporaryDirectory() as tmp, _working_root(tmp) as root:
        out = memory_note("save", key="..%2f..%2fpwned", content="x")
        assert str(root / "memory" / "..%2f..%2fpwned.md") in out, out


def test_note_dotdot_inside_the_root_still_works() -> None:
    """The guard must reject escapes without breaking normalised keys."""
    with tempfile.TemporaryDirectory() as tmp, _working_root(tmp) as root:
        out = memory_note("save", key="a/../b", content="x")
        assert str(root / "memory" / "b.md") in out, out


def test_recall_traversal_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp, _working_root(tmp) as root:
        (root.parent / "secret.md").write_text("leak", encoding="utf-8")
        try:
            memory_note("recall", key="../secret")
        except ValueError:
            return
        raise AssertionError("recall read a note outside the root")


# --------------------------------------------------------------- git denylist
DESTRUCTIVE = [
    "git clean -fd",
    "git clean -xdf",
    "git clean -ffdx",
    "git -C /tmp/other clean -fd",
    "git --git-dir=/tmp/x/.git clean -f",
    "ls && git clean -fd",
    "git status; git clean -xdf",
    "cd pleiades-vault && git clean -fd",
    "git checkout .",
    "git checkout -- main.py",
    "git checkout -f main",
    "git restore main.py",
    "git restore --staged .",
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git stash clear",
    "git stash drop",
]

ALLOWED = [
    "git status",
    "git status --short",
    "git diff main.py",
    "git log --oneline -5",
    "git switch main",
    "git switch -c feature/x",
    "git branch -a",
    "git add tests/",
    "git commit -m 'msg'",
    "git show HEAD",
    "git reset HEAD main.py",      # unstage only: cannot lose work
    "git stash",                   # stash itself is recoverable
    "grep -rn git README.md",      # the word 'git', not a git command
    "gh pr status",                # different binary
]


def test_destructive_commands_refused() -> None:
    for command in DESTRUCTIVE:
        try:
            refuse_destructive_git(command)
        except ValueError as exc:
            assert "Refused" in str(exc), exc
        else:
            raise AssertionError(f"not refused: {command!r}")


def test_safe_commands_allowed() -> None:
    for command in ALLOWED:
        try:
            refuse_destructive_git(command)
        except ValueError as exc:
            raise AssertionError(f"wrongly refused: {command!r} ({exc})") from None


def test_both_shell_tools_are_guarded() -> None:
    """The denylist must sit in front of bash *and* run_shell, not just one."""
    for call in (run_bash, run_shell):
        try:
            call("git clean -fd")
        except ValueError as exc:
            assert "Refused" in str(exc), exc
        else:
            raise AssertionError(f"{call.__name__} ran `git clean`")


def test_guard_does_not_block_real_work() -> None:
    """A guarded call still executes: `git status` runs in the repo."""
    out = run_shell("git status --short")
    assert isinstance(out, str) and out, out


def main() -> None:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} checks passed")


if __name__ == "__main__":
    main()
