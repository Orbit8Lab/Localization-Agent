"""Source-side template grouping and typed difference detection.

Spec: docs/skills/source-grouping.md.

The thing under test is a CLASSIFICATION, not a score. Every case here
pins a distinction the tool exists to preserve — and each would fail
silently: a collapsed numeric variant still produces fluent output, and a
case difference reported as "lexical" sends a reviewer hunting for a
terminology decision that was never made.
"""
from __future__ import annotations

import pytest

from orbit8.templates import (CLASS_WEIGHT, DiffClass, SlotType,
                              build_families, classify_pair, diff_family,
                              ngrams, normalize, precision_by_class,
                              profile_corpus, sample_for_review, script_of,
                              similarity, templatize)


# ------------------------------------------------------- normalization

def test_nfkc_runs_before_anything_else():
    """Full-width and half-width forms are otherwise distinct characters,
    which silently fragments Japanese families — `ＨＰ` would never meet
    `HP`."""
    assert normalize("ＨＰ").text == "HP"
    assert normalize("ＨＰ").nfkc_changed is True


def test_normalization_is_recorded_not_destructive():
    """Casing and spacing differences are themselves findings; a pipeline
    that folded them away could not report them."""
    result = normalize("  Start   Game  ")
    assert result.text == "Start Game"
    assert result.raw == "  Start   Game  "      # recoverable
    assert result.whitespace_changed is True


def test_case_is_never_folded_by_normalization():
    assert normalize("Start Game").text == "Start Game"


# ---------------------------------------------------------- templating

def test_numeric_variants_share_a_template():
    """D1, the decision the whole design rests on: one family (so they
    batch and translate consistently), two instances (so neither is
    silently collapsed)."""
    a, b = templatize("+10 HP"), templatize("+20 HP")
    assert a.template == b.template == "<NUM> HP"
    assert a.slots[0].value == "+10" and b.slots[0].value == "+20"


@pytest.mark.parametrize("text,expected,kind", [
    ("{0} gold", "<VAR> gold", SlotType.VAR),
    ("%s gold", "<VAR> gold", SlotType.VAR),
    ("<color=#FF0000>x</color>", "<TAG>x<TAG>", SlotType.TAG),
    ("see https://a.io/b now", "see <URL> now", SlotType.URL),
    ("+10 HP", "<NUM> HP", SlotType.NUM),
])
def test_slot_types(text, expected, kind):
    result = templatize(text)
    assert result.template == expected
    assert result.slots[0].type is kind


def test_templating_reuses_the_gate_placeholder_patterns():
    """Two placeholder inventories that drift would mean the templater and
    the T1 gate disagree about what a placeholder is — and the gate is the
    one that blocks a release."""
    from orbit8 import gate_checks, templates
    declared = {p for _t, p in templates._SLOT_PATTERNS}
    assert set(gate_checks.PLACEHOLDER_PATTERNS) <= declared
    assert gate_checks.MARKUP_PATTERN in declared


def test_cjk_needs_no_tokenizer():
    """D3: `攻撃力が上がった` and `Attack power increased` run through
    identical code — no segmenter, no per-language dependency."""
    assert templatize("攻撃力が上がった").template == "攻撃力が上がった"
    assert script_of("攻撃力が上がった") == "ja"
    assert script_of("攻击力") == "zh"
    assert script_of("Attack") == "latin"


def test_kana_decides_japanese_over_chinese():
    """Han-only ⇒ Chinese; any kana ⇒ Japanese. Stdlib script ranges
    fully resolve the split this pipeline actually needs."""
    assert script_of("回復する") == "ja"
    assert script_of("回复") == "zh"


# ------------------------------------------------ difference taxonomy

@pytest.mark.parametrize("a,b,expected", [
    ("确定", "确定", DiffClass.EXACT_DUP),
    ("Start Game", "Start game", DiffClass.CASE),
    ("Are you sure?", "Are you sure ?", DiffClass.FORMAT),
    ("{0} gold", "{1} gold", DiffClass.VAR),
    ("%s gold", "%d gold", DiffClass.VAR),
    ("+10 HP", "+20 HP", DiffClass.NUMERIC),
    ("Card", "Cards", DiffClass.INFLECTION),
    ("Karte", "Karten", DiffClass.INFLECTION),
    ("Sword", "Blade", DiffClass.LEXICAL),
])
def test_the_taxonomy(a, b, expected):
    assert classify_pair(a, b) is expected


def test_placeholder_index_is_not_a_numeric_change():
    """`{0}` / `{1}` differ in WHICH argument is substituted; reporting it
    as a value change sends the reviewer after the wrong thing. The
    tokenizer must hold placeholders together rather than splitting `{0}`
    into `0`."""
    assert classify_pair("{0} gold", "{1} gold") is DiffClass.VAR


def test_cjk_numeric_variants_are_numeric_not_lexical():
    """The regression this exists for. `_tokens` keeps a CJK run whole —
    correct, there is no segmenter — so `恢复5点生命` never yields the digit
    as its own token and the pair fell through to LEXICAL. A numeric
    variant reported as a terminology question sends a reviewer after a
    glossary decision that was never made.

    The fix compares STRUCTURE first: matching templates mean the strings
    differ only in slot values, and the slot TYPES give the class.
    """
    assert classify_pair("恢复5点生命", "恢复10点生命") is DiffClass.NUMERIC
    assert classify_pair("攻击力+1", "攻击力+2") is DiffClass.NUMERIC
    assert classify_pair("回復<b>5</b>", "回復<i>5</i>") is DiffClass.VAR


def test_mixed_divergence_fails_toward_review():
    """When token pairs disagree about their class, LEXICAL wins — the
    class that gets human eyes. Same rule as the domain classifier: fail
    expensive (design §8)."""
    assert classify_pair("Restore 5 Mana", "Recover 10 Health") is (
        DiffClass.LEXICAL)


def test_an_inserted_word_does_not_smear_the_diff():
    """Aligning with difflib rather than a naive zip: an insertion would
    otherwise shift every later token and report the whole tail."""
    assert classify_pair("Save Game", "Save The Game") is DiffClass.LEXICAL


def test_agreement_needs_an_explicit_inventory():
    """Out of scope unless the SOURCE is German or French (spec §13 Q1);
    it is never inferred."""
    articles = frozenset({"der", "die", "das"})
    assert classify_pair("der Held", "die Held") is DiffClass.LEXICAL
    assert classify_pair("der Held", "die Held",
                         closed_class=articles) is DiffClass.AGREEMENT


def test_lexical_outranks_cosmetic_in_the_review_queue():
    assert (CLASS_WEIGHT[DiffClass.LEXICAL]
            > CLASS_WEIGHT[DiffClass.INFLECTION]
            > CLASS_WEIGHT[DiffClass.CASE]
            > CLASS_WEIGHT[DiffClass.FORMAT])


# ------------------------------------------------------ family formation

def test_a_case_difference_is_reported_not_hidden():
    """Members must MEET to be compared, so the family key folds case —
    but the difference is then reported, because normalization kept the
    real text. Relying on n-gram near-merge does not work: `Start Game` /
    `Start game` score 0.54, and any threshold loose enough to merge them
    also merges unrelated strings."""
    family = diff_family(build_families(
        [("a", "Start Game"), ("b", "Start Game"), ("c", "Start game")])[0])
    assert family.size == 3
    assert family.canonical() == "Start Game"
    odd = [m for m in family.members if m.diffs]
    assert len(odd) == 1
    assert odd[0].id == "c"
    assert odd[0].diffs[0].cls is DiffClass.CASE


def test_numeric_instances_stay_distinct_inside_one_family():
    family = diff_family(build_families(
        [("a", "+10 HP"), ("b", "+20 HP"), ("c", "+10 HP")])[0])
    assert family.size == 3
    assert {m.diffs[0].cls for m in family.members if m.diffs} == {
        DiffClass.NUMERIC}
    assert len({m.normalized for m in family.members}) == 2   # not collapsed


def test_scripts_never_share_a_family():
    """An identical template across scripts is a coincidence, not a
    family; merging them would put zh and ja strings in one batch."""
    families = build_families([("a", "回复"), ("b", "回復する")])
    assert len(families) == 2


def test_unrelated_strings_stay_apart():
    assert len(build_families([("a", "Sword"), ("b", "Blade")])) == 2


def test_near_merge_does_not_chain_apart():
    """Merging against a SEED template, not running membership, stops
    "a≈b, b≈c, a≉c" from dragging unrelated families together."""
    families = build_families([("a", "aaaaaaaaaa"), ("b", "aaaaabbbbb"),
                               ("c", "bbbbbbbbbb")], threshold=0.6)
    for family in families:
        ids = {m.id for m in family.members}
        assert ids != {"a", "c"}


def test_family_formation_is_deterministic():
    """Batch layout rides on every model fingerprint, so two runs of one
    job must lay out identically."""
    records = [("c", "+30 HP"), ("a", "+10 HP"), ("b", "Start Game")]
    first = [[m.id for m in f.members] for f in build_families(records)]
    second = [[m.id for m in f.members]
              for f in build_families(list(reversed(records)))]
    assert first == second


def test_nothing_is_dropped():
    records = [("a", "+10 HP"), ("b", "Start Game"), ("c", "攻撃力"),
               ("d", "")]
    seen = {m.id for f in build_families(records) for m in f.members}
    assert seen == {"a", "b", "c", "d"}


# ---------------------------------------------------------- evaluation

def test_profile_reports_the_short_string_regime():
    """Several heuristics degrade on very short input, so the length
    distribution decides whether the defaults apply at all (spec §12)."""
    profile = profile_corpus([("a", "OK"), ("b", "Yes"), ("c", "No"),
                              ("d", "A rather longer sentence here")])
    assert profile.strings == 4
    assert profile.under_5_chars == 0.75
    assert profile.scripts["latin"] == 4


def test_profile_counts_differences_by_class():
    profile = profile_corpus([("a", "Start Game"), ("b", "Start game"),
                              ("c", "+10 HP"), ("d", "+20 HP")])
    assert profile.class_counts["case"] == 1
    assert profile.class_counts["numeric"] == 1


def test_review_sample_is_stratified_by_class():
    """A tool 95% precise on CASE and 30% on LEXICAL needs different
    thresholds per class, not one global cutoff — so the sample cannot be
    swamped by whichever class is most common."""
    records = [("n%d" % i, "+%d HP" % i) for i in range(30)]
    records += [("c1", "Start Game"), ("c2", "Start game")]
    families = [diff_family(f) for f in build_families(records)]
    rows = sample_for_review(families, per_class=5)
    counts = {}
    for row in rows:
        counts[row["class"]] = counts.get(row["class"], 0) + 1
    assert counts["case"] == 1
    assert counts["numeric"] == 5           # capped, not 29


def test_review_rows_start_unlabelled():
    """Nothing here is trusted until a human labels it (spec §12)."""
    families = [diff_family(f) for f in build_families(
        [("a", "Start Game"), ("b", "Start game")])]
    rows = sample_for_review(families)
    assert rows and all(r["is_real_inconsistency"] is None for r in rows)


def test_precision_is_measured_per_class():
    labelled = [{"class": "case", "is_real_inconsistency": True},
                {"class": "case", "is_real_inconsistency": True},
                {"class": "lexical", "is_real_inconsistency": False},
                {"class": "lexical", "is_real_inconsistency": True},
                {"class": "numeric", "is_real_inconsistency": None}]
    scored = precision_by_class(labelled)
    assert scored["case"]["precision"] == 1.0
    assert scored["lexical"]["precision"] == 0.5
    assert "numeric" not in scored          # unlabelled rows do not count


def test_sampling_is_reproducible():
    records = [("n%d" % i, "+%d HP" % i) for i in range(50)]
    families = [diff_family(f) for f in build_families(records)]
    first = sample_for_review(families, per_class=10, seed=7)
    second = sample_for_review(families, per_class=10, seed=7)
    assert [r["id"] for r in first] == [r["id"] for r in second]


# ---------------------------------------------------------- n-grams

def test_ngrams_work_on_two_character_cjk():
    """Unpadded, a 2-character CJK label has one trigram, and one gram
    cannot overlap with anything."""
    assert len(ngrams("确定")) > 1
    assert similarity("确定", "确定") == 1.0
