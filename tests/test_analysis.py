"""Corpus analysis: word-count rules (latin + CJK, placeholders excluded),
dedup bases, label reuse, and the story/instruction rollup."""
from pathlib import Path

from orbit8.analysis import (analyze_corpus, count_words,
                             labels_from_run_dbs)
from orbit8.memory import RunDB
from orbit8.schemas import Domain, SourceString, UniqueString


def test_count_words_latin_cjk_placeholders():
    assert count_words("Press any key") == 3
    assert count_words("老国王低声说") == 6            # CJK: 1 char = 1 word
    assert count_words("Gain {0} points [Attack]") == 2   # placeholders out
    assert count_words("Level 3 完成") == 4           # mixed: 2 latin + 2 cjk


def test_analyze_rollup_and_bases():
    records = [
        SourceString(key="A", text="The king spoke slowly."),
        SourceString(key="A2", text="The king spoke slowly."),  # dup
        SourceString(key="B", text="Apply Settings"),
        SourceString(key="C", text="Save failed. Retry?"),
        SourceString(key="D", text="Misty Forest"),
        SourceString(key="E", text="Unknown thing"),
    ]
    labels = {"The king spoke slowly.": "dialogue",
              "Apply Settings": "ui",
              "Save failed. Retry?": "system",
              "Misty Forest": "map"}
    report = analyze_corpus(records, labels=labels)
    assert report.total_strings == 6
    assert report.unique_strings == 5
    assert report.words_all_records - report.words_unique == 4  # the dup
    assert report.story_lines == 1
    assert report.instructions == 2                  # ui + system
    assert report.other == 2                         # map + unlabeled
    assert report.unlabeled == 1
    assert report.by_domain == {"dialogue": 1, "map": 1, "system": 1,
                                "ui": 1}


def test_labels_harvested_from_run_dbs(tmp_path: Path):
    runs = tmp_path / "runs"
    runs.mkdir()
    db = RunDB(runs / "lqa-x.db")
    db.seed([UniqueString(uid="u0", text="The king spoke.", keys=["K"]),
             UniqueString(uid="u1", text="Menu", keys=["K2"])])
    db.label("u0", Domain.DIALOGUE, 1.0)
    db.label("u1", Domain.UI, 0.5)              # low-confidence fallback
    labels = labels_from_run_dbs(runs)
    assert labels == {"The king spoke.": "dialogue"}   # 0.5 not trusted
