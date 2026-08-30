"""orbit8 CLI — drive a job through the lifecycle.

    orbit8 job init jobs/ <job-id> --game "<title>" --source strings.json \
        --source-lang zh --targets ko,ja --genre <genre>
    orbit8 next jobs/ <job-id> [--dry-run]      # run one step / show gate
    orbit8 approve jobs/ <job-id> G1 --by <operator>
    orbit8 status jobs/ <job-id>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .controller import GATES, GATE_NAMES, Job, Stage
from .llm import OpenAICompatProvider, PROVIDER_PRESETS
from .schemas import IntakeBrief, SourceBatch


def _print_stage(stage: Stage) -> None:
    where = f" [{stage.target}]" if stage.target else ""
    print(f"phase:    {stage.phase}{where}")
    if stage.gate:
        print(f"⏸ WAITING on {stage.gate} ({GATE_NAMES[stage.gate]}) — "
              f"a human must review:")
        print(f"          {stage.action}")
        print(f"          approve with: orbit8 approve <root> <job> "
              f"{stage.gate} --by <name>")
    else:
        print(f"next:     {stage.action}")
    if stage.detail:
        print(f"detail:   {stage.detail}")


def _cmd_init(args) -> int:
    intake = IntakeBrief(
        game=args.game, source_lang=args.source_lang,
        target_locales=args.targets.split(","),
        genre=args.genre.split(",") if args.genre else [],
        engine=args.engine, client_lang=args.client_lang,
        platforms=args.platforms.split(",") if args.platforms else [],
        reference_titles=(args.references.split(",")
                          if args.references else []),
        tenant_id=args.tenant)
    # Co-locating two organizations under one jobs root defeats the file
    # boundary: they share a project folder, and the file tools treat that
    # folder as home ground for both.
    from .tenancy import mixed_tenant_warning
    warning = mixed_tenant_warning(Path(args.root), intake.tenant_id)
    if warning:
        print(warning, file=sys.stderr)
    job = Job.init(Path(args.root), args.job_id, intake=intake,
                   source_files=args.source, pilot_size=args.pilot_size,
                   tester_hours=args.tester_hours)
    print(f"job initialized: {job.store.job_dir}")
    _print_stage(job.derive())
    return 0


def _cmd_next(args) -> int:
    job = Job(Path(args.root), args.job_id)
    factory = None
    if not args.dry_run:
        factory = lambda locale: OpenAICompatProvider(
            args.provider, model=args.model, api_key=args.api_key)
    before = job.derive()
    acted = job.next_step(factory, dry_run=args.dry_run)
    if acted.gate:
        _print_stage(acted)
        return 0
    where = f" [{acted.target}]" if acted.target else ""
    print(f"✓ ran {acted.phase}{where}: {acted.action}")
    _print_stage(job.derive())
    return 0


def _cmd_approve(args) -> int:
    job = Job(Path(args.root), args.job_id)
    try:
        job.approve(args.gate, by=args.by, note=args.note)
    except ValueError as err:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    print(f"✓ {args.gate} approved by {args.by}")
    _print_stage(job.derive())
    return 0


def _cmd_glossary_import(args) -> int:
    """Human-locked review files (docx/xlsx/csv/…) → staged T1 + RAG json +
    csv. Sandboxed converters parse the formats; the merge is deterministic
    (first alternative wins, (verify) rows flagged, dated audit notes)."""
    from .codegen import AdapterRecord
    from .glossary_import import (emit_csv, emit_rag_json, emit_staged_t1,
                                  import_review_files, merge_review)
    from .graphs.asset import health_check

    job = Job(Path(args.root), args.job_id)
    intake = job.store.read(0, "intake", IntakeBrief)
    locale = args.locale or intake.target_locales[0]
    provider = OpenAICompatProvider(args.provider, model=args.model,
                                    api_key=args.api_key)

    class _Cache:
        def get(self, key):
            if job.store.exists(3, key):
                return job.store.read(3, key, AdapterRecord).script
            return None
        def put(self, key, record, fingerprint):
            job.store.write(3, key, record,
                            produced_by="agent:adapter-writer@1",
                            model_fingerprint=fingerprint)

    rows, _ = import_review_files(
        provider, [Path(f) for f in args.files], adapter_cache=_Cache())
    terms, report = merge_review(rows)
    report.locale = locale

    out_dir = Path(args.out) if args.out else job.store.stage_dir(3)
    staged = job.store.stage_dir(3) / f"t1.{locale}.staged.json"
    rag = out_dir / f"glossary_{locale}_terms.json"
    sheet = out_dir / f"glossary_{locale}_terms.csv"
    emit_staged_t1(terms, staged)
    emit_rag_json(terms, rag, game=intake.game, locale=locale,
                  source_lang=intake.source_lang)
    emit_csv(terms, sheet, locale)
    report.outputs = {"staged_t1": str(staged), "rag_json": str(rag),
                      "csv": str(sheet)}
    job.store.write(3, f"glossary_import.{locale}", report,
                    produced_by="code:glossary-import@1")

    # Re-run the deterministic health check over the imported glossary so
    # G1 judges the human-locked asset, not the machine draft.
    corpus = [r.text for r in job.store.read(1, "uniques",
                                             SourceBatch).records]
    t1 = {t: {"translation": e["translation"]} for t, e in terms.items()}
    health = health_check(t1, locale, corpus)
    job.store.write(3, f"health.{locale}", health,
                    produced_by="code:health-check@1")

    print(f"imported: {report.base_terms} terms "
          f"({report.changes_applied} changes applied, "
          f"{report.needs_review} flagged needs_review, "
          f"{report.dropped_keep_n} dropped keep=N)")
    for conflict in report.conflicts:
        print(f"⚠ conflict: {conflict}")
    print(f"health:   {len(health.blockers)} blockers, "
          f"{len(health.warnings)} warnings")
    for name, path in report.outputs.items():
        print(f"{name+':':10s}{path}")
    _print_stage(job.derive())
    return 0


def _cmd_lqa_run(args) -> int:
    """Audit external translations (docs/skills/lqa-batch-split.md):
    content-classify → split story/strings files → tier cascade with
    per-class batch sizes."""
    from .external_lqa import run_external_lqa
    job = Job(Path(args.root), args.job_id)
    provider = (None if args.deterministic_only else
                OpenAICompatProvider(args.provider, model=args.model,
                                     api_key=args.api_key))
    report = run_external_lqa(
        job, provider, Path(args.pairs), name=args.name,
        batch_story=args.batch_story, batch_string=args.batch_string,
        t3_threshold=args.t3_threshold,
        deterministic_only=args.deterministic_only)
    if report.cascade_ledger:
        print(f"cascade:  {json.dumps(report.cascade_ledger)}")
    if report.t3_stats:
        print(f"tier3:    {json.dumps(report.t3_stats)}")
    print(f"checked:  {report.checked} strings")
    print(f"flagged:  {report.flagged_strings} strings, "
          f"{report.findings_total} findings "
          f"(confirmed {report.confirmed} / overturned {report.overturned} "
          f"/ uncertain {report.uncertain})")
    if report.by_severity:
        print(f"severity: {json.dumps(report.by_severity)}")
    if report.by_bug_type:
        print(f"bug type: {json.dumps(report.by_bug_type)}")
    attempt = job.store.latest_attempt(5)
    print(f"report:   {job.store.stage_dir(5, attempt)}/"
          f"lqa_report.{args.name}.json")
    if report.block_ship:
        print("✋ BLOCK SHIP: high-severity findings survived review")
        return 2
    return 0


def _cmd_lqa_report(args) -> int:
    """Client deliverable from a stored LQA report (skill:
    bug-report-builder): xlsx + technical summary, Repair-agent
    suggestions, round-trip safe."""
    import json as json_mod
    from .bug_report import (build_suggestions, load_locations,
                             write_bug_report_xlsx, write_tech_summary)
    from .schemas import LQAReport, StyleBrief
    job = Job(Path(args.root), args.job_id)
    intake = job.store.read(0, "intake", IntakeBrief)
    attempt = args.attempt or job.store.latest_attempt(5)
    report = job.store.read(5, f"lqa_report.{args.name}", LQAReport,
                            attempt=attempt)
    split_counts = None
    split_path = (job.store.stage_dir(5, attempt)
                  / f"split_summary.{args.name}.json")
    if split_path.exists():
        split_counts = json_mod.loads(
            split_path.read_text(encoding="utf-8"))["split"]

    suggestions = {}
    if not args.no_suggestions:
        provider = OpenAICompatProvider(args.provider, model=args.model,
                                        api_key=args.api_key)
        suggestions = build_suggestions(
            provider, report.items, game=intake.game,
            source_lang=intake.source_lang, locale=report.locale,
            glossary=job._glossary(report.locale),
            style_brief=job._style())

    locations = (load_locations(Path(args.locations_from))
                 if args.locations_from else None)

    slug = intake.game.replace(" ", "")
    out_dir = (Path(args.out) if args.out
               else job.store.stage_dir(5, attempt))
    tag = f".{args.tag}" if args.tag else ""
    xlsx = out_dir / f"{slug}_Bug_Report_{report.locale}{tag}.xlsx"
    summary = out_dir / f"{slug}_LQA_Summary_{report.locale}{tag}.md"
    count = write_bug_report_xlsx(report, xlsx, suggestions=suggestions,
                                  game=intake.game, locations=locations)
    write_tech_summary(report, summary, game=intake.game,
                       split_counts=split_counts,
                       suggestions_count=len(suggestions))
    print(f"bugs:     {count} rows ({len(suggestions)} suggested fixes)")
    print(f"xlsx:     {xlsx}")
    print(f"summary:  {summary}")
    return 0


def _cmd_lqa_deliver(args) -> int:
    """Apply post-editing decisions (Decision / Modify Version columns of
    the reviewed bug report) to the shipped .po files and write a
    timestamped delivery folder. Zero LLM calls — decisions are human."""
    from .memory import TranslationMemory
    from .po_patch import deliver_from_review
    job = Job(Path(args.root), args.job_id)
    intake = job.store.read(0, "intake", IntakeBrief)
    locale = args.locale or intake.target_locales[0]
    out_dir = (Path(args.out) if args.out
               else Path(args.root).resolve().parent / "30-deliverables")
    tm = None if args.no_tm else TranslationMemory(job.store.tm_path())
    report = deliver_from_review(
        Path(args.review), [Path(p) for p in args.po], out_dir,
        timestamp=args.timestamp, tm=tm, locale=locale,
        sanity_check=not args.no_sanity_check,
        relabel=not args.no_relabel, team=args.team)
    print(f"decisions: {json.dumps(report.counts())}")
    for output in report.outputs:
        print(f"po:        {output}")
    for name, result in report.sanity.items():
        print(f"sanity:    {name}: {result['verdict']} "
              f"({result['errors']} errors, {result['warnings']} warnings)")
        for detail in result["error_details"][:5]:
            print(f"           {detail}")
    print(f"report:    {report.delivery_dir}/DELIVERY_REPORT.md")
    if report.inconsistent:
        print(f"⚠ {len(report.inconsistent)} source(s) ship with "
              f"inconsistent renderings — see Consistency warnings")
    if report.blocked:
        print("⛔ sanity gate FAILED — do not deliver")
        return 2
    if report.conflicts or report.unmatched:
        print("⚠ conflicts/unmatched rows present — see the report")
        return 2
    return 0


def _cmd_glossary_update(args) -> int:
    """Refresh the asset-pair glossary from post-editing results:
    decisions xlsx + PE'd bilingual .po + operator-supplied term pairs.
    Deterministic; originals untouched; audit report always written."""
    from .glossary_update import (TermDecision, load_decisions_xlsx,
                                  refresh_glossary, write_update_outputs)
    decisions = (load_decisions_xlsx(Path(args.decisions))
                 if args.decisions else [])
    for pair in args.term or []:
        if "=" not in pair:
            print(f"error: --term needs zh=EN, got {pair!r}")
            return 2
        zh, en = pair.split("=", 1)
        decisions.append(TermDecision(zh=zh.strip(), en=en.strip()))
    result = refresh_glossary(Path(args.assets), Path(args.pe_po),
                              decisions)
    md = write_update_outputs(result, Path(args.out), Path(args.assets))
    print(f"decisions: {len(decisions)} "
          f"({sum(1 for d in decisions if d.origin == 'user')} from "
          f"--term)")
    print(f"buckets:   {json.dumps(result.counts())}")
    for entry in result.conflicts_open[:5]:
        print(f"⚠ CONFLICT {entry['asset']}: {entry['zh'][:40]!r}")
    for entry in result.term_violations[:5]:
        print(f"⚠ TERM     {entry['asset']}: {entry['term']} → "
              f"{entry['expected_en']!r} missing")
    for s in result.suggestions[:8]:
        print(f"suggest:   {s['old_en']!r} -> {s['new_en']!r} "
              f"(×{s['occurrences']})")
    print(f"audit:     {md}")
    print(f"review:    {Path(args.out) / 'glossary_review.xlsx'} "
          f"(Glossary PE form — fill PE_Decision: Accept Suggested "
          f"Translation / Reject&Modification / Reject&Keep-as-it-is / "
          f"Reject&Cannot Answer)")
    return 1 if (result.conflicts_open or result.term_violations) else 0


def _cmd_glossary_distill(args) -> int:
    """PE-refresh the asset glossary, then boil it down to a compact
    term-level glossary (decisions locked, term-like assets mined) in
    the pipeline T1 file shape."""
    import json as json_mod
    from .glossary_update import (TermDecision, distill_term_glossary,
                                  load_decisions_xlsx, refresh_glossary,
                                  write_term_glossary)
    decisions = (load_decisions_xlsx(Path(args.decisions))
                 if args.decisions else [])
    for pair in args.term or []:
        if "=" not in pair:
            print(f"error: --term needs zh=EN, got {pair!r}")
            return 2
        zh, en = pair.split("=", 1)
        decisions.append(TermDecision(zh=zh.strip(), en=en.strip()))
    result = refresh_glossary(Path(args.assets), Path(args.pe_po),
                              decisions)
    zh_asset = json_mod.loads(
        (Path(args.assets) / "zh_asset.json").read_text("utf-8"))
    glossary, review = distill_term_glossary(
        zh_asset, result.updated_en, decisions,
        game=args.game or "", locale=args.locale)
    path = write_term_glossary(glossary, review, Path(args.out))
    meta = glossary["metadata"]
    print(f"terms:    {meta['locked_terms']} locked (decisions) + "
          f"{meta['mined_terms']} mined = {len(glossary['terms'])} total")
    print(f"ties:     {len(review)} excluded (see 'Ties' sheet)")
    print(f"xlsx:     {path}")
    print(f"json:     {Path(args.out) / 'glossary_terms.json'} "
          f"(pipeline T1 shape)")
    return 0


def _cmd_glossary_extract(args) -> int:
    """Corpus-first extraction, stages 0-3: dedup/strip → Han n-gram
    mining (sentence interiors included) → LLM or heuristic noise filter
    → assembly with locked decisions. Term-level review only."""
    from .glossary_update import TermDecision, load_decisions_xlsx
    from .term_extract import extract_glossary, write_extraction_outputs
    decisions = (load_decisions_xlsx(Path(args.decisions))
                 if args.decisions else [])
    for pair in args.term or []:
        if "=" not in pair:
            print(f"error: --term needs zh=EN, got {pair!r}")
            return 2
        zh, en = pair.split("=", 1)
        decisions.append(TermDecision(zh=zh.strip(), en=en.strip()))
    provider = None
    if args.provider != "none":
        from .llm import OpenAICompatProvider, autoload_env
        autoload_env()
        provider = OpenAICompatProvider(args.provider, model=args.model,
                                        api_key=args.api_key)
    result = extract_glossary(
        [Path(p) for p in args.po], decisions, provider=provider,
        min_freq=args.min_freq, game=args.game or "", locale=args.locale)
    write_extraction_outputs(result, Path(args.out))
    s = result.stats
    print(f"stage 0:  {s['corpus_strings']} strings, "
          f"{s['corpus_unique']} unique")
    print(f"stage 1:  {s['candidates_mined']} candidates mined")
    print(f"stage 2:  {s['kept_candidates']} kept, {len(result.dropped)} "
          f"dropped ({s['filter_mode']})")
    print(f"stage 3:  {s['terms_locked']} locked + {s['terms_mined']} "
          f"mined terms; {s['conflicts']} conflicts, {s['violations']} "
          f"violated locked terms, {s['needs_en']} need EN, "
          f"{s['flagged_against_locked']} flagged")
    print(f"review:   {Path(args.out) / 'extract_review.xlsx'} "
          f"(term-level Glossary PE form)")
    print(f"glossary: {Path(args.out) / 'glossary_terms.json'} (T1 shape)")
    return 1 if (result.conflicts or result.violations) else 0


def _cmd_po_translate(args) -> int:
    """Translate the untranslated strings of a received bilingual .po,
    glossary-constrained, into a work-product folder (patched copy +
    MTPE form + report). Not a delivery."""
    from .llm import OpenAICompatProvider, autoload_env
    from .po_translate import translate_untranslated
    from .project_paths import resolve_glossary
    glossary, notes = resolve_glossary(
        hint=Path(args.glossary) if args.glossary else None,
        start=Path(args.po))
    for note in notes:
        print(note)
    if glossary is None:
        print("error: no glossary — pass --glossary or promote one with "
              "`orbit8 glossary promote`")
        return 2
    print(f"glossary:   {glossary}")
    autoload_env()
    provider = OpenAICompatProvider(args.provider, model=args.model,
                                    api_key=args.api_key)
    run = translate_untranslated(
        Path(args.po), glossary, Path(args.out),
        provider=provider, game=args.game or "", locale=args.locale,
        batch_size=args.batch_size,
        reuse_from=Path(args.reuse_from) if args.reuse_from else None)
    print(f"entries:    {run.total} total, {run.todo} untranslated")
    print(f"reused:     {len(run.reused)} carried from previous run")
    print(f"prefilled:  {len(run.prefilled)} (glossary exact hits, "
          f"no LLM)")
    print(f"translated: {len(run.translated)} via {run.model} "
          f"({int(run.tokens)} tokens)")
    print(f"repaired:   {len(run.repaired)} after glossary-gate retry")
    print(f"violations: {len(run.violations)} still flagged")
    print(f"sanity:     {run.sanity}")
    print(f"outputs:    {Path(args.out)} (patched po + mtpe_form.xlsx + "
          f"report)")
    return 1 if run.violations or run.sanity != "ok" else 0


def _cmd_glossary_add(args) -> int:
    """Operator edits straight into a T1 glossary. Added terms land
    LOCKED with provenance; unlocked entries are overwritten; existing
    RULINGS are reported as conflicts unless --force."""
    from datetime import datetime
    from .glossary_edit import edit_glossary_file, parse_term_arg
    try:
        edits = [parse_term_arg(raw) for raw in (args.term or [])]
    except ValueError as err:
        print(f"error: {err}")
        return 2
    if not edits:
        print("error: nothing to do — pass --term zh=EN (repeatable)")
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    origin = args.origin or f"operator {datetime.now():%Y-%m-%d}"
    _glossary, result, backup = edit_glossary_file(
        Path(args.glossary), edits, origin=origin, force=args.force,
        backup_stamp=stamp)
    for entry in result.added:
        print(f"+ added     {entry['zh']} = {entry['en']}")
    for entry in result.aliased:
        print(f"+ alias     {entry['zh']} = {entry['en']} "
              f"(of {entry['alias_of']})")
    for entry in result.overwritten:
        print(f"~ replaced  {entry['zh']}: {entry['was']!r} → "
              f"{entry['en']!r}")
    for entry in result.retired:
        print(f"- retired   {entry['zh']} ({entry['was']!r}) "
              f"superseded by {entry['by']}")
    for entry in result.unchanged:
        print(f"= unchanged {entry['zh']} (already ruled {entry['en']!r})")
    for entry in result.conflicts:
        print(f"⚠ CONFLICT  {entry['zh']}: locked as "
              f"{entry['current']!r} ({entry['evidence']}), you asked "
              f"for {entry['en']!r} — rerun with --force to supersede")
    for entry in result.flagged:
        print(f"⚠ family    {entry['zh']} violates rule {entry['rule']}")
    if backup:
        print(f"backup:   {backup}")
        print(f"written:  {args.glossary} (+ xlsx view re-rendered)")
    return 1 if result.conflicts else 0


def _cmd_po_scan(args) -> int:
    """Standalone LQA scan of a bilingual .po — the full tier cascade
    without a job pipeline. Outputs a client bug report + LQA PE form."""
    from .po_scan import scan_po
    from .project_paths import resolve_glossary
    glossary, notes = resolve_glossary(
        hint=Path(args.glossary) if args.glossary else None,
        start=Path(args.po))
    for note in notes:
        print(note)
    provider = None
    if not args.deterministic_only:
        from .llm import OpenAICompatProvider, autoload_env
        autoload_env()
        provider = OpenAICompatProvider(
            args.provider, model=args.model, api_key=args.api_key,
            timeout=args.timeout, max_retries=args.retries,
            on_retry=lambda attempt, msg: print(
                f"\n  ↻ retry {attempt}/{args.retries} after {msg}",
                flush=True))
    if glossary is None and not args.no_glossary:
        print("error: no glossary resolved — pass --glossary, promote one "
              "with `orbit8 glossary promote`, or accept mechanical-only "
              "checks with --no-glossary")
        return 2
    print(f"glossary:   {glossary or '(none — mechanical checks only)'}")

    progress = {"done": 0, "failed": 0}

    def on_progress(event: str, detail: dict) -> None:
        if event == "t3_batch":
            progress["done"] += detail.get("size", 0)
            print(f"\r  T3 reviewed {progress['done']} strings"
                  + (f", {progress['failed']} unreviewed (batch errors)"
                     if progress["failed"] else ""),
                  end="", flush=True)
        elif event == "t3_batch_failed":
            progress["failed"] += detail.get("size", 0)
            print(f"\n  ⚠ batch failed ({detail.get('error', '')[:80]}) — "
                  f"continuing", flush=True)

    result = scan_po(
        Path(args.po), glossary, Path(args.out), provider=provider,
        game=args.game or "", locale=args.locale,
        source_lang=args.source_lang,
        deterministic_only=args.deterministic_only,
        suggestions=not args.no_suggestions,
        on_progress=None if args.deterministic_only else on_progress)
    if progress["done"] or progress["failed"]:
        print()
    report = result.report
    print(f"checked:    {report.checked} unique strings")
    print(f"flagged:    {report.flagged_strings} strings, "
          f"{report.findings_total} findings "
          f"{json.dumps(report.by_severity)}")
    print(f"cascade:    {json.dumps(report.cascade_ledger)}")
    unreviewed = report.cascade_ledger.get("t3_unreviewed", 0)
    if unreviewed:
        print(f"⚠ COVERAGE GAP: {unreviewed} strings went unreviewed in "
              f"T3 ({report.cascade_ledger.get('t3_batches_failed')} batch "
              f"failure(s)) — they are NOT known-clean; see t3_errors in "
              f"the report")
    if result.inconsistent:
        print(f"⚠ {len(result.inconsistent)} source(s) ship with "
              f"different renderings (inconsistent_renderings.json)")
    print(f"bug report: {result.outputs['bug_report']} "
          f"({result.bug_rows} rows)")
    print(f"PE form:    {result.outputs['pe_form']} "
          f"({result.pe_rows} rows)")
    return 1 if report.block_ship else 0


def _cmd_glossary_variants(args) -> int:
    """Record operator-approved alternate renderings on a term."""
    import shutil
    from datetime import datetime
    from .glossary_edit import add_variants
    from .glossary_update import write_term_glossary
    from .project_paths import resolve_glossary
    path, notes = resolve_glossary(
        hint=Path(args.glossary) if args.glossary else None,
        start=Path.cwd())
    for note in notes:
        print(note)
    if path is None:
        print("error: no glossary resolved — pass --glossary")
        return 2
    glossary = json.loads(path.read_text("utf-8"))
    origin = args.origin or f"operator {datetime.now():%Y-%m-%d}"
    try:
        for raw in args.term:
            if "=" not in raw:
                print(f"error: --term needs zh=Variant, got {raw!r}")
                return 2
            zh, variant = raw.split("=", 1)
            entry = add_variants(glossary, zh.strip(),
                                 [v.strip() for v in variant.split("|")],
                                 origin=origin)
            print(f"{zh.strip()}: preferred {entry['translation']!r}, "
                  f"also accepted {entry['variants']}")
    except KeyError as err:
        print(f"error: {err}")
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    shutil.copy2(path, path.with_suffix(f".bak-{stamp}.json"))
    path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    write_term_glossary(glossary, [], path.parent)
    print(f"written:  {path} (+ xlsx view)")
    return 0


def _cmd_style_init(args) -> int:
    """Write a starter style guide for a language pair into the project's
    40-reference/style/, plus a rendered markdown doc."""
    from .project_paths import canonical_style, find_project_root
    from .style_defaults import default_guide
    from .style_guide import render_markdown
    guide = default_guide(args.source_lang, args.target_lang)
    if guide is None:
        print(f"error: no starter guide for {args.source_lang}→"
              f"{args.target_lang} — author one by copying an existing "
              f"json from 40-reference/style/")
        return 2
    root = (Path(args.project) if args.project
            else find_project_root(Path.cwd()))
    if root is None:
        print("error: no project workspace found — pass --project")
        return 2
    path = canonical_style(root, args.source_lang, args.target_lang)
    if path.exists() and not args.force:
        print(f"error: {path} exists — pass --force to overwrite")
        return 2
    guide.save(path)
    doc = path.with_suffix(".md")
    doc.write_text(render_markdown(guide), encoding="utf-8")
    counts = guide.counts()
    print(f"style guide: {path}")
    print(f"doc:         {doc}")
    print(f"rules:       {len(guide.rules)} — "
          + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    print("mechanical rules run in the T1 gate; llm rules become the T3 "
          "rubric; advisory rules only steer the translator")
    return 0


def _cmd_style_check(args) -> int:
    """Validate a style guide against the format standard (§2.2) and
    show exactly what a prompt would receive."""
    from .project_paths import resolve_style_guide
    from .style_guide import StyleGuide
    if args.guide:
        guide, notes = StyleGuide.load(Path(args.guide)), []
    else:
        guide, notes = resolve_style_guide(
            args.source_lang, args.target_lang, start=Path.cwd())
    for note in notes:
        print(note)
    if guide is None:
        return 1
    problems = guide.validate()
    print(f"guide:   {guide.source_lang} → {guide.target_lang} "
          f"v{guide.version}")
    print(f"rules:   {len(guide.rules)} — {json.dumps(guide.counts())}")
    print(f"morph:   {guide.morphology.strategy}")
    for problem in problems:
        print(f"  ⚠ {problem}")
    if problems:
        print(f"⚠ {len(problems)} format problem(s) — rules that cannot "
              f"run look like coverage and provide none")
    else:
        print("format: OK")
    if args.prompt:
        print(f"\n--- prompt for domain={args.prompt!r} "
              f"(what the model actually sees) ---")
        print(guide.render_prompt(
            None if args.prompt == "all" else args.prompt) or "(empty)")
    return 1 if problems else 0


def _cmd_style_show(args) -> int:
    """Print the rules that apply to a language pair (and string type)."""
    from .project_paths import resolve_style_guide
    guide, notes = resolve_style_guide(args.source_lang, args.target_lang,
                                       start=Path.cwd())
    for note in notes:
        print(note)
    if guide is None:
        return 1
    rules = guide.for_domain(args.domain)
    print(f"{len(rules)} rule(s) for {args.source_lang}→"
          f"{args.target_lang}"
          + (f" / {args.domain}" if args.domain else " (all string types)"))
    for rule in rules:
        scope = ",".join(rule.domains) if rule.domains else "all"
        print(f"  [{rule.id}] {rule.enforcement:10s} {scope:20s} "
              f"{rule.text}")
    return 0


def _cmd_classify(args) -> int:
    """Classify a .po's strings into UI / dialogue / system / … and
    persist the labels as a project asset."""
    from .classify import LabelStore, classify_batch
    from .exports import read_po_entries
    from .schemas import Domain
    entries = [(k, zh, loc)
               for k, zh, _en, loc in read_po_entries(Path(args.po)) if k]
    provider = None
    if args.llm:
        from .llm import OpenAICompatProvider, autoload_env
        autoload_env()
        provider = OpenAICompatProvider(args.provider, model=args.model,
                                        api_key=args.api_key)
    labels = classify_batch(
        entries, provider=provider,
        on_progress=lambda event, detail: print(f"  {event}: {detail}"))
    store = LabelStore(Path(args.out))
    counts = store.merge(labels)
    for raw in args.correct or []:
        if "=" not in raw:
            print(f"error: --correct needs key=domain, got {raw!r}")
            return 2
        key, domain = raw.split("=", 1)
        store.correct(key.strip(), Domain(domain.strip()), by=args.by)
    path = store.save()
    print(f"labels:   {path}")
    print(f"merged:   {counts}")
    print(f"domains:  {json.dumps(store.counts())}")
    print(f"decided:  {json.dumps(store.by_source())}")
    untrusted = store.untrusted()
    if untrusted:
        print(f"⚠ {len(untrusted)} string(s) unclassified (low confidence) "
              f"— they route to human review; rerun with --llm or fix "
              f"with --correct <key>=<domain>")
    return 0


def _cmd_glossary_unify(args) -> int:
    """Declare a source-variant group: several source spellings of one
    concept that must all render as the same target term."""
    import shutil
    from datetime import datetime
    from .glossary_edit import unify_terms
    from .glossary_update import write_term_glossary
    from .project_paths import resolve_glossary
    path, notes = resolve_glossary(
        hint=Path(args.glossary) if args.glossary else None,
        start=Path.cwd())
    for note in notes:
        print(note)
    if path is None:
        print("error: no glossary resolved — pass --glossary")
        return 2
    glossary = json.loads(path.read_text("utf-8"))
    origin = args.origin or f"operator {datetime.now():%Y-%m-%d}"
    try:
        summary = unify_terms(
            glossary, args.canonical, args.variant,
            translation=args.translation, origin=origin,
            keep=not args.retire)
    except KeyError as err:
        print(f"error: {err}")
        return 2
    print(f"canonical:  {summary['canonical']} = "
          f"{summary['translation']!r} (locked)")
    if summary["variants"]:
        print(f"variants:   {', '.join(summary['variants'])} → all render "
              f"as {summary['translation']!r}")
    if summary["retired"]:
        print(f"retired:    {', '.join(summary['retired'])}")
    if summary["missing"]:
        print(f"⚠ not in glossary (skipped): "
              f"{', '.join(summary['missing'])}")
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    shutil.copy2(path, path.with_suffix(f".bak-{stamp}.json"))
    path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    write_term_glossary(glossary, [], path.parent)
    print(f"written:    {path} (+ xlsx view)")
    return 0


def _cmd_glossary_check(args) -> int:
    """Integrity audit of a T1 glossary: duplicate renderings, variant
    collisions, broken rename chains, family/locked contradictions."""
    from .glossary_check import check_glossary_file, write_check_report
    from .project_paths import resolve_glossary, resolve_style_guide
    path, notes = resolve_glossary(
        hint=Path(args.glossary) if args.glossary else None,
        start=Path.cwd())
    for note in notes:
        print(note)
    if path is None:
        print("error: no glossary resolved — pass --glossary")
        return 2
    morphology = None
    if args.source_lang and args.target_lang:
        guide, _ = resolve_style_guide(args.source_lang, args.target_lang,
                                       start=path)
        morphology = getattr(guide, "morphology", None)
    report = check_glossary_file(path, morphology=morphology)
    print(f"glossary: {path}")
    print(f"terms:    {report.terms_total} "
          f"({report.locked_total} locked)")
    print(f"issues:   {json.dumps(report.counts())}")
    for issue in report.issues:
        if issue.severity == "INFO" and not args.verbose:
            continue
        print(f"  {issue}")
    if args.out:
        print(f"report:   {write_check_report(report, Path(args.out))}")
    if report.blocked:
        print("⚠ NOT PUBLISHABLE — the file contradicts itself; fix the "
              "ERRORs above before promoting it")
    return 1 if report.blocked else 0


def _cmd_glossary_promote(args) -> int:
    """Publish a run's glossary as the project's active termbase
    (40-reference/glossary/) so every later stage resolves the same
    file. The run folder stays as the audit record."""
    from .glossary_check import check_glossary_file
    from .project_paths import find_project_root, promote_glossary
    source = Path(args.source)
    root = (Path(args.project) if args.project
            else find_project_root(source))
    if root is None:
        print("error: no project workspace found above "
              f"{source} — pass --project")
        return 2
    # integrity gate: a self-contradictory termbase must never become the
    # project's single source of truth
    candidate = source / "glossary_terms.json" if source.is_dir() else source
    report = check_glossary_file(candidate)
    print(f"check:    {json.dumps(report.counts())}")
    for issue in report.errors + report.warnings:
        print(f"  {issue}")
    if report.blocked and not args.force:
        print("⚠ REFUSING to promote: the glossary contradicts itself. "
              "Fix the ERRORs (or pass --force to publish anyway).")
        return 1
    target = promote_glossary(source, root)
    import json as json_mod
    meta = json_mod.loads(target.read_text("utf-8")).get("metadata", {})
    print(f"promoted: {target}")
    print(f"terms:    {meta.get('locked_terms', '?')} locked + "
          f"{meta.get('mined_terms', '?')} mined "
          f"(round {meta.get('round', 'n/a')})")
    print("every stage now resolves this file by default")
    return 0


def _cmd_glossary_merge(args) -> int:
    """Round merge: filled Glossary PE review + previous draft glossary
    → new versioned T1 glossary with a delta report."""
    import json as json_mod
    from .glossary_merge import (load_draft_xlsx, merge_round,
                                 read_glossary_pe_form,
                                 write_merge_outputs)
    current = json_mod.loads(Path(args.current).read_text("utf-8"))
    rows, problems = read_glossary_pe_form(Path(args.review))
    draft = load_draft_xlsx(Path(args.draft)) if args.draft else []
    glossary, delta = merge_round(
        current, rows, draft, round_id=args.round_id,
        game=args.game or "", locale=args.locale)
    md = write_merge_outputs(glossary, delta, Path(args.out))
    meta = glossary["metadata"]
    print(f"terms:    {meta['locked_terms']} locked + "
          f"{meta['mined_terms']} mined = {len(glossary['terms'])} total")
    for bucket in ("locked_from_review", "reconfirmed", "draft_agrees",
                   "draft_carried", "draft_drift", "draft_superseded"):
        print(f"{bucket + ':':22s}{len(delta[bucket])}")
    for problem in problems + delta["incomplete"]:
        zh = problem.get("Source") or problem.get("zh")
        print(f"⚠ INCOMPLETE {zh}: {problem['problem']}")
    print(f"delta:    {md}")
    print(f"glossary: {Path(args.out) / 'glossary_terms.json'} (T1 shape)")
    return 1 if (problems or delta["incomplete"]) else 0


def _cmd_po_compare(args) -> int:
    """Diff a newly received .po against the previous drop: per-key
    buckets, red flags (lost/stale translations), incremental word
    counts. Zero LLM calls."""
    from .po_compare import compare_po, write_compare_report
    result = compare_po(Path(args.old), Path(args.new))
    print(f"compare:  {Path(args.old).name} -> {Path(args.new).name}")
    print(f"buckets:  {json.dumps(result.counts())}")
    ws = result.work_summary()
    src, upd, out = (ws["source"], ws["target_updated"],
                     ws["target_outstanding"])
    print(f"source:   new {src['new']['entries']} "
          f"({src['new']['words']}w) | edited "
          f"{src['edited']['entries']} "
          f"(+{src['edited']['words_added']}/"
          f"-{src['edited']['words_removed']}w) | removed "
          f"{src['removed']['entries']} ({src['removed']['words']}w)")
    print(f"target:   updated in drop {upd['entries']} "
          f"(+{upd['words_added']}w) | outstanding: "
          f"new {out['new_strings']['entries']} "
          f"({out['new_strings']['source_words']}w), stale "
          f"{out['stale_translations']['entries']} "
          f"({out['stale_translations']['source_words_full']}w scope), "
          f"lost {out['lost_translations']['entries']}")
    for entry in result.translation_lost:
        print(f"⛔ LOST   {entry['key']}: {entry['source']!r}")
    for entry in result.source_changed:
        if entry.get("stale"):
            print(f"⚠ STALE  {entry['key']}: source changed, "
                  f"translation did not")
    if args.out:
        md = write_compare_report(result, Path(args.out))
        print(f"report:   {md}")
    return 2 if result.needs_attention else 0


def _cmd_analyze(args) -> int:
    """Corpus text analysis with story/instruction rollup."""
    from .analysis import analyze_corpus, labels_from_run_dbs
    from .ingest import ingest_any
    job = Job(Path(args.root), args.job_id)
    provider = (OpenAICompatProvider(args.provider, model=args.model,
                                     api_key=args.api_key)
                if args.classify else None)
    records = []
    for file in args.files:
        records.extend(ingest_any(
            Path(file),
            fallback=job._adapter_fallback(provider, provider is None)))
    labels = labels_from_run_dbs(job.store.job_dir / "runs")
    report = analyze_corpus(records, labels=labels, provider=provider)
    print(f"strings:      {report.total_strings} total, "
          f"{report.unique_strings} unique")
    print(f"words:        {report.words_all_records} total, "
          f"{report.words_unique} unique (quoting basis), "
          f"avg {report.avg_words_per_string}/string")
    print(f"chars:        {report.chars_unique} unique · "
          f"placeholders: {report.placeholders}")
    print(f"story lines:  {report.story_lines}   "
          f"instructions: {report.instructions}   "
          f"other: {report.other}"
          + (f"   (unlabeled: {report.unlabeled} — rerun with --classify)"
             if report.unlabeled else ""))
    print(f"by domain:    {json.dumps(report.by_domain)}")
    return 0


CHAT_HELP = """\
commands:
  /debug          toggle verbose mode (tool args + result preview live)
  /last [n]       replay the last n tool calls of this session (default 1)
  /trace          where the full JSONL trace of this session is written
  /help           this list
  exit            leave the session
Everything else is sent to the agent."""


def _format_call(record: dict, *, full: bool = False) -> str:
    args = json.dumps(record.get("args", {}), ensure_ascii=False,
                      default=str)
    result = str(record.get("result", ""))
    if not full:
        args = args if len(args) <= 400 else args[:400] + "…"
        result = result if len(result) <= 800 else result[:800] + "…"
    head = (f"  ⚙ {record['tool']}  ({record.get('seconds', '?')}s)"
            + ("  ⚠ FAILED" if record.get("error") else ""))
    lines = [head, f"    args:   {args}"]
    if record.get("error"):
        lines.append(f"    error:  {record['error']}")
    lines.append(f"    result: {result}")
    return "\n".join(lines)


class _LiveStatus:
    """Keeps a one-line 'still running' indicator on screen while a tool
    or the model is working, so a slow call is never mistaken for a hang.
    Falls back to plain prints when stdout is not a TTY."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self) -> None:
        self.thread = None
        self.stop_flag = None
        self.tty = sys.stdout.isatty()

    def start(self, label: str) -> None:
        self.stop("")
        if not self.tty:
            print(f"  ⚙ {label} …", flush=True)
            return
        import threading
        self.stop_flag = threading.Event()

        def spin() -> None:
            started = time.monotonic()
            i = 0
            while not self.stop_flag.wait(0.12):
                elapsed = time.monotonic() - started
                hint = "  (Ctrl-C to interrupt)" if elapsed > 20 else ""
                sys.stdout.write(
                    f"\r  {self.FRAMES[i % len(self.FRAMES)]} {label} "
                    f"{elapsed:5.1f}s{hint}\033[K")
                sys.stdout.flush()
                i += 1

        self.thread = threading.Thread(target=spin, daemon=True)
        self.thread.start()

    def stop(self, final: str = "") -> None:
        if self.stop_flag is not None:
            self.stop_flag.set()
            if self.thread is not None:
                self.thread.join(timeout=1)
            self.stop_flag, self.thread = None, None
            if self.tty:
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()
        if final:
            print(final, flush=True)


def _cmd_new(args) -> int:
    """Describe a project in prose; a model proposes the intake; you commit.

    The confirmation step is not a courtesy. The intake form is the job's
    constitution — `source_lang` and `target_locales` decide what every
    later stage does — so the model's role stops at *proposing*. Nothing is
    written until a human says yes, and validation runs before they are
    asked, so an obviously wrong locale never reaches the prompt.
    """
    from .intake_wizard import propose_intake, render, review, to_intake

    from .project_paths import discover_sources, scaffold_project

    # `orbit8 new` takes the PROJECT folder; the jobs root is `jobs/`
    # inside it. These are different things and conflating them breaks the
    # file boundary: the project root is derived as the PARENT of the jobs
    # root, so a job created directly under the project folder makes its
    # parent (the folder holding every client's project) the agent's
    # readable ground.
    root = Path(args.root)
    jobs_root = root / "jobs"
    if not root.exists():
        root.mkdir(parents=True)
        print(f"created {root}")
    # A project folder is routinely made before there is anything in it —
    # the client is signed, the folder exists, the strings arrive later.
    # Scaffolding here means `orbit8 new` works at that moment instead of
    # demanding a layout the operator has not built yet.
    created = scaffold_project(root)
    if created:
        print(f"created project structure: {', '.join(created)}")

    sources = list(args.source or ())
    if not sources:
        # Discovery, not guessing: look under the project's 10-received/
        # and SHOW what was found. A single unambiguous candidate is
        # offered as a default; several are a question, because silently
        # ingesting last month's drop builds a job that looks correct and
        # translates the wrong text.
        found = discover_sources(root)
        # Only worth saying when something WAS found but partly skipped.
        # On a fresh project "no .json or .po files under 10-received/" is
        # the expected state, not a problem, and printing it before the
        # friendlier explanation below reads like an error.
        if found.found:
            for note in found.notes:
                print(f"  {note}", file=sys.stderr)
            print(f"\nSource files under {found.project_root}:")
            for index, candidate in enumerate(found.candidates, 1):
                print(f"  {index}. {candidate.describe()}")
            only = found.unambiguous
            prompt = ("Use this source? [Y/n] " if only else
                      f"Which source? [1-{len(found.candidates)}] ")
            answer = input(f"\n{prompt}").strip().lower()
            if only and answer in ("", "y", "yes"):
                sources = [str(only.path)]
            elif answer.isdigit() and 1 <= int(answer) <= len(
                    found.candidates):
                sources = [str(found.candidates[int(answer) - 1].path)]
        if not sources:
            # No source is a VALID state, not a failure. The job sits at
            # INTAKE/G0 perfectly well; only S1 (INGEST) needs the
            # strings, which is exactly where a missing source should
            # stop things. Refusing here would block the common case of
            # setting a project up before the drop arrives.
            print(f"\nNo source file yet — the job will be created and wait "
                  f"at INTAKE.\nDrop the strings into "
                  f"{root.name}/10-received/ and re-run "
                  f"`orbit8 next` when they arrive.")
            if input("Continue without a source? [Y/n] ").strip().lower() \
                    not in ("", "y", "yes"):
                print("cancelled")
                return 1

    description = args.describe or input(
        "Describe the project (game, source language, target locales, "
        "genre):\n> ").strip()
    if not description:
        print("nothing to propose from", file=sys.stderr)
        return 2

    provider = OpenAICompatProvider(args.provider, model=args.model,
                                    api_key=args.api_key)
    print("proposing intake…")
    result = propose_intake(provider, description,
                            source_files=sources)

    while True:
        print(render(result, sources))
        if not result.ok:
            # Errors block creation rather than warn: these are the values
            # the whole pipeline inherits, and "confirm anyway" would make
            # the validation decorative.
            print("\nFix the errors above (edit with --describe, or pass "
                  "explicit flags to `orbit8 job init`).")
            return 1
        answer = input("\nCreate this job? [y/N/edit] ").strip().lower()
        if answer in ("y", "yes"):
            break
        if answer in ("e", "edit"):
            note = input("What should change? ").strip()
            if note:
                result = propose_intake(
                    provider, f"{description}\n\nCorrection: {note}",
                    source_files=sources)
                continue
        print("cancelled — nothing was created")
        return 1

    from .tenancy import mixed_tenant_warning
    warning = mixed_tenant_warning(jobs_root, args.tenant)
    if warning:
        print(warning, file=sys.stderr)
    job = Job.init(jobs_root, result.proposal.job_id,
                   intake=to_intake(result.proposal, tenant_id=args.tenant),
                   source_files=sources,
                   pilot_size=args.pilot_size)
    print(f"\njob initialized: {job.store.job_dir}")
    _print_stage(job.derive())
    print(f"\nNext:  uv run orbit8 next {jobs_root} {result.proposal.job_id} "
          f"--dry-run")
    return 0


def _cmd_job_list(args) -> int:
    """Answer "what am I running" without needing to know a job id first.

    Every other command takes the job id as a required argument, so an
    operator returning to a machine — or opening someone else's — had no
    way to discover what was there short of reading the directory. Phase
    and pending gate come from the artifact tree, the same authoritative
    derivation `status` uses.
    """
    root = Path(args.root)
    job_ids = _existing_jobs(root)
    if not job_ids:
        print(f"no jobs under {root}")
        print(f"\nStart one:\n  uv run orbit8 job init {root} <job-id> \\\n"
              f"      --game <name> --source <file> --source-lang zh \\\n"
              f"      --targets <locales> --genre <genre>")
        return 0

    print(f"{'job':<24}{'phase':<14}{'gate':<6}next")
    print("-" * 78)
    for job_id in job_ids:
        try:
            stage = Job(root, job_id).derive()
        except Exception as err:            # a damaged tree is still a job
            print(f"{job_id:<24}{'?':<14}{'?':<6}unreadable: {err}")
            continue
        gate = stage.gate or "-"
        detail = stage.action
        if stage.target:
            detail += f" [{stage.target}]"
        print(f"{job_id:<24}{stage.phase:<14}{gate:<6}{detail[:34]}")
    print(f"\n{len(job_ids)} job(s). Open one:  "
          f"uv run orbit8 chat {root} <job-id> --by <name>")
    return 0


def _existing_jobs(root: Path) -> list:
    """Job ids under `root`. A mistyped id is at least as likely as a
    missing one, so naming what IS there turns a dead end into a fix."""
    if not root.is_dir():
        return []
    return sorted(child.name for child in root.iterdir()
                  if (child / "job.json").exists())


def _cmd_chat(args) -> int:
    from datetime import datetime
    from .orchestrator import ChatOrchestrator
    job = Job(Path(args.root), args.job_id)
    # Refuse to open a session on a job that does not exist. Without this
    # the agent answers questions about a phantom job: `derive()` reports
    # INTAKE/"waiting on intake form" for a missing tree exactly as it does
    # for a real new one, the stage playbook loads, and the model states a
    # phase it never verified. Every tool call then fails identically until
    # the step budget runs out.
    if not job.store.job_json.exists():
        print(f"no job at {args.root}/{args.job_id}", file=sys.stderr)
        print(f"\nCreate it first:\n"
              f"  uv run orbit8 job init {args.root} {args.job_id} \\\n"
              f"      --game <name> --source <file> --source-lang zh \\\n"
              f"      --targets <locales> --genre <genre>\n",
              file=sys.stderr)
        existing = _existing_jobs(Path(args.root))
        if existing:
            print(f"Jobs under {args.root}: {', '.join(existing)}",
                  file=sys.stderr)
        return 2
    provider = OpenAICompatProvider(args.provider, model=args.model,
                                    api_key=args.api_key)
    factory = lambda locale: OpenAICompatProvider(
        args.provider, model=args.model, api_key=args.api_key)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    trace_path = (Path(args.trace) if args.trace
                  else job.store.job_dir / "chat-traces"
                  / f"session-{stamp}.jsonl")
    verbose = {"on": bool(args.debug)}
    live = _LiveStatus()

    def on_start(tool: str, tool_args: dict) -> None:
        label = tool
        if verbose["on"] and tool_args:
            detail = json.dumps(tool_args, ensure_ascii=False,
                                default=str)
            label += f"  {detail[:110]}"
        live.start(label)

    def on_action(tool: str, result: str) -> None:
        live.stop(f"  ⚙ {tool}" if not verbose["on"] else "")

    # PLAN §8: stage playbooks. Loaded non-strictly here — a malformed doc
    # must not stop an operator from working, though the test suite loads
    # strictly so the malformed doc still gets caught in CI.
    from .skill_docs import SkillLibrary, default_skills_dir
    skills = SkillLibrary.load(
        default_skills_dir(),
        known_tools=set(ChatOrchestrator.tool_names()), strict=False)
    # Episodic memory over THIS job's traces only (never across jobs — a
    # cross-job read is a cross-tenant read; see episodic.py).
    from .episodic import EpisodicMemory
    episodic = EpisodicMemory(job.store.job_dir / "chat-traces",
                              job_id=args.job_id)
    chat = ChatOrchestrator(
        job, provider, operator=args.by, provider_factory=factory,
        dry_run=args.dry_run, on_action=on_action, on_start=on_start,
        trace_path=trace_path, skills=skills, episodic=episodic)
    print(f"orbit8 chat — job {args.job_id}, operator {args.by}")
    stage = job.derive()
    active = skills.for_stage(stage.phase, stage.gate)
    if active:
        print(f"playbook: {active.name} ({stage.phase}"
              + (f"/{stage.gate}" if stage.gate else "") + ")")
    print(f"trace: {trace_path}   (/help for debug commands)")
    seen = 0
    while True:
        try:
            message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if message.lower() in ("exit", "quit"):
            return 0
        if not message:
            continue
        if message.lower() in ("/help", "?"):
            print(CHAT_HELP)
            continue
        if message.lower() == "/debug":
            verbose["on"] = not verbose["on"]
            print(f"verbose mode {'ON' if verbose['on'] else 'OFF'}")
            continue
        if message.lower() == "/trace":
            calls = [r for r in chat.trace if r["event"] == "tool"]
            print(f"{trace_path}\n{len(calls)} tool calls this session")
            continue
        if message.lower().startswith("/last"):
            parts = message.split()
            count = int(parts[1]) if len(parts) > 1 and \
                parts[1].isdigit() else 1
            calls = [r for r in chat.trace if r["event"] == "tool"]
            if not calls:
                print("(no tool calls yet)")
            for record in calls[-count:]:
                print(_format_call(record, full=True))
            continue
        try:
            reply = chat.turn(message)
        except KeyboardInterrupt:
            live.stop("")
            last = [r for r in chat.trace if r["event"] == "tool_start"]
            running = last[-1]["tool"] if last else "(thinking)"
            print(f"\n⏹ interrupted while running {running!r}. The job "
                  f"artifacts are unchanged beyond completed steps.\n"
                  f"   /last shows what finished; the full trace is at "
                  f"{trace_path}")
            seen = len(chat.trace)
            continue
        finally:
            live.stop("")
        if verbose["on"]:
            for record in chat.trace[seen:]:
                if record["event"] == "tool":
                    print(_format_call(record))
        seen = len(chat.trace)
        print(reply)


def _cmd_chat_trace(args) -> int:
    """Post-mortem: read a chat session trace. Without --session, lists
    this job's sessions newest first."""
    job = Job(Path(args.root), args.job_id)
    trace_dir = job.store.job_dir / "chat-traces"
    sessions = sorted(trace_dir.glob("session-*.jsonl"), reverse=True)
    if not sessions:
        print(f"no traces yet under {trace_dir}")
        return 1
    if not args.session:
        print(f"{len(sessions)} session(s) under {trace_dir}:")
        for path in sessions[:20]:
            records = [json.loads(line) for line
                       in path.read_text("utf-8").splitlines() if line]
            calls = [r for r in records if r["event"] == "tool"]
            failed = sum(1 for r in calls if r.get("error"))
            turns = max((r["turn"] for r in records), default=0)
            print(f"  {path.name}  {turns} turn(s), {len(calls)} calls"
                  + (f", {failed} FAILED" if failed else ""))
        print("\nreplay one with: orbit8 chat-trace <root> <job> "
              "--session <name> [--tool <name>] [--failed]")
        return 0
    path = trace_dir / args.session
    if not path.exists():
        path = Path(args.session)
    if not path.exists():
        print(f"error: no such trace: {args.session}")
        return 2
    records = [json.loads(line) for line
               in path.read_text("utf-8").splitlines() if line]
    for record in records:
        if record["event"] == "operator":
            if args.tool or args.failed:
                continue
            print(f"\n─── turn {record['turn']} ───\nyou> "
                  f"{record['message']}")
        elif record["event"] == "respond":
            if args.tool or args.failed:
                continue
            print(f"orbit8> {record['message']}")
        elif record["event"] == "tool":
            if args.tool and record["tool"] != args.tool:
                continue
            if args.failed and not record.get("error"):
                continue
            print(_format_call(record, full=args.full))
    return 0


def _cmd_status(args) -> int:
    job = Job(Path(args.root), args.job_id)
    control = job.control
    print(f"job:      {args.job_id} (tenant {control['tenant_id']})")
    approvals = control["approvals"]
    line = "  ".join(f"{g}:{'✓' if g in approvals else '·'}"
                     for g, _ in GATES)
    print(f"gates:    {line}")
    for gate, record in approvals.items():
        print(f"          {gate} by {record['by']} at {record['at']}")
    _print_stage(job.derive())
    return 0


def _cmd_observations(args) -> int:
    """Read the PLAN §3 observation log.

    This is the report Phase 1 exists to produce. It answers the two
    questions that decide whether Phase 4 (retrieval + promotion) should be
    built at all (PLAN §6.1):

    1. Do defect signatures RECUR across strings? `strings` counts distinct
       segments, not observations — a signature seen 9 times on one string
       is a repair loop flapping, not a defect class, and only the former
       can become a skill.
    2. Does our own scorer agree with the human at G3? `overturned` counts
       strings a reviewer edited or rejected. A signature that improves
       badness and gets overturned anyway is evidence a gate check is
       miscalibrated — promotion trained on badness would learn the wrong
       thing (PLAN §5.6).
    """
    from .observation import ObservationLog

    job = Job(Path(args.root), args.job_id)
    path = job.store.observations_path()
    log = ObservationLog(path)
    rows = log.all_rows()
    if not rows:
        print(f"no observations yet ({path})")
        print("Stage 4 writes these as the ratchet accepts or rolls back "
              "candidates.")
        return 0

    verdicts = log.counts()
    ruled = [r for r in rows if r["g3_verdict"] != "pending"]
    print(f"observations: {len(rows)}   strings: "
          f"{len({r['uid'] for r in rows})}   "
          f"attempts: {sorted({r['attempt'] for r in rows})}")
    print("ratchet:      " + "  ".join(
        f"{k}:{v}" for k, v in sorted(verdicts.items())))
    print(f"G3 ruled:     {len(ruled)}/{len(rows)}"
          + ("   (no human verdicts yet — approve G3 to populate)"
             if not ruled else ""))

    tally = log.signature_tally()
    recurring = [t for t in tally if t["distinct_strings"] > 1]
    print(f"\nsignatures:   {len(tally)} distinct, "
          f"{len(recurring)} seen on more than one string")
    if not recurring:
        # Stated plainly because it is the single most useful negative
        # result this log can produce: no recurrence means no skill to
        # learn, and Phase 4 should not be built (PLAN §6.1).
        print("  NOTE: nothing recurs across strings yet. Until it does, "
              "there is no\n        defect CLASS to learn — only a "
              "per-string cache (PLAN §4.2, §6.1).")

    limit = args.limit
    print(f"\n{'signature':<44}{'n':>5}{'strings':>9}{'acc':>6}{'rej':>6}"
          f"{'G3ok':>7}{'G3ovr':>7}")
    print("-" * 84)
    for row in tally[:limit]:
        print(f"{row['signature'][:43]:<44}{row['n']:>5}"
              f"{row['distinct_strings']:>9}{row['accepted'] or 0:>6}"
              f"{row['rejected'] or 0:>6}{row['g3_accepted'] or 0:>7}"
              f"{row['g3_overturned'] or 0:>7}")
    if len(tally) > limit:
        print(f"... {len(tally) - limit} more (--limit to widen)")

    disagreements = [t for t in tally
                     if (t["g3_overturned"] or 0) > (t["g3_accepted"] or 0)]
    if disagreements:
        print(f"\n{len(disagreements)} signature(s) overturned by G3 more "
              "often than upheld —\nthese are the miscalibrated checks "
              "(PLAN §5.6), not promotion candidates:")
        for row in disagreements[:10]:
            print(f"  {row['signature']}  "
                  f"upheld {row['g3_accepted'] or 0} / "
                  f"overturned {row['g3_overturned'] or 0}")
    return 0


def _cmd_calibrate(args) -> int:
    """Sweep promotion thresholds over an EXISTING observation log.

    The point of this command is that it costs nothing: the observations
    are fixed input, the policy is the variable, so exploring the
    threshold space is milliseconds of replay rather than repeated Stage 4
    runs. PLAN leaves every one of these constants deliberately unset —
    this is how they stop being guesses.

    The useful output is not the promote count but the BLOCKER histogram:
    a signature blocked only by `min_g3_reviewed` is waiting on reviewers,
    while one blocked by `min_g3_agreement` is telling you the fix is
    wrong. Those two need opposite responses.
    """
    from .observation import ObservationLog
    from .skills import PromotionPolicy, group_by_signature, replay

    job = Job(Path(args.root), args.job_id)
    rows = ObservationLog(job.store.observations_path()).all_rows()
    if not rows:
        print("no observations to calibrate against — run Stage 4 first")
        return 0
    grouped = group_by_signature(rows)
    if not grouped:
        print(f"{len(rows)} observations, but none carry a defect signature.")
        print("Nothing to calibrate: no candidate ever had a finding to fix.")
        return 0

    policy = PromotionPolicy(
        min_samples=args.min_samples,
        min_g3_reviewed=args.min_g3_reviewed,
        min_g3_agreement=args.min_g3_agreement,
        min_utility=args.min_utility)
    results = replay(grouped, policy)
    promoted = [r for r in results if r["would_promote"]]

    print(f"observations {len(rows)}   signatures {len(results)}")
    print(f"policy: min_samples={policy.min_samples} "
          f"min_g3_reviewed={policy.min_g3_reviewed} "
          f"min_g3_agreement={policy.min_g3_agreement} "
          f"min_utility={policy.min_utility}")
    print(f"would promote: {len(promoted)}/{len(results)}\n")

    print(f"{'signature':<38}{'strings':>8}{'util':>7}{'acc':>6}"
          f"{'G3n':>5}{'G3ok':>6}{'Δbad':>7}{'ctr':>5}  blockers")
    print("-" * 96)
    for row in results[:args.limit]:
        mark = "✓" if row["would_promote"] else " "
        print(f"{mark}{row['signature'][:37]:<37}"
              f"{row['distinct_strings']:>8}{row['utility']:>7.3f}"
              f"{row['accept_rate']:>6.2f}{row['g3_reviewed']:>5}"
              f"{row['g3_agreement']:>6.2f}"
              f"{row['mean_badness_delta']:>7.1f}"
              f"{row['counter_examples']:>5}  "
              f"{','.join(row['blockers'])}")
    if len(results) > args.limit:
        print(f"... {len(results) - args.limit} more (--limit to widen)")

    counts: dict = {}
    for row in results:
        for blocker in row["blockers"]:
            counts[blocker] = counts.get(blocker, 0) + 1
    if counts:
        print("\nbinding blockers (what to change, and what it means):")
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {name:<20}{n:>5}")
        dominant = max(counts, key=lambda k: counts[k])
        if dominant == "min_g3_reviewed":
            print("\n  min_g3_reviewed dominates: the log is waiting on "
                  "HUMAN review, not on a\n  threshold. Lowering it would "
                  "promote on unreviewed data (PLAN §5.6).")
        elif dominant == "min_g3_agreement":
            print("\n  min_g3_agreement dominates: reviewers are OVERRULING "
                  "these fixes. That is\n  a miscalibrated gate check, not "
                  "a threshold to lower (PLAN §5.6).")
        elif dominant == "min_samples":
            print("\n  min_samples dominates: not enough distinct strings "
                  "yet. More data, not\n  a lower floor — unless recurrence "
                  "never arrives, which is PLAN §6.1's\n  stop condition.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="orbit8",
        description="Localization agent — LangGraph stage executors under a "
                    "deterministic Job Controller")
    sub = parser.add_subparsers(dest="command", required=True)

    job = sub.add_parser("job", help="job-level commands")
    job_sub = job.add_subparsers(dest="job_command", required=True)
    new = sub.add_parser(
        "new",
        help="describe a project in words; a model proposes the intake "
             "form and you confirm it before anything is created")
    new.add_argument("root", nargs="?", default="jobs",
                     help="jobs root directory (default: ./jobs)")
    new.add_argument("--describe", help="skip the prompt and pass the "
                                        "description directly")
    new.add_argument("--source", nargs="+", help="source files (.json/.po)")
    new.add_argument("--tenant", default="default")
    new.add_argument("--pilot-size", type=int, default=30)
    new.add_argument("--provider", default="deepseek",
                     choices=sorted(PROVIDER_PRESETS))
    new.add_argument("--model")
    new.add_argument("--api-key")
    new.set_defaults(func=_cmd_new)

    job_list = job_sub.add_parser(
        "list", help="what jobs exist here, and where each one is")
    job_list.add_argument("root", nargs="?", default="jobs",
                          help="jobs root directory (default: ./jobs)")
    job_list.set_defaults(func=_cmd_job_list)

    init = job_sub.add_parser("init", help="create a job from an intake form")
    init.add_argument("root", help="jobs root directory")
    init.add_argument("job_id")
    init.add_argument("--game", required=True)
    init.add_argument("--source", required=True, nargs="+",
                      help="source files (.json / .po)")
    init.add_argument("--source-lang", default="zh")
    init.add_argument("--targets", required=True,
                      help="comma-separated locales")
    init.add_argument("--genre", help="comma-separated genres")
    init.add_argument("--engine", default="unknown")
    init.add_argument("--client-lang")
    init.add_argument("--platforms", help="comma-separated (steam,…)")
    init.add_argument("--references", help="comma-separated titles")
    init.add_argument("--tenant", default="default")
    init.add_argument("--pilot-size", type=int, default=30)
    init.add_argument("--tester-hours", type=float, default=8.0)
    init.set_defaults(func=_cmd_init)

    nxt = sub.add_parser("next",
                         help="run the next step, or print the pending gate")
    nxt.add_argument("root")
    nxt.add_argument("job_id")
    nxt.add_argument("--provider", default="deepseek",
                     choices=sorted(PROVIDER_PRESETS))
    nxt.add_argument("--model")
    nxt.add_argument("--api-key")
    nxt.add_argument("--dry-run", action="store_true",
                     help="echo stubs + deterministic fallbacks; zero LLM calls")
    nxt.set_defaults(func=_cmd_next)

    approve = sub.add_parser("approve", help="record a human gate approval")
    approve.add_argument("root")
    approve.add_argument("job_id")
    approve.add_argument("gate", choices=[g for g, _ in GATES])
    approve.add_argument("--by", required=True)
    approve.add_argument("--note")
    approve.set_defaults(func=_cmd_approve)

    analyze = sub.add_parser(
        "analyze", help="corpus text analysis (strings/words/story vs "
                        "instructions)")
    analyze.add_argument("root")
    analyze.add_argument("job_id")
    analyze.add_argument("--files", required=True, nargs="+")
    analyze.add_argument("--classify", action="store_true",
                         help="LLM-classify strings not labeled by any "
                              "earlier run")
    analyze.add_argument("--provider", default="deepseek",
                         choices=sorted(PROVIDER_PRESETS))
    analyze.add_argument("--model")
    analyze.add_argument("--api-key")
    analyze.set_defaults(func=_cmd_analyze)

    lqa = sub.add_parser("lqa", help="LQA commands")
    lsub = lqa.add_subparsers(dest="lqa_command", required=True)
    lrun = lsub.add_parser(
        "run", help="audit external translations (story/string split, "
                    "per-class batch sizes)")
    lrun.add_argument("root")
    lrun.add_argument("job_id")
    lrun.add_argument("--pairs", required=True,
                      help="bilingual JSONL (key/source_text/target_text)")
    lrun.add_argument("--name", default="dev-audit")
    lrun.add_argument("--batch-story", type=int, default=5)
    lrun.add_argument("--batch-string", type=int, default=20)
    lrun.add_argument("--t3-threshold", type=float, default=0.75,
                      help="min verifier confidence for a confirmed T3 "
                           "finding to surface (default 0.75; the audit "
                           "trail records everything either way)")
    lrun.add_argument("--deterministic-only", action="store_true",
                      help="T1+T2 only; zero LLM calls (skips content "
                           "classification)")
    lrun.add_argument("--provider", default="deepseek",
                      choices=sorted(PROVIDER_PRESETS))
    lrun.add_argument("--model")
    lrun.add_argument("--api-key")
    lrun.set_defaults(func=_cmd_lqa_run)

    lreport = lsub.add_parser(
        "report", help="client bug report xlsx + tech summary from a "
                       "stored LQA report")
    lreport.add_argument("root")
    lreport.add_argument("job_id")
    lreport.add_argument("--name", default="dev-audit")
    lreport.add_argument("--attempt", type=int,
                         help="s5 attempt (default: latest)")
    lreport.add_argument("--out", help="output dir (default: the s5 "
                                       "attempt dir)")
    lreport.add_argument("--no-suggestions", action="store_true",
                         help="skip Repair-agent suggested translations "
                              "(zero LLM calls)")
    lreport.add_argument("--locations-from",
                         help=".po (UE #: comments) or pairs .jsonl with a "
                              "location field — String IDs then show the "
                              "real widget/asset path per game key")
    lreport.add_argument("--tag",
                         help="suffix for the output filenames, e.g. "
                              "--tag v2 → …_Bug_Report_<locale>.v2.xlsx")
    lreport.add_argument("--provider", default="deepseek",
                         choices=sorted(PROVIDER_PRESETS))
    lreport.add_argument("--model")
    lreport.add_argument("--api-key")
    lreport.set_defaults(func=_cmd_lqa_report)

    ldeliver = lsub.add_parser(
        "deliver", help="apply post-editing decisions (Decision / Modify "
                        "Version) to .po files → timestamped delivery")
    ldeliver.add_argument("root")
    ldeliver.add_argument("job_id")
    ldeliver.add_argument("--review", required=True,
                          help="reviewed bug-report xlsx with Decision + "
                               "Modify Version columns")
    ldeliver.add_argument("--po", required=True, nargs="+",
                          help="the shipped .po file(s) to patch")
    ldeliver.add_argument("--out",
                          help="deliverables dir (default: <root>/../"
                               "30-deliverables)")
    ldeliver.add_argument("--timestamp",
                          help="YYYYMMDD folder stamp (default: today)")
    ldeliver.add_argument("--locale",
                          help="TM locale (default: first target locale)")
    ldeliver.add_argument("--no-tm", action="store_true",
                          help="skip origin=human TM write-back")
    ldeliver.add_argument("--no-sanity-check", action="store_true",
                          help="skip the po_sanity pre-delivery gate "
                               "(format/label/summary checks)")
    ldeliver.add_argument("--no-relabel", action="store_true",
                          help="do not refresh PO-Revision-Date / "
                               "Language-Team header labels")
    ldeliver.add_argument("--team", default="Orbit8 Lab",
                          help="localization-source label stamped into and "
                               "expected in the header")
    ldeliver.set_defaults(func=_cmd_lqa_deliver)

    po = sub.add_parser("po", help="PO file utilities")
    posub = po.add_subparsers(dest="po_command", required=True)
    pcompare = posub.add_parser(
        "compare", help="diff a new .po drop against the previous one "
                        "(added/removed/changed, lost & stale flags)")
    pcompare.add_argument("old", help="previous .po")
    pcompare.add_argument("new", help="newly received .po")
    pcompare.add_argument("--out", help="write <new>_compare.md/.json "
                                        "report files to this dir")
    pcompare.set_defaults(func=_cmd_po_compare)

    ptrans = posub.add_parser(
        "translate", help="translate a received .po's untranslated "
                          "strings (glossary-constrained, batched LLM) "
                          "→ patched copy + MTPE form")
    ptrans.add_argument("--po", required=True,
                        help="received bilingual .po (msgid=zh)")
    ptrans.add_argument("--glossary",
                        help="glossary_terms.json (T1 shape, family "
                             "rules honored). Default: the project's "
                             "40-reference/glossary/glossary_terms.json")
    ptrans.add_argument("--out", required=True,
                        help="output work folder")
    ptrans.add_argument("--provider", default="deepseek",
                        choices=sorted(PROVIDER_PRESETS))
    ptrans.add_argument("--model")
    ptrans.add_argument("--api-key")
    ptrans.add_argument("--game", help="game name for the prompt")
    ptrans.add_argument("--locale", default="en")
    ptrans.add_argument("--batch-size", type=int, default=12)
    ptrans.add_argument("--reuse-from",
                        help="previous translated .po — matching "
                             "untranslated entries (by key, then by "
                             "identical source) are carried over "
                             "instead of re-translated")
    ptrans.set_defaults(func=_cmd_po_translate)

    pscan = posub.add_parser(
        "scan", help="standalone LQA scan of a bilingual .po (full tier "
                     "cascade) → bug report xlsx + LQA PE form")
    pscan.add_argument("--po", required=True,
                       help="translated bilingual .po to audit")
    pscan.add_argument("--glossary",
                       help="glossary_terms.json (default: the project's "
                            "40-reference/glossary/glossary_terms.json)")
    pscan.add_argument("--out", required=True, help="output work folder")
    pscan.add_argument("--game", help="game name for prompts/report")
    pscan.add_argument("--locale", default="en")
    pscan.add_argument("--source-lang", default="zh-CN")
    pscan.add_argument("--provider", default="deepseek",
                       choices=sorted(PROVIDER_PRESETS))
    pscan.add_argument("--model")
    pscan.add_argument("--api-key")
    pscan.add_argument("--deterministic-only", action="store_true",
                       help="T1+T2 only — zero LLM calls")
    pscan.add_argument("--no-glossary", action="store_true",
                       help="run without a glossary (mechanical checks "
                            "only) instead of failing when none is found")
    pscan.add_argument("--timeout", type=float, default=120.0,
                       help="seconds per LLM request before giving up "
                            "on it (default 120)")
    pscan.add_argument("--retries", type=int, default=3,
                       help="attempts per request on timeout/5xx/429 "
                            "(default 3, exponential backoff)")
    pscan.add_argument("--no-suggestions", action="store_true",
                       help="skip generating suggested fixes")
    pscan.set_defaults(func=_cmd_po_scan)

    glossary = sub.add_parser("glossary", help="glossary subsystem")
    gsub = glossary.add_subparsers(dest="glossary_command", required=True)
    imp = gsub.add_parser(
        "import", help="human-locked review files → staged T1 + RAG json/csv")
    imp.add_argument("root")
    imp.add_argument("job_id")
    imp.add_argument("--files", required=True, nargs="+",
                     help="locked sheet and/or review docs (docx/xlsx/csv…)")
    imp.add_argument("--locale", help="default: first target locale")
    imp.add_argument("--out", help="dir for RAG json + csv "
                                   "(default: the job's s3/)")
    imp.add_argument("--provider", default="deepseek",
                     choices=sorted(PROVIDER_PRESETS))
    imp.add_argument("--model")
    imp.add_argument("--api-key")
    imp.set_defaults(func=_cmd_glossary_import)

    gupd = gsub.add_parser(
        "update", help="refresh asset-pair glossary (zh/en_asset.json) "
                       "from PE results: decisions xlsx + PE .po + "
                       "--term pairs")
    gupd.add_argument("--assets", required=True,
                      help="dir with zh_asset.json / en_asset.json / "
                           "dedup_index.json")
    gupd.add_argument("--pe-po", required=True,
                      help="post-edited bilingual .po (msgid=zh, "
                           "msgstr=final EN)")
    gupd.add_argument("--decisions",
                      help="decisions workbook (映射留档/翻译裁定 sheets)")
    gupd.add_argument("--term", action="append", metavar="zh=EN",
                      help="extra term ruling not in the workbook "
                           "(repeatable)")
    gupd.add_argument("--out", required=True,
                      help="output dir for updated en_asset.json + "
                           "termbase delta + audit")
    gupd.set_defaults(func=_cmd_glossary_update)

    gdst = gsub.add_parser(
        "distill", help="compact term-level glossary from PE-refreshed "
                        "assets + decisions (pipeline T1 shape)")
    gdst.add_argument("--assets", required=True,
                      help="dir with zh_asset.json / en_asset.json / "
                           "dedup_index.json")
    gdst.add_argument("--pe-po", required=True,
                      help="post-edited bilingual .po")
    gdst.add_argument("--decisions", help="decisions workbook")
    gdst.add_argument("--term", action="append", metavar="zh=EN",
                      help="extra term ruling (repeatable)")
    gdst.add_argument("--game", help="metadata: game name")
    gdst.add_argument("--locale", default="en")
    gdst.add_argument("--out", required=True,
                      help="output dir for glossary_terms.xlsx/.json")
    gdst.set_defaults(func=_cmd_glossary_distill)

    gext = gsub.add_parser(
        "extract", help="corpus-first term extraction (stages 0-3): "
                        "mine terms from FULL bilingual .po corpus incl. "
                        "sentence interiors; term-level review output")
    gext.add_argument("--po", required=True, nargs="+",
                      help="bilingual .po file(s) (msgid=zh, msgstr=EN)")
    gext.add_argument("--decisions", help="decisions workbook "
                                          "(映射留档/翻译裁定 sheets)")
    gext.add_argument("--term", action="append", metavar="zh=EN",
                      help="extra locked term ruling (repeatable)")
    gext.add_argument("--provider", default="none",
                      choices=["none"] + sorted(PROVIDER_PRESETS),
                      help="stage-2 noise filter LLM (default: none = "
                           "deterministic heuristic)")
    gext.add_argument("--model")
    gext.add_argument("--api-key")
    gext.add_argument("--min-freq", type=int, default=3,
                      help="stage-1 minimum corpus frequency (default 3)")
    gext.add_argument("--game", help="metadata: game name")
    gext.add_argument("--locale", default="en")
    gext.add_argument("--out", required=True,
                      help="output dir for glossary_terms.{json,xlsx} + "
                           "extract_review.xlsx + audit")
    gext.set_defaults(func=_cmd_glossary_extract)

    gmrg = gsub.add_parser(
        "merge", help="round merge: filled Glossary PE review + previous "
                      "draft glossary → new versioned T1 glossary")
    gmrg.add_argument("--current", required=True,
                      help="current glossary_terms.json (T1 shape)")
    gmrg.add_argument("--review", required=True,
                      help="filled Glossary PE review xlsx")
    gmrg.add_argument("--draft",
                      help="previous draft glossary xlsx "
                           "(分类/中文/英文/状态)")
    gmrg.add_argument("--round-id", required=True,
                      help="round label stamped as provenance "
                           "(e.g. R2-20260802)")
    gmrg.add_argument("--game", help="metadata: game name")
    gmrg.add_argument("--locale", default="en")
    gmrg.add_argument("--out", required=True,
                      help="output dir for merged glossary + delta")
    gmrg.set_defaults(func=_cmd_glossary_merge)

    gadd = gsub.add_parser(
        "add", help="add/replace terms directly in a T1 glossary "
                    "(operator rulings — locked, provenance stamped)")
    gadd.add_argument("--glossary", required=True,
                      help="glossary_terms.json to edit in place "
                           "(a timestamped .bak is kept)")
    gadd.add_argument("--term", action="append", required=True,
                      metavar="zh=EN",
                      help="zh=EN · aliases zh1/zh2=EN · rename "
                           "old>new=EN (repeatable)")
    gadd.add_argument("--origin", help="provenance stamp "
                                       "(default: operator <date>)")
    gadd.add_argument("--force", action="store_true",
                      help="supersede an existing LOCKED ruling")
    gadd.set_defaults(func=_cmd_glossary_add)

    gvar = gsub.add_parser(
        "variants", help="record accepted alternate renderings for a "
                         "term (a decision, not a fuzzy match)")
    gvar.add_argument("--term", action="append", required=True,
                      metavar="zh=Variant[|Variant2]",
                      help="repeatable; plurals/inflections do NOT go "
                           "here — those come from the style guide's "
                           "morphology profile")
    gvar.add_argument("--glossary",
                      help="default: the project's active termbase")
    gvar.add_argument("--origin")
    gvar.set_defaults(func=_cmd_glossary_variants)

    gpro = gsub.add_parser(
        "promote", help="publish a run's glossary as the project's "
                        "active termbase (40-reference/glossary/)")
    gpro.add_argument("source",
                      help="glossary_terms.json (or its run folder)")
    gpro.add_argument("--project",
                      help="project root (default: detected upward "
                           "from source)")
    gpro.add_argument("--force", action="store_true",
                      help="publish even when the integrity check finds "
                           "contradictions")
    gpro.set_defaults(func=_cmd_glossary_promote)

    guni = gsub.add_parser(
        "unify", help="declare that several SOURCE spellings of one "
                      "concept all render as the same target term")
    guni.add_argument("canonical",
                      help="the source spelling we treat as canonical")
    guni.add_argument("--variant", action="append", required=True,
                      help="other source spelling of the same concept "
                           "(repeatable)")
    guni.add_argument("--translation",
                      help="the agreed target term (default: the "
                           "canonical entry's current rendering)")
    guni.add_argument("--retire", action="store_true",
                      help="delete the variant entries instead of "
                           "keeping them mapped (only when the source "
                           "spelling is truly obsolete)")
    guni.add_argument("--glossary",
                      help="default: the project's active termbase")
    guni.add_argument("--origin")
    guni.set_defaults(func=_cmd_glossary_unify)

    gchk = gsub.add_parser(
        "check", help="integrity audit of a glossary (duplicates, "
                      "variant collisions, broken rename chains) — the "
                      "gate before promotion")
    gchk.add_argument("--glossary",
                      help="default: the project's active termbase")
    gchk.add_argument("--source-lang", help="with --target-lang, load "
                                            "the morphology profile")
    gchk.add_argument("--target-lang")
    gchk.add_argument("--out", help="write glossary_check.{json,md} here")
    gchk.add_argument("--verbose", action="store_true",
                      help="include INFO findings")
    gchk.set_defaults(func=_cmd_glossary_check)

    style = sub.add_parser("style", help="per-language-pair style guides")
    ssub = style.add_subparsers(dest="style_command", required=True)
    sinit = ssub.add_parser(
        "init", help="write a starter style guide + doc into the "
                     "project's 40-reference/style/")
    sinit.add_argument("source_lang")
    sinit.add_argument("target_lang")
    sinit.add_argument("--project", help="project root (default: cwd)")
    sinit.add_argument("--force", action="store_true")
    sinit.set_defaults(func=_cmd_style_init)
    schk = ssub.add_parser(
        "check", help="validate a guide against the format standard "
                      "and preview its prompt section")
    schk.add_argument("source_lang")
    schk.add_argument("target_lang")
    schk.add_argument("--guide", help="explicit json (default: the "
                                      "project's guide for the pair)")
    schk.add_argument("--prompt", metavar="DOMAIN",
                      help="print the prompt section for this string "
                           "type ('all' for the global slice)")
    schk.set_defaults(func=_cmd_style_check)

    sshow = ssub.add_parser(
        "show", help="list the rules that apply to a pair/string type")
    sshow.add_argument("source_lang")
    sshow.add_argument("target_lang")
    sshow.add_argument("--domain", help="ui / dialogue / system / …")
    sshow.set_defaults(func=_cmd_style_show)

    classify = sub.add_parser(
        "classify", help="label strings by type (ui/dialogue/system/…) "
                         "and persist them as a project asset")
    classify.add_argument("--po", required=True)
    classify.add_argument("--out", required=True,
                          help="labels json (e.g. "
                               "40-reference/labels.json)")
    classify.add_argument("--llm", action="store_true",
                          help="classify strings no structural rule "
                               "could decide")
    classify.add_argument("--provider", default="deepseek",
                          choices=sorted(PROVIDER_PRESETS))
    classify.add_argument("--model")
    classify.add_argument("--api-key")
    classify.add_argument("--correct", action="append", metavar="key=domain",
                          help="operator correction (repeatable) — human "
                               "labels survive every re-run")
    classify.add_argument("--by", default="operator")
    classify.set_defaults(func=_cmd_classify)

    chat = sub.add_parser(
        "chat", help="natural-language operator interface over the controller")
    chat.add_argument("root")
    chat.add_argument("job_id")
    chat.add_argument("--by", required=True,
                      help="operator name recorded on gate approvals")
    chat.add_argument("--provider", default="deepseek",
                      choices=sorted(PROVIDER_PRESETS))
    chat.add_argument("--model")
    chat.add_argument("--api-key")
    chat.add_argument("--dry-run", action="store_true",
                      help="stage steps run with echo stubs (chat itself "
                           "still needs a real provider)")
    chat.add_argument("--debug", action="store_true",
                      help="start in verbose mode: print every tool's "
                           "arguments and result (toggle with /debug)")
    chat.add_argument("--trace",
                      help="JSONL trace path (default: "
                           "<job>/chat-traces/session-<stamp>.jsonl)")
    chat.set_defaults(func=_cmd_chat)

    ctrace = sub.add_parser(
        "chat-trace", help="inspect past chat sessions (what the agent "
                           "called, with which arguments, and got back)")
    ctrace.add_argument("root")
    ctrace.add_argument("job_id")
    ctrace.add_argument("--session", help="session-<stamp>.jsonl "
                                          "(default: list sessions)")
    ctrace.add_argument("--tool", help="show only this tool's calls")
    ctrace.add_argument("--failed", action="store_true",
                        help="show only calls that errored")
    ctrace.add_argument("--full", action="store_true",
                        help="do not truncate args/results")
    ctrace.set_defaults(func=_cmd_chat_trace)

    status = sub.add_parser("status", help="phase + gate states")
    status.add_argument("root")
    status.add_argument("job_id")
    status.set_defaults(func=_cmd_status)

    obs = sub.add_parser(
        "observations",
        help="repair-attempt log: do defect signatures recur, and does the "
             "gate agree with G3? (PLAN §3)")
    obs.add_argument("root")
    obs.add_argument("job_id")
    obs.add_argument("--limit", type=int, default=25,
                     help="signature rows to print (default 25)")
    obs.set_defaults(func=_cmd_observations)

    cal = sub.add_parser(
        "calibrate",
        help="sweep promotion thresholds over an existing observation log "
             "— no API spend (PLAN §6.2)")
    cal.add_argument("root")
    cal.add_argument("job_id")
    cal.add_argument("--min-samples", type=int, default=20,
                     dest="min_samples",
                     help="distinct strings before a signature is a "
                          "candidate (default 20, uncalibrated)")
    cal.add_argument("--min-g3-reviewed", type=int, default=10,
                     dest="min_g3_reviewed",
                     help="human-reviewed cases required (default 10)")
    cal.add_argument("--min-g3-agreement", type=float, default=0.9,
                     dest="min_g3_agreement",
                     help="fraction upheld at G3 (default 0.9)")
    cal.add_argument("--min-utility", type=float, default=0.5,
                     dest="min_utility",
                     help="utility floor for retrieval (default 0.5)")
    cal.add_argument("--limit", type=int, default=25)
    cal.set_defaults(func=_cmd_calibrate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
