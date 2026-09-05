"""External-translation LQA: audit translations we did NOT produce (a
developer's existing target text) through the same tier cascade.

Implements docs/skills/lqa-batch-split.md: pairs are content-classified
(keys are opaque GUIDs, so the rules classifier is useless here), split
into a story file and a pure-strings file, and Tier 3 reviews each class
with its own batch size (story n=5, strings n=20).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import agents
from .gate_checks import GateConfig
from .graphs.lqa import LQAConfig, LQAContext, run_lqa_stage
from .llm import Provider
from .memory import RunDB, TranslationMemory
from .schemas import (Domain, IntakeBrief, LQAReport, UniqueString)

# Skill: story class = narrative/persuasive domains (small batches).
STORY_DOMAINS = {Domain.DIALOGUE.value, Domain.MARKETING.value}
CLASSIFY_BATCH = 40


def load_pairs(path: Path) -> List[dict]:
    pairs = []
    for i, line in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        row = json.loads(line)
        for field in ("key", "source_text", "target_text"):
            if not isinstance(row.get(field), str):
                raise ValueError(f"line {i + 1}: missing {field!r}")
        pairs.append(row)
    if not pairs:
        raise ValueError(f"no pairs in {path}")
    return pairs


def seed_audit_db(db: RunDB, pairs: List[dict]) -> None:
    """Dedup by (source text, target text); the dev's target rides in as
    the 'accepted' translation under audit.

    Deduping by SOURCE ALONE was wrong here, and wrong in a way that
    corrupts the client report rather than merely losing coverage. Game
    dialogue repeats a line verbatim — "Who are you?", "Yes." — and the
    same English is legitimately rendered differently by speaker, gender
    or context. Keeping the first target and attaching it to every
    occurrence produced bug rows whose Source Text and Current
    Translation were DIFFERENT LINES: an audit reporting a defect in a
    translation that was never there.

    Pairing on both sides keeps each distinct rendering as its own row,
    so a real inconsistency surfaces as two rows to compare instead of
    one row silently overwriting the other. Identical (source, target)
    repeats still collapse, which is the saving the dedup exists for.
    """
    by_pair: Dict[Tuple[str, str], List[str]] = {}
    for pair in pairs:
        by_pair.setdefault(
            (pair["source_text"], pair["target_text"]), []
        ).append(pair["key"])
    uniques, targets = [], {}
    for index, ((text, target), keys) in enumerate(by_pair.items()):
        uid = f"u{index:04d}"
        uniques.append(UniqueString(uid=uid, text=text, keys=keys))
        targets[uid] = target
    db.seed(uniques)
    for unique in uniques:
        db.record(unique.uid, status="accepted",
                  target=targets[unique.uid], resolution="external")


def classify_content(provider: Provider, db: RunDB) -> Dict[str, int]:
    """LLM content classification (skill: GUID keys ⇒ no prefix rules).
    Uncertainty prefers story — the smaller batch (fail expensive)."""
    rows = db.by_status("accepted")
    for start in range(0, len(rows), CLASSIFY_BATCH):
        batch = rows[start:start + CLASSIFY_BATCH]
        labels, _fp = agents.classify_domains(
            provider, [(r["uid"], r["text"]) for r in batch])
        for item in labels.items:
            db.label(item.key, item.domain, item.confidence)
    counts: Dict[str, int] = {}
    for row in db.by_status("accepted"):
        counts[row["domain"]] = counts.get(row["domain"], 0) + 1
    return counts


def split_files(db: RunDB, out_dir: Path, name: str, *, source_lang: str,
                target_lang: str) -> Tuple[Path, Path, Dict[str, int]]:
    """The two review artifacts (skill contract outputs 1+2)."""
    story_path = out_dir / f"split_story.{name}.jsonl"
    strings_path = out_dir / f"split_strings.{name}.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {"story": 0, "string": 0}
    with story_path.open("w", encoding="utf-8") as story_fh, \
            strings_path.open("w", encoding="utf-8") as strings_fh:
        for row in db.by_status("accepted"):
            is_story = row["domain"] in STORY_DOMAINS
            fh = story_fh if is_story else strings_fh
            counts["story" if is_story else "string"] += 1
            for key in row["keys"]:
                fh.write(json.dumps(
                    {"key": key, "source_language": source_lang,
                     "target_language": target_lang,
                     "source_text": row["text"],
                     "target_text": row["target"] or "",
                     "domain": row["domain"]}, ensure_ascii=False) + "\n")
    return story_path, strings_path, counts


class LocaleConflict(ValueError):
    """An explicit --locale disagrees with the pairs file's own
    `target_language`. Fail loudly: one of the two is a mistake, and
    guessing which produces an audit scored against the wrong language."""


def pairs_locale(pairs: List[dict]) -> Optional[str]:
    """The locale the pairs file declares for itself, or None.

    Exporters written by `exports.emit_bilingual_jsonl` stamp
    `target_language` on every line, so an audit can read its locale off
    its own input instead of inferring one. A file carrying more than one
    target language is not a single-locale audit and is refused here
    rather than silently reviewed under whichever line came first.
    """
    seen = {row["target_language"] for row in pairs
            if isinstance(row.get("target_language"), str)
            and row["target_language"].strip()}
    if not seen:
        return None
    if len(seen) > 1:
        raise LocaleConflict(
            "pairs file mixes target languages: "
            + ", ".join(sorted(seen))
            + " — audit one locale per file")
    return seen.pop()


def resolve_locale(intake: IntakeBrief, pairs: List[dict],
                   requested: Optional[str] = None) -> str:
    """Which language this audit is actually reviewing.

    Precedence: explicit `--locale` > the pairs file's own
    `target_language` > `intake.target_locales[0]`.

    The last step is a fallback, not a default worth trusting: it used to
    be the ONLY step, which made every audit a review of
    `target_locales[0]` no matter which pairs file was handed in. A ja
    audit under a zh-CN-first intake was configured with the zh-CN
    reviewer prompt, the zh-CN glossary, the zh-CN gate and the zh-CN
    translation memory, then stamped `locale: zh-CN` on its own report —
    so it flagged every correct Japanese line as "not Simplified
    Chinese". Reading the locale off the input makes that failure
    impossible instead of merely correctable by a flag.
    """
    declared = pairs_locale(pairs)
    if requested:
        if declared and declared != requested:
            raise LocaleConflict(
                f"--locale {requested!r} disagrees with the pairs file's "
                f"target_language {declared!r} — refusing to audit "
                f"{declared!r} text under {requested!r} rules")
        return requested
    if declared:
        return declared
    if not intake.target_locales:
        raise ValueError(
            "cannot determine target locale: pairs file declares no "
            "target_language and the intake brief lists no target locales; "
            "pass --locale")
    return intake.target_locales[0]


def smoke_audit(job, provider: Optional[Provider], pairs_path: Path, *,
                size: int = 5,
                locale: Optional[str] = None) -> "SmokeResult":
    """Pre-flight for `orbit8 lqa run`: resolve the locale, load the
    glossary, and put a few real pairs through Tier 3 — without writing
    an attempt, a report or a run DB into the job.

    This exists because of a specific failure. An audit whose locale came
    from the intake order rather than the input reviewed Japanese under
    Simplified Chinese rules and returned ~450 confident findings; the
    output looked like a catastrophic translation, not a misconfiguration,
    and the whole batch was wasted. Every fact needed to catch that in
    five seconds — the resolved locale, which glossary loaded, how many
    terms it enforces, and what the reviewer says about three real pairs —
    is on the SmokeResult.

    `locale` follows the same precedence as the audit itself, so a
    contradiction between the flag and the pairs file surfaces here rather
    than mid-batch.
    """
    from .schemas import SmokeResult
    import time

    intake: IntakeBrief = job.store.read(0, "intake", IntakeBrief)
    result = SmokeResult(locale="", kind="lqa", sampled=0, pending_total=0,
                         ok=False, source_lang=intake.source_lang)
    try:
        pairs = load_pairs(pairs_path)
        result.pending_total = len(pairs)
        # Resolve FIRST: a LocaleConflict here is the single most valuable
        # thing this function can report, and it costs nothing.
        resolved = resolve_locale(intake, pairs, locale)
        result.locale = result.target_lang = resolved
    except Exception as err:
        result.error = f"{type(err).__name__}: {err}"
        return result

    glossary = job._glossary(resolved)
    result.glossary_terms = len(glossary.locked_map()) if glossary else 0
    result.glossary_source = f"t1:{glossary.game}" if glossary else "none"
    result.style_brief = job._style_or_none() is not None
    if not glossary:
        result.warnings.append(
            f"no glossary resolved for {resolved} — the terminology check "
            f"will find nothing, which reads as a clean bill of health")
    if provider is None:
        result.warnings.append(
            "no provider: T1+T2 only, so this checks config and NOT the "
            "reviewer prompt")

    sample = pairs[:max(1, size)]
    result.sampled = len(sample)
    result.samples = [{"key": p["key"], "source": p["source_text"],
                       "target": p["target_text"]} for p in sample]
    result.provider = type(provider).__name__ if provider else ""
    result.model = str(getattr(provider, "model", "") or "") if provider else ""

    started = time.monotonic()
    try:
        # A scratch RunDB under smoke/, never the audit's own: seeding the
        # real one would leave a half-populated audit behind.
        db = RunDB(job.store.smoke_db_path(f"lqa-{resolved}"))
        seed_audit_db(db, sample)
        cfg = LQAConfig(
            game=intake.game, source_lang=intake.source_lang,
            locale=resolved, batch_size=len(sample),
            batch_size_story=len(sample),
            deterministic_only=provider is None,
            client_lang=intake.client_lang,
            gate=GateConfig(source_lang=intake.source_lang,
                            target_lang=resolved,
                            locked_terms=(glossary.locked_map()
                                          if glossary else {})))
        ctx = LQAContext(provider=provider, cfg=cfg, run_db=db,
                         tm=TranslationMemory(job.store.tm_path()),
                         glossary=glossary,
                         style_brief=job._style_or_none(),
                         tenant=job._tenant())
        report = run_lqa_stage(ctx, f"{job.job_id}-smoke")
        # Findings hang off items, not the report; flatten them with the
        # source they were filed against, because "is this finding real?"
        # is unanswerable without seeing the string.
        result.findings = [
            {"uid": item.uid, "source": item.source[:80],
             "target": item.target[:80],
             "bug_type": vf.finding.bug_type.value,
             "severity": vf.finding.severity.value,
             "message": vf.finding.message[:160]}
            for item in report.items for vf in item.findings][:20]
        # "No findings" and "never looked" are different claims, and a
        # pre-flight that conflates them is worse than none.
        if report.t3_errors:
            result.warnings.append(
                f"{len(report.t3_errors)} T3 batch(es) FAILED — those "
                f"strings were not reviewed at all; check the provider "
                f"and model before launching")
        # The tell. A pre-flight where nearly every sampled string is
        # flagged is the signature of a wrong-language or wrong-glossary
        # configuration, not of uniformly bad translation — so say so
        # here, where it costs five strings instead of the whole batch.
        if report.flagged_strings >= max(1, result.sampled):
            result.warnings.append(
                f"EVERY sampled string was flagged "
                f"({report.flagged_strings}/{result.sampled}) — check the "
                f"locale ({resolved}) and glossary before launching; a "
                f"wrong-language audit looks exactly like this")
        result.ok = True
    except Exception as err:
        result.error = f"{type(err).__name__}: {err}"
        result.ok = False
    result.elapsed_s = round(time.monotonic() - started, 2)
    return result


def run_external_lqa(job, provider: Optional[Provider], pairs_path: Path, *,
                     name: str = "dev-audit", batch_story: int = 5,
                     batch_string: int = 20, t3_threshold: float = 0.75,
                     deterministic_only: bool = False,
                     locale: Optional[str] = None) -> LQAReport:
    """Seed → classify → split → tier cascade → attempt-versioned report.
    `job` is a controller.Job; all artifact writes go through its store.

    `locale` overrides the audited language; see `resolve_locale` for the
    precedence and for why the intake brief is the last resort."""
    intake: IntakeBrief = job.store.read(0, "intake", IntakeBrief)
    pairs = load_pairs(pairs_path)
    locale = resolve_locale(intake, pairs, locale)
    db = RunDB(job.store.run_db_path(f"lqa-{name}"))
    seed_audit_db(db, pairs)

    if provider is not None and not deterministic_only:
        classify_content(provider, db)

    attempt = job.store.new_attempt(5)
    out_dir = job.store.stage_dir(5, attempt)
    story_path, strings_path, split_counts = split_files(
        db, out_dir, name, source_lang=intake.source_lang,
        target_lang=locale)

    glossary = job._glossary(locale)
    cfg = LQAConfig(
        game=intake.game, source_lang=intake.source_lang, locale=locale,
        batch_size=batch_string, batch_size_story=batch_story,
        t3_confidence_threshold=t3_threshold,
        deterministic_only=deterministic_only or provider is None,
        client_lang=intake.client_lang,
        gate=GateConfig(source_lang=intake.source_lang, target_lang=locale,
                        locked_terms=(glossary.locked_map()
                                      if glossary else {})))
    ctx = LQAContext(provider=provider, cfg=cfg, run_db=db,
                     tm=TranslationMemory(job.store.tm_path()),
                     glossary=glossary, style_brief=job._style_or_none(),
                     tenant=job._tenant())
    report = run_lqa_stage(ctx, job.job_id)
    job.store.write(5, f"lqa_report.{name}", report,
                    produced_by="code:external-lqa@1", attempt=attempt)
    report_dict = {"story_file": str(story_path),
                   "strings_file": str(strings_path),
                   "split": split_counts}
    (out_dir / f"split_summary.{name}.json").write_text(
        json.dumps(report_dict, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return report
