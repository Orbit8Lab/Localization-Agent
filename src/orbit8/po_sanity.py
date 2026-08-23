#!/usr/bin/env python3
"""PO sanity checker for Unreal / game-engine deliverables (Orbit8 Lab).

Ported verbatim from ../localization-pipeline/po_sanity_check.py (that file
remains the source of truth — keep the two in sync). Runs by default as the
pre-delivery gate in po_patch.deliver_from_review: an ERROR verdict marks
the delivery folder DO-NOT-DELIVER.

Three checks:
  1. check_format   - is the file a structurally valid PO that Unreal/Steam
                      will accept (single-line strings, escapes, msgctxt keys,
                      BOM, header, duplicates, placeholder parity ...)
  2. check_label    - is the header label up to date (PO-Revision-Date) and
                      does it carry the localization-source branding (Orbit8 Lab)
  3. summarize      - brief stats: total / translated / empty strings, and if a
                      reference PO is given, how many strings were modified.

Usage:
    python po_sanity_check.py DELIVERABLE.po [--ref ORIGINAL.po]
        [--team "Orbit8"] [--max-age-days 30] [--json report.json]

Exit code: 0 = clean, 1 = warnings only, 2 = errors (do not deliver).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# Escapes GNU gettext allows; game engines only reliably round-trip \n \t \" \\
SAFE_ESCAPES = {'n', 't', '"', '\\'}
SUSPICIOUS_ESCAPES = {'r', 'a', 'b', 'f', 'v', "'"}

KEYWORD_RE = re.compile(r'^(msgctxt|msgid_plural|msgid|msgstr(?:\[\d+\])?)\s+(".*")\s*$')
CONTINUATION_RE = re.compile(r'^(".*")\s*$')
STRING_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"$')
# Unreal msgctxt = "Namespace,Key" (namespace may be empty)
UNREAL_CTXT_RE = re.compile(r'^[^,]*,.+$')
PLACEHOLDER_RE = re.compile(r'\{[A-Za-z0-9_]+\}|%(?:\d+\$)?[sdif]')
# CJK unified ideographs, hiragana/katakana, hangul syllables
CJK_RE = re.compile('[一-鿿぀-ヿ가-힯]')
LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
CJK_LANGS = ('zh', 'ja', 'ko')
HEADER_DATE_FORMATS = ('%Y-%m-%d %H:%M%z', '%Y-%m-%d %H:%M')


@dataclass
class Issue:
    severity: str          # ERROR | WARN | INFO
    line: int              # 0 = file-level
    message: str

    def __str__(self) -> str:
        loc = f'line {self.line}' if self.line else 'file'
        return f'[{self.severity}] {loc}: {self.message}'


@dataclass
class Entry:
    line: int = 0
    msgctxt: str | None = None
    msgid: str = ''
    msgstr: str = ''
    msgid_lines: int = 1       # physical string-lines used (1 = single-line)
    msgstr_lines: int = 1
    comments: list[str] = field(default_factory=list)
    fuzzy: bool = False


def _unescape(s: str, line: int, issues: list[Issue]) -> str:
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in SAFE_ESCAPES or nxt in SUSPICIOUS_ESCAPES:
                # \r is native in Unreal PO exports (line breaks as \r\n)
                if nxt in SUSPICIOUS_ESCAPES and nxt != 'r':
                    issues.append(Issue('WARN', line,
                        f'unusual escape \\{nxt} in string'))
                out.append({'n': '\n', 't': '\t', 'r': '\r'}.get(nxt, nxt))
                i += 2
                continue
            issues.append(Issue('ERROR', line,
                f'invalid escape sequence \\{nxt}'))
            i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def parse_po(path: Path, issues: list[Issue] | None = None
             ) -> tuple[Entry | None, list[Entry], list[Issue], bool]:
    """Parse a PO file. Returns (header_entry, entries, issues, had_bom)."""
    if issues is None:
        issues = []
    raw = path.read_bytes()
    had_bom = raw.startswith(b'\xef\xbb\xbf')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        issues.append(Issue('ERROR', 0, f'file is not valid UTF-8: {exc}'))
        text = raw.decode('utf-8-sig', errors='replace')
    if '\r' in text:
        issues.append(Issue('WARN', 0, 'file uses CRLF line endings'))
        text = text.replace('\r\n', '\n')
    if not text.endswith('\n'):
        issues.append(Issue('WARN', 0, 'file does not end with a newline'))

    header: Entry | None = None
    entries: list[Entry] = []
    cur = Entry()
    field_name: str | None = None   # which string field continuations extend

    def flush(at_line: int) -> None:
        nonlocal cur, field_name, header
        if field_name is None and not cur.comments:
            cur = Entry()
            return
        if field_name is None:
            cur = Entry()
            return
        if 'msgstr' not in (field_name or '') and field_name != 'msgstr':
            issues.append(Issue('ERROR', cur.line,
                'entry has no msgstr (truncated entry)'))
        if header is None and cur.msgctxt is None and cur.msgid == '':
            header = cur
        else:
            entries.append(cur)
        cur = Entry()
        field_name = None

    for lineno, line in enumerate(text.split('\n'), start=1):
        stripped = line.strip()
        if stripped == '':
            flush(lineno)
            continue
        if stripped.startswith('#'):
            if field_name is not None:      # comment after strings = new entry
                flush(lineno)
            cur.comments.append(stripped)
            if stripped.startswith('#,') and 'fuzzy' in stripped:
                cur.fuzzy = True
            continue
        m = KEYWORD_RE.match(stripped)
        if m:
            kw, quoted = m.group(1), m.group(2)
            sm = STRING_RE.match(quoted)
            if not sm:
                issues.append(Issue('ERROR', lineno,
                    f'unbalanced or malformed quotes on {kw} line'))
                continue
            value = _unescape(sm.group(1), lineno, issues)
            if kw == 'msgctxt':
                if field_name is not None:
                    flush(lineno)
                cur.msgctxt = value
                cur.line = cur.line or lineno
                field_name = 'msgctxt'
            elif kw == 'msgid':
                if field_name not in (None, 'msgctxt'):
                    flush(lineno)
                cur.msgid = value
                cur.line = cur.line or lineno
                field_name = 'msgid'
            elif kw == 'msgid_plural':
                field_name = 'msgid'   # treat as msgid continuation target
            else:                       # msgstr / msgstr[n]
                if field_name not in ('msgid',):
                    issues.append(Issue('ERROR', lineno,
                        f'{kw} without a preceding msgid'))
                cur.msgstr = value
                field_name = 'msgstr'
            continue
        m = CONTINUATION_RE.match(stripped)
        if m:
            sm = STRING_RE.match(m.group(1))
            if not sm:
                issues.append(Issue('ERROR', lineno,
                    'unbalanced quotes on continuation line'))
                continue
            value = _unescape(sm.group(1), lineno, issues)
            if field_name == 'msgid':
                cur.msgid += value
                cur.msgid_lines += 1
            elif field_name == 'msgstr':
                cur.msgstr += value
                cur.msgstr_lines += 1
            elif field_name == 'msgctxt':
                cur.msgctxt = (cur.msgctxt or '') + value
            else:
                issues.append(Issue('ERROR', lineno,
                    'string continuation line outside an entry'))
            continue
        issues.append(Issue('ERROR', lineno,
            f'unrecognized line: {stripped[:60]!r}'))
    flush(lineno)
    return header, entries, issues, had_bom


def _header_fields(header: Entry | None) -> dict[str, str]:
    fields: dict[str, str] = {}
    if header is None:
        return fields
    for line in header.msgstr.split('\n'):
        if ':' in line:
            k, v = line.split(':', 1)
            fields[k.strip()] = v.strip()
    return fields


# Localization-workflow metalanguage that should never appear in game text.
REVIEW_NOTE_PATTERNS = (
    # CJK reviewer jargon
    ('cjk-meta', re.compile(
        '备选|待定|待译|校对|润色|正样本|负样本|机翻|漏译|误译|'
        '译文|原文|译为|译作|或译|术语表|直译|意译|略机械|已确认|待确认')),
    # talking ABOUT a translation: "X" 译 "Y" / 译 "Y"
    ('quoted-pair', re.compile('["“”][^"“”]{1,40}["“”]\\s*译|译\\s*["“”]')),
    # English workflow markers - uppercase only, so "Mt. Hua" etc. don't match
    ('latin-meta', re.compile(
        r'\b(TODO|FIXME|TBD|WIP|PLACEHOLDER|MT|QA)\b'
        r'|(?i:\((?:alt|option|note)\s*:)')),
)


def detect_review_note(e: Entry) -> str | None:
    """Return the matched signal name if msgstr looks like a leaked
    reviewer/QA annotation rather than a translation, else None."""
    if not e.msgstr.strip():
        return None
    for name, pat in REVIEW_NOTE_PATTERNS:
        m = pat.search(e.msgstr)
        # ignore markers the source itself contains (then it IS the text)
        if m and m.group(0) not in e.msgid:
            return name
    return None


def word_count(s: str) -> int:
    """Word count that works for mixed CJK/Latin text: each CJK character
    counts as one word, plus each run of Latin letters/digits."""
    return len(CJK_RE.findall(s)) + len(LATIN_WORD_RE.findall(CJK_RE.sub(' ', s)))


def classify_untranslated(e: Entry, target_lang: str) -> str | None:
    """Return why an entry counts as untranslated, or None if translated.

    empty            - msgstr is blank
    same_as_source   - msgstr is a verbatim copy of msgid
    source_leftover  - target language is not CJK but msgstr still
                       contains CJK characters (untranslated remnant)
    """
    if not e.msgstr.strip():
        return 'empty'
    if e.msgstr.strip() == e.msgid.strip():
        return 'same_as_source'
    if (not target_lang.lower().startswith(CJK_LANGS)
            and CJK_RE.search(e.msgstr)):
        return 'source_leftover'
    return None


# ---------------------------------------------------------------- function 1
def check_format(path: Path, expect_bom: bool = True) -> list[Issue]:
    """Structural / completeness check: will this PO survive an engine import?"""
    issues: list[Issue] = []
    header, entries, issues, had_bom = parse_po(path, issues)

    if expect_bom and not had_bom:
        issues.append(Issue('WARN', 0,
            'UTF-8 BOM missing - Unreal exports PO files with a BOM; '
            'deliverables should match the original'))

    if header is None:
        issues.append(Issue('ERROR', 0,
            'missing header entry (msgid "" / msgstr "..." block at top)'))
    else:
        fields = _header_fields(header)
        ctype = fields.get('Content-Type', '')
        if 'UTF-8' not in ctype.upper():
            issues.append(Issue('ERROR', header.line,
                f'Content-Type charset is not UTF-8: {ctype!r}'))
        for required in ('Project-Id-Version', 'Content-Type',
                         'Content-Transfer-Encoding'):
            if required not in fields:
                issues.append(Issue('WARN', header.line,
                    f'header field {required!r} missing'))

    if not entries:
        issues.append(Issue('ERROR', 0, 'file contains no translation entries'))

    target_lang = _header_fields(header).get('Language', '')
    seen: dict[tuple[str | None, str], int] = {}
    n_wrapped = 0
    for e in entries:
        key = (e.msgctxt, e.msgid)
        if key in seen:
            issues.append(Issue('ERROR', e.line,
                f'duplicate entry (msgctxt {e.msgctxt!r}), first at line '
                f'{seen[key]} - gettext/Unreal will reject or drop one'))
        else:
            seen[key] = e.line

        if e.msgctxt is None:
            issues.append(Issue('ERROR', e.line,
                'entry has no msgctxt - Unreal identifies strings by '
                'msgctxt (Namespace,Key); this entry will not map back'))
        elif not UNREAL_CTXT_RE.match(e.msgctxt):
            issues.append(Issue('WARN', e.line,
                f'msgctxt {e.msgctxt!r} is not in Unreal "Namespace,Key" form'))

        if e.msgid_lines > 1 or e.msgstr_lines > 1:
            n_wrapped += 1
            if n_wrapped <= 5:
                issues.append(Issue('ERROR', e.line,
                    'string wrapped across multiple lines - Unreal exports '
                    'one line per string; editor re-wrapping is the classic '
                    'cause of broken imports. Normalize with: '
                    'msgcat --no-wrap file.po -o fixed.po'))

        # literal "\n" token in source vs real newline in translation:
        # the original Unreal export mixes these too, so warn only
        if '\\n' in e.msgid.replace('\\\\', '') and '\n' in e.msgstr:
            issues.append(Issue('WARN', e.line,
                r'source uses a literal "\n" token but translation contains '
                'a real newline - verify this matches the original export style'))
        if e.msgstr.strip():
            # effective breaks = real newlines + literal "\n" tokens
            def breaks(s: str) -> int:
                return s.count('\n') + s.replace('\\\\', '').count('\\n')
            if breaks(e.msgid) != breaks(e.msgstr):
                issues.append(Issue('WARN', e.line,
                    f'line-break count differs: source has {breaks(e.msgid)}, '
                    f'translation has {breaks(e.msgstr)}'))
            src_ph = sorted(PLACEHOLDER_RE.findall(e.msgid))
            dst_ph = sorted(PLACEHOLDER_RE.findall(e.msgstr))
            if src_ph != dst_ph:
                issues.append(Issue('WARN', e.line,
                    f'placeholder mismatch: source {src_ph} vs translation {dst_ph}'))
        if e.fuzzy:
            issues.append(Issue('WARN', e.line,
                'entry is marked fuzzy - it will be ignored on import'))
        note = detect_review_note(e)
        if note:
            issues.append(Issue('ERROR', e.line,
                f'msgstr looks like a leaked reviewer/QA note, not a '
                f'translation ({note}): {e.msgstr[:50]!r}'))
        reason = classify_untranslated(e, target_lang)
        if reason == 'same_as_source':
            issues.append(Issue('WARN', e.line,
                'untranslated: msgstr is a verbatim copy of the source'))
        elif reason == 'source_leftover':
            issues.append(Issue('WARN', e.line,
                f'untranslated: msgstr still contains CJK text but target '
                f'language is {target_lang!r}: {e.msgstr[:40]!r}'))
    if n_wrapped > 5:
        issues.append(Issue('ERROR', 0,
            f'... {n_wrapped} entries total have wrapped multi-line strings'))
    return issues


# ---------------------------------------------------------------- function 2
def check_label(path: Path, team: str = 'Orbit8',
                max_age_days: int = 30) -> list[Issue]:
    """Header label check: revision date up to date + Orbit8 Lab branding."""
    issues: list[Issue] = []
    header, _entries, _parse_issues, _bom = parse_po(path)
    if header is None:
        return [Issue('ERROR', 0, 'no header entry - cannot check labels')]
    fields = _header_fields(header)

    def parse_date(value: str) -> datetime | None:
        for fmt in HEADER_DATE_FORMATS:
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=None)
            except ValueError:
                continue
        return None

    rev_raw = fields.get('PO-Revision-Date', '')
    rev = parse_date(rev_raw)
    if not rev_raw:
        issues.append(Issue('ERROR', header.line, 'PO-Revision-Date missing'))
    elif rev is None:
        issues.append(Issue('ERROR', header.line,
            f'PO-Revision-Date not parseable: {rev_raw!r}'))
    else:
        now = datetime.now()
        if rev > now + timedelta(days=1):
            issues.append(Issue('WARN', header.line,
                f'PO-Revision-Date {rev_raw!r} is in the future'))
        if rev < now - timedelta(days=max_age_days):
            issues.append(Issue('WARN', header.line,
                f'PO-Revision-Date {rev_raw!r} is older than '
                f'{max_age_days} days - label may be stale'))
        pot = parse_date(fields.get('POT-Creation-Date', ''))
        if pot and rev < pot:
            issues.append(Issue('ERROR', header.line,
                'PO-Revision-Date is earlier than POT-Creation-Date'))
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if mtime - rev > timedelta(days=1):
            issues.append(Issue('WARN', header.line,
                f'file was modified {mtime:%Y-%m-%d} but PO-Revision-Date '
                f'says {rev:%Y-%m-%d} - update the label when editing'))

    brand_fields = ('Language-Team', 'Last-Translator', 'X-Generator')
    norm = lambda s: re.sub(r'[\s_-]+', '', s).lower()
    carriers = [f for f in brand_fields if norm(team) in norm(fields.get(f, ''))]
    if carriers:
        issues.append(Issue('INFO', header.line,
            f'localization-source label {team!r} found in: {", ".join(carriers)}'))
    else:
        issues.append(Issue('ERROR', header.line,
            f'localization-source label {team!r} not found in any of '
            f'{brand_fields} - deliverable is unbranded'))
    if 'Last-Translator' not in fields:
        issues.append(Issue('WARN', header.line,
            'Last-Translator missing (msgfmt also warns about this)'))
    return issues


# ---------------------------------------------------------------- function 3
def summarize(path: Path, reference: Path | None = None) -> dict:
    """Brief summary: totals, translated/empty, and changes vs a reference PO."""
    header, entries, _issues, _bom = parse_po(path)
    fields = _header_fields(header)
    total = len(entries)
    target_lang = fields.get('Language', '')
    reasons = [classify_untranslated(e, target_lang) for e in entries]
    untranslated = sum(1 for r in reasons if r is not None)
    translated = total - untranslated
    summary: dict = {
        'file': str(path),
        'project': fields.get('Project-Id-Version', ''),
        'language': target_lang,
        'revision_date': fields.get('PO-Revision-Date', ''),
        'total_strings': total,
        'translated': translated,
        'untranslated': untranslated,
        'untranslated_empty': reasons.count('empty'),
        'untranslated_same_as_source': reasons.count('same_as_source'),
        'untranslated_source_leftover': reasons.count('source_leftover'),
        'suspected_review_notes': sum(
            1 for e in entries if detect_review_note(e)),
        'fuzzy': sum(1 for e in entries if e.fuzzy),
        'completion_pct': round(100 * translated / total, 1) if total else 0.0,
        'source_chars': sum(len(e.msgid) for e in entries),
        'translation_chars': sum(len(e.msgstr) for e in entries),
        'source_words': sum(word_count(e.msgid) for e in entries),
        'translation_words': sum(word_count(e.msgstr) for e in entries),
    }
    if reference is not None:
        _rh, ref_entries, _ri, _rb = parse_po(reference)
        ref = {e.msgctxt: e for e in ref_entries}
        cur = {e.msgctxt: e for e in entries}
        common = ref.keys() & cur.keys()
        summary['reference'] = str(reference)
        summary['keys_added'] = len(cur.keys() - ref.keys())
        summary['keys_removed'] = len(ref.keys() - cur.keys())
        summary['source_changed'] = sum(
            1 for k in common if ref[k].msgid != cur[k].msgid)
        summary['translations_modified'] = sum(
            1 for k in common
            if ref[k].msgstr != cur[k].msgstr and ref[k].msgstr.strip())
        summary['newly_translated'] = sum(
            1 for k in common
            if cur[k].msgstr.strip() and not ref[k].msgstr.strip())
        summary['translations_lost'] = sum(
            1 for k in common
            if ref[k].msgstr.strip() and not cur[k].msgstr.strip())
        summary['unchanged'] = sum(
            1 for k in common if ref[k].msgstr == cur[k].msgstr)

        # word counts per change bucket, in both languages; removed entries
        # only exist in the reference, so their counts come from there
        def bucket(es: list[Entry]) -> dict:
            return {
                'entries': len(es),
                'source_words': sum(word_count(e.msgid) for e in es),
                'translation_words': sum(word_count(e.msgstr) for e in es),
            }
        changed = [k for k in common
                   if ref[k].msgid != cur[k].msgid
                   or ref[k].msgstr != cur[k].msgstr]
        summary['word_counts'] = {
            # no original translation existed: new key, or ref msgstr empty
            'added': bucket([cur[k] for k in cur.keys() - ref.keys()]
                            + [cur[k] for k in changed
                               if not ref[k].msgstr.strip()]),
            # an existing original translation was edited
            'modified': bucket([cur[k] for k in changed
                                if ref[k].msgstr.strip()]),
            'untouched': bucket([cur[k] for k in common
                                 if ref[k].msgid == cur[k].msgid
                                 and ref[k].msgstr == cur[k].msgstr]),
            'removed': bucket([ref[k] for k in ref.keys() - cur.keys()]),
        }
    return summary


# ------------------------------------------------------------------ fix mode
def write_unreal_po(path: Path, out: Path) -> None:
    """Rewrite a PO in Unreal-native form: UTF-8 BOM, one physical line per
    string, line breaks inside strings escaped as \\r\\n."""
    header, entries, _issues, _bom = parse_po(path)

    def esc(s: str, crlf: bool = False) -> str:
        if crlf:  # normalize any newline style to CRLF, as Unreal exports do
            s = s.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
        return (s.replace('\\', '\\\\').replace('"', '\\"')
                 .replace('\t', '\\t').replace('\r', '\\r').replace('\n', '\\n'))

    lines: list[str] = []
    if header is not None:
        lines += header.comments
        lines.append('msgid ""')
        lines.append('msgstr ""')
        for hline in header.msgstr.rstrip('\n').split('\n'):
            lines.append(f'"{esc(hline)}\\n"')
        lines.append('')
    for e in entries:
        lines += e.comments
        if e.msgctxt is not None:
            lines.append(f'msgctxt "{esc(e.msgctxt)}"')
        lines.append(f'msgid "{esc(e.msgid)}"')
        lines.append(f'msgstr "{esc(e.msgstr, crlf=True)}"')
        lines.append('')
    out.write_bytes(b'\xef\xbb\xbf' + '\n'.join(lines).encode('utf-8'))


# ---------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('po_file', type=Path, help='PO file to check')
    ap.add_argument('--ref', type=Path, default=None,
                    help='original/reference PO to diff against for the summary')
    ap.add_argument('--team', default='Orbit8',
                    help='localization-source label expected in the header')
    ap.add_argument('--max-age-days', type=int, default=30,
                    help='PO-Revision-Date older than this is flagged stale')
    ap.add_argument('--no-bom-check', action='store_true',
                    help='do not require a UTF-8 BOM')
    ap.add_argument('--json', type=Path, default=None,
                    help='also write the full report as JSON')
    ap.add_argument('--fix', type=Path, default=None, metavar='OUT.po',
                    help='write a normalized Unreal-style copy (BOM, '
                         'single-line strings, CRLF line breaks) to OUT.po')
    args = ap.parse_args(argv)

    if not args.po_file.exists():
        print(f'error: {args.po_file} not found', file=sys.stderr)
        return 2

    fmt_issues = check_format(args.po_file, expect_bom=not args.no_bom_check)
    label_issues = check_label(args.po_file, team=args.team,
                               max_age_days=args.max_age_days)
    summary = summarize(args.po_file, reference=args.ref)

    def show(title: str, issues: list[Issue], per_kind: int = 5) -> None:
        print(f'\n== {title} ==')
        if not issues:
            print('  OK - no issues')
        shown: dict[tuple[str, str], int] = {}
        for iss in issues:
            kind = (iss.severity, iss.message[:40])
            shown[kind] = shown.get(kind, 0) + 1
            if shown[kind] <= per_kind:
                print(f'  {iss}')
        for (sev, msg), count in shown.items():
            if count > per_kind:
                print(f'  [{sev}] ... {count - per_kind} more like: {msg}...')

    print(f'PO sanity check: {args.po_file.name}')
    show('1. Format / completeness', fmt_issues)
    show('2. Label / timing', label_issues)
    print('\n== 3. Summary ==')
    for k, v in summary.items():
        if k == 'word_counts':
            continue
        print(f'  {k:22} {v}')
    wc = summary.get('word_counts')
    if wc:
        lang = summary.get('language') or 'target'
        print(f'\n== 4. Word counts vs reference (source / {lang}) ==')
        print(f'  {"category":<10} {"entries":>8} {"src words":>10} {"tgt words":>10}')
        totals = {'entries': 0, 'source_words': 0, 'translation_words': 0}
        for cat in ('added', 'modified', 'untouched', 'removed'):
            b = wc[cat]
            print(f'  {cat:<10} {b["entries"]:>8} '
                  f'{b["source_words"]:>10} {b["translation_words"]:>10}')
            for key in totals:
                totals[key] += b[key]
        print(f'  {"total":<10} {totals["entries"]:>8} '
              f'{totals["source_words"]:>10} {totals["translation_words"]:>10}')

    all_issues = fmt_issues + label_issues
    n_err = sum(1 for i in all_issues if i.severity == 'ERROR')
    n_warn = sum(1 for i in all_issues if i.severity == 'WARN')
    verdict = 'FAIL - do not deliver' if n_err else (
        'PASS with warnings' if n_warn else 'PASS')
    print(f'\nResult: {verdict}  ({n_err} errors, {n_warn} warnings)')

    if args.json:
        report = {
            'file': str(args.po_file),
            'verdict': verdict,
            'errors': n_err,
            'warnings': n_warn,
            'format_issues': [vars(i) for i in fmt_issues],
            'label_issues': [vars(i) for i in label_issues],
            'summary': summary,
        }
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                             encoding='utf-8')
        print(f'JSON report written to {args.json}')
    if args.fix:
        write_unreal_po(args.po_file, args.fix)
        print(f'Normalized Unreal-style PO written to {args.fix}')
    return 2 if n_err else (1 if n_warn else 0)


if __name__ == '__main__':
    sys.exit(main())
