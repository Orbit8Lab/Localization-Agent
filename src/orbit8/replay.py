"""Replay a past run against today's code, and diff the results.

The unit tests prove each piece behaves as designed on inputs a developer
invented. This proves the whole cascade still behaves the same on inputs a
CLIENT actually sent — 1,349 real strings with their real glossary, not a
fixture with three.

Two questions, and they need to be kept apart:

1. **Is today's pipeline deterministic?** Run the same inputs twice on the
   current code. Any difference is a bug in us, full stop.
2. **Did today's pipeline change relative to a past run?** Run today's
   code against a stored report. A difference here is EXPECTED whenever
   code changed — the value is in seeing *which* strings moved and why,
   not in demanding equality.

Question 2 comes with a trap worth stating plainly: a stored report
records its inputs but NOT the code version or the full config that
produced it. An old run made with a style guide, compared against a new
run made without one, shows a large diff that has nothing to do with the
code change under test. So a mismatch is a prompt to investigate, never a
test failure — which is why `compare_runs` reports and does not assert.

Only the deterministic tiers (T1/T2) are replayed. T3 calls a model, and
a model is not a fixture.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# What a finding is, reduced to the parts that must not drift. Deliberately
# NOT the message text: rewording a message is not a behaviour change, and
# a harness that failed on it would be abandoned within a week.
Fingerprint = Tuple[str, str, Tuple[Tuple[str, str, int], ...]]


def fingerprint_report(report) -> List[Fingerprint]:
    """(uid, source, sorted findings) per item — order-independent."""
    return sorted(
        (item.uid, item.source,
         tuple(sorted((f.finding.bug_type.value, f.finding.severity.value,
                       f.finding.tier) for f in item.findings)))
        for item in report.items)


def fingerprint_stored(path: Path) -> List[Fingerprint]:
    """The same shape, read from a stored baseline.

    Reads a baseline written by ``write_baseline``. It does NOT read a
    historical ``scan_report.json``: those store only COUNTS
    (``findings: 681``), and the run DB beside them persists an empty
    ``findings_json`` — the per-item detail was only ever written into the
    xlsx. So item-level comparison against pre-existing runs is not
    possible, and pretending otherwise by parsing whatever keys happen to
    be present would silently compare nothing. Ledger comparison IS
    possible against those runs, and `read_run` exposes it.

    Every lookup tolerates absence: baselines outlive the code that wrote
    them, and a KeyError on a two-month-old file would make the harness
    useless exactly when it is most wanted.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: List[Fingerprint] = []
    for item in data.get("items", []):
        findings = [(f.get("bug_type", ""), f.get("severity", ""),
                     int(f.get("tier", 0)))
                    for f in item.get("findings", [])]
        out.append((item.get("uid", ""), item.get("source", ""),
                    tuple(sorted(findings))))
    return sorted(out)


def write_baseline(report, path: Path, *, note: str = "") -> Path:
    """Freeze a report as a golden file for later comparison.

    This exists because the pipeline does not persist per-item findings
    anywhere durable, so a baseline has to be taken deliberately rather
    than recovered after the fact. Capture one BEFORE a refactor; compare
    after.

    The code version is recorded alongside, because the most misleading
    diff is one where the code moved and nobody wrote down that it did.
    """
    import subprocess

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).resolve().parent).stdout.strip()
    except Exception:                      # not a checkout, or no git
        commit = ""
    payload = {
        "note": note, "commit": commit,
        "locale": report.locale,
        "checked": report.checked,
        "flagged_strings": report.flagged_strings,
        "cascade_ledger": report.cascade_ledger,
        "items": [
            {"uid": item.uid, "source": item.source,
             "findings": [{"bug_type": f.finding.bug_type.value,
                           "severity": f.finding.severity.value,
                           "tier": f.finding.tier}
                          for f in item.findings]}
            for item in report.items],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


@dataclass
class RunInputs:
    """What a stored report says it was run on."""
    po: Path
    glossary: Optional[Path]
    report_path: Path
    ledger: Dict[str, int] = field(default_factory=dict)
    flagged: Optional[int] = None
    missing: List[str] = field(default_factory=list)

    @property
    def replayable(self) -> bool:
        return not self.missing


def read_run(report_path: Path) -> RunInputs:
    """Read a stored scan report and locate the inputs it names.

    A report that points at a moved or deleted .po is not replayable, and
    saying so is the whole job — silently scanning a *different* file
    would produce a diff that means nothing.
    """
    path = Path(report_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    po = Path(data["po"]) if data.get("po") else None
    glossary = Path(data["glossary"]) if data.get("glossary") else None
    missing: List[str] = []
    if po is None:
        missing.append("report records no source .po")
    elif not po.exists():
        missing.append(f"source .po is gone: {po}")
    if glossary is not None and not glossary.exists():
        missing.append(f"glossary is gone: {glossary}")
    return RunInputs(
        po=po, glossary=glossary, report_path=path,
        ledger=data.get("cascade_ledger", {}) or {},
        flagged=data.get("flagged_strings"), missing=missing)


@dataclass
class Comparison:
    """What moved between two runs. Counts AND identities — a net-zero
    change of "10 gained, 10 lost" is not "no change", and only the
    identities can tell them apart."""
    same: int
    only_old: List[Fingerprint]
    only_new: List[Fingerprint]
    changed: List[Tuple[str, tuple, tuple]]     # uid, old findings, new
    ledger_old: Dict[str, int]
    ledger_new: Dict[str, int]

    @property
    def identical(self) -> bool:
        return not (self.only_old or self.only_new or self.changed)

    def summary(self) -> str:
        lines = [
            f"unchanged items : {self.same}",
            f"only in old     : {len(self.only_old)}",
            f"only in new     : {len(self.only_new)}",
            f"findings changed: {len(self.changed)}",
        ]
        keys = sorted(set(self.ledger_old) | set(self.ledger_new))
        if keys:
            lines.append("")
            lines.append(f"{'ledger':18s} {'OLD':>7} {'NEW':>7}")
            for key in keys:
                old = self.ledger_old.get(key, "-")
                new = self.ledger_new.get(key, "-")
                mark = "" if old == new else "   <-- differs"
                lines.append(f"{key:18s} {old:>7} {new:>7}{mark}")
        return "\n".join(lines)


def compare(old: List[Fingerprint], new: List[Fingerprint], *,
            ledger_old: Optional[Dict[str, int]] = None,
            ledger_new: Optional[Dict[str, int]] = None) -> Comparison:
    old_map = {(uid, source): findings for uid, source, findings in old}
    new_map = {(uid, source): findings for uid, source, findings in new}
    same = 0
    changed: List[Tuple[str, tuple, tuple]] = []
    for key in old_map.keys() & new_map.keys():
        if old_map[key] == new_map[key]:
            same += 1
        else:
            changed.append((key[0], old_map[key], new_map[key]))
    return Comparison(
        same=same,
        only_old=sorted(f for f in old if (f[0], f[1]) not in new_map),
        only_new=sorted(f for f in new if (f[0], f[1]) not in old_map),
        changed=sorted(changed),
        ledger_old=dict(ledger_old or {}), ledger_new=dict(ledger_new or {}))


def replay(run: RunInputs, out_dir: Path, *, game: str = "",
           locale: str = "en", source_lang: str = "zh-CN"):
    """Re-run the DETERMINISTIC tiers over a past run's inputs.

    ``deterministic_only`` is not a limitation to apologise for: T1 and T2
    are where a regression is both possible and detectable. T3 asks a
    model, and two model calls disagreeing tells you nothing about your
    code.
    """
    from .po_scan import scan_po

    if not run.replayable:
        raise FileNotFoundError("; ".join(run.missing))
    return scan_po(run.po, run.glossary, Path(out_dir),
                   game=game, locale=locale, source_lang=source_lang,
                   deterministic_only=True, suggestions=False)


def find_runs(root: Path) -> List[Path]:
    """Every stored ``scan_report.json`` under ``root``, newest first."""
    reports = sorted(Path(root).rglob("scan_report.json"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    return reports
