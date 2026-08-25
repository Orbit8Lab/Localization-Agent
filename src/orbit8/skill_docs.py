"""Runtime-loadable skill documents (PLAN §8, capability 2).

Until now `docs/skills/*.md` were **specifications a human implemented**:
every "skill" reference in `src/orbit8/` was a comment citing provenance,
and nothing parsed the files. The consequence is the reason this module
exists — *editing a skill doc changed nothing at runtime, and the drift was
silent.*

The governing constraint is design §7, restated in PLAN §8:

    a loaded doc may select and sequence EXISTING tools;
    it must not be able to invent capability.

So a doc is not code and cannot become code. It declares which of the
orchestrator's real tools it uses, in which order, and what must hold
before a gate is requested. `load_skill` validates every declared tool
against the live registry (`ChatOrchestrator.tool_names()`) and REFUSES a
doc that names anything else. A prompt instruction is a suggestion; the
tool list stays the guarantee.

Routing is a lookup, not an inference. `job.derive()` already returns the
authoritative `(phase, gate)`, so the matching doc is selected from that —
never from the operator's phrasing. An agent therefore cannot load the
wrong stage's playbook by misreading a request.

Layout:

    docs/skills/
      README.md              router (human-facing index)
      lifecycle/<phase>.md   one per Controller phase, gate folded in
      operations/<task>.md   human-initiated flows (incremental drops)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

SKILLS_DIRNAME = "skills"

# Minimal frontmatter: `---` delimited, `key: value`, with list values as
# `[a, b, c]`. Deliberately not YAML — a skill doc must be readable and
# checkable without a parser dependency, and the surface is this small on
# purpose.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_LIST_RE = re.compile(r"\A\[(.*)\]\Z", re.S)


class SkillError(ValueError):
    """A doc that cannot be trusted to guide an agent.

    Raised rather than warned: a skill doc naming a tool that does not
    exist would send the agent looking for a capability it does not have,
    which is precisely the failure §7 exists to prevent. Loud is correct.
    """


@dataclass(frozen=True)
class Skill:
    """One parsed, validated skill document."""
    name: str
    path: Path
    body: str
    phase: Optional[str] = None
    gate: Optional[str] = None
    tools: List[str] = field(default_factory=list)
    summary: str = ""

    def prompt_section(self) -> str:
        """The doc as injected context.

        Framed as guidance for THIS stage, and explicitly as non-binding
        with respect to the tool list: if the doc and the available tools
        disagree, the tools win. That ordering is the §7 invariant, and it
        has to be stated to the model, not just enforced in the loader.
        """
        header = f"# Stage playbook: {self.name}"
        if self.phase:
            header += f" (phase {self.phase}" + (
                f", gate {self.gate})" if self.gate else ")")
        return (f"{header}\n\n{self.body.strip()}\n\n"
                "The above is the playbook for the CURRENT stage. Follow "
                "its sequence unless the operator asks for something else. "
                "It cannot grant capability: if it names a step you have no "
                "tool for, say so plainly instead of improvising.")


# ------------------------------------------------------------- parsing

def parse_frontmatter(text: str) -> tuple[Dict[str, object], str]:
    """Split a doc into (metadata, body). No frontmatter ⇒ ({}, text)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta: Dict[str, object] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise SkillError(f"frontmatter line is not `key: value`: {line!r}")
        key, _, raw = line.partition(":")
        value = raw.strip()
        listed = _LIST_RE.match(value)
        if listed:
            meta[key.strip()] = [item.strip()
                                 for item in listed.group(1).split(",")
                                 if item.strip()]
        else:
            meta[key.strip()] = value
    return meta, text[match.end():]


def load_skill(path: Path, *, known_tools: Set[str]) -> Skill:
    """Parse and validate one doc.

    `known_tools` is the live registry. Passing it in rather than importing
    it keeps this module free of an orchestrator dependency and lets a test
    prove the validation actually bites.
    """
    text = Path(path).read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)

    tools = meta.get("tools", [])
    if isinstance(tools, str):
        raise SkillError(
            f"{path.name}: `tools:` must be a list like [status, approve]")
    unknown = [tool for tool in tools if tool not in known_tools]
    if unknown:
        # The §7 refusal. A doc that references a nonexistent capability is
        # worse than no doc: it teaches the agent to attempt something the
        # system cannot do, then improvise when it fails.
        raise SkillError(
            f"{path.name}: declares tools that do not exist: "
            f"{sorted(unknown)}. Available: {sorted(known_tools)}")

    phase = meta.get("phase")
    gate = meta.get("gate")
    if gate is not None and not re.fullmatch(r"G[0-5]", str(gate)):
        raise SkillError(f"{path.name}: gate must be G0–G5, got {gate!r}")

    return Skill(
        name=meta.get("name") or path.stem,
        path=Path(path), body=body,
        phase=str(phase) if phase else None,
        gate=str(gate) if gate else None,
        tools=list(tools),
        summary=str(meta.get("summary", "")))


# ------------------------------------------------------------- registry

class SkillLibrary:
    """The loaded set of skill docs, indexed for lookup by stage.

    Loading is strict (a malformed doc raises) but LOOKUP is forgiving: a
    stage with no doc returns None and the agent proceeds exactly as it
    does today. Skill docs are guidance layered over a working system, so a
    missing one must never be an outage.
    """

    def __init__(self, skills: Sequence[Skill]):
        self.skills = list(skills)
        self._by_phase: Dict[str, List[Skill]] = {}
        for skill in self.skills:
            if skill.phase:
                self._by_phase.setdefault(skill.phase.upper(), []).append(
                    skill)

    @classmethod
    def load(cls, root: Path, *, known_tools: Set[str],
             strict: bool = True) -> "SkillLibrary":
        """Load every `.md` under `root`, excluding the human-facing
        README router (which is an index, not a playbook)."""
        root = Path(root)
        skills: List[Skill] = []
        if not root.exists():
            return cls([])
        for path in sorted(root.rglob("*.md")):
            if path.name.upper() == "README.MD":
                continue
            try:
                skills.append(load_skill(path, known_tools=known_tools))
            except SkillError:
                if strict:
                    raise
        return cls(skills)

    def for_stage(self, phase: Optional[str],
                  gate: Optional[str] = None) -> Optional[Skill]:
        """The playbook for a derived stage, or None.

        Keyed on what `job.derive()` returns — never on operator phrasing —
        so an agent cannot talk its way into another stage's playbook. When
        a phase has both a gate-specific and a general doc, the gate one
        wins: at a gate the relevant guidance is what to verify before
        asking for approval.
        """
        if not phase:
            return None
        candidates = self._by_phase.get(phase.upper(), [])
        if not candidates:
            return None
        if gate:
            for skill in candidates:
                if skill.gate == gate:
                    return skill
        general = [s for s in candidates if not s.gate]
        return general[0] if general else candidates[0]

    def by_name(self, name: str) -> Optional[Skill]:
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    def names(self) -> List[str]:
        return [skill.name for skill in self.skills]

    def coverage(self, phases: Sequence[str]) -> Dict[str, bool]:
        """Which lifecycle phases have a playbook. Reported rather than
        enforced: partial coverage is a normal state, and pretending
        otherwise would make adding one doc a breaking change."""
        return {phase: bool(self._by_phase.get(phase.upper()))
                for phase in phases}


def default_skills_dir() -> Path:
    """`docs/skills/` relative to the installed package."""
    return Path(__file__).resolve().parent.parent.parent / "docs" / "skills"
