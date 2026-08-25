---
name: release
phase: RELEASE
gate: G5
tools: [status, next_step, read_artifact, list_artifacts, compare_po, deliver_po, approve]
summary: emit deliverables in the received format, byte-for-byte; G5 is the client sign-off
---

# RELEASE — format fidelity is the deliverable

The translation is finished. What remains is handing it over in exactly the
shape the client sent, and this is where otherwise-correct work gets
rejected.

**The received file's format is part of the contract.** A deliverable is
patched line-by-line from the source, never regenerated: regenerating
normalizes things nobody agreed to normalize — separator conventions, the
BOM, header fields, per-entry escaping — and the diff the client runs will
show changes on strings nobody touched.

## Sequence

1. `next_step` — marketing kit / store copy for the locale.
2. `next_step` — emits the deliverables manifest.
3. `deliver_po` — produce the deliverable **through the delivery path**,
   not by copying a file. This relabels the header and preserves the BOM.
4. `compare_po` — diff the deliverable against the received source.
5. `read_artifact manifest` — confirm the inventory matches what was
   promised at intake.
6. `approve G5` once the client signs off.

## Before requesting G5

- [ ] `compare_po` shows **zero unexpected source changes**. Any msgid
      drift means the deliverable was regenerated rather than patched.
- [ ] The BOM survived. A missing BOM is a WARN, not an ERROR — so it does
      not block, and it will still break the client's tooling.
- [ ] Separator conventions match **per entry**. The asset may use several
      and they are not interchangeable; a file-wide "house style" pass is
      how a printed backslash reaches the screen.
- [ ] Every locale promised at intake is present in the manifest.
- [ ] Key fan-out is right: deduped uids expand back to **all** original
      game keys, so a string shared by three keys appears three times.

## What NOT to do here

- Do not hand-edit a deliverable. If it needs a change, change the source
  of truth and re-emit — a hand edit is invisible to `compare_po` next time.
- Do not treat a WARN as safe to ignore because it is not an ERROR. The
  blocking classification is about pipeline safety, not client acceptance.
