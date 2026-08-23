"""Post-editing delivery: apply the review team's decisions from a client
bug-report xlsx back onto the shipped .po files.

The post-editing team returns the bug report with two extra columns:

- ``Decision`` — one of Accept / Decline & Modify / Decline & Keep-as-it
  (free-form; matched case- and whitespace-insensitively). Blank means the
  row is still undecided.
- ``Modify Version`` — the post-editor's own rendering, authoritative when
  the decision is Decline & Modify.

Apply rules (all enforced here, in code):

- **Accept** → write the report's suggested translation to the .po.
- **Decline & Modify** → write the Modify Version text (it OVERRIDES the
  suggestion; a modify decision with a blank Modify Version is a review
  error and is surfaced, never guessed).
- **Decline & Keep-as-it** / blank → the .po is left untouched; undecided
  rows are listed in the delivery report as still-open.
- Two decided rows disagreeing on the same key is a **conflict**: nothing
  is applied for that key and the conflict is reported. Silence is never
  an option — every row lands in exactly one bucket of the report.

The patch itself is a LINE STREAM, never a parse-and-regenerate: the
source .po is read line by line and each line is copied to the delivery
file verbatim (``newline=""`` — no newline or encoding translation, BOM
included). The ONLY lines ever substituted are the ``msgstr`` lines of
entries the review decided; an untouched or keep-as-is string cannot be
damaged because its bytes are never interpreted, only forwarded. The
source file itself is opened read-only and never modified.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

TEAM_LABEL = "Orbit8 Lab"        # branding po_sanity.check_label looks for

ID_COL = "Location/String ID"
SOURCE_COL = "Source Text"
CURRENT_COL = "Current Translation"
SUGGESTION_COL = "Expected Result / Suggested Translation"
DECISION_COL = "Decision"
MODIFY_COL = "Modify Version"
BUG_COL = "Bug#"

_GUID = re.compile(r"[0-9A-F]{32}")


@dataclass
class ReviewRow:
    bug: str
    location_id: str
    source: str
    decision: str                    # accept | modify | keep | undecided
    replacement: Optional[str]       # text to write (accept/modify only)
    note: Optional[str] = None       # parse problem, surfaced in the report


def normalize_decision(raw: object) -> str:
    text = str(raw or "").strip().lower()
    if not text or text == "none":
        return "undecided"
    if text.startswith("accept"):
        return "accept"
    if "modify" in text:
        return "modify"
    if text.startswith("decline") or "keep" in text:
        return "keep"
    return "undecided"


def _parse_pe_form(xlsx: Path, header: List[str],
                   rows: List[tuple]) -> List[ReviewRow]:
    """Filled standard PE form (docs/STANDARDS.md §4.1 MTPE / §4.2 LQA)
    → the same normalized decision rows as the bug-report shape.

    Decision mapping (standards.LQA_PE_DECISIONS / MTPE_DECISIONS):
      Accept Translation / Accept Suggested Translation → accept the
        agent-proposed target (Target_MT or Target_Suggested)
      Reject&Modification → PE_Modification wins (required)
      Reject&Keep-as-it-is → string untouched
      Reject&Cannot Answer → untouched; PE_Query is a dev question
      (blank) → undecided, untouched
    """
    idx = {name: i for i, name in enumerate(header)}

    def cell(row: tuple, column: str) -> str:
        if column not in idx or idx[column] >= len(row):
            return ""
        value = row[idx[column]]
        return "" if value is None else str(value)

    proposed_col = ("Target_Suggested" if "Target_Suggested" in idx
                    else "Target_MT")
    parsed: List[ReviewRow] = []
    for number, row in enumerate(rows, 1):
        if not any(row):
            continue
        raw = cell(row, "PE_Decision").strip()
        low = raw.lower()
        replacement, note = None, None
        if not raw:
            decision = "undecided"
        elif low.startswith("accept"):
            decision = "accept"
            replacement = cell(row, proposed_col).strip()
            if not replacement:
                decision, note = "undecided", (
                    f"{raw} but {proposed_col} is empty")
        elif "modification" in low:
            decision = "modify"
            replacement = cell(row, "PE_Modification").strip()
            if not replacement:
                decision, note = "undecided", (
                    "Reject&Modification but PE_Modification is empty")
        elif "keep" in low:
            decision = "keep"
        elif "cannot answer" in low:
            decision = "keep"
            query = cell(row, "PE_Query").strip()
            note = (f"Reject&Cannot Answer — dev query: {query}" if query
                    else "Reject&Cannot Answer but PE_Query is empty")
        else:
            decision, note = "undecided", f"unknown decision {raw!r}"
        parsed.append(ReviewRow(
            bug=str(number), location_id=cell(row, "StringID"),
            source=cell(row, "Source"), decision=decision,
            replacement=replacement, note=note))
    if not parsed:
        raise ValueError(f"{xlsx}: no review rows")
    return parsed


def parse_review(xlsx: Path) -> List[ReviewRow]:
    """Reviewed decision workbook → normalized decision rows. Accepts
    either the client bug-report shape or a filled standard PE form
    (MTPE / LQA PE), detected by its header."""
    import openpyxl
    book = openpyxl.load_workbook(xlsx, data_only=True)
    sheet = book.worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise ValueError(f"{xlsx}: empty workbook")
    header = [str(h) if h is not None else "" for h in rows[0]]
    if "PE_Decision" in header and "StringID" in header:
        return _parse_pe_form(xlsx, header, rows[1:])
    for column in (ID_COL, SUGGESTION_COL, DECISION_COL):
        if column not in header:
            raise ValueError(
                f"{xlsx}: unrecognized review workbook — expected either "
                f"the bug-report columns ({ID_COL!r}, {DECISION_COL!r}) "
                f"or a standard PE form ('StringID', 'PE_Decision'); "
                f"got {header[:6]}")
    idx = {name: header.index(name) for name in header}

    def cell(row: tuple, column: str) -> str:
        if column not in idx or idx[column] >= len(row):
            return ""
        value = row[idx[column]]
        return "" if value is None else str(value)

    parsed: List[ReviewRow] = []
    for row in rows[1:]:
        if not any(row):
            continue
        decision = normalize_decision(cell(row, DECISION_COL))
        replacement, note = None, None
        if decision == "accept":
            replacement = cell(row, SUGGESTION_COL).strip()
            if not replacement:
                decision, note = "undecided", ("accepted but the "
                                               "suggestion cell is empty")
        elif decision == "modify":
            replacement = cell(row, MODIFY_COL).strip()
            if not replacement:
                decision, note = "undecided", ("Decline & Modify but "
                                               "Modify Version is empty")
        bug = cell(row, BUG_COL).removesuffix(".0")
        parsed.append(ReviewRow(
            bug=bug or "?", location_id=cell(row, ID_COL),
            source=cell(row, SOURCE_COL), decision=decision,
            replacement=replacement, note=note))
    if not parsed:
        raise ValueError(f"{xlsx}: no review rows")
    return parsed


def match_keys(location_id: str, po_keys: set) -> List[str]:
    """Map a bug row's Location/String ID onto .po msgctxt keys. Handles
    every ID shape this repo has ever emitted: the pipeline's
    ``<path> :: <GUID>``, the agent's ``uid :: <path> :: <key>`` and the
    legacy ``uid :: ,<GUID>,,<GUID>`` — plus a bare key."""
    matched: List[str] = []

    def add(candidate: str) -> None:
        if candidate in po_keys and candidate not in matched:
            matched.append(candidate)

    for token in re.split(r"\s*(?:::|;)\s*", str(location_id)):
        token = token.strip()
        add(token)
        add("," + token)
    for guid in _GUID.findall(str(location_id)):
        add(guid)
        add("," + guid)
    return matched


def _po_escape(text: str, crlf: bool) -> str:
    """Escape for a single-line msgstr, matching the file's own newline
    convention (UE exports escape newlines as ``\\r\\n``)."""
    text = (text.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\t", "\\t").replace("\r\n", "\n").replace("\r", "\n"))
    return text.replace("\n", "\\r\\n" if crlf else "\\n")


def _po_unescape(text: str) -> str:
    return (text.replace("\\r", "\r").replace("\\n", "\n")
            .replace("\\t", "\t").replace('\\"', '"')
            .replace("\\\\", "\\"))


_LITERAL_BREAK = re.compile(r"\\r\\n|\\n")


def normalize_break_notation(replacement: str) -> str:
    """Turn a line break the reviewer TYPED into a real one.

    A reviewer editing a .po-derived xlsx sees the file's escape (``\\n``)
    and sometimes reproduces it literally in the replacement cell. Shipped
    as-is, the player reads the characters "\\n" on screen instead of
    getting a line break — the exact opposite of the intent.
    """
    return _LITERAL_BREAK.sub("\n", replacement or "")


def align_break_marker(original: str, replacement: str) -> str:
    """Give ``replacement`` the break marker ``original`` used.

    This asset writes most multi-line strings as ``\\`` immediately before
    the newline — a real backslash the engine consumes as a hard break —
    but not all of them (77 of 95 in one live asset). The convention is
    therefore a property of the individual STRING, not the file, and a
    rewrite that arrives with bare newlines must inherit whichever marker
    the entry it replaces was already using.

    Getting this wrong is invisible in the xlsx and silent in the diff:
    the text reads correctly while the in-game line break disappears.
    """
    if not replacement:
        return replacement
    bare = original.replace("\r", "")
    if "\\\n" not in bare:                 # entry uses plain breaks
        return replacement
    # A marker whose newline went missing ("destination.\The goal") is a
    # LOST BREAK, not content: the backslash only ever precedes one in
    # this asset, so a bare "\" mid-string means the break was dropped
    # somewhere upstream. Restore it before aligning the rest.
    # Consume genuine escape PAIRS first so the second character of "\\"
    # is never itself read as a dangling marker. A marker at the very END
    # of the string is left alone: there is no following line for it to
    # separate, and the source keeps trailing markers as-is.
    out = re.sub(r"\\[\\nrt\"']|\\(?!\n)(?=.)",
                 lambda m: m.group(0) if len(m.group(0)) == 2 else "\\\n",
                 replacement)
    if "\n" not in out:
        return out
    # Re-apply the marker to every break that lacks it, leaving any the
    # reviewer already typed correctly alone.
    return re.sub(r"(?<!\\)\r?\n", "\\\\\n", out)


# ------------------------------------------------- inbound separator repair
#
# A RECEIVED drop can carry the opposite defect from the one above: the
# translator's tooling escaped the target one level too many, so a
# separator the source writes as ``\n`` comes back as ``\\\r\n``. The
# engine reads that extra backslash as a literal character and PRINTS it
# where the line break belonged.
#
# Three layers have to be kept apart to see it at all:
#
#   layer 1  file bytes     what is literally in the .po      ``\\n``
#   layer 2  PO-decoded     what gettext hands the engine     ``\n``
#   layer 3  engine render  what the player sees              a line break
#
# one observed UE asset uses THREE layer-2 separator tokens, and they are
# not interchangeable: ``\n`` (backslash + letter n, 157 msgids), a real
# CRLF (29), and ``\\`` (a literal double backslash, 12). The repair rule
# is therefore NOT "normalize the file" but:
#
#     the target must decode to the SAME layer-2 separator token as the
#     msgid of ITS OWN entry
#
# — the same per-entry principle align_break_marker applies on the way
# out, enforced here on the way in.

def po_unescape_raw(raw: str) -> str:
    """File bytes → PO-decoded (layer 1 → layer 2). Exactly ONE level.

    Unlike ``_po_unescape`` (a chain of str.replace calls, which can
    re-read the output of an earlier replacement) this consumes each
    backslash pair once, left to right, so ``\\\\n`` decodes to the two
    characters ``\\`` + ``n`` and never to a newline.
    """
    return re.sub(r"\\(.)", lambda m: {
        "n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\",
    }.get(m.group(1), m.group(1)), raw or "")


def po_escape_raw(text: str) -> str:
    """PO-decoded → file bytes (layer 2 → layer 1). Inverse of the above.

    Lossless for every string in the asset, which is what lets an
    untouched entry round-trip byte-identical.
    """
    out = (text or "").replace("\\", "\\\\").replace('"', '\\"')
    return (out.replace("\r", "\\r").replace("\n", "\\n")
            .replace("\t", "\\t"))


#: Layer-2 separator tokens, longest/most specific FIRST — ``\\`` has to
#: be consumed before the ``\n`` inside it can be mistaken for a token.
SEP_TOKENS = ("\\\\", "\\r\\n", "\\n", "\r\n", "\n", "\\r")

_SEP_RE = re.compile("|".join(re.escape(t) for t in SEP_TOKENS))

#: A corrupted separator decodes to an ADJACENT RUN of tokens, not to one:
#: the drop's ``\\\\\\r\\n`` comes back as ``\\`` + ``\r`` + ``\n``. Those
#: three are one damaged break, so the run is matched — and replaced — as
#: a single unit. Substituting token by token would triple every break.
_SEP_RUN_RE = re.compile(f"(?:{_SEP_RE.pattern})+")


def separators(decoded: str) -> List[str]:
    """Every layer-2 separator RUN in ``decoded``, in order — one entry
    per break, however many tokens the damage split it into."""
    return _SEP_RUN_RE.findall(decoded or "")


def separator_convention(decoded: str) -> Optional[str]:
    """The entry's own separator token, or None when it is single-line.

    A string mixing tokens reports its FIRST: a msgid in this asset never
    legitimately mixes, so a mix in a msgstr is itself the defect.

    Only an UNDAMAGED run — a single token — can define the convention. A
    msgid whose own separator arrived corrupted has no clean token to
    donate, so it reports None and its entry is left for a human rather
    than repaired against a guess.
    """
    for run in separators(decoded):
        if _SEP_RE.fullmatch(run):
            return run
    return None


def match_source_format(src_decoded: str, tgt_decoded: str) -> str:
    """Rewrite ``tgt_decoded`` so its separators are the token
    ``src_decoded`` uses. Text content is never touched.

    Two things are deliberately NOT done, because both are content
    decisions wearing a formatting costume:

    - a target with no separator at all keeps none — a LOST break is
      reported for post-editing, never invented here (in this drop three
      of those entries also dropped clauses, which no format pass can
      restore);
    - break COUNT is not forced — a translator may legitimately merge or
      split lines, so drift is reported rather than re-flowed.
    """
    want = separator_convention(src_decoded)
    if want is None or not separators(tgt_decoded):
        return tgt_decoded
    # Replace whole RUNS: one damaged break becomes one correct break.
    return _SEP_RUN_RE.sub(lambda _m: want, tgt_decoded)


def repair_separator_raw(src_raw: str, tgt_raw: str) -> str:
    """Full round trip at the file-bytes level: decode both sides, match
    the source's convention, re-encode the target."""
    return po_escape_raw(match_source_format(po_unescape_raw(src_raw),
                                             po_unescape_raw(tgt_raw)))


@dataclass
class SeparatorAudit:
    """What a separator sweep found, per entry, for the report."""
    repaired: List[dict] = field(default_factory=list)
    lost_break: List[dict] = field(default_factory=list)
    count_drift: List[dict] = field(default_factory=list)

    def counts(self) -> Dict[str, int]:
        return {"repaired": len(self.repaired),
                "lost_break": len(self.lost_break),
                "count_drift": len(self.count_drift)}


def audit_separators(entries: Iterable[Tuple[str, str, str]]
                     ) -> Tuple[Dict[str, str], SeparatorAudit]:
    """Scan ``(key, msgid_raw, msgstr_raw)`` triples for separator defects.

    Returns the raw msgstr replacements to apply (keyed by msgctxt) and an
    audit of everything that needs a human instead.
    """
    fixes: Dict[str, str] = {}
    audit = SeparatorAudit()
    for key, src_raw, tgt_raw in entries:
        if not (tgt_raw or "").strip():
            continue                       # untranslated: not our business
        src_dec, tgt_dec = po_unescape_raw(src_raw), po_unescape_raw(tgt_raw)
        src_seps, tgt_seps = separators(src_dec), separators(tgt_dec)
        if src_seps and not tgt_seps:
            audit.lost_break.append(
                {"key": key, "source": src_dec, "target": tgt_dec})
            continue
        if src_seps and tgt_seps and len(src_seps) != len(tgt_seps):
            audit.count_drift.append(
                {"key": key, "source_breaks": len(src_seps),
                 "target_breaks": len(tgt_seps)})
        repaired = repair_separator_raw(src_raw, tgt_raw)
        if repaired != tgt_raw:
            fixes[key] = repaired
            audit.repaired.append(
                {"key": key, "before": tgt_raw, "after": repaired,
                 "convention": separator_convention(src_dec)})
    return fixes, audit


def read_raw_entries(path: Path) -> List[Tuple[str, str, str]]:
    """``(msgctxt, msgid, msgstr)`` as RAW file bytes — still escaped.

    Deliberately not ``exports.read_po_entries``: that decodes, and the
    whole defect being audited lives in the escaping level, which decoding
    would flatten away.
    """
    out: List[Tuple[str, str, str]] = []
    key = src = None
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("msgctxt "):
                key = stripped[len("msgctxt "):].strip()[1:-1]
            elif stripped.startswith("msgid "):
                src = stripped[len("msgid "):].strip()[1:-1]
            elif stripped.startswith("msgstr ") and key is not None:
                out.append((key, src or "",
                            stripped[len("msgstr "):].strip()[1:-1]))
                key = src = None
    return out


def patch_po_raw_lines(lines: Iterable[str], raw_replacements: Dict[str, str],
                       applied: List[str]) -> Iterator[str]:
    """Line stream that substitutes ALREADY-ESCAPED msgstr text.

    ``patch_po_lines`` re-escapes a decoded replacement through
    ``_po_escape``, which normalizes CR/LF and so cannot express a
    per-entry convention. A separator repair has to write exact bytes, so
    it goes through here instead. Every other line is forwarded verbatim.
    """
    current_key: Optional[str] = None
    skipping = False
    for line in lines:
        stripped = line.strip()
        if skipping:
            if stripped.startswith('"'):
                continue                  # old continuation line, drop
            skipping = False
        if stripped.startswith("msgctxt "):
            current_key = stripped[len("msgctxt "):].strip()[1:-1]
        elif (stripped.startswith("msgstr ")
                and current_key in raw_replacements):
            newline = line[len(line.rstrip("\r\n")):] or "\n"
            yield f'msgstr "{raw_replacements[current_key]}"{newline}'
            applied.append(current_key)
            skipping = True
            continue
        yield line


def repair_po_separators(src: Path, dst: Path
                         ) -> Tuple[List[str], SeparatorAudit]:
    """Stream ``src`` → ``dst``, rewriting only msgstr lines whose
    separators disagree with their own msgid. Every other byte — BOM,
    comments, line endings, untouched entries — is forwarded unchanged.
    Returns the keys repaired and the audit of what a human still owes."""
    fixes, audit = audit_separators(read_raw_entries(Path(src)))
    applied: List[str] = []
    with open(src, encoding="utf-8-sig", newline="") as reader, \
            open(dst, "w", encoding="utf-8-sig", newline="") as writer:
        for line in patch_po_raw_lines(reader, fixes, applied):
            writer.write(line)
    return applied, audit


def patch_po_lines(lines: Iterable[str], replacements: Dict[str, str],
                   applied: List[str], *, crlf: bool) -> Iterator[str]:
    """The core line stream: yield every input line UNCHANGED except the
    msgstr line of entries whose msgctxt key is in ``replacements`` (plus
    that msgstr's old continuation lines, which the new single line
    replaces). Lines keep their own terminators; nothing is re-serialized.
    Keys actually patched are appended to ``applied``."""
    current_key: Optional[str] = None
    skipping = False                     # dropping old msgstr continuations
    for line in lines:
        stripped = line.strip()
        if skipping:
            if stripped.startswith('"'):
                continue                 # old continuation line, drop
            skipping = False
        if stripped.startswith("msgctxt "):
            current_key = _po_unescape(
                stripped[len("msgctxt "):].strip().strip('"'))
        elif (stripped.startswith("msgstr ")
                and current_key in replacements):
            newline = line[len(line.rstrip("\r\n")):] or "\n"
            yield (f'msgstr "'
                   f'{_po_escape(replacements[current_key], crlf)}"'
                   f"{newline}")
            applied.append(current_key)
            skipping = True
            continue
        yield line


def relabel_header_lines(lines: Iterable[str], *, team: str,
                         now_label: str,
                         relabeled: Dict[str, str]) -> Iterator[str]:
    """Stream transform for the PO header block only: refresh
    ``PO-Revision-Date`` and stamp the localization-source branding
    (``Language-Team`` / ``Last-Translator``) that po_sanity.check_label
    requires on a deliverable. Entry lines pass through untouched; every
    field changed is recorded in ``relabeled`` so the delivery report can
    list these as intended edits."""
    in_header, in_header_msgstr = True, False
    seen_team = seen_translator = False
    for line in lines:
        if in_header:
            stripped = line.strip()
            term = line[len(line.rstrip("\r\n")):] or "\n"
            if stripped.startswith("msgctxt"):
                # entry starts: file had no header msgstr → nothing to
                # relabel (a header-less file is po_sanity's job to flag)
                in_header = False
                if in_header_msgstr:
                    yield from _label_inserts(
                        seen_team, seen_translator, team, term, relabeled)
                yield line
                continue
            if in_header_msgstr and (stripped == ""
                                     or stripped.startswith("#")):
                yield from _label_inserts(
                    seen_team, seen_translator, team, term, relabeled)
                in_header = False
                yield line
                continue
            if stripped.startswith("msgstr"):
                in_header_msgstr = True
            elif in_header_msgstr:
                if stripped.startswith('"PO-Revision-Date:'):
                    relabeled["PO-Revision-Date"] = now_label
                    yield f'"PO-Revision-Date: {now_label}\\n"{term}'
                    continue
                if stripped.startswith('"Language-Team:'):
                    seen_team = True
                    relabeled["Language-Team"] = team
                    yield f'"Language-Team: {team}\\n"{term}'
                    continue
                if stripped.startswith('"Last-Translator:'):
                    seen_translator = True
                    relabeled["Last-Translator"] = team
                    yield f'"Last-Translator: {team}\\n"{term}'
                    continue
        yield line


def _label_inserts(seen_team: bool, seen_translator: bool, team: str,
                   term: str, relabeled: Dict[str, str]) -> Iterator[str]:
    if not seen_team:
        relabeled["Language-Team"] = team
        yield f'"Language-Team: {team}\\n"{term}'
    if not seen_translator:
        relabeled["Last-Translator"] = team
        yield f'"Last-Translator: {team}\\n"{term}'


def _uses_crlf_escapes(path: Path) -> bool:
    """Does the file escape newlines as ``\\r\\n`` (the UE convention)?
    Streamed check — stops at the first evidence."""
    with open(path, encoding="utf-8", newline="") as fh:
        for line in fh:
            if "\\r\\n" in line:
                return True
    return False


def patch_po_file(src: Path, dst: Path, replacements: Dict[str, str], *,
                  team: Optional[str] = None,
                  relabeled: Optional[Dict[str, str]] = None) -> List[str]:
    """Stream ``src`` → ``dst`` line by line (no newline translation in
    either direction — untouched lines are forwarded byte-identical, BOM
    and line endings included). ``src`` is opened read-only and never
    written. With ``team`` set, the header label lines are additionally
    refreshed (relabel_header_lines) — the only edits besides the decided
    msgstr lines. Returns the keys actually patched."""
    applied: List[str] = []
    crlf = _uses_crlf_escapes(Path(src))
    with open(src, encoding="utf-8", newline="") as reader, \
            open(dst, "w", encoding="utf-8", newline="") as writer:
        stream: Iterable[str] = reader
        if team:
            stream = relabel_header_lines(
                stream, team=team,
                now_label=datetime.now().strftime("%Y-%m-%d %H:%M"),
                relabeled=relabeled if relabeled is not None else {})
        for line in patch_po_lines(stream, replacements, applied,
                                   crlf=crlf):
            writer.write(line)
    return applied


def patch_po_text(po_text: str,
                  replacements: Dict[str, str]) -> Tuple[str, List[str]]:
    """In-memory convenience wrapper over the same line stream."""
    applied: List[str] = []
    patched = "".join(patch_po_lines(
        po_text.splitlines(keepends=True), replacements, applied,
        crlf="\\r\\n" in po_text))
    return patched, applied


@dataclass
class DeliveryReport:
    delivery_dir: str
    outputs: List[str] = field(default_factory=list)
    applied: List[dict] = field(default_factory=list)
    kept: List[dict] = field(default_factory=list)
    undecided: List[dict] = field(default_factory=list)
    unmatched: List[dict] = field(default_factory=list)
    conflicts: List[dict] = field(default_factory=list)
    # Same source text rendered differently in the DELIVERED files —
    # happens when reviewers decide duplicate locations differently
    # (accept here, modify there). Warned, not blocked: the per-row
    # decisions are human and final, but the client should hear about it.
    inconsistent: List[dict] = field(default_factory=list)
    # Intended header edits (relabel) and the po_sanity verdicts, per file.
    relabeled: Dict[str, Dict[str, str]] = field(default_factory=dict)
    sanity: Dict[str, dict] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        """True when po_sanity found ERRORs — do not deliver."""
        return any(result["errors"] for result in self.sanity.values())

    def counts(self) -> Dict[str, int]:
        return {"applied": len(self.applied), "kept": len(self.kept),
                "undecided": len(self.undecided),
                "unmatched": len(self.unmatched),
                "conflicts": len(self.conflicts),
                "inconsistent_sources": len(self.inconsistent)}


def deliver_from_review(review_xlsx: Path, po_files: List[Path],
                        out_dir: Path, *, timestamp: Optional[str] = None,
                        tm=None, locale: Optional[str] = None,
                        sanity_check: bool = True, relabel: bool = True,
                        team: str = TEAM_LABEL) -> DeliveryReport:
    """The whole flow: parse decisions → patch each .po (header relabel +
    decided msgstr lines only) → write the delivery folder
    (``<out>/<YYYYMMDD>-po-delivery/``) with the patched files plus a
    delivery report (md + json) → run the po_sanity gate on every output
    (format/import safety, label freshness+branding, summary vs the
    source .po as reference). ERRORs set ``report.blocked`` — the report
    banner and callers then say DO NOT DELIVER. Accepted/modified pairs
    write back to the job TM as origin='human' when ``tm`` is given —
    post-editing decisions are the human ground truth the TM exists for."""
    rows = parse_review(Path(review_xlsx))
    stamp = timestamp or date.today().strftime("%Y%m%d")
    delivery_dir = Path(out_dir) / f"{stamp}-po-delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)

    # per-file entry maps: key -> msgid (for TM write-back and matching)
    from .exports import read_po_entries
    file_entries: Dict[Path, Dict[str, str]] = {}
    po_targets: Dict[str, str] = {}       # key -> CURRENT translation, so a
    for po in po_files:                   # replacement can inherit its style
        entries = list(read_po_entries(Path(po)))
        file_entries[Path(po)] = {key: source for key, source, _t, _loc
                                  in entries}
        for key, _source, target, _loc in entries:
            po_targets.setdefault(key, target)
    all_keys = {key for entries in file_entries.values() for key in entries}

    report = DeliveryReport(delivery_dir=str(delivery_dir))
    desired: Dict[str, str] = {}          # key -> replacement text
    claimed: Dict[str, str] = {}          # key -> bug# that set it
    conflicted: set = set()
    for row in rows:
        entry = {"bug": row.bug, "source": row.source[:60],
                 "id": row.location_id[:120]}
        if row.note:
            entry["note"] = row.note
        if row.decision == "keep":
            report.kept.append(entry)
            continue
        if row.decision == "undecided":
            report.undecided.append(entry)
            continue
        keys = match_keys(row.location_id, all_keys)
        if not keys:
            report.unmatched.append(entry)
            continue
        # Line-break FIDELITY. The received file's conventions are
        # authoritative even where they look inconsistent, so a rewrite
        # inherits the marker of the entry it replaces — never a
        # house style. Two separate corrections:
        #   1. a break the reviewer typed as the literal characters "\n"
        #      would otherwise ship as those characters on screen;
        #   2. a rewrite that came back with bare newlines must regain
        #      the "\" marker if that entry used one.
        if row.replacement:
            current = po_targets.get(keys[0], "")
            # A cell edited in Excel often carries a trailing newline the
            # editor added, which would ship as an empty final line.
            fixed = normalize_break_notation(row.replacement).rstrip()
            fixed = align_break_marker(current, fixed)
            if fixed != row.replacement:
                entry["break_marker_aligned"] = True
                row = replace(row, replacement=fixed)
        entry["keys"] = keys
        entry["decision"] = row.decision
        for key in keys:
            if key in desired and desired[key] != row.replacement:
                conflicted.add(key)
                report.conflicts.append(
                    {"key": key, "bugs": [claimed[key], row.bug],
                     "texts": [desired[key][:60], row.replacement[:60]]})
            else:
                desired[key] = row.replacement
                claimed[key] = row.bug
        report.applied.append(entry)
    for key in conflicted:                # conflicts apply NOTHING
        desired.pop(key, None)
    report.applied = [e for e in report.applied
                      if any(k not in conflicted for k in e["keys"])]

    for po in po_files:
        po = Path(po)
        wanted = {key: text for key, text in desired.items()
                  if key in file_entries[po]}
        target = delivery_dir / po.name
        relabeled: Dict[str, str] = {}
        applied_keys = patch_po_file(po, target, wanted,
                                     team=team if relabel else None,
                                     relabeled=relabeled)
        report.outputs.append(str(target))
        if relabeled:
            report.relabeled[po.name] = relabeled
        if sanity_check:
            report.sanity[po.name] = _run_sanity(po, target, team=team)
        if tm is not None and locale:
            for key in applied_keys:
                tm.store(file_entries[po][key], desired[key], locale,
                         origin="human")

    # post-patch consistency sweep over what will actually ship
    renderings: Dict[str, set] = {}
    for output in report.outputs:
        for _key, source, target, _loc in read_po_entries(Path(output)):
            if target.strip():
                renderings.setdefault(source.strip(), set()).add(
                    target.strip())
    for source, targets in sorted(renderings.items()):
        if len(targets) > 1:
            report.inconsistent.append(
                {"source": source[:80],
                 "renderings": sorted(t[:80] for t in targets)})

    _write_report_files(report, delivery_dir, review_xlsx)
    return report


def _run_sanity(source: Path, delivered: Path, *, team: str) -> dict:
    """po_sanity gate on one delivered file: format/import safety, label
    freshness + branding, and the summary diffed against the source .po.
    BOM is expected exactly when the source itself had one."""
    from .po_sanity import check_format, check_label, summarize
    expect_bom = Path(source).read_bytes()[:3] == b"\xef\xbb\xbf"
    issues = (check_format(Path(delivered), expect_bom=expect_bom)
              + check_label(Path(delivered), team=team))
    errors = [str(i) for i in issues if i.severity == "ERROR"]
    warnings = [str(i) for i in issues if i.severity == "WARN"]
    summary = summarize(Path(delivered), reference=Path(source))
    return {
        "verdict": ("FAIL - do not deliver" if errors
                    else "PASS with warnings" if warnings else "PASS"),
        "errors": len(errors), "warnings": len(warnings),
        "error_details": errors[:20], "warning_details": warnings[:20],
        "summary": {k: summary.get(k) for k in (
            "total_strings", "translated", "untranslated",
            "completion_pct", "translations_modified", "newly_translated",
            "translations_lost", "keys_added", "keys_removed")},
    }


def _write_report_files(report: DeliveryReport, delivery_dir: Path,
                        review_xlsx: Path) -> None:
    (delivery_dir / "DELIVERY_REPORT.json").write_text(
        json.dumps({"review": str(review_xlsx), "counts": report.counts(),
                    "outputs": report.outputs, "applied": report.applied,
                    "kept": report.kept, "undecided": report.undecided,
                    "unmatched": report.unmatched,
                    "conflicts": report.conflicts,
                    "inconsistent": report.inconsistent,
                    "relabeled": report.relabeled,
                    "sanity": report.sanity,
                    "blocked": report.blocked},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# PO delivery — {delivery_dir.name}"]
    if report.blocked:
        lines += ["", "> ⛔ **DO NOT DELIVER** — the po_sanity gate found "
                      "errors (see Sanity check below)."]
    lines += [f"Review file: `{Path(review_xlsx).name}`", "",
              "| Bucket | Rows |", "|---|---|"]
    lines += [f"| {name} | {count} |"
              for name, count in report.counts().items()]
    lines += ["", "## Outputs"]
    lines += [f"- `{Path(output).name}`" for output in report.outputs]
    if report.undecided:
        lines += ["", "## Still undecided (NOT applied — needs follow-up)"]
        lines += [f"- Bug {e['bug']}: {e['source']}"
                  + (f" — {e['note']}" if e.get("note") else "")
                  for e in report.undecided]
    if report.unmatched:
        lines += ["", "## Unmatched rows (no .po key found)"]
        lines += [f"- Bug {e['bug']}: {e['id']}" for e in report.unmatched]
    if report.conflicts:
        lines += ["", "## Conflicts (nothing applied for these keys)"]
        lines += [f"- `{c['key']}`: bugs {c['bugs']} disagree"
                  for c in report.conflicts]
    if report.sanity:
        lines += ["", "## Sanity check (po_sanity, pre-delivery gate)"]
        for name, result in report.sanity.items():
            lines.append(f"- `{name}`: **{result['verdict']}** "
                         f"({result['errors']} errors, "
                         f"{result['warnings']} warnings) · "
                         f"{result['summary'].get('completion_pct')}% "
                         f"translated, "
                         f"{result['summary'].get('translations_modified')}"
                         f" modified vs source")
            lines += [f"  - {detail}"
                      for detail in result["error_details"]]
    if report.relabeled:
        lines += ["", "## Header relabel (intended edits)"]
        for name, fields in report.relabeled.items():
            lines += [f"- `{name}`: " + ", ".join(
                f"{field} → {value}" for field, value in fields.items())]
    if report.inconsistent:
        lines += ["", "## Consistency warnings",
                  "Same source shipped with different renderings "
                  "(reviewers decided duplicate locations differently):"]
        for entry in report.inconsistent:
            lines.append(f"- {entry['source']}")
            lines += [f"  - {rendering}"
                      for rendering in entry["renderings"]]
    (delivery_dir / "DELIVERY_REPORT.md").write_text(
        "\n".join(lines), encoding="utf-8")
