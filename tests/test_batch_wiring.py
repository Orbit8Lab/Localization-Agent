"""Grouping wired through ingest → run DB → the two stages.

The unit tests in test_grouping.py prove the planners are correct. These
prove the pipeline actually USES them — a correct planner nothing calls
is worth nothing, and that gap would not show up as a failure anywhere.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from orbit8.ingest import dedup, run_ingest
from orbit8.memory import RunDB
from orbit8.schemas import (STORY_DOMAINS, STORY_DOMAIN_VALUES, Domain,
                            UniqueString)


def _tmpdb() -> Path:
    return Path(tempfile.mkdtemp()) / "run.db"


# ---------------------------------------------------------------- ingest

def test_dedup_carries_the_conversation_through():
    from orbit8.schemas import SourceString
    recs = [SourceString(key="dlg.ch01.s03.001", text="你好"),
            SourceString(key="dlg.ch01.s03.002", text="再见")]
    uniques = {u.text: u for u in dedup(recs)}
    assert uniques["你好"].group_id == "dlg.ch01.s03"
    assert uniques["你好"].seq == 1
    assert uniques["再见"].seq == 2


def test_a_repeated_line_keeps_its_first_position():
    """`是。` appears in two scenes. Dedup translates it once — correct for
    cost and for consistency — and it keeps the position of the exchange
    it first opens rather than drifting to the last one seen."""
    from orbit8.schemas import SourceString
    recs = [SourceString(key="dlg.s03.007", text="是。"),
            SourceString(key="dlg.s04.001", text="是。")]
    unique = dedup(recs)[0]
    assert unique.group_id == "dlg.s03" and unique.seq == 7
    assert unique.keys == ["dlg.s03.007", "dlg.s04.001"]


def test_ingest_reports_what_grouping_achieved(tmp_path: Path):
    import json
    src = tmp_path / "s.json"
    src.write_text(json.dumps({
        "dlg.s1.001": "一", "dlg.s1.002": "二", "ui.ok": "确定"}),
        encoding="utf-8")
    _records, _uniques, report = run_ingest([src])
    assert report.grouping["grouped_keys"] == 2
    assert report.grouping["groups"] >= 1


# ---------------------------------------------------------------- run DB

def test_run_db_round_trips_the_group():
    db = RunDB(_tmpdb())
    db.seed([UniqueString(uid="u0", text="你好", keys=["dlg.s1.001"],
                          group_id="dlg.s1", seq=1)])
    assert db.get("u0")["group_id"] == "dlg.s1"
    assert db.by_status("pending")[0]["seq"] == 1
    ref = db.refs("pending")[0]
    assert ref.group_id == "dlg.s1" and ref.seq == 1


def test_a_pre_grouping_job_migrates_without_losing_work():
    """Dropping the DB to gain grouping would throw away every
    translation already accepted in it."""
    path = _tmpdb()
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE segments (
        uid TEXT PRIMARY KEY, text TEXT NOT NULL, keys_json TEXT NOT NULL,
        context TEXT, domain TEXT NOT NULL DEFAULT 'ui',
        confidence REAL NOT NULL DEFAULT 1.0,
        status TEXT NOT NULL DEFAULT 'pending', target TEXT,
        resolution TEXT, findings_json TEXT NOT NULL DEFAULT '[]')""")
    conn.execute("INSERT INTO segments (uid, text, keys_json, status, target) "
                 "VALUES ('u0', '旧', '[\"k\"]', 'accepted', 'old')")
    conn.commit()
    conn.close()

    row = RunDB(path).get("u0")
    assert row["target"] == "old"          # the work survived
    assert row["group_id"] is None         # it just batches the old way


# ----------------------------------------------------------- the stages

def test_story_domains_have_one_definition():
    """Translate and LQA both split on this; two copies would drift the
    moment a domain is added."""
    assert STORY_DOMAINS == {Domain.DIALOGUE, Domain.MARKETING}
    assert STORY_DOMAIN_VALUES == {"dialogue", "marketing"}


def test_translate_stage_uses_conversation_batching():
    import inspect

    from orbit8.graphs import translate
    source = inspect.getsource(translate.run_translate_stage)
    assert "plan_story_batches" in source
    assert "STORY_DOMAINS" in source


def test_lqa_uses_both_axes():
    import inspect

    from orbit8.graphs import lqa
    source = inspect.getsource(lqa.build_lqa_graph)
    assert "plan_story_batches" in source, "story is not conversation-batched"
    assert "plan_similarity_batches" in source, "strings are not clustered"


def test_translate_only_claims_a_conversation_when_it_has_one():
    """A lone leftover line framed as 'consecutive lines of one
    conversation' is a lie the model will try to honour."""
    import inspect

    from orbit8.graphs import translate
    source = inspect.getsource(translate.build_translate_graph)
    assert "len(groups) == 1" in source
    assert "len(items) > 1" in source


def test_the_conversation_prompt_is_opt_in():
    """Default off: UI batches are not exchanges, and telling the model
    they are would invent continuity that is not there."""
    import inspect

    from orbit8 import agents
    sig = inspect.signature(agents.translate_batch)
    assert sig.parameters["conversation"].default is False
