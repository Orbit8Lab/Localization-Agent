"""Runtime-loadable skill documents (PLAN §8, capability 2).

The failure this module exists to prevent is silent: before it, editing
`docs/skills/*.md` changed nothing at runtime and nothing noticed. So the
tests here are mostly about things being LOUD —

- a doc naming a tool that does not exist must refuse to load, because
  otherwise it teaches the agent to attempt the impossible and improvise
  (design §7);
- the shipped docs must all load, so a typo in frontmatter fails in CI
  rather than mid-session;
- the documented Tier-3 batch policy must match `LQAConfig`, which is the
  cheap drift check PLAN §8 asks for by name.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from orbit8.graphs.lqa import LQAConfig
from orbit8.orchestrator import ChatOrchestrator
from orbit8.skill_docs import (Skill, SkillError, SkillLibrary,
                               default_skills_dir, load_skill,
                               parse_frontmatter)

LIFECYCLE_PHASES = ["INTAKE", "INGEST", "CONTEXT", "ASSET", "PILOT",
                    "PRODUCTION", "LQA", "FLAGGED", "TESTING", "RELEASE"]

TOOLS = set(ChatOrchestrator.tool_names())


def _write(tmp_path: Path, text: str, name: str = "s.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------- parsing

def test_frontmatter_is_split_from_the_body():
    meta, body = parse_frontmatter(
        "---\nname: x\nphase: ASSET\n---\n# Heading\ntext\n")
    assert meta == {"name": "x", "phase": "ASSET"}
    assert body.strip().startswith("# Heading")


def test_a_list_value_is_parsed():
    meta, _ = parse_frontmatter("---\ntools: [status, approve]\n---\nbody\n")
    assert meta["tools"] == ["status", "approve"]


def test_a_doc_without_frontmatter_is_all_body():
    meta, body = parse_frontmatter("# Just a spec\n")
    assert meta == {} and body == "# Just a spec\n"


def test_a_malformed_frontmatter_line_is_rejected():
    with pytest.raises(SkillError):
        parse_frontmatter("---\nthis is not a pair\n---\nbody\n")


# ------------------------------------- §7: a doc cannot invent capability

def test_a_doc_naming_a_nonexistent_tool_refuses_to_load(tmp_path):
    """THE §7 guarantee. A doc referencing a capability that does not exist
    is worse than no doc: it teaches the agent to attempt something the
    system cannot do, then improvise when it fails."""
    path = _write(tmp_path,
                  "---\nname: bad\ntools: [status, summon_a_translator]\n"
                  "---\nbody\n")
    with pytest.raises(SkillError) as excinfo:
        load_skill(path, known_tools=TOOLS)
    assert "summon_a_translator" in str(excinfo.value)


def test_the_error_lists_what_is_actually_available(tmp_path):
    """A refusal that does not say what IS possible just moves the guessing
    one step back."""
    path = _write(tmp_path, "---\ntools: [nope]\n---\nbody\n")
    with pytest.raises(SkillError) as excinfo:
        load_skill(path, known_tools={"status", "approve"})
    assert "status" in str(excinfo.value)


def test_tools_must_be_a_list_not_a_string(tmp_path):
    path = _write(tmp_path, "---\ntools: status\n---\nbody\n")
    with pytest.raises(SkillError):
        load_skill(path, known_tools=TOOLS)


def test_a_bad_gate_name_is_rejected(tmp_path):
    path = _write(tmp_path, "---\ngate: G9\n---\nbody\n")
    with pytest.raises(SkillError):
        load_skill(path, known_tools=TOOLS)


def test_a_doc_with_no_tools_is_fine(tmp_path):
    """A policy spec (lqa-batch-split.md) declares none and is not a
    playbook. Requiring tools would force fake declarations."""
    skill = load_skill(_write(tmp_path, "# spec\n"), known_tools=TOOLS)
    assert skill.tools == []


# ------------------------------------------------- the tool registry

def test_the_registry_and_the_handlers_agree():
    """`tool_names()` derives from `_t_` prefixes while `_tools()` is a
    hand-written dict. Two sources of truth is the exact drift this module
    exists to stop, so they must not diverge."""
    import inspect
    source = inspect.getsource(ChatOrchestrator._tools)
    declared = set(re.findall(r'"(\w+)":\s*self\._t_', source))
    assert declared == TOOLS, (
        f"registry/handler mismatch: only in dict {declared - TOOLS}, "
        f"only as handler {TOOLS - declared}")


# --------------------------------------------------- the shipped docs

@pytest.fixture(scope="module")
def library() -> SkillLibrary:
    """Strict load: a typo in any shipped doc fails here rather than in a
    live session."""
    return SkillLibrary.load(default_skills_dir(), known_tools=TOOLS,
                             strict=True)


def test_every_shipped_doc_loads(library: SkillLibrary):
    assert library.skills, "no skill docs found"


def test_every_lifecycle_phase_has_a_playbook(library: SkillLibrary):
    missing = [phase for phase, present
               in library.coverage(LIFECYCLE_PHASES).items() if not present]
    assert not missing, f"phases with no playbook: {missing}"


def test_the_readme_router_is_not_loaded_as_a_playbook(library: SkillLibrary):
    """It is a human-facing index; injecting it would spend context on a
    table of contents."""
    assert "README" not in library.names()


def test_every_declared_tool_exists(library: SkillLibrary):
    for skill in library.skills:
        assert set(skill.tools) <= TOOLS, skill.name


def test_gated_phases_declare_the_approve_tool(library: SkillLibrary):
    """A playbook whose stage ends at a gate has to be able to talk about
    approving it; omitting the tool would make the doc describe a step the
    agent cannot take."""
    for skill in library.skills:
        if skill.gate:
            assert "approve" in skill.tools, skill.name


def test_each_gated_playbook_states_its_stop_conditions(library: SkillLibrary):
    """The prescriptive half of PLAN §8: a gate playbook without a
    pre-approval checklist is a description, not guidance."""
    for skill in library.skills:
        if skill.gate:
            assert "- [ ]" in skill.body, (
                f"{skill.name} has gate {skill.gate} but no checklist")


# --------------------------------------------------------- routing

def test_routing_is_keyed_on_the_derived_stage(library: SkillLibrary):
    assert library.for_stage("ASSET").name == "asset"
    assert library.for_stage("FLAGGED").name == "flagged"


def test_routing_is_case_insensitive_on_phase(library: SkillLibrary):
    assert library.for_stage("asset") is library.for_stage("ASSET")


def test_an_unknown_phase_returns_nothing_rather_than_guessing(library):
    """Silence is correct: playbooks are guidance over a working system, so
    an unmatched stage must degrade to today's behavior."""
    assert library.for_stage("NOT_A_PHASE") is None
    assert library.for_stage(None) is None


def test_a_gate_specific_doc_wins_over_a_general_one(tmp_path):
    """At a gate, the relevant guidance is what to verify before asking for
    approval — not the general stage description."""
    general = Skill(name="g", path=tmp_path / "g.md", body="general",
                    phase="ASSET")
    gated = Skill(name="gate", path=tmp_path / "x.md", body="gated",
                  phase="ASSET", gate="G1")
    library = SkillLibrary([general, gated])
    assert library.for_stage("ASSET", "G1").name == "gate"
    assert library.for_stage("ASSET").name == "g"


def test_a_non_strict_load_skips_a_broken_doc(tmp_path):
    """The live CLI loads non-strictly: one malformed doc must not stop an
    operator from working. CI still loads strictly."""
    _write(tmp_path, "---\ntools: [nope]\n---\nbad\n", "bad.md")
    _write(tmp_path, "---\nname: ok\nphase: ASSET\n---\nfine\n", "ok.md")
    library = SkillLibrary.load(tmp_path, known_tools=TOOLS, strict=False)
    assert library.names() == ["ok"]


def test_a_missing_directory_is_empty_not_an_error(tmp_path):
    assert SkillLibrary.load(tmp_path / "nope", known_tools=TOOLS).skills == []


# ------------------------------------------------------- injection

def test_the_prompt_section_subordinates_the_doc_to_the_tools(library):
    """§7 stated to the model, not only enforced in the loader: if the doc
    and the tool list disagree, the tools win."""
    text = library.for_stage("ASSET").prompt_section()
    assert "cannot grant capability" in text
    assert "ASSET" in text


def test_the_prompt_section_names_the_current_stage(library: SkillLibrary):
    text = library.for_stage("FLAGGED", "G3").prompt_section()
    assert "FLAGGED" in text and "G3" in text


# ------------------------- the drift test PLAN §8 asks for by name

def _documented_batch_sizes() -> dict:
    """The n= values from the doc's batch-policy table."""
    doc = (default_skills_dir() / "lqa-batch-split.md").read_text(
        encoding="utf-8")
    found = {}
    for line in doc.splitlines():
        match = re.match(r"\|\s*(story|string)\s*\|.*?n\s*=\s*(\d+)", line)
        if match:
            found[match.group(1)] = int(match.group(2))
    return found


def test_the_doc_still_states_its_batch_policy():
    sizes = _documented_batch_sizes()
    assert sizes.keys() == {"story", "string"}, (
        "lqa-batch-split.md no longer states both batch sizes in its table; "
        "the drift test below cannot check anything")


def test_the_documented_batch_policy_matches_the_code():
    """PLAN §8's "cheap first step", and it found a real drift immediately.

    The doc specifies string n=20 / story n=5, hand-transcribed into
    LQAConfig. `batch_size` was 10 — and because `controller.py` builds
    LQAConfig without overriding it, the MAIN pipeline ran Tier-3 string
    batches at 10 while `external_lqa.py` (which passes 20 explicitly)
    followed the spec. Two paths, two behaviors, one doc, no test.
    """
    sizes = _documented_batch_sizes()
    cfg = LQAConfig(game="G", source_lang="zh", locale="en")
    assert cfg.batch_size_story == sizes["story"]
    assert cfg.batch_size == sizes["string"]


def test_the_external_audit_path_agrees_with_the_default():
    """The two entry points must not diverge again: whatever the doc says,
    both the Controller path (LQAConfig defaults) and the external-audit
    path (run_external_audit's parameters) have to honor it."""
    import inspect

    from orbit8.external_lqa import run_external_lqa
    params = inspect.signature(run_external_lqa).parameters
    cfg = LQAConfig(game="G", source_lang="zh", locale="en")
    assert params["batch_string"].default == cfg.batch_size
    assert params["batch_story"].default == cfg.batch_size_story
