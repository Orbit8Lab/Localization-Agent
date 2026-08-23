"""Sandbox containment, adapter-writer retry loop, and controller
integration (unknown format ingested via generated adapter, then reused)."""
import json
from pathlib import Path

import pytest

from orbit8.codegen import (AdapterRecord, generate_adapter, run_adapter,
                            validate_stdout)
from orbit8.controller import Job
from orbit8.sandbox import run_sandboxed
from orbit8.schemas import IntakeBrief, SourceBatch

# A correct stdlib-only adapter for a "key<TAB>text" format.
GOOD_TSV_ADAPTER = """\
import json, sys
rows = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = line.rstrip("\\n")
    if not line or "\\t" not in line:
        continue
    key, text = line.split("\\t", 1)
    if text.strip():
        rows.append({"key": key, "text": text})
print(json.dumps(rows, ensure_ascii=False))
"""

BROKEN_ADAPTER = "import sys\nraise RuntimeError('boom')\n"


class ScriptedProvider:
    """Returns queued completions in order; records prompts."""
    name = "scripted"
    model = "test"
    tokens_spent = 0.0

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        self.prompts.append((system, user))
        return self.outputs.pop(0)


# ---------------------------------------------------------------- sandbox

def test_sandbox_runs_and_captures_stdout(tmp_path: Path):
    data = tmp_path / "in.tsv"
    data.write_text("K1\t你好\nK2\t再见\n", encoding="utf-8")
    result = run_sandboxed(GOOD_TSV_ADAPTER, data)
    assert result.ok
    assert json.loads(result.stdout)[0] == {"key": "K1", "text": "你好"}


def test_sandbox_timeout(tmp_path: Path):
    data = tmp_path / "in.txt"
    data.write_text("x", encoding="utf-8")
    result = run_sandboxed("while True: pass", data, timeout=1.0)
    assert result.timed_out and not result.ok


def test_sandbox_env_is_empty(tmp_path: Path):
    data = tmp_path / "in.txt"
    data.write_text("x", encoding="utf-8")
    result = run_sandboxed(
        "import os, json; print(json.dumps(dict(os.environ)))", data)
    leaked = {k: v for k, v in json.loads(result.stdout).items()
              if k not in ("LC_CTYPE", "__CF_USER_TEXT_ENCODING")}
    assert leaked == {}          # no API keys, no HOME, no proxies


def test_sandbox_side_effects_discarded(tmp_path: Path):
    data = tmp_path / "in.txt"
    data.write_text("x", encoding="utf-8")
    result = run_sandboxed(
        "open('evil.txt', 'w').write('x'); print('[]')", data)
    assert result.returncode == 0
    assert not (tmp_path / "evil.txt").exists()   # wrote only in scratch dir


# ------------------------------------------------------------- validation

def test_validate_rejects_bad_shapes():
    with pytest.raises(ValueError):
        validate_stdout("not json", "f")
    with pytest.raises(ValueError):
        validate_stdout("[]", "f")
    with pytest.raises(ValueError):
        validate_stdout('[{"key": "a"}]', "f")
    with pytest.raises(ValueError):
        validate_stdout('[{"key":"a","text":"x"},{"key":"a","text":"y"}]', "f")
    records = validate_stdout(
        '[{"key":"a","text":"x"},{"key":"b","text":"  "}]', "f")
    assert [r.key for r in records] == ["a"]      # blank text skipped


# ---------------------------------------------------------- codegen loop

def test_generate_adapter_retries_then_succeeds(tmp_path: Path):
    data = tmp_path / "game.tsv"
    data.write_text("K1\t你好\nK2\t再见\n", encoding="utf-8")
    provider = ScriptedProvider([BROKEN_ADAPTER,
                                 f"```python\n{GOOD_TSV_ADAPTER}```"])
    record, records, fingerprint = generate_adapter(provider, data)
    assert record.attempts == 2
    assert [r.key for r in records] == ["K1", "K2"]
    assert fingerprint.startswith("scripted/test#")
    # the retry prompt carried the previous error back to the agent
    assert "FAILED" in provider.prompts[1][1]
    assert "boom" in provider.prompts[1][1]


def test_generate_adapter_hard_fails(tmp_path: Path):
    data = tmp_path / "game.tsv"
    data.write_text("K1\tx\n", encoding="utf-8")
    provider = ScriptedProvider([BROKEN_ADAPTER] * 3)
    with pytest.raises(RuntimeError, match="3 attempts"):
        generate_adapter(provider, data)


# ------------------------------------------------- controller integration

def test_ingest_via_generated_adapter_and_reuse(tmp_path: Path):
    source = tmp_path / "strings.tsv"
    source.write_text("UI_START\t开始游戏\nDLG_A\t你好，旅人。\n",
                      encoding="utf-8")
    intake = IntakeBrief(game="G", source_lang="zh", target_locales=["ko"])
    job = Job.init(tmp_path / "jobs", "tsv-job", intake=intake,
                   source_files=[str(source)])
    job.next_step(dry_run=True)                     # INTAKE (market stub)
    job.approve("G0", by="t")

    provider = ScriptedProvider([GOOD_TSV_ADAPTER])
    job.next_step(lambda locale: provider)          # INGEST via adapter
    batch = job.store.read(1, "strings", SourceBatch)
    assert {r.key for r in batch.records} == {"UI_START", "DLG_A"}
    stored = job.store.read(1, "adapter_tsv", AdapterRecord)
    assert stored.record_count == 2                 # auditable artifact

    # reuse path: stored script re-runs deterministically, zero LLM calls
    assert run_adapter(stored.script, source)[0].key == "UI_START"
    assert provider.outputs == []                   # exactly one generation
