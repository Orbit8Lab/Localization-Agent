---
name: ingest
phase: INGEST
tools: [status, next_step, read_artifact, list_artifacts, list_files, inspect_file]
summary: deterministic source ingest and dedup; no gate, but the dedup ratio is worth reading
---

# INGEST — deterministic, and deliberately not a graph

S1 is 100% deterministic and is a plain function rather than a LangGraph
stage (design §2). There is no model judgment to supervise here, so the
work is verification: confirm what came out matches what went in.

## Sequence

1. `next_step` — runs ingest.
2. `read_artifact ingest_report` — the numbers that matter:
   - records read vs **unique strings** after dedup
   - any file that produced zero records
3. `read_artifact uniques` (or `list_artifacts`) to spot-check that keys
   and text look right.

## What to actually check

- **The dedup ratio.** A game corpus normally has real duplication; a
  ratio near 1.0 can mean the key, not the text, is being used as the
  dedup basis. A ratio near zero means something collapsed strings that
  are not actually identical.
- **Zero-record files.** Silent in the report's totals, fatal to the
  locale that needed them.
- **Unknown formats.** If S1 generated an adapter, it is stored as a
  fingerprinted s1 artifact and will be re-run deterministically on later
  drops. Read it once — it was written by a model and audited exactly
  never until someone looks.

## No gate here

INGEST flows straight into CONTEXT. If something is wrong, the fix is to
correct the source and delete the s1 artifacts — the tree is authoritative,
so deleting rewinds the job.
