"""Regression tests for the creation pipeline (README steps 8-10).

Run with no test runner and no plugins::

    .venv/bin/python tests/test_creation.py

What is pinned here, and why:

1. **The execution boundary.** A created tool must not run in the harness
   process. ``test_tool_runs_out_of_process`` asserts the tool reports a
   different PID and cannot see the harness module — the property that makes
   "provisional" mean something.

2. **Validation is real, not decorative.** A tool whose own test cases fail is
   rejected *and left as a draft*, so it never becomes callable. A skill whose
   held-out example does not produce the expected tool calls is rejected for the
   same reason (step 9). SOP drafting is gated on *promoted* skills (step 10).

3. **Lifecycle transitions hold.** promote/prune move tiers and prune unregisters
   the tool from the harness.

No network: behavioural skill validation uses a scripted ``Wrapper`` stand-in.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import providers                                                    # noqa: E402
import main as pleiades                                             # noqa: E402
from creation import CreationPipeline, CreationStore, TOOL_TIMEOUT  # noqa: E402


class _sandbox:
    """Temp repo root + temp cwd, so created files and SQLite stay out of the repo."""

    def __init__(self, tmp: str) -> None:
        self.tmp = Path(tmp)
        self.root = self.tmp / "root"
        (self.root / "skills").mkdir(parents=True)
        self.prev_root = None
        self.prev_cwd = None

    def __enter__(self):
        self.prev_root = pleiades.WORKING_ROOT
        pleiades.WORKING_ROOT = self.root
        self.prev_cwd = os.getcwd()
        os.chdir(self.tmp)
        return self

    def __exit__(self, *exc):
        pleiades.WORKING_ROOT = self.prev_root
        os.chdir(self.prev_cwd)
        return False


class ScriptedWrapper:
    """Returns a fixed list of tool-call names, without a socket."""

    def __init__(self, names=()) -> None:
        self.names = list(names)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        calls = [pleiades.ToolCall(call_id=i, name=n, args={})
                 for i, n in enumerate(self.names)]
        return pleiades.LLMResponse(
            content="", tool_calls=calls, message={"role": "assistant", "content": ""},
            stats={})


def _pipeline(tmp: str, *, wrapper=None, known_tools=(), known_skills=(),
              timeout: int = TOOL_TIMEOUT):
    """A pipeline wired to the current (sandboxed) WORKING_ROOT."""
    store = CreationStore(str(Path(tmp) / "state.db"))
    pipe = CreationPipeline(
        store, pleiades.resolve_safe_path, pleiades.WORKING_ROOT,
        known_tools=lambda: set(known_tools),
        known_skills=lambda: set(known_skills),
        wrapper=lambda: wrapper,
        tool_schemas=lambda: [],
        timeout=timeout)
    return store, pipe


TOOL_SPEC = {"parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
             "code": "def run(x=0):\n    return f'got {x * 2}'\n"}
TOOL_TESTS = [{"args": {"x": 21}, "expect": "got 42"}]


# ------------------------------------------------------------------- tools
def test_tool_create_validate_and_call() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        store, pipe = _pipeline(tmp)
        result = pipe.create("tool", "double_it", "Double an integer and return it as text.",
                             TOOL_SPEC, TOOL_TESTS)
        assert result.ok, result
        assert result.tier == "provisional", result.tier
        assert store.get("tool", "double_it")["uses"] == 0

        fn = pipe.tool_callable("double_it")
        assert fn(x=4) == "got 8", fn(x=4)
        assert store.get("tool", "double_it")["uses"] == 1, "a successful call must be counted"


def test_tool_runs_out_of_process() -> None:
    """The boundary that makes 'provisional' meaningful: no shared process."""
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        _, pipe = _pipeline(tmp)
        code = ("import json, os, sys\n"
                "\n"
                "def run():\n"
                "    return json.dumps({'pid': os.getpid(), 'sees_main': 'main' in sys.modules})\n")
        spec = {"parameters": {"type": "object", "properties": {}}, "code": code}
        result = pipe.create("tool", "probe", "Report the sandbox process identity for tests.",
                             spec, [{"args": {}, "expect": '"sees_main": false'}])
        assert result.ok, result
        out = json.loads(pipe.tool_callable("probe")())
        assert out["pid"] != os.getpid(), "created tool ran in the harness process"
        assert out["sees_main"] is False, "created tool could see harness internals"


def test_failing_test_rejects_and_leaves_a_draft() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        store, pipe = _pipeline(tmp)
        spec = {"parameters": {"type": "object", "properties": {}},
                "code": "def run():\n    return '1'\n"}
        result = pipe.create("tool", "always_one", "Return one, for a test that expects two.",
                             spec, [{"args": {}, "expect": "2"}])
        assert not result.ok, result
        assert "sandbox validation failed" in result.message
        assert store.get("tool", "always_one")["tier"] == "draft"
        assert pipe.registrable_tools() == [], "a rejected draft must not be callable"


def test_tool_without_run_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        _, pipe = _pipeline(tmp)
        spec = {"parameters": {"type": "object", "properties": {}}, "code": "x = 1\n"}
        result = pipe.create("tool", "no_run", "A tool whose code defines no run function.",
                             spec, [{"args": {}, "expect": "x"}])
        assert not result.ok and "run" in result.message, result


def test_timeout_is_enforced() -> None:
    """A tool that overruns its timeout is rejected, not allowed to hang the turn."""
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        _, pipe = _pipeline(tmp, timeout=1)
        code = "import time\n\ndef run():\n    time.sleep(3)\n    return 'late'\n"
        spec = {"parameters": {"type": "object", "properties": {}}, "code": code}
        result = pipe.create("tool", "slow_poke", "Sleep past the sandbox timeout.",
                             spec, [{"args": {}, "expect": "late"}])
        assert not result.ok and "timed out" in result.message, result


def test_name_description_and_collision_guards() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        _, pipe = _pipeline(tmp, known_tools={"bash"})
        spec = {"parameters": {"type": "object", "properties": {}},
                "code": "def run():\n    return 'x'\n"}
        assert not pipe.create("tool", "Bash_Tool", "A name with capitals is not allowed here.",
                               spec, TOOL_TESTS).ok
        assert not pipe.create("tool", "bash", "Clashes with a hand-written tool that exists.",
                               spec, TOOL_TESTS).ok
        bad_desc = "line one\nline two"
        assert not pipe.create("tool", "two_line", bad_desc, spec, TOOL_TESTS).ok
        assert not pipe.create("tool", "no_tests", "A tool drafted with no test cases at all.",
                               spec, []).ok


# ------------------------------------------------------------------- skills
SKILL_BODY = ("# Deploy helper\n\nUse when the user asks to check a deployment.\n"
              "1. Inspect the working tree.\n2. Report the result.\n")


def _skill_spec(**over):
    spec = {"body": SKILL_BODY, "tools": ["bash"],
            "example_task": "is the deployment clean?", "expected_tools": ["bash"]}
    spec.update(over)
    return spec


def test_skill_behavioral_validation_passes_and_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        good = ScriptedWrapper(["bash"])
        store, pipe = _pipeline(tmp, wrapper=good, known_tools={"bash"})
        result = pipe.create("skill", "deploy_helper",
                             "Check a deployment's state. Use when the user asks about a deploy.",
                             _skill_spec())
        assert result.ok, result
        assert result.tier == "provisional"
        assert good.calls == 1, "behavioural validation must make exactly one model call"

        bad = ScriptedWrapper(["websearch"])
        _, pipe2 = _pipeline(tmp, wrapper=bad, known_tools={"bash"})
        result2 = pipe2.create("skill", "bad_helper",
                               "A helper whose held-out example calls the wrong tool.",
                               _skill_spec())
        assert not result2.ok and "behavioural validation failed" in result2.message, result2
        assert store.get("skill", "bad_helper")["tier"] == "draft"


def test_skill_rejects_unknown_tools() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        _, pipe = _pipeline(tmp, wrapper=ScriptedWrapper(["bash"]), known_tools={"bash"})
        result = pipe.create("skill", "ghost_tool",
                             "A helper that names a tool which does not exist yet.",
                             _skill_spec(tools=["does_not_exist"]))
        assert not result.ok and "do not exist" in result.message, result


def test_skill_requires_a_held_out_example() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        _, pipe = _pipeline(tmp, wrapper=ScriptedWrapper(["bash"]), known_tools={"bash"})
        result = pipe.create("skill", "no_example",
                             "A helper drafted without any behavioural example.",
                             _skill_spec(example_task="", expected_tools=[]))
        assert not result.ok and "example_task" in result.message, result


# --------------------------------------------------------------------- SOPs
def test_sop_requires_promoted_skills() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        store, pipe = _pipeline(tmp, wrapper=ScriptedWrapper(["bash"]), known_tools={"bash"})
        assert pipe.create("skill", "deploy_helper",
                           "Check a deployment's state. Use when the user asks about a deploy.",
                           _skill_spec()).ok
        sop = {"steps": [{"skill": "deploy_helper", "note": "inspect"},
                         {"tool": "bash", "note": "run the check"}]}

        blocked = pipe.create("sop", "deploy_run", "A fixed playbook for checking a deployment.", sop)
        assert not blocked.ok and "not promoted" in blocked.message, blocked

        assert pipe.promote("skill", "deploy_helper").ok
        allowed = pipe.create("sop", "deploy_run", "A fixed playbook for checking a deployment.", sop)
        assert allowed.ok, allowed
        assert allowed.tier == "provisional"


def test_sop_rejects_unknown_step_targets() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        _, pipe = _pipeline(tmp)
        result = pipe.create("sop", "ghost_sop", "A playbook that names things that do not exist.",
                             {"steps": [{"tool": "nope"}]})
        assert not result.ok and "no tool named" in result.message, result


# ---------------------------------------------------------------- lifecycle
def test_promote_and_prune_transitions() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        store, pipe = _pipeline(tmp)
        assert pipe.create("tool", "double_it", "Double an integer and return it as text.",
                           TOOL_SPEC, TOOL_TESTS).ok
        assert pipe.promote("tool", "double_it").ok
        assert store.get("tool", "double_it")["tier"] == "promoted"
        assert pipe.promote("tool", "double_it").ok, "re-promoting is a no-op, not an error"

        assert pipe.prune("tool", "double_it").ok
        assert store.get("tool", "double_it")["tier"] == "pruned"
        assert pipe.registrable_tools() == [], "a pruned tool must drop out of the live set"
        assert pipe.promote("tool", "missing").ok is False


def test_draft_cannot_be_promoted() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        store, pipe = _pipeline(tmp)
        spec = {"parameters": {"type": "object", "properties": {}},
                "code": "def run():\n    return '1'\n"}
        pipe.create("tool", "always_one", "Return one, for a test that expects two.",
                    spec, [{"args": {}, "expect": "2"}])
        result = pipe.promote("tool", "always_one")
        assert not result.ok and "never passed validation" in result.message, result


# ------------------------------------------------------------ harness wiring
FAKE_PROVIDER = providers.Provider(
    name="fake", label="Fake Backend", base_url="http://127.0.0.1:1/v1",
    api_key_env="PLEIADES_TEST_KEY", default_model="fake-1",
    context_length=8192, model_env="PLEIADES_TEST_MODEL")


def _harness(wrapper=None):
    harness = pleiades.AgentHarness("fake-1", "system prompt", provider=FAKE_PROVIDER)
    harness.wrapper = wrapper or ScriptedWrapper()
    return harness


def test_harness_create_registers_tool_and_writes_lineage() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        harness = _harness()
        harness._ensure_session()  # so the artifact's created_in edge has an endpoint
        out = harness.create_artifact("tool", "double_it",
                                      "Double an integer and return it as text.",
                                      TOOL_SPEC, TOOL_TESTS)
        assert out.startswith("OK"), out
        assert "double_it" in harness.tools, "a created tool must be callable this turn"
        assert harness.tools["double_it"].fn(x=3) == "got 6"

        node = harness.graph.get_node("artifact", "tool:double_it")
        assert node and node["properties"]["tier"] == "provisional", node
        created_in = harness.graph.edges(src_id=node["id"], relation="created_in")
        assert created_in, "the artifact must record which conversation created it"
        assert "double_it" in harness.graph_query("artifacts")


def test_harness_prune_unregisters_tool() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        harness = _harness()
        harness.create_artifact("tool", "double_it",
                                "Double an integer and return it as text.", TOOL_SPEC, TOOL_TESTS)
        assert "double_it" in harness.tools
        harness.artifact_admin("prune", "tool", "double_it")
        assert "double_it" not in harness.tools
        assert "pruned" in harness.artifact_admin("list")


def test_harness_created_tools_persist_across_restart() -> None:
    """A created tool registered in one process is loaded by the next."""
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        first = _harness()
        first.create_artifact("tool", "double_it",
                              "Double an integer and return it as text.", TOOL_SPEC, TOOL_TESTS)

        second = _harness()  # same cwd -> same agent_state.db
        loaded = second.load_created_tools()
        assert loaded == 1, loaded
        assert "double_it" in second.tools
        assert second.tools["double_it"].fn(x=5) == "got 10"


def test_harness_skill_creation_links_referenced_tools() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        harness = _harness(ScriptedWrapper(["bash"]))
        harness.tools["bash"] = pleiades.Tool("bash", "run bash", {"type": "object"}, lambda **k: "")
        out = harness.create_artifact(
            "skill", "deploy_helper",
            "Check a deployment's state. Use when the user asks about a deploy.",
            _skill_spec())
        assert out.startswith("OK"), out
        harness._ensure_session()
        node = harness.graph.get_node("artifact", "skill:deploy_helper")
        assert node is not None
        refs = [e for e in harness.graph.edges(src_id=node["id"], relation="references")]
        assert refs, "a skill must record which tools it references"


def test_harness_sop_links_promoted_skills() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        harness = _harness(ScriptedWrapper(["bash"]))
        harness.tools["bash"] = pleiades.Tool("bash", "run bash", {"type": "object"}, lambda **k: "")
        harness.create_artifact(
            "skill", "deploy_helper",
            "Check a deployment's state. Use when the user asks about a deploy.",
            _skill_spec())
        harness.artifact_admin("promote", "skill", "deploy_helper")
        out = harness.create_artifact(
            "sop", "deploy_run", "A fixed playbook for checking a deployment.",
            {"steps": [{"skill": "deploy_helper"}, {"tool": "bash"}]})
        assert out.startswith("OK"), out
        harness._ensure_session()
        node = harness.graph.get_node("artifact", "sop:deploy_run")
        composed = [e for e in harness.graph.edges(src_id=node["id"], relation="composed_of")]
        assert len(composed) == 1, "the SOP must record its skill composition"


def test_prune_removes_the_promoted_skill_file() -> None:
    """A pruned promoted skill left in skills/ would silently reload on restart."""
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp) as box:
        _, pipe = _pipeline(tmp, wrapper=ScriptedWrapper(["bash"]), known_tools={"bash"})
        pipe.create("skill", "deploy_helper",
                    "Check a deployment's state. Use when the user asks about a deploy.",
                    _skill_spec())
        pipe.promote("skill", "deploy_helper")
        promoted = box.root / "skills" / "deploy_helper" / "SKILL.md"
        assert promoted.is_file(), "promotion must copy the skill into skills/"
        pipe.prune("skill", "deploy_helper")
        assert not promoted.exists(), "the pruned skill would reload on the next start"


def test_prune_never_deletes_a_file_it_did_not_write() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp) as box:
        _, pipe = _pipeline(tmp, wrapper=ScriptedWrapper(["bash"]), known_tools={"bash"})
        pipe.create("skill", "deploy_helper",
                    "Check a deployment's state. Use when the user asks about a deploy.",
                    _skill_spec())
        pipe.promote("skill", "deploy_helper")
        promoted = box.root / "skills" / "deploy_helper" / "SKILL.md"
        promoted.write_text("---\nname: deploy_helper\ndescription: hand written\n---\nbody\n",
                            encoding="utf-8")
        pipe.prune("skill", "deploy_helper")
        assert promoted.exists(), "prune deleted a file whose content was not ours"


def test_failed_skill_draft_can_be_retried() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        _, pipe = _pipeline(tmp, wrapper=ScriptedWrapper(["websearch"]), known_tools={"bash"})
        first = pipe.create("skill", "retry_me",
                            "A helper whose first draft fails behavioural validation.",
                            _skill_spec())
        assert not first.ok, first
        pipe.wrapper = lambda: ScriptedWrapper(["bash"])  # the model now behaves
        second = pipe.create("skill", "retry_me",
                             "A helper whose second draft passes behavioural validation.",
                             _skill_spec())
        assert second.ok, second


def test_a_draft_upsert_does_not_demote_a_promoted_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        store, pipe = _pipeline(tmp)
        pipe.create("tool", "double_it", "Double an integer and return it as text.",
                    TOOL_SPEC, TOOL_TESTS)
        pipe.promote("tool", "double_it")
        store.upsert("tool", "double_it", tier="draft", description="re-draft attempt")
        assert store.get("tool", "double_it")["tier"] == "promoted", "a draft upsert demoted it"


def test_set_tier_rejects_an_unknown_tier() -> None:
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        store, _ = _pipeline(tmp)
        try:
            store.set_tier("tool", "anything", "banana")
        except ValueError:
            return
        raise AssertionError("an unknown tier was accepted")


def test_promoted_skill_stays_in_the_trigger_index() -> None:
    """Promotion writes skills/<name>/SKILL.md; the index must not drop it until restart."""
    with tempfile.TemporaryDirectory() as tmp, _sandbox(tmp):
        harness = _harness(ScriptedWrapper(["bash"]))
        harness.tools["bash"] = pleiades.Tool("bash", "run bash", {"type": "object"}, lambda **k: "")
        harness.create_artifact(
            "skill", "deploy_helper",
            "Check a deployment's state. Use when the user asks about a deploy.",
            _skill_spec())
        assert "deploy_helper [provisional]" in harness.skills, harness.skills
        harness.artifact_admin("promote", "skill", "deploy_helper")
        assert "deploy_helper [promoted]" in harness.skills, \
            "promotion dropped the skill from the index until restart"


def main() -> None:
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} checks passed")


if __name__ == "__main__":
    main()
