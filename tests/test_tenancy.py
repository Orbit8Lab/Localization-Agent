"""Organization boundaries for file access (tenancy.py).

The old rule was one folder: the project holding the current job. That is
simultaneously too narrow and too wide.

Too narrow — an agent could not read a sibling project of the SAME
organization, so the org's accumulated glossaries and prior decisions were
invisible and every project started from nothing.

Too wide — the boundary was the folder, not the client. Two jobs sharing a
jobs root share a project folder, so a session on either could read the
other's received drops. Across clients that is a confidentiality breach
with no warning and no trace.

The boundary is now the ORGANIZATION (`tenant_id`, already on IntakeBrief
and already the skills namespace per PLAN §5.4): reads may cross within an
org, writes never leave the project, and anything unproven is refused.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orbit8.tenancy import (Ownership, TenantError, mixed_tenant_warning,
                            owner_of, resolve_read, resolve_write, same_org,
                            sibling_projects)


def _project(root: Path, name: str, tenant: str, *jobs: str) -> Path:
    """A project folder owned by `tenant`, with one or more jobs in it."""
    project = root / name
    for job_id in (jobs or ("job-1",)):
        job_dir = project / "jobs" / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text(
            json.dumps({"job_id": job_id, "tenant_id": tenant}),
            encoding="utf-8")
    (project / "10-received").mkdir(exist_ok=True)
    (project / "10-received" / "drop.po").write_text("x", encoding="utf-8")
    return project


# ------------------------------------------------------------- ownership

def test_a_projects_owner_is_read_from_its_job(tmp_path):
    project = _project(tmp_path, "acme-game", "acme")
    assert owner_of(project).tenant_id == "acme"


def test_a_folder_with_no_job_has_no_known_owner(tmp_path):
    (tmp_path / "mystery").mkdir()
    assert not owner_of(tmp_path / "mystery").known


def test_an_unreadable_job_json_is_not_evidence(tmp_path):
    """A corrupt marker must not grant access — unreadable is not the same
    as belonging."""
    project = tmp_path / "broken" / "jobs" / "j"
    project.mkdir(parents=True)
    (project / "job.json").write_text("{not json", encoding="utf-8")
    assert not owner_of(tmp_path / "broken").known


def test_a_folder_holding_two_tenants_has_no_single_owner(tmp_path):
    """Mixed ownership means the folder no longer corresponds to an
    organization. Picking one would grant access on the strength of
    whichever job sorted first."""
    project = tmp_path / "shared"
    for job_id, tenant in (("a", "acme"), ("b", "rival")):
        job = project / "jobs" / job_id
        job.mkdir(parents=True)
        (job / "job.json").write_text(
            json.dumps({"tenant_id": tenant}), encoding="utf-8")

    owner = owner_of(project)
    assert not owner.known
    assert "mixed tenants" in owner.reason


def test_same_org_requires_proof(tmp_path):
    _project(tmp_path, "acme-game", "acme")
    (tmp_path / "unmarked").mkdir()
    assert same_org(tmp_path / "acme-game", "acme")
    assert not same_org(tmp_path / "acme-game", "rival")
    assert not same_org(tmp_path / "unmarked", "acme")


# ------------------------------------------------------------ reads

def test_the_own_project_is_always_readable(tmp_path):
    mine = _project(tmp_path, "mine", "acme")
    assert resolve_read(str(mine / "10-received" / "drop.po"),
                        project_root=mine, tenant_id="acme")


def test_a_same_org_sibling_is_readable(tmp_path):
    """The capability this adds: an agent can learn from the org's other
    projects instead of starting from nothing."""
    mine = _project(tmp_path, "acme-one", "acme")
    sibling = _project(tmp_path, "acme-two", "acme")
    assert resolve_read(str(sibling / "10-received" / "drop.po"),
                        project_root=mine, tenant_id="acme")


def test_another_organisation_is_refused(tmp_path):
    mine = _project(tmp_path, "acme-one", "acme")
    theirs = _project(tmp_path, "rival-game", "rival")
    with pytest.raises(TenantError) as excinfo:
        resolve_read(str(theirs / "10-received" / "drop.po"),
                     project_root=mine, tenant_id="acme")
    assert "rival" in str(excinfo.value)


def test_an_unmarked_folder_is_treated_as_foreign(tmp_path):
    """THE fail-closed rule. Defaulting an unmarked folder to accessible
    makes the folder with no metadata the most permissive one, and gets a
    client's assets read because someone forgot a field."""
    mine = _project(tmp_path, "acme-one", "acme")
    stray = tmp_path / "mystery"
    (stray / "10-received").mkdir(parents=True)
    (stray / "10-received" / "f.po").write_text("x", encoding="utf-8")

    with pytest.raises(TenantError) as excinfo:
        resolve_read(str(stray / "10-received" / "f.po"),
                     project_root=mine, tenant_id="acme")
    assert "cannot confirm" in str(excinfo.value)


def test_a_mixed_tenant_folder_is_refused(tmp_path):
    """If a sibling itself mixes organizations, it cannot be vouched for."""
    mine = _project(tmp_path, "acme-one", "acme")
    shared = tmp_path / "shared"
    for job_id, tenant in (("a", "acme"), ("b", "rival")):
        job = shared / "jobs" / job_id
        job.mkdir(parents=True)
        (job / "job.json").write_text(json.dumps({"tenant_id": tenant}),
                                      encoding="utf-8")
    (shared / "f.po").write_text("x", encoding="utf-8")

    with pytest.raises(TenantError):
        resolve_read(str(shared / "f.po"), project_root=mine,
                     tenant_id="acme")


def test_same_org_does_not_mean_anywhere_on_disk(tmp_path):
    """"Same organization" must not stretch into "any path that happens to
    carry a matching marker" — the org root bounds the reach."""
    mine = _project(tmp_path / "workspace", "acme-one", "acme")
    far = _project(tmp_path / "elsewhere", "acme-two", "acme")
    with pytest.raises(TenantError) as excinfo:
        resolve_read(str(far / "10-received" / "drop.po"),
                     project_root=mine, tenant_id="acme")
    assert "outside the organization workspace" in str(excinfo.value)


def test_the_organisation_root_itself_is_not_readable(tmp_path):
    mine = _project(tmp_path, "acme-one", "acme")
    with pytest.raises(TenantError):
        resolve_read(str(tmp_path), project_root=mine, tenant_id="acme")


# ------------------------------------------------------------ writes

def test_writes_stay_in_the_current_project(tmp_path):
    """Cross-org reads are a feature; cross-project writes are not. Even
    inside one organization, writing into another project modifies assets
    nobody asked this job to touch."""
    mine = _project(tmp_path, "acme-one", "acme")
    sibling = _project(tmp_path, "acme-two", "acme")
    with pytest.raises(TenantError):
        resolve_write(str(sibling / "out.json"), project_root=mine)


def test_a_write_inside_the_project_is_allowed(tmp_path):
    mine = _project(tmp_path, "acme-one", "acme")
    assert resolve_write(str(mine / "20-work" / "out.json"),
                         project_root=mine)


def test_a_relative_path_hangs_off_the_project(tmp_path):
    mine = _project(tmp_path, "acme-one", "acme")
    assert resolve_write("20-work/out.json", project_root=mine) == (
        mine / "20-work" / "out.json").resolve()


def test_a_dot_dot_path_reaches_a_sibling_on_read(tmp_path):
    """The natural way to name a sibling is "../other-project/". Stripping
    the "../" (a tolerance that made sense when everything outside the
    project was forbidden) rewrites the path back into the current project,
    where it becomes a confusing "not a directory" and the cross-org read
    feature is unreachable."""
    mine = _project(tmp_path, "acme-one", "acme")
    _project(tmp_path, "acme-two", "acme")
    resolved = resolve_read("../acme-two/10-received/drop.po",
                            project_root=mine, tenant_id="acme")
    assert resolved.parent.parent.name == "acme-two"


def test_a_dot_dot_path_is_still_refused_across_orgs(tmp_path):
    mine = _project(tmp_path, "acme-one", "acme")
    _project(tmp_path, "rival-game", "rival")
    with pytest.raises(TenantError):
        resolve_read("../rival-game/10-received/drop.po",
                     project_root=mine, tenant_id="acme")


def test_a_dot_dot_prefix_cannot_escape(tmp_path):
    """Operators paste "../10-received/..." out of habit; the prefix is
    tolerated but the confinement still applies."""
    mine = _project(tmp_path, "acme-one", "acme")
    resolved = resolve_write("../../../etc/passwd", project_root=mine)
    assert resolved.is_relative_to(mine)


# --------------------------------------------------------- discovery

def test_sibling_projects_lists_only_the_same_org(tmp_path):
    mine = _project(tmp_path, "acme-one", "acme")
    _project(tmp_path, "acme-two", "acme")
    _project(tmp_path, "rival-game", "rival")
    (tmp_path / "unmarked").mkdir()

    names = [p.name for p in sibling_projects(mine, "acme")]
    assert names == ["acme-two"]


# ------------------------------------------------- the co-location warning

def test_a_root_holding_another_org_warns(tmp_path):
    """The boundary defeated from the other direction: two orgs under one
    jobs root share a project folder, so each is home ground for both."""
    root = tmp_path / "jobs"
    job = root / "existing"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"tenant_id": "acme"}),
                                  encoding="utf-8")

    warning = mixed_tenant_warning(root, "rival")
    assert warning and "acme" in warning
    assert "separate project folder" in warning


def test_one_org_with_several_jobs_does_not_warn(tmp_path):
    """The common case — one client, several target locales — must stay
    quiet, or the warning becomes noise people learn to ignore."""
    root = tmp_path / "jobs"
    for job_id in ("game-ko", "game-ja"):
        job = root / job_id
        job.mkdir(parents=True)
        (job / "job.json").write_text(json.dumps({"tenant_id": "acme"}),
                                      encoding="utf-8")
    assert mixed_tenant_warning(root, "acme") is None


def test_an_empty_root_does_not_warn(tmp_path):
    assert mixed_tenant_warning(tmp_path / "nope", "acme") is None
