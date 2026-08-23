"""Measured zh→en display-width expansion in SHIPPED, professionally
localized games.

Ground truth for the UI-overflow budgets in gate_checks.width_budget: the
strings here survived a real studio's QA in a real UI, so the ratios they
exhibit are ones that DEMONSTRABLY FIT. That makes this an upper bound on
acceptable expansion, not merely a description of one project's style.

Direction note: these games are authored in English and translated INTO
Chinese, whereas this pipeline runs zh→en. Width ratio is measured
consistently as en_width / zh_width in both directions, so the numbers are
comparable — but the translation direction differs, and compression into
Chinese is not the mirror image of expansion out of it.
"""
from __future__ import annotations

import os
import re
import statistics
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Tuple

# Point this at a local folder of installed games with parallel
# en/zh localization files (override with $ORBIT8_GAME_CORPUS).
GAMES = Path(os.environ.get("ORBIT8_GAME_CORPUS", "./game-corpus"))


def display_width(text: str) -> int:
    total = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


# Placeholders and markup are not rendered as themselves; counting them
# would dilute the ratio with identical bytes on both sides.
_NOISE = re.compile(r"__[A-Z0-9_]+__|\[[^\]\n]*\]|<[^>\n]*>|%[sd]|\{[^}\n]*\}"
                    r"|\$[A-Za-z_]+\$")


def clean(text: str) -> str:
    return _NOISE.sub("", text).strip()


def read_cfg(path: Path) -> Dict[str, str]:
    """Factorio .cfg — INI-ish: [section] headers, key=value lines."""
    out, section = {}, ""
    for line in path.read_text("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            out[f"{section}.{key.strip()}"] = value.strip()
    return out


def read_el_xml(path: Path) -> Dict[str, str]:
    """Endless Legend — <LocalizationPair Name="%Key">text</LocalizationPair>"""
    out = {}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return out
    for node in root.iter():
        if node.tag.endswith("LocalizationPair"):
            name = node.get("Name")
            if name and node.text:
                out[name] = node.text
    return out


def pairs_for(game: str) -> List[Tuple[str, str, str]]:
    """(key, english, chinese) for one game."""
    rows: List[Tuple[str, str, str]] = []
    if game == "factorio":
        base = GAMES / "factorio.app/Contents/data"
        for part in ("base", "core"):
            en = read_cfg(base / part / "locale/en" /
                          f"{part}.cfg")
            zh = read_cfg(base / part / "locale/zh-CN" / f"{part}.cfg")
            for key in en.keys() & zh.keys():
                rows.append((f"{part}.{key}", en[key], zh[key]))
    elif game == "endless-legend":
        base = (GAMES / "endless legend/EndlessLegend.app/Contents/Public"
                / "Localization")
        for name in ("EF_Localization_Locales.xml",
                     "EF_Localization_DLC_21_Locales.xml"):
            en = read_el_xml(base / "english" / name)
            zh = read_el_xml(base / "schinese" / name)
            for key in en.keys() & zh.keys():
                rows.append((f"{name}:{key}", en[key], zh[key]))
    return rows


def analyze(rows: List[Tuple[str, str, str]], *, min_zh_width: int = 6):
    """Ratio = english width / chinese width, matching how the gate frames
    zh→en expansion."""
    ratios, kept = [], []
    for key, en, zh in rows:
        en_c, zh_c = clean(en), clean(zh)
        if not en_c or not zh_c:
            continue
        if "\n" in en_c or "\n" in zh_c:
            continue                       # multi-line reflows; not a widget
        zw = display_width(zh_c)
        if zw < min_zh_width:
            continue                       # too short for a stable ratio
        ratio = display_width(en_c) / zw
        ratios.append(ratio)
        kept.append((ratio, key, zh_c, en_c))
    return ratios, kept


def pct(values: List[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def summarize(label: str, ratios: List[float]) -> None:
    if len(ratios) < 20:
        print(f"{label:<18}{len(ratios):>7}   (too few to summarize)")
        return
    print(f"{label:<18}{len(ratios):>7}{statistics.median(ratios):>9.2f}"
          f"{pct(ratios, .75):>8.2f}{pct(ratios, .90):>8.2f}"
          f"{pct(ratios, .95):>8.2f}{pct(ratios, .99):>8.2f}"
          f"{max(ratios):>8.2f}")


# Shipped key namespaces mapped onto standards.STRING_TYPES. Approximate
# by nature — every studio names keys differently — which is why the
# per-type numbers below sit close together and close to the global p95.
TYPE_PATTERNS = {
    "UI": re.compile(r"gui|button|label|menu|title|tooltip|header|caption"
                     r"|tab\b", re.I),
    "Item": re.compile(r"item|entity|equipment|recipe|resource|unit-name"
                       r"|building", re.I),
    "Skill": re.compile(r"skill|abilit|tech|research|spell|effect|modifier"
                        r"|trait", re.I),
    "System": re.compile(r"message|error|warning|notification|status|system"
                         r"|connect|save|load", re.I),
}

# Residual placeholder markers: a pair where either side still carries one
# is dropped entirely, since identical bytes on both sides dilute the ratio.
_RESIDUE = re.compile(r"__|%\d|\{|\}|\$")


def strict_pairs() -> List[Tuple[float, str]]:
    """(ratio, key) over every shipped game, placeholders excluded."""
    out: List[Tuple[float, str]] = []
    for game in ("factorio", "endless-legend"):
        for key, en, zh in pairs_for(game):
            if _RESIDUE.search(en) or _RESIDUE.search(zh):
                continue
            en_c, zh_c = clean(en), clean(zh)
            if not en_c or not zh_c or "\n" in en_c or "\n" in zh_c:
                continue
            zw = display_width(zh_c)
            if zw < 6:
                continue
            out.append((display_width(en_c) / zw, key))
    return out


if __name__ == "__main__":
    print(f"{'game':<18}{'n':>7}{'median':>9}{'p75':>8}{'p90':>8}"
          f"{'p95':>8}{'p99':>8}{'max':>8}")
    print("-" * 74)
    everything: List[float] = []
    for game in ("factorio", "endless-legend"):
        ratios, _kept = analyze(pairs_for(game))
        summarize(game, ratios)
        everything += ratios
    print("-" * 74)
    summarize("ALL SHIPPED", everything)

    # The numbers gate_checks.width_budget is anchored on.
    strict = strict_pairs()
    print(f"\nPlaceholder-free pairs: {len(strict)}")
    print(f"\n{'string type':<14}{'n':>7}{'median':>9}{'p90':>8}{'p95':>8}"
          f"{'p99':>8}   budget")
    print("-" * 62)
    for name, pattern in TYPE_PATTERNS.items():
        values = [r for r, key in strict if pattern.search(key)]
        if len(values) < 50:
            continue
        print(f"{name:<14}{len(values):>7}{statistics.median(values):>9.2f}"
              f"{pct(values, .90):>8.2f}{pct(values, .95):>8.2f}"
              f"{pct(values, .99):>8.2f}   <- p95 anchors the budget")
    allv = [r for r, _ in strict]
    print("-" * 62)
    print(f"{'ALL':<14}{len(allv):>7}{statistics.median(allv):>9.2f}"
          f"{pct(allv, .90):>8.2f}{pct(allv, .95):>8.2f}"
          f"{pct(allv, .99):>8.2f}")
