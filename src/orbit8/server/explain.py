"""Job context for the chat assistant.

The assistant answers from the job's own artifacts, not from the model's
impression of how localization works. This module gathers the facts;
`chat.py` turns them into prose.

Why a fixed context bundle rather than giving the model tools to roam:
the questions a non-specialist asks are predictable ("what is this
waiting for?", "what happens if I approve?", "what did it find?"), and
every one is answered by the same handful of artifacts. A tool loop
would add latency and a way to be wrong, for no extra coverage.

Nothing here can write. The assistant explains; humans act.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..controller import GATE_NAMES, Job
from ..schemas import HealthReport, IntakeBrief

# Who normally clears each gate, and what signing it commits you to. The
# second half is the part a producer or engineer actually needs: the
# console already says "asset lock", and that phrase is the problem.
GATE_MEANING: Dict[str, Dict[str, str]] = {
    "G0": {
        "who": "PM or client",
        "means": "Confirms the scope: which languages, roughly how much "
                 "text, and the market read. Nothing has been translated "
                 "yet.",
        "after": "The source file is parsed and analysed for domains and "
                 "style.",
    },
    "G1": {
        "who": "dev team",
        "means": "Freezes the glossary. From here on, the listed terms "
                 "translate exactly one way, everywhere.",
        "after": "A small pilot batch is translated so you can check the "
                 "result before paying for the whole file.",
    },
    "G2": {
        "who": "PM",
        "means": "Accepts the pilot translation as representative.",
        "after": "The full file is translated.",
    },
    "G3": {
        "who": "linguist",
        "means": "Accepts the review of strings the checker flagged. "
                 "Rejections you supply become glossary entries and style "
                 "rules for next time.",
        "after": "In-game test plans are produced.",
    },
    "G4": {
        "who": "QA",
        "means": "Confirms the build was tested in-game, not just on paper.",
        "after": "Deliverables and store copy are assembled.",
    },
    "G5": {
        "who": "PM or client",
        "means": "Signs off the delivery.",
        "after": "The job is complete.",
    },
}


def _safe(fn, default=None):
    """Artifacts are read best-effort: a missing one is normal for a job
    that has not reached that stage, and must not fail the whole answer."""
    try:
        return fn()
    except Exception:                                   # noqa: BLE001
        return default


def _glossary_health(job: Job, locales: List[str]) -> Dict[str, Any]:
    """Blockers are why G1 refuses to open, so they are the single most
    useful thing to surface when a job is stuck there."""
    out: Dict[str, Any] = {}
    for locale in locales:
        report = _safe(
            lambda loc=locale: job.store.read(3, f"health.{loc}", HealthReport))
        if report is None:
            continue
        out[locale] = {
            "blockers": [getattr(b, "message", str(b))
                         for b in report.blockers][:10],
            "warnings": [getattr(w, "message", str(w))
                         for w in report.warnings][:10],
            "stats": dict(list(report.stats.items())[:12]),
        }
    return out


def collect(job: Job) -> Dict[str, Any]:
    """Everything the assistant is allowed to know about one job."""
    stage = job.derive()
    control = job.control
    intake = _safe(lambda: job.store.read(0, "intake", IntakeBrief))
    locales = list(getattr(intake, "target_locales", []) or [])

    artifacts: List[Dict[str, Any]] = []
    for n in range(8):
        for name, attempts in sorted(job.store.artifact_names(n).items()):
            artifacts.append({"stage": n, "name": name, "attempts": attempts})
        flat = job.store.job_dir / f"s{n}"
        if flat.is_dir():
            seen = set(job.store.artifact_names(n))
            for path in sorted(flat.glob("*.json")):
                if path.stem not in seen:
                    artifacts.append({"stage": n, "name": path.stem})

    context: Dict[str, Any] = {
        "job_id": job.job_id,
        "game": getattr(intake, "game", None),
        "source_lang": getattr(intake, "source_lang", None),
        "target_locales": locales,
        "stage": {"phase": stage.phase, "action": stage.action,
                  "gate": stage.gate, "target": stage.target,
                  "detail": stage.detail},
        "pending_gate": stage.gate,
        "approvals": control.get("approvals", {}),
        "artifacts": artifacts,
    }

    if stage.gate:
        context["gate_explainer"] = {
            "id": stage.gate,
            "label": GATE_NAMES.get(stage.gate, stage.gate),
            **GATE_MEANING.get(stage.gate, {}),
        }

    health = _glossary_health(job, locales)
    if health:
        context["glossary_health"] = health

    return context


def opening_message(context: Dict[str, Any]) -> str:
    """What the panel says before anyone types.

    A chat box that waits to be asked is useless to someone who does not
    know the vocabulary — which is exactly this audience. So the panel
    opens by naming the current decision in plain words.
    """
    game = context.get("game") or "This job"
    gate = context.get("gate_explainer")
    if gate:
        blocked = []
        for locale, health in (context.get("glossary_health") or {}).items():
            blocked.extend(f"{locale}: {b}" for b in health.get("blockers", []))
        lines = [
            f"**{game}** is waiting on **{gate['id']} — {gate.get('label','')}**.",
            "",
            gate.get("means", ""),
            "",
            f"Normally signed by: {gate.get('who', 'a human')}. "
            f"After it clears: {gate.get('after', 'the job continues')}",
        ]
        if blocked:
            lines += ["", "Problems to resolve first:",
                      *(f"- {b}" for b in blocked[:5])]
        lines += ["", "Ask me what any of this means, or what happens if you "
                      "approve."]
        return "\n".join(x for x in lines if x is not None)

    phase = context["stage"]["phase"]
    return (f"**{game}** is in the **{phase}** stage: "
            f"{context['stage']['action']}.\n\n"
            "Nothing needs you right now. Ask me what is happening or what "
            "comes next.")
