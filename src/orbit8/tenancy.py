"""Organization boundaries for file access.

`_confine` previously allowed exactly one thing: the project folder holding
the current job. That is both too narrow and too wide.

**Too narrow.** An agent working on one project cannot read a sibling
project of the same organization — so the accumulated glossaries, style
guides and prior decisions of the org are invisible, and every project
starts from nothing.

**Too wide.** The boundary is the *folder*, not the *client*. Two jobs
sharing a jobs root share a project folder, so a session on either can read
the other's received drops. If those jobs belong to different clients, that
is a confidentiality breach with no warning and no trace.

## The rule

The confidentiality boundary is the **organization** (`tenant_id`, already
carried on `IntakeBrief` and stored in `job.json`, and already the
namespace for skills per PLAN §5.4):

- **writes** stay inside the current project, always;
- **reads** may cross into a sibling project whose `tenant_id` matches;
- anything else is refused.

## Fail closed

A folder with no readable tenant marker is treated as **foreign**, not as
public. The alternative — defaulting an unmarked folder to accessible —
makes the safest-looking directory (one with no metadata at all) the most
permissive, and gets a client's assets read because someone forgot a field.

The same applies to a marker that cannot be parsed: an unreadable
`job.json` is not evidence of belonging.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# How deep to look for a job.json when identifying a project's owner. A
# project folder holds `jobs/<id>/job.json`, so two levels below the
# candidate root is the normal case; three covers a nested layout.
_MARKER_DEPTH = 3


class TenantError(PermissionError):
    """A path was refused because it belongs to another organization."""


@dataclass(frozen=True)
class Ownership:
    """Who owns a directory, and how confidently we know."""
    tenant_id: Optional[str]
    evidence: Optional[Path]        # the job.json that said so
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.tenant_id is not None


def _read_tenant(job_json: Path) -> Optional[str]:
    try:
        data = json.loads(job_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None                 # unreadable is not evidence of belonging
    tenant = data.get("tenant_id")
    return tenant if isinstance(tenant, str) and tenant else None


def owner_of(project_root: Path) -> Ownership:
    """The organization owning a project folder, from its jobs' job.json.

    A project with jobs from several tenants is reported as UNKNOWN rather
    than picking one: mixed ownership means the folder boundary no longer
    corresponds to an organization, and guessing would grant access on the
    strength of whichever job happened to sort first.
    """
    root = Path(project_root)
    if not root.is_dir():
        return Ownership(None, None, "not a directory")

    found: Dict[str, Path] = {}
    for depth in range(1, _MARKER_DEPTH + 1):
        for marker in root.glob("/".join(["*"] * depth) + "/job.json"):
            tenant = _read_tenant(marker)
            if tenant:
                found.setdefault(tenant, marker)
        if found:
            break

    if not found:
        return Ownership(None, None, "no job.json with a tenant_id")
    if len(found) > 1:
        return Ownership(
            None, None,
            f"mixed tenants in one folder: {', '.join(sorted(found))}")
    tenant, marker = next(iter(found.items()))
    return Ownership(tenant, marker)


def same_org(project_root: Path, tenant_id: str) -> bool:
    """True only when the folder demonstrably belongs to `tenant_id`."""
    owner = owner_of(project_root)
    return owner.known and owner.tenant_id == tenant_id


def sibling_projects(project_root: Path, tenant_id: str) -> List[Path]:
    """Projects beside `project_root` that belong to the same org.

    Used to tell an agent what it MAY look at, rather than leaving it to
    probe paths and collect refusals.
    """
    parent = Path(project_root).resolve().parent
    if not parent.is_dir():
        return []
    out = []
    for child in sorted(parent.iterdir()):
        if not child.is_dir() or child.resolve() == Path(
                project_root).resolve():
            continue
        if same_org(child, tenant_id):
            out.append(child)
    return out


def resolve_read(raw: str, *, project_root: Path, tenant_id: str,
                 org_root: Optional[Path] = None) -> Path:
    """Resolve a path for READING, allowing same-org siblings.

    `org_root` bounds how far a cross-project read may reach — by default
    the parent of the project folder, so "same organization" cannot be
    stretched into "anywhere on the filesystem" by a path that happens to
    carry a matching marker.
    """
    project_root = Path(project_root).resolve()
    ceiling = Path(org_root).resolve() if org_root else project_root.parent
    path = _resolve_relative(raw, project_root, strip_parents=False)

    if path.is_relative_to(project_root):
        return path                                  # own project: always

    if not path.is_relative_to(ceiling):
        raise TenantError(
            f"path is outside the organization workspace ({ceiling}): {path}")

    # Which sibling project does this path fall in?
    relative = path.relative_to(ceiling)
    if not relative.parts:
        raise TenantError(f"cannot read the organization root itself: {path}")
    sibling = ceiling / relative.parts[0]

    owner = owner_of(sibling)
    if not owner.known:
        raise TenantError(
            f"refusing to read {sibling.name}/: cannot confirm which "
            f"organization it belongs to ({owner.reason}). An unmarked "
            f"folder is treated as another org's, not as public.")
    if owner.tenant_id != tenant_id:
        raise TenantError(
            f"refusing to read {sibling.name}/: it belongs to "
            f"organization {owner.tenant_id!r}, this job is {tenant_id!r}.")
    return path


def resolve_write(raw: str, *, project_root: Path) -> Path:
    """Resolve a path for WRITING. Never leaves the current project.

    Cross-org reads are a feature; cross-project writes are not. Even
    within one organization, a job writing into another project's tree
    would modify assets nobody asked it to touch — and
    `30-deliverables/` is supposed to be immutable.
    """
    project_root = Path(project_root).resolve()
    path = _resolve_relative(raw, project_root, strip_parents=True)
    if not path.is_relative_to(project_root):
        raise TenantError(
            f"writes stay inside this project ({project_root}): {path}")
    return path


def _resolve_relative(raw: str, project_root: Path, *,
                      strip_parents: bool) -> Path:
    """Resolve a relative path against the project folder.

    `strip_parents` removes a leading "../" run. That tolerance exists
    because operators paste paths out of a jobs root out of habit, and it
    was harmless when EVERYTHING outside the project was forbidden — the
    path was going to be refused either way.

    It is not harmless now. "../sibling-project/" is a legitimate
    destination for a read, and stripping it rewrites the path back into
    the current project, where it quietly becomes "not a directory". So
    writes keep the tolerance (they cannot leave the project anyway) and
    reads do not (they are allowed to point at a sibling).
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        cleaned = raw.lstrip("/")
        if strip_parents:
            import re
            cleaned = re.sub(r"^(\.\./)+", "", cleaned)
        candidate = project_root / cleaned
    return candidate.resolve()


def mixed_tenant_warning(jobs_root: Path,
                         tenant_id: str) -> Optional[str]:
    """Warn when a jobs root already holds another organization's job.

    Co-locating two organizations under one jobs root defeats the
    boundary from the other direction: they then share a project folder,
    and the file tools treat that folder as home ground for both.
    """
    root = Path(jobs_root)
    if not root.is_dir():
        return None
    others: Dict[str, List[str]] = {}
    for child in sorted(root.iterdir()):
        marker = child / "job.json"
        if not marker.exists():
            continue
        tenant = _read_tenant(marker)
        if tenant and tenant != tenant_id:
            others.setdefault(tenant, []).append(child.name)
    if not others:
        return None
    listed = "; ".join(f"{name} ({', '.join(jobs)})"
                       for name, jobs in sorted(others.items()))
    return (
        f"WARNING: {root} already holds jobs for another organization: "
        f"{listed}.\n"
        f"  Jobs under one root share a project folder, and file tools "
        f"treat that folder as home ground — so a session on either job "
        f"can read the other's files.\n"
        f"  Use a separate project folder per organization:\n"
        f"    <workspace>/{tenant_id}/jobs/   and   "
        f"<workspace>/{sorted(others)[0]}/jobs/")
