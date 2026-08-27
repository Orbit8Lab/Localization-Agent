---
name: intake
phase: INTAKE
gate: G0
tools: [status, next_step, read_artifact, list_files, inspect_file, approve]
summary: confirm scope and the source inventory before any work is committed
---

# INTAKE — scope sign-off

The intake form is the job's constitution: the first artifact, and the one
every later stage reads its configuration from. A wrong locale list or
source language here is not a bug that surfaces immediately — it surfaces
as a whole pipeline run pointed at the wrong target.

## Sequence

1. `status` — confirm the phase and which gate is pending.
2. `next_step` — runs market analysis, producing `market_report`.
3. `read_artifact intake` — verify the constitution against what the
   client actually sent:
   - source language and target locales
   - `client_lang` (the language the client reads bug reports in — not
     necessarily either of the above)
   - genre, which selects the T2 wording layer later
4. `list_files` / `inspect_file` — confirm the source files exist and look
   like what the intake claims. A 4KB sample is enough to catch a format
   surprise.
5. `read_artifact market_report`.

## Before requesting G0

- [ ] Target locales match the contract, spelled the way the pipeline
      expects (`zh-CN`, not `zh`, when the distinction matters).
- [ ] Source files are present and their format is one the ingest stage
      recognizes — or you have accepted that S1 will generate an adapter.
- [ ] `client_lang` is right. Getting it wrong means the client receives a
      bug report they cannot read.

## What NOT to do here

- Do not approve G0 to see what happens next. Everything downstream
  inherits this configuration, and the artifact tree is authoritative —
  rewinding means deleting artifacts.
