---
name: po-roundtrip
phase: INCREMENTAL
tools: [list_files, inspect_file, scan_po, compare_po, translate_po, deliver_po, standardize]
summary: received .po → format audit → repair → delivery, preserving the received format exactly
---

# Operation — `.po` round trip

A human-initiated flow, usually an incremental drop that re-enters the
lifecycle at S1 against locked assets. The work is as much **format
forensics** as translation.

## The three-layer escaping model

Most `.po` defects are invisible unless you know which layer you are
looking at:

| Layer | What it is | What you see |
|---|---|---|
| 1 | file bytes | `\\n` — a backslash and an `n` |
| 2 | PO-decoded | `\n` — an escape sequence |
| 3 | engine render | an actual line break |

A target escaped one level too many decodes to a *printed backslash*
instead of a break. It is invisible in an xlsx export, invisible in a
wording diff, and wrong on screen. Separator conventions are **per entry**,
not per file — an asset routinely uses several and they are not
interchangeable.

## Sequence

1. `list_files` / `inspect_file` — locate the drop and read its head.
   Check for the BOM and the header.
2. `scan_po` — audit the received file: format integrity, placeholders,
   locked terms, width.
3. `compare_po` — against the previous received file, if this is a delta.
   This is what tells you which entries actually changed.
4. `translate_po` — only the entries that need it.
5. `deliver_po` — emit through the delivery path so the header is relabeled
   and the BOM preserved.
6. `compare_po` again — deliverable against received source. **Expect zero
   unexpected msgid changes.**

## Non-negotiables

- [ ] Patch line-by-line; never regenerate. Regeneration normalizes
      separators, BOM, and header fields that nobody agreed to change.
- [ ] Preserve each entry's **own** separator convention.
- [ ] A lost line break is never invented back. If the source has a
      separator and the target has none, a human decides — that entry may
      also have dropped a clause, and no format pass can restore meaning.
- [ ] Break-count drift is **reported, not reflowed**. A translator merging
      11 lines into 4 made a content decision.

## What NOT to do

- Do not apply a file-wide separator style. That is how one printed
  backslash becomes a thousand.
- Do not copy a file to produce a deliverable. Copying skips the header
  relabel and can drop the BOM.
