"""Authoritative string classification."""
import json
from pathlib import Path

from orbit8.classify import (Label, LabelStore, classify_batch,
                             classify_deterministic)
from orbit8.schemas import Domain


def test_location_beats_key_rule():
    # UE keys are opaque GUIDs; the #: path is the reliable signal
    label = classify_deterministic(
        ",AAAA1111", "/Game/UI/WDG_Menu.WDG_Menu_C:WidgetTree.Back.Text")
    assert label.domain == Domain.UI and label.source == "location"
    # a key convention still works when there is no path
    label = classify_deterministic("DLG_intro_01", "")
    assert label.domain == Domain.DIALOGUE and label.source == "key_rule"
    # location wins when both are present and disagree
    label = classify_deterministic("SYS_msg", "/Game/Dialogue/NPC.Line")
    assert label.domain == Domain.DIALOGUE and label.source == "location"


def test_fallback_is_untrusted_by_design():
    label = classify_deterministic(",DEADBEEF", "")
    assert label.source == "fallback"
    assert not label.trusted          # routes TO human review, not away


class FakeProvider:
    name, model = "fake", "test"

    def __init__(self):
        self.tokens_spent = 0.0
        self.batches = 0

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        self.batches += 1
        import re
        keys = re.findall(r"^### (\S+)$", user, flags=re.M)
        return json.dumps({"items": [
            {"key": k, "domain": "dialogue", "confidence": 0.9}
            for k in keys if k != "END"]})


def test_llm_only_sees_the_leftovers():
    provider = FakeProvider()
    labels = classify_batch([
        (",K1", "开始游戏", "/Game/UI/Menu.Text"),      # location decides
        ("DLG_a", "你好", ""),                          # key decides
        (",K3", "很久以前，这片土地…", ""),               # needs the LLM
    ], provider=provider)
    assert labels[",K1"].source == "location"
    assert labels["DLG_a"].source == "key_rule"
    assert labels[",K3"].source == "llm"
    assert labels[",K3"].domain == Domain.DIALOGUE
    assert provider.batches == 1        # one call, for one string


def test_llm_failure_leaves_fallback_not_crash():
    class Broken:
        name, model = "broken", "t"
        tokens_spent = 0.0

        def complete(self, *a, **k):
            raise RuntimeError("api down")

    labels = classify_batch([(",K1", "很久以前", "")], provider=Broken())
    assert labels[",K1"].source == "fallback"      # degraded, not lost


def test_store_persists_and_protects_human_corrections(tmp_path: Path):
    path = tmp_path / "labels.json"
    store = LabelStore(path)
    store.merge(classify_batch([
        (",K1", "开始", "/Game/UI/Menu.Text"),
        (",K2", "神秘的传说", ""),
    ]))
    # operator fixes the fallback
    store.correct(",K2", Domain.DIALOGUE, by="tian", note="lore line")
    store.save()

    reloaded = LabelStore(path)
    assert reloaded.labels[",K2"].source == "human"
    assert reloaded.domain_of(",K2") == Domain.DIALOGUE
    # a re-run must NOT overwrite the human ruling
    counts = reloaded.merge(classify_deterministic_map([",K2"]))
    assert counts["human_kept"] == 1
    assert reloaded.labels[",K2"].domain == Domain.DIALOGUE
    # untrusted labels are reportable work
    assert reloaded.domain_of(",K1") == Domain.UI
    saved = json.loads(path.read_text("utf-8"))
    assert saved["metadata"]["by_source"]["human"] == 1


def classify_deterministic_map(keys):
    return {k: classify_deterministic(k, "") for k in keys}
