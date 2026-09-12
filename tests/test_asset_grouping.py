"""Grouping from the UE asset path, not the game key.

project002's game keys are GUIDs (`,FDBF5602…`), so `derive_groups`
correctly reports them opaque and gives up. But every PO entry also
carries a `#:` reference — the untruncated UE asset path — and THAT is
richly structured:

    /Game/.../DT_CraftFormula_A.DT_CraftFormula_A.B_072.ItemName
    /Game/.../DT_CraftFormula_A.DT_CraftFormula_A.B_072.ItemCommit

Same entity `B_072`, two fields: a name and its description blurb. They
must agree, and reviewing them in one call is the item-level coherence
that template clustering cannot give — templates group by surface form,
and a name shares no surface form with its own description.

Measured on the real 1,349-string PO: 1,033 distinct entities, 92 with
2+ strings (191 rows), 57 carrying both a name and a description (114
rows). Subtitles additionally carry real sequence numbers
(`DT_Subtitles.0.Character`, `.1.Character`), so a conversation IS
reconstructible here.

A caution recorded deliberately: an earlier analysis run against the
xlsx test-kit — whose StringLocation column is truncated to
34 chars + "…" + 34 chars — concluded that paths were unusable for
84.7% of rows and that conversations could not be reconstructed. Both
were artifacts of reading the wrong file. The PO is the source of
truth and carries full paths; `read_po_entries` already captures them.
"""
from __future__ import annotations

from orbit8.asset_paths import (asset_entity, conversation_ref, field_role,
                                group_by_entity, subsystem)

CRAFT = ("/Game/Program/Config/GameConfig/SynthesisItemConfig/CraftConfig/"
         "DT_CraftFormula_A.DT_CraftFormula_A.B_072.ItemName")
CRAFT_D = CRAFT.replace("ItemName", "ItemCommit")
OBJ = ("/Game/Program/Config/GameConfig/ObjectiveSystem/"
       "DT_Obj_Definition.DT_Obj_Definition.BeginnerObj_19.DisplayName")
GUIDY = ("/Game/Program/Config/UIConfig/LobbyConfig/Wiki/DT_Wiki_A."
         "DT_Wiki_A.0.Conent_5_C6AE360C4AB5A40D2A3975A480A85D81")
SUB = ("/Game/Program/Config/UIConfig/Subtitles/"
       "DT_Subtitles.DT_Subtitles.2.Character_4_36C138684B60D6CE4F0BD2A96BA1B83A")


# ------------------------------------------------------- D1: entity id

def test_an_entity_is_the_asset_plus_row():
    """Row ids are NOT globally unique — `B_006` exists under
    CraftFormula_B and CraftFormula_Werewolf — so the asset must be part
    of the key or two unrelated items merge."""
    assert asset_entity(CRAFT) == asset_entity(CRAFT_D)
    other = CRAFT.replace("CraftFormula_A", "CraftFormula_B")
    assert asset_entity(other) != asset_entity(CRAFT)


def test_name_and_description_group_together():
    segs = [{"uid": "a", "location": CRAFT, "text": "致幻陷阱"},
            {"uid": "b", "location": CRAFT_D, "text": "赋予致幻魔法的地面陷阱"}]
    groups = group_by_entity(segs)
    assert len(groups) == 1
    assert {s["uid"] for s in groups[0]} == {"a", "b"}


def test_unrelated_entities_do_not_group():
    segs = [{"uid": "a", "location": CRAFT, "text": "x"},
            {"uid": "b", "location": OBJ, "text": "y"}]
    assert len(group_by_entity(segs)) == 2


def test_a_missing_location_is_its_own_group_not_a_crash():
    """Jobs seeded before locations were captured must still batch."""
    segs = [{"uid": "a", "location": "", "text": "x"},
            {"uid": "b", "text": "y"}]
    groups = group_by_entity(segs)
    assert sum(len(g) for g in groups) == 2


# ---------------------------------------------------- D2: field role

def test_the_column_guid_is_stripped_from_a_field_role():
    """UE appends a column GUID to struct fields. `Conent_5_C6AE…`
    is the SAME role as a bare `Conent`, and only 26 distinct GUIDs
    exist across 1,349 rows — the GUID identifies a column, not a
    string, so leaving it in shatters the role into noise."""
    assert field_role(GUIDY) == "Conent"
    assert field_role(CRAFT) == "ItemName"


def test_roles_are_stable_across_assets():
    a = CRAFT
    b = CRAFT.replace("CraftFormula_A", "CraftFormula_Magic").replace(
        "B_072", "A_001")
    assert field_role(a) == field_role(b) == "ItemName"


def test_a_role_survives_a_trailing_index():
    """`Text_1_5_F0A2…` is Text, not Text_1."""
    loc = ("/Game/x/DT_Subtitles.DT_Subtitles.0."
           "Text_1_5_F0A29B944D6049CE05729C9E184336C6")
    assert field_role(loc) == "Text"


# ------------------------------------------- D3: subsystem fallback

def test_subsystem_is_coarse_and_always_available():
    """The universal axis: it works even for rows whose only handle is
    a column GUID.

    Depth is 4 DIRECTORY segments. The asset filename is dropped first:
    keeping it made every data table its own subsystem — 265 groups of
    median 6 on a 1,349-row corpus, which is not a grouping. Depth 4
    measured 22 groups, median 11.
    """
    assert subsystem(GUIDY) == "Game/Program/Config/UIConfig"
    assert subsystem(CRAFT) == "Game/Program/Config/GameConfig"
    assert subsystem("") == ""


def test_subsystem_separates_ui_from_game_content():
    """It is a DOMAIN signal, not an entity one — UI chrome apart from
    gameplay config."""
    assert subsystem(GUIDY) != subsystem(CRAFT)


def test_subsystem_is_deliberately_coarser_than_the_asset():
    """Two different tables in one directory share a subsystem. That is
    the point: this axis answers "what part of the game", and the
    entity axis answers "which thing"."""
    craft_b = CRAFT.replace("CraftFormula_A", "CraftFormula_B")
    assert subsystem(craft_b) == subsystem(CRAFT)
    assert asset_entity(craft_b) != asset_entity(CRAFT)


# ------------------------------------------------- conversations

def test_a_subtitle_carries_a_sequence_number():
    """Real storyline grouping IS available here — the truncated
    analysis wrongly concluded it was not."""
    ref = conversation_ref(SUB)
    assert ref is not None
    group, seq = ref
    assert seq == 2
    assert "Subtitles" in group


def test_a_non_conversation_path_has_no_conversation_ref():
    assert conversation_ref(CRAFT) is None
