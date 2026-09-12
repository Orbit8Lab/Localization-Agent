"""Source-side string grouping by TEMPLATE, and typed difference detection.

Spec: docs/skills/source-grouping.md (source of truth; keep code in sync).

The problem this replaces: a similarity score says two strings are 94%
alike. It does not say whether they differ by a number, a capital letter,
a plural suffix or a glossary term — and those four call for four
different actions. A threshold can only trade "group them" against "keep
them apart"; templating gives both.

    "+10 HP"  →  template "+<NUM> HP"   slots [10]
    "+20 HP"  →  template "+<NUM> HP"   slots [20]

One family (so they batch and translate consistently), two instances (so
neither is silently collapsed into the other).

Two decisions worth not relitigating (spec §14):

- **Character n-grams, never sentence embeddings.** Every distinction this
  module must PRESERVE — numeric value, case, plural suffix, single-token
  lexical swap — is a surface distinction, and sentence encoders are
  trained to erase exactly those. Measured on this repo's own cases: an
  encoder rates `恢复5点生命` / `恢复10点生命` at 0.775 while character
  bigrams rate them 1.000. The encoder is not malfunctioning; it is doing
  its job, and its job is the wrong one here.
- **No word tokenization.** `攻撃力が上がった` and `Attack power
  increased` run through identical code. A segmenter per language is a
  dependency and a failure mode per language, for worse near-duplicate
  detection.

Everything here is deterministic and stdlib-only.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from .gate_checks import MARKUP_PATTERN, PLACEHOLDER_PATTERNS, SCRIPT_RANGES

# ---------------------------------------------------------------- slots

class SlotType(str, Enum):
    NUM = "NUM"
    VAR = "VAR"
    TAG = "TAG"
    URL = "URL"
    PATH = "PATH"


# Order matters: URL before PATH (a URL contains slashes), and both before
# NUM (an address contains digits). VAR/TAG reuse the GATE's patterns
# rather than declaring their own — two placeholder inventories that drift
# would mean the templater and the T1 gate disagree about what a
# placeholder even is, and the gate is the one that blocks a release.
_SLOT_PATTERNS: List[Tuple[SlotType, str]] = (
    [(SlotType.URL, r"\b[a-z][a-z0-9+.-]*://[^\s<>\"]+")]
    + [(SlotType.TAG, MARKUP_PATTERN)]
    + [(SlotType.VAR, p) for p in PLACEHOLDER_PATTERNS]
    + [(SlotType.PATH, r"(?:[A-Za-z]:)?(?:/[\w.-]+){2,}/?")]
    + [(SlotType.NUM, r"[+-]?\d+(?:[.,]\d+)*%?")]
)
_SLOT_RE = re.compile(
    "|".join(f"(?P<{t.value}_{i}>{p})"
             for i, (t, p) in enumerate(_SLOT_PATTERNS)))


@dataclass(frozen=True)
class Slot:
    type: SlotType
    value: str
    position: int          # index among slots, left to right


def slot_token(kind: SlotType) -> str:
    return f"<{kind.value}>"


# --------------------------------------------------------- normalization

@dataclass(frozen=True)
class Normalized:
    """Normalization is RECORDED, never destructive.

    Casing and spacing differences are themselves findings we must report,
    so a pipeline that folded them away could not report them. Every
    transform here is reversible from ``raw``.
    """
    raw: str
    text: str                       # NFKC + whitespace-collapsed
    nfkc_changed: bool
    whitespace_changed: bool


def normalize(raw: str) -> Normalized:
    """NFKC first, then whitespace. Case is NOT folded.

    NFKC before anything else because full-width and half-width forms are
    otherwise distinct characters, which silently fragments Japanese
    families — `ＨＰ` and `HP` would never meet.
    """
    nfkc = unicodedata.normalize("NFKC", raw)
    collapsed = " ".join(nfkc.split())
    return Normalized(raw=raw, text=collapsed,
                      nfkc_changed=nfkc != raw,
                      whitespace_changed=collapsed != nfkc)


# ------------------------------------------------------------ bucketing

def script_of(text: str) -> str:
    """Unicode script bucket — stdlib, no model, no guessing.

    Kana present ⇒ Japanese; Han without kana ⇒ Chinese. That fully
    resolves the CJK/Latin split, which is the only split this pipeline
    actually needs (spec §13 Q1: the source is zh or ja, never German or
    French). Statistical LID would only be needed to separate languages
    WITHIN Latin script, and it is unreliable below ~20 characters —
    which describes most game UI text.
    """
    stripped = _SLOT_RE.sub(" ", text)
    found = {name for name, ranges in SCRIPT_RANGES.items()
             if re.search(f"[{ranges}]", stripped)}
    if "kana" in found:
        return "ja"
    if "hangul" in found:
        return "ko"
    if "han" in found:
        return "zh"
    if "cyrillic" in found:
        return "cyrillic"
    return "latin" if re.search(r"[A-Za-z]", stripped) else "other"


# ------------------------------------------------------------ templating

@dataclass(frozen=True)
class Template:
    template: str
    slots: Tuple[Slot, ...]

    @property
    def key(self) -> str:
        """What families hash on. The template alone: two strings whose
        only difference is a slot VALUE are one family."""
        return self.template


def templatize(text: str) -> Template:
    """Replace variable spans with typed slots, keeping the values.

    This is what makes the numbers requirement structural rather than a
    threshold to tune.
    """
    slots: List[Slot] = []
    out: List[str] = []
    last = 0
    for match in _SLOT_RE.finditer(text):
        kind = SlotType(match.lastgroup.rsplit("_", 1)[0])
        out.append(text[last:match.start()])
        out.append(slot_token(kind))
        slots.append(Slot(type=kind, value=match.group(),
                          position=len(slots)))
        last = match.end()
    out.append(text[last:])
    return Template(template="".join(out), slots=tuple(slots))


# ------------------------------------------------------- n-gram matching

def ngrams(text: str, n: int = 3) -> frozenset:
    """Character n-grams — the only similarity measure here (spec D3).

    Padded so short strings still produce grams: a 2-character CJK label
    has exactly one trigram without padding, and one gram cannot overlap
    with anything.
    """
    padded = f"\x02{text}\x03"
    if len(padded) <= n:
        return frozenset([padded])
    return frozenset(padded[i:i + n] for i in range(len(padded) - n + 1))


def similarity(a: str, b: str, n: int = 3) -> float:
    """Jaccard overlap of character n-grams, 0.0 – 1.0."""
    ga, gb = ngrams(a, n), ngrams(b, n)
    if not ga or not gb:
        return 1.0 if ga == gb else 0.0
    return len(ga & gb) / len(ga | gb)


# ----------------------------------------------------- difference classes

class DiffClass(str, Enum):
    """Spec §7. Ordered loosely by how much a reviewer should care.

    The CLASSIFICATION is the product. A reviewer acts on "differs only in
    case"; nobody can act on "0.94".
    """
    EXACT_DUP = "exact_dup"      # identical post-normalization
    CASE = "case"                # equal under casefold
    FORMAT = "format"            # equal after stripping punctuation
    VAR = "var"                  # differing tokens are placeholders
    NUMERIC = "numeric"          # differing tokens are numeric
    INFLECTION = "inflection"    # differing tokens share a long stem
    LEXICAL = "lexical"          # differing tokens unrelated
    AGREEMENT = "agreement"      # closed-class / agreement suffixes


# Weight per class for the review queue: a lexical swap is a terminology
# question, a format difference is a tidy-up. Used by ranking (stage 6).
CLASS_WEIGHT = {
    DiffClass.LEXICAL: 1.0, DiffClass.INFLECTION: 0.7,
    DiffClass.AGREEMENT: 0.7, DiffClass.NUMERIC: 0.5,
    DiffClass.VAR: 0.5, DiffClass.CASE: 0.3,
    DiffClass.FORMAT: 0.2, DiffClass.EXACT_DUP: 0.0,
}

# Divergence at the tail after a long shared prefix is morphological
# variation: Card/Cards 4/5, Karte/Karten 5/6, carte/cartes 5/6. Covers
# en/de/fr/es/it with zero language-specific code. zh and ja do not
# inflect for number, so their exclusion is correct, not a gap.
INFLECTION_PREFIX_RATIO = 0.7

_PUNCT = re.compile(r"[\s\.,!?;:'\"()\[\]{}…·、。！？：；「」『』〜~\-—–/\\]+")


def _fold_template(template: str) -> str:
    """The bucketing key: template with case and punctuation folded away.

    Differences these fold out are still REPORTED — they are the CASE and
    FORMAT classes — but they must not keep two members out of the same
    family, because a difference is only visible between members that met.
    """
    return _PUNCT.sub("", template).casefold()
_NUMERIC_TOKEN = re.compile(r"^[+-]?\d+(?:[.,]\d+)*%?$")


@dataclass(frozen=True)
class Difference:
    cls: DiffClass
    span: Tuple[int, int]        # token range in the member
    self_value: str
    other_value: str
    confidence: float = 1.0


def _tokens(text: str) -> List[str]:
    """Split on punctuation and whitespace, keeping the pieces.

    Placeholders and markup are held together FIRST. Splitting on
    punctuation would tear `{0}` into `0` and then classify `{0} gold` /
    `{1} gold` as NUMERIC — a placeholder-index difference reported as a
    value change, which is a different finding with a different action.

    Otherwise NOT a linguistic tokenizer: a CJK run stays one token. That
    is deliberate — the classifier only needs to locate WHERE two
    near-identical strings diverge, and for CJK the whole-run comparison
    plus the prefix heuristic does that without a segmenter.
    """
    out: List[str] = []
    last = 0
    for match in _SLOT_RE.finditer(text):
        kind = SlotType(match.lastgroup.rsplit("_", 1)[0])
        if kind is SlotType.NUM:      # a bare number IS a plain token
            continue
        out += [t for t in _PUNCT.split(text[last:match.start()]) if t]
        out.append(match.group())
        last = match.end()
    out += [t for t in _PUNCT.split(text[last:]) if t]
    return out


def _strip_punct(text: str) -> str:
    return _PUNCT.sub("", text)


def _prefix_ratio(a: str, b: str) -> float:
    shared = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        shared += 1
    return shared / max(len(a), len(b), 1)


def classify_pair(a: str, b: str, *,
                  closed_class: Optional[frozenset] = None) -> DiffClass:
    """Classify how two whole strings differ. Cheapest tests first."""
    if a == b:
        return DiffClass.EXACT_DUP
    if a.casefold() == b.casefold():
        return DiffClass.CASE
    if _strip_punct(a) == _strip_punct(b):
        return DiffClass.FORMAT
    if _strip_punct(a).casefold() == _strip_punct(b).casefold():
        return DiffClass.CASE

    # Compare STRUCTURE before text. Templating has already located every
    # variable span, so two strings whose templates match differ only in
    # slot values — and their class follows from the slot TYPES.
    #
    # Without this, CJK is misclassified: `_tokens` keeps a CJK run whole
    # (correct — there is no segmenter), so `恢复5点生命` never yields the
    # digit as its own token and the pair falls through to LEXICAL. A
    # numeric variant reported as a terminology question sends a reviewer
    # after a glossary decision that was never made.
    ta, tb = templatize(a), templatize(b)
    if ta.template == tb.template and ta.slots and tb.slots:
        changed = [(x, y) for x, y in zip(ta.slots, tb.slots)
                   if x.value != y.value]
        if changed and all(x.type is SlotType.NUM for x, _y in changed):
            return DiffClass.NUMERIC
        if changed and all(x.type is not SlotType.NUM for x, _y in changed):
            return DiffClass.VAR

    return classify_tokens(_differing_tokens(a, b), closed_class=closed_class)


def _differing_tokens(a: str, b: str) -> List[Tuple[str, str]]:
    """The token pairs that actually diverge, via an alignment.

    difflib rather than a naive zip: an inserted word would otherwise
    shift every later token and report the whole tail as different.
    """
    ta, tb = _tokens(a), _tokens(b)
    pairs: List[Tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(a=ta, b=tb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        left, right = ta[i1:i2], tb[j1:j2]
        for k in range(max(len(left), len(right))):
            pairs.append((left[k] if k < len(left) else "",
                          right[k] if k < len(right) else ""))
    return pairs


def classify_tokens(pairs: Sequence[Tuple[str, str]], *,
                    closed_class: Optional[frozenset] = None) -> DiffClass:
    """Classify a set of diverging token pairs.

    Every pair must agree for a specific class to win; a mixed divergence
    falls through to LEXICAL, which is the class that gets human eyes.
    Failing toward MORE review rather than less is the same rule the
    domain classifier follows (design §8).
    """
    if not pairs:
        return DiffClass.EXACT_DUP
    slot_tokens = {slot_token(k) for k in SlotType}

    def both(fn) -> bool:
        return all(fn(x, y) for x, y in pairs)

    def _is_var(token: str) -> bool:
        if token in slot_tokens:      # already-templated form
            return True
        match = _SLOT_RE.fullmatch(token)
        return match is not None and SlotType(
            match.lastgroup.rsplit("_", 1)[0]) is not SlotType.NUM

    if both(lambda x, y: _is_var(x) and _is_var(y)):
        return DiffClass.VAR
    if both(lambda x, y: bool(_NUMERIC_TOKEN.match(x))
            and bool(_NUMERIC_TOKEN.match(y))):
        return DiffClass.NUMERIC
    if closed_class and both(lambda x, y: x.casefold() in closed_class
                             and y.casefold() in closed_class):
        return DiffClass.AGREEMENT
    if both(lambda x, y: bool(x) and bool(y)
            and _prefix_ratio(x, y) > INFLECTION_PREFIX_RATIO
            and x.casefold() != y.casefold()):
        return DiffClass.INFLECTION
    return DiffClass.LEXICAL


# ------------------------------------------------------ family formation

@dataclass
class Member:
    id: str
    raw: str
    normalized: str
    template: str
    slots: Tuple[Slot, ...]
    script: str
    diffs: List[Difference] = field(default_factory=list)


@dataclass
class Family:
    family_id: str
    template: str                    # the canonical (majority) template
    members: List[Member]
    templates: List[str] = field(default_factory=list)   # merged variants

    @property
    def size(self) -> int:
        return len(self.members)

    def canonical(self) -> str:
        """The form a standardization proposal rewrites TOWARD.

        Majority vote on the normalized text, ties broken by the
        lexicographically first — arbitrary but stable, and stability is
        what lets two runs of one job propose the same fix.
        """
        counts = Counter(m.normalized for m in self.members)
        top = max(counts.values())
        return sorted(t for t, n in counts.items() if n == top)[0]


# Below this, brute-force pairwise comparison is exact and fast enough
# that MinHash/LSH would be complexity without benefit (spec §13 Q2).
# Above it, near-merge is skipped rather than silently taking minutes —
# a loud limit beats a mysterious hang.
NEAR_MERGE_LIMIT = 50_000
NEAR_MERGE_THRESHOLD = 0.8


def build_families(records: Sequence[Tuple[str, str]], *,
                   near_merge: bool = True,
                   threshold: float = NEAR_MERGE_THRESHOLD) -> List[Family]:
    """Two passes (spec §5 stage 4).

    1. **Exact**: hash the template. `+10 HP` and `+20 HP` are one family
       because their TEMPLATE is identical — no threshold involved.
    2. **Near**: n-gram similarity across TEMPLATES (never raw strings),
       merging families that differ only by punctuation or one token.

    Comparing templates rather than raw strings is what keeps pass 2
    honest: the slot values are already factored out, so the comparison
    is about structure and cannot be dominated by a long number.
    """
    members: List[Member] = []
    for ident, raw in records:
        norm = normalize(raw)
        tpl = templatize(norm.text)
        members.append(Member(
            id=ident, raw=raw, normalized=norm.text,
            template=tpl.template, slots=tpl.slots,
            script=script_of(norm.text)))

    exact: Dict[Tuple[str, str], List[Member]] = {}
    for member in members:
        # Hash on the CASE- AND PUNCTUATION-FOLDED template. `Start Game`
        # and `Start game` are one family whose members differ by CASE —
        # that is the finding, and it is only reportable if they meet.
        #
        # Folding here is safe precisely because normalization is
        # recorded, not destructive: `member.normalized` still holds the
        # real text, so diff_family reports the case difference it would
        # otherwise have had no way to see. Relying on the near-merge pass
        # for this does not work — trigram Jaccard puts `Start Game` /
        # `Start game` at 0.54, so any threshold loose enough to merge
        # them also merges strings that are genuinely unrelated.
        #
        # Script is part of the key: an identical template across scripts
        # is a coincidence, not a family, and merging them would put zh
        # and ja strings in one batch.
        exact.setdefault(
            (member.script, _fold_template(member.template)), []
        ).append(member)

    groups: List[Tuple[str, List[str], List[Member]]] = []
    for (_script, _folded), mem in sorted(exact.items()):
        # Report the majority RAW template, not the folded key: the key is
        # a bucketing device, the template is what a human reads.
        tpl = Counter(m.template for m in mem).most_common(1)[0][0]
        groups.append((tpl, sorted({m.template for m in mem}), mem))

    if near_merge and len(members) <= NEAR_MERGE_LIMIT:
        groups = _merge_near(groups, threshold=threshold)

    families: List[Family] = []
    for index, (tpl, variants, mem) in enumerate(groups):
        families.append(Family(family_id=f"f{index:04d}", template=tpl,
                               members=sorted(mem, key=lambda m: m.id),
                               templates=sorted(variants)))
    return families


def _merge_near(groups, *, threshold: float):
    """Greedy merge against a SEED template, not running membership.

    Seed comparison is what stops "a≈b, b≈c, a≉c" from chaining unrelated
    families into one, and it is what makes the output reproducible: the
    result does not depend on the order members happened to arrive in.
    """
    merged: List[Tuple[str, List[str], List[Member]]] = []
    # Largest first, so the biggest family is the seed rather than
    # whichever singleton sorted first.
    for tpl, variants, mem in sorted(groups, key=lambda g: (-len(g[2]), g[0])):
        for i, (seed, seed_variants, seed_mem) in enumerate(merged):
            if similarity(seed, tpl) >= threshold:
                seed_mem.extend(mem)
                seed_variants.extend(variants)
                break
        else:
            merged.append((tpl, list(variants), list(mem)))
    return merged


def diff_family(family: Family, *,
                closed_class: Optional[frozenset] = None) -> Family:
    """Classify how every member diverges from the family canonical form.

    This is the half that makes the tool actionable: not "these 40 strings
    are similar" but "39 say 'Start Game' and one says 'Start game', and
    the difference is CASE."
    """
    canonical = family.canonical()
    for member in family.members:
        member.diffs = []
        if member.normalized == canonical:
            continue
        cls = classify_pair(canonical, member.normalized,
                            closed_class=closed_class)
        member.diffs.append(Difference(
            cls=cls, span=(0, len(member.normalized)),
            self_value=member.normalized, other_value=canonical))
    return family


# ------------------------------------------------------------ evaluation

@dataclass
class CorpusProfile:
    """What the corpus looks like BEFORE thresholds are chosen (spec §12).

    Several heuristics here degrade on very short input — the prefix ratio
    is meaningless on two characters — so the length distribution decides
    whether the defaults apply at all. Plotting it first is the spec's
    instruction, and a tool that skipped it would be tuned on assumptions.
    """
    strings: int
    scripts: Dict[str, int]
    length_p50: int
    length_p90: int
    under_5_chars: float             # fraction; high ⇒ retune
    families: int
    singletons: int
    largest_family: int
    grouped_fraction: float
    class_counts: Dict[str, int]


def _percentile(values: Sequence[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * pct)))
    return ordered[index]


def profile_corpus(records: Sequence[Tuple[str, str]], *,
                   closed_class: Optional[frozenset] = None) -> CorpusProfile:
    families = [diff_family(f, closed_class=closed_class)
                for f in build_families(records)]
    lengths = [len(normalize(raw).text) for _ident, raw in records]
    scripts: Counter = Counter()
    classes: Counter = Counter()
    for family in families:
        for member in family.members:
            scripts[member.script] += 1
            for diff in member.diffs:
                classes[diff.cls.value] += 1
    multi = [f for f in families if f.size > 1]
    return CorpusProfile(
        strings=len(records),
        scripts=dict(scripts),
        length_p50=_percentile(lengths, 0.5),
        length_p90=_percentile(lengths, 0.9),
        under_5_chars=round(
            sum(1 for n in lengths if n < 5) / max(len(lengths), 1), 4),
        families=len(families),
        singletons=sum(1 for f in families if f.size == 1),
        largest_family=max((f.size for f in families), default=0),
        grouped_fraction=round(
            sum(f.size for f in multi) / max(len(records), 1), 4),
        class_counts=dict(classes))


def sample_for_review(families: Sequence[Family], *, per_class: int = 40,
                      seed: int = 0) -> List[dict]:
    """Stratified sample for hand-labelling (spec §12).

    Stratified BY CLASS because precision differs per class — a tool 95%
    precise on CASE and 30% on LEXICAL needs different thresholds per
    class, not one global cutoff, and an unstratified sample would be
    swamped by whichever class happens to be most common.

    Deterministic given a seed, so a labelled sample stays comparable
    across runs.
    """
    import random

    by_class: Dict[str, List[dict]] = {}
    for family in families:
        canonical = family.canonical()
        for member in family.members:
            for diff in member.diffs:
                by_class.setdefault(diff.cls.value, []).append({
                    "family_id": family.family_id,
                    "template": family.template,
                    "family_size": family.size,
                    "id": member.id,
                    "class": diff.cls.value,
                    "self": diff.self_value,
                    "canonical": canonical,
                    # Left blank for the human. The whole point is that
                    # nothing here is trusted until it is labelled.
                    "is_real_inconsistency": None,
                })
    rng = random.Random(seed)
    out: List[dict] = []
    for cls in sorted(by_class):
        rows = sorted(by_class[cls], key=lambda r: (r["family_id"], r["id"]))
        out.extend(rows if len(rows) <= per_class
                   else rng.sample(rows, per_class))
    return out


def precision_by_class(labelled: Sequence[dict]) -> Dict[str, dict]:
    """Score a hand-labelled sample. Ship nothing on intuition (§12)."""
    out: Dict[str, dict] = {}
    for row in labelled:
        label = row.get("is_real_inconsistency")
        if label is None:
            continue
        bucket = out.setdefault(row["class"], {"labelled": 0, "true": 0})
        bucket["labelled"] += 1
        bucket["true"] += 1 if label else 0
    for cls, bucket in out.items():
        bucket["precision"] = round(
            bucket["true"] / bucket["labelled"], 3) if bucket["labelled"] else 0.0
    return out
