"""Adapter-Writer: agent-generated ingest adapters for unknown formats.

When a source file arrives in a format `ingest.ADAPTERS` doesn't cover, the
Adapter-Writer agent sees a SAMPLE of the raw bytes plus the output
contract, writes a short stdlib-only Python script, and the Controller runs
it through the sandbox with a validate-retry loop (≤ MAX_ATTEMPTS, each
retry fed the previous error).

The generated script is stored as an s1 artifact with a model fingerprint —
a human can inspect exactly what code produced the ingest, and INCREMENTAL
deltas re-run the SAME adapter deterministically instead of regenerating.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .llm import Provider, model_fingerprint
from .sandbox import run_sandboxed
from .schemas import SourceString, Strict

MAX_ATTEMPTS = 3
SAMPLE_BYTES = 4096


class AdapterRecord(Strict):
    """The auditable artifact: which code ingested this format."""
    suffix: str
    script: str
    attempts: int
    sample_sha256: str
    record_count: int


ADAPTER_SYSTEM = (
    "You write small Python ingest adapters for a game-localization "
    "pipeline. Rules:\n"
    "- Python 3 STDLIB ONLY (no pip installs; the sandbox has no network "
    "and no site-packages).\n"
    "- The script receives the input file path as sys.argv[1].\n"
    "- It must print to STDOUT one JSON array of objects, each exactly "
    '{"key": "<unique string id>", "text": "<source text>"} — nothing '
    "else on stdout.\n"
    "- Keys must be unique and stable (use the file's own ids/columns "
    "when present; otherwise derive row-based keys like ROW_0001).\n"
    "- Skip empty/whitespace-only texts. Preserve the text verbatim — "
    "never strip placeholders or markup.\n"
    "- Handle the file with encoding='utf-8', errors='replace'.\n"
    "Output ONLY the Python code. No prose, no markdown fences."
)


def _strip_fences(code: str) -> str:
    return re.sub(r"^```(?:python)?\s*|\s*```$", "", code.strip(), flags=re.M)


def _sample_of(path: Path, raw: bytes) -> str:
    """Text files: raw head. Zip containers (docx/xlsx): member listing plus
    heads of the content-bearing XML parts, so the agent sees the actual
    structure instead of compressed bytes."""
    if not raw.startswith(b"PK\x03\x04"):
        return raw[:SAMPLE_BYTES].decode("utf-8", "replace")
    import io
    import zipfile
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return raw[:SAMPLE_BYTES].decode("utf-8", "replace")
    names = archive.namelist()
    parts = [f"(zip archive; members: {', '.join(names[:30])})"]
    interesting = [n for n in names
                   if n in ("word/document.xml", "xl/sharedStrings.xml",
                            "xl/workbook.xml")
                   or re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
    budget = SAMPLE_BYTES
    for name in interesting[:4]:
        head = archive.read(name)[:budget // max(1, len(interesting[:4]))]
        parts.append(f"--- {name} (head) ---\n"
                     + head.decode("utf-8", "replace"))
    return "\n".join(parts)[:SAMPLE_BYTES * 2]


def _write_converter(provider: Provider, system_prompt: str, suffix: str,
                     sample: str, previous_script: Optional[str],
                     previous_error: Optional[str],
                     context: str = "") -> Tuple[str, str]:
    sections = []
    if context:
        # What the adapter was previously denied. Told only "find the
        # source and target columns in this file", it answered correctly
        # for a single-language export — one text column into `source`,
        # `target` empty — and the result was useless. The file NAMES and
        # the job's languages are what make the layout legible.
        sections.append(context)
    sections.append(f"Input format: a {suffix!r} file. First "
                    f"{SAMPLE_BYTES} bytes of a real example:"
                    f"\n---\n{sample}\n---")
    if previous_script:
        sections.append("Your previous adapter:\n" + previous_script)
        sections.append("It FAILED with:\n" + (previous_error or "unknown")
                        + "\nFix the problem and output the corrected "
                          "script. Output ONLY the Python code.")
    else:
        sections.append("Write the adapter now. Output ONLY the Python code.")
    raw = provider.complete(system_prompt, "\n\n".join(sections),
                            temperature=0.0, max_tokens=2500)
    return _strip_fences(raw), model_fingerprint(provider, system_prompt)


def validate_stdout(stdout: str, file_ref: str) -> List[SourceString]:
    """Wall 2: only validated data crosses out of the sandbox."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as err:
        raise ValueError(f"stdout is not valid JSON: {err}") from err
    if not isinstance(data, list) or not data:
        raise ValueError("expected a non-empty JSON array of "
                         '{"key", "text"} objects')
    records, seen = [], set()
    for i, item in enumerate(data):
        if (not isinstance(item, dict)
                or not isinstance(item.get("key"), str)
                or not isinstance(item.get("text"), str)):
            raise ValueError(f'item {i} is not {{"key": str, "text": str}}: '
                             f"{str(item)[:120]}")
        if item["key"] in seen:
            raise ValueError(f"duplicate key {item['key']!r} at item {i}")
        seen.add(item["key"])
        if item["text"].strip():
            records.append(SourceString(key=item["key"], text=item["text"],
                                        file_ref=file_ref))
    if not records:
        raise ValueError("adapter produced zero non-empty texts")
    return records


def run_converter(script: str, path, validate) -> list:
    """Deterministic re-run of a stored converter script.

    ``path`` is one file or a sequence — a stored adapter must be re-run
    against the same shape of input it was generated for.
    """
    paths = ([Path(path)] if isinstance(path, (str, Path))
             else [Path(p) for p in path])
    result = run_sandboxed(script, paths)
    if not result.ok:
        raise ValueError(f"adapter failed (rc={result.returncode}"
                         f"{', timeout' if result.timed_out else ''}): "
                         f"{result.stderr[:500]}")
    return validate(result.stdout, str(paths[0]))


def generate_converter(provider: Provider, path, *,
                       system_prompt: str, validate,
                       context: str = "",
                       max_attempts: int = MAX_ATTEMPTS
                       ) -> Tuple[AdapterRecord, list, str]:
    """The generic generate → sandbox → validate → retry loop.

    ``validate(stdout, file_ref) -> list`` is the caller's Wall-2 contract;
    different converters (source-string ingest, translation pairs, …)
    supply their own system prompt and validator.

    ``path`` is one file or a sequence of them. Several exist because the
    converter often has to see more than one to do its job — a per-locale
    export keeps the source text and the translation in separate files,
    and an adapter shown one at a time cannot pair them no matter how
    capable the model is. ``context`` carries what the filenames alone do
    not say: which language is the source, which target is wanted.
    """
    paths = ([Path(path)] if isinstance(path, (str, Path))
             else [Path(p) for p in path])
    primary = paths[0]
    raw = primary.read_bytes()
    if len(paths) == 1:
        sample = _sample_of(primary, raw)
    else:
        # Every file gets a sample, labelled with its real name and its
        # argv position — the name is usually what identifies the locale
        # ("Game_ja.xlsx"), and the position is how the script reaches it.
        sample = "\n\n".join(
            f"=== argv[{index + 1}] — {each.name} ===\n"
            + _sample_of(each, each.read_bytes())
            for index, each in enumerate(paths))
    script: Optional[str] = None
    error: Optional[str] = None
    fingerprint = ""
    for attempt in range(1, max_attempts + 1):
        script, fingerprint = _write_converter(
            provider, system_prompt, primary.suffix, sample, script, error,
            context=context)
        result = run_sandboxed(script, paths)
        if not result.ok:
            error = (f"exit code {result.returncode}"
                     f"{' (timeout)' if result.timed_out else ''}; "
                     f"stderr: {result.stderr[:800]}")
            continue
        try:
            records = validate(result.stdout, str(primary))
        except ValueError as err:
            error = f"output validation failed: {err}"
            continue
        record = AdapterRecord(
            suffix=primary.suffix, script=script, attempts=attempt,
            sample_sha256=hashlib.sha256(raw).hexdigest(),
            record_count=len(records))
        return record, records, fingerprint
    raise RuntimeError(
        f"adapter generation failed after {max_attempts} attempts for "
        f"{', '.join(p.name for p in paths)}; last error: {error}")


def run_adapter(script: str, path, validate=None):
    """Re-run a STORED adapter (INCREMENTAL path).

    `validate` selects the contract: the default ingest one ({key, text}),
    or `validate_pairs` for a bilingual adapter ({key, source, target}).
    A stored script is re-run against the same wall it was generated
    behind — reuse must not become a way around validation.
    """
    return run_converter(script, path, validate or validate_stdout)


def generate_adapter(provider: Provider, path: Path
                     ) -> Tuple[AdapterRecord, List[SourceString], str]:
    """Ingest wrapper over the generic loop."""
    return generate_converter(provider, path, system_prompt=ADAPTER_SYSTEM,
                              validate=validate_stdout)


# ---------------------------------------------- bilingual (LQA) adapters

BILINGUAL_SYSTEM = (
    "You write small Python adapters that extract TRANSLATION PAIRS from "
    "a game-localization file, for quality review. Rules:\n"
    "- Python 3 STDLIB ONLY (no pip installs; the sandbox has no network "
    "and no site-packages). For .xlsx, unzip it and parse the XML with "
    "zipfile + xml.etree — openpyxl is NOT available.\n"
    "- The script receives ONE OR MORE input file paths as sys.argv[1:], "
    "in the order shown to you. Read ALL of them.\n"
    "- TWO LAYOUTS EXIST, and you must tell them apart from the samples:\n"
    "  (a) ONE file holding both languages in different COLUMNS — take "
    "the source column and the target column from the same row.\n"
    "  (b) SEPARATE files per language (a source export plus a "
    "per-locale export, e.g. 'Game (Source).xlsx' + 'Game_ja.xlsx') — "
    "each file holds ONE language, so read the source text from the "
    "source file, the translation from the target file, and JOIN them on "
    "the shared key column. Emitting a single file's text as `source` "
    "with an empty `target` is WRONG in this layout: it produces rows "
    "that look valid and contain nothing to review.\n"
    "- If a key exists in the source but not the target, emit it with an "
    "empty target — an untranslated string is a finding, not a row to "
    "drop.\n"
    "- It must print to STDOUT one JSON array of objects, each exactly "
    '{"key": "<id>", "source": "<source-language text>", '
    '"target": "<translated text>"} — nothing else on stdout.\n'
    "- Identify the SOURCE column and the TARGET column from the header. "
    "The source is the language the game was written in; the target is "
    "the translation being reviewed. If several translation columns "
    "exist, choose the one named for the requested target language.\n"
    "- Keep rows whose target is EMPTY (use an empty string): an "
    "untranslated string is exactly what a review must flag, so dropping "
    "it hides a defect.\n"
    "- Skip rows with no source text. Preserve text verbatim — never "
    "strip placeholders or markup.\n"
    "- Keys must be unique and stable (use the file's own ids when "
    "present; otherwise ROW_0001 style).\n"
    "- Handle the file with encoding='utf-8', errors='replace'.\n"
    "Output ONLY the Python code. No prose, no markdown fences."
)


def validate_pairs(stdout: str, file_ref: str) -> List[tuple]:
    """Wall 2 for bilingual adapters: only validated pairs cross out.

    Mirrors `validate_stdout`, with one deliberate difference — an EMPTY
    target is kept rather than dropped. For ingest an empty string is
    noise; for LQA it is the finding.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as err:
        raise ValueError(f"stdout is not valid JSON: {err}") from err
    if not isinstance(data, list) or not data:
        raise ValueError('expected a non-empty JSON array of '
                         '{"key", "source", "target"} objects')
    pairs, seen = [], set()
    for i, item in enumerate(data):
        if (not isinstance(item, dict)
                or not isinstance(item.get("key"), str)
                or not isinstance(item.get("source"), str)
                or not isinstance(item.get("target"), str)):
            raise ValueError(
                f'item {i} is not {{"key": str, "source": str, '
                f'"target": str}}: {str(item)[:120]}')
        if item["key"] in seen:
            raise ValueError(f"duplicate key {item['key']!r} at item {i}")
        seen.add(item["key"])
        if item["source"].strip():
            pairs.append((item["key"], item["source"], item["target"],
                          file_ref))
    if not pairs:
        raise ValueError("no rows with source text")
    return pairs


def generate_bilingual_adapter(provider: Provider, path, *,
                               context: str = ""):
    """Adapter that extracts (key, source, target) pairs from any format.

    The source path has had this since the Adapter-Writer existed; the
    bilingual path hard-rejected everything but .po, so a client sending
    an xlsx of translations for review had no way in at all. Same sandbox,
    same generate-validate-retry loop, different contract.
    """
    return generate_converter(provider, path,
                              system_prompt=BILINGUAL_SYSTEM,
                              validate=validate_pairs, context=context)
