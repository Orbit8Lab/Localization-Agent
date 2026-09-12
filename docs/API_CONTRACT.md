# Orbit8 Agent — HTTP API Contract (v0.1)

The interface `orbit8-web` builds against. Generated from the running
app, not written from memory: every field below appears in the live
OpenAPI schema at `GET /openapi.json` (interactive docs at `/docs`).

**Base URL** — beta: `https://orbit8-agent.fly.dev`; local: `http://127.0.0.1:8000`

---

## The one idea that shapes everything

**There is no job table.** Status is computed by `Job.derive()`, which
reads the artifact tree on every request. A job's stage is therefore a
*derived* value, not a stored one, and it cannot disagree with the
artifacts that produced it.

Two consequences for the front end:

1. **Poll `GET /jobs/{job_id}`.** It is cheap and always truthful. Do not
   cache a stage and assume it holds.
2. **A job is never "lost."** If the server restarts mid-run, the next
   request re-derives the same stage from the artifacts on disk. Call
   `POST /jobs/{job_id}/run` to resume.

---

## Two different "states" — do not conflate them

This trips people up, so it is worth being explicit.

| Field | Source | Meaning | Survives restart |
|---|---|---|---|
| `stage` | `Job.derive()`, from artifacts | **Where the job is in the lifecycle** | yes — durable |
| `run_state` | in-memory runner | **What the server is doing right now** | no — resets to `idle` |

`stage` is the truth. `run_state` is a live hint for spinners and error
banners. After a restart, a job mid-LQA shows `stage.phase = "LQA"` with
`run_state = "idle"` — that is correct, not a bug, and the UI should
offer a "Resume" button.

---

## Endpoints

### `GET /health`
```json
{ "ok": true, "jobs_root": "/data/jobs" }
```

### `POST /jobs` → `201`

`multipart/form-data`. **Upload directly to this API** — do not proxy
through Next.js, whose 4.5MB body cap is smaller than real game exports.

| Field | Req | Default | Notes |
|---|---|---|---|
| `file` | yes | — | source file; cap `ORBIT8_MAX_UPLOAD_BYTES` (100MB) |
| `game` | yes | — | title, shown in the job list |
| `source_lang` | yes | — | e.g. `zh` |
| `target_locales` | yes | — | comma-separated: `en,ja,ko` |
| `tenant_id` | no | `"default"` | see Tenancy below |
| `engine` | no | `"unknown"` | |
| `genre` | no | `""` | comma-separated |
| `job_id` | no | generated | `job-YYYYMMDD-xxxxxxxx` |
| `autostart` | no | `true` | `false` to create without running |

Returns **JobDetail**. Returns as soon as intake is written — a
two-hour job does not hold the request open.

Errors: `400` no valid locale · `409` duplicate `job_id` ·
`413` over the size cap (refused *while streaming*; no partial job is left)

### `GET /jobs?tenant_id=` → `JobSummary[]`

### `GET /jobs/{job_id}` → `JobDetail` · `404` unknown · `400` bad id

**This is the polling endpoint.** Suggested interval: 2–5s while
`run_state = "running"`, back off when `waiting_gate` or `idle`.

### `POST /jobs/{job_id}/approve` → `JobDetail`

```json
{ "gate": "G1", "by": "maotian", "note": "optional" }
```

Clears a gate and resumes the job. **`409` if `gate` is not the pending
one** — the controller refuses out-of-order approval, so the API cannot
be used to skip G1 and translate against an unfrozen glossary. Surface
that 409 as a real message; it means the UI's view was stale.

### `POST /jobs/{job_id}/run` → `JobDetail`
Resume an idle job (after a restart, a crash, or `autostart=false`).
Idempotent: calling it on a running job is a no-op, so a retrying client
cannot start two workers on one artifact tree.

### `GET /jobs/{job_id}/artifacts` → `ArtifactRef[]`
### `GET /jobs/{job_id}/artifacts/{stage}/{name}?attempt=` → JSON file
`attempt` is only meaningful for stages 4 and 5 (the attempt-versioned
ones); elsewhere omit it.

---

## Types

```ts
type Stage = {
  phase:  "INTAKE" | "INGEST" | "CONTEXT" | "ASSET" | "PILOT"
        | "PRODUCTION" | "LQA" | "FLAGGED" | "TESTING" | "RELEASE"
        | "INCREMENTAL";
  action: string;          // human-readable next action — render verbatim
  gate:   "G0"|"G1"|"G2"|"G3"|"G4"|"G5" | null;   // non-null ⇒ needs a human
  target: string | null;   // locale, when the step is per-locale
  detail: string | null;   // why, when a gate is blocked
};

type RunState = "idle" | "running" | "waiting_gate" | "error";

type JobSummary = {
  job_id: string; tenant_id: string;
  created_at: string | null; game: string | null;
  stage: Stage; run_state: RunState; run_error: string | null;
};

type JobDetail = JobSummary & {
  source_lang: string | null;
  target_locales: string[];
  approvals: Record<string, { by: string; note: string|null; at: string }>;
  progress: Record<string, unknown> | null;
};

type ArtifactRef = { stage: number; name: string; attempts: number[] };
```

**Render `action` verbatim.** It is written to be shown to an operator
("dev team reviews + locks the glossary"). Do not map it to your own
strings — it will drift from the controller.

---

## The six gates

A gate is a ledger row a human flips, **not a paused thread**. A gate may
stay open for a two-week legal review; nothing is holding a process.

| Gate | Meaning | Who |
|---|---|---|
| `G0` | scope sign-off | PM / client |
| `G1` | asset lock (freezes the glossary) | dev team |
| `G2` | pilot sign-off | PM |
| `G3` | flagged-strings review | linguist |
| `G4` | test sign-off | QA |
| `G5` | delivery sign-off | PM / client |

`G1` will not open while glossary health blockers remain; `stage.detail`
says so. Show it.

---

## Typical flow

```
POST /jobs                        → job_id, autostart
GET  /jobs/{job_id}  (poll)           → running … → waiting_gate, gate=G0
POST /jobs/{job_id}/approve {G0}      → resumes; runs INGEST→CONTEXT→ASSET
GET  /jobs/{job_id}  (poll)           → waiting_gate, gate=G1
POST /jobs/{job_id}/approve {G1}      → glossary frozen; translation begins
   … PILOT → G2 → PRODUCTION → LQA → G3 → TESTING → G4 → RELEASE → G5
GET  /jobs/{job_id}/artifacts         → download the bug report
```

---

## Not in v0.1 — plan around these

| Gap | Today | When |
|---|---|---|
| **Auth** | none | Step 5 — basic auth at Step 3 |
| **Tenancy** | `tenant_id` is recorded and filterable, **not enforced** | before any external customer |
| **Progress detail** | `progress` is coarse (`phase`/`action`) | the agent emits `verify_progress`; not yet surfaced |
| **Streaming** | poll only | SSE later |
| **Token caps** | none — the verifier is ~80% of calls and uncapped | Step 5, before non-authors run large files |
| **Cancel** | no endpoint | — |

**Read `tenant_id` as a label, not a security boundary, until it is
enforced.** Filtering happens server-side on request, but nothing stops
a caller asking for another tenant's job by id.

---

## Testing the API

Three layers, cheapest first.

**1. Automated tests — no server, no network, 1.4s.** 24 tests against a
`tmp_path` job root with a dry-run runner.

```bash
.venv/bin/python -m pytest tests/test_server_api.py tests/test_server_runner.py -q
```

Run these on every change. They cover the refusals a browser can
trigger: path traversal in a job id and artifact name, oversized upload
(refused *while streaming*, leaving no partial job), duplicate job id,
out-of-order gate approval, and double-start of a running job.

**2. Smoke script — against a running server, local or deployed.**

```bash
./scripts/smoke_api.sh                              # localhost:8000
./scripts/smoke_api.sh https://orbit8-agent.fly.dev # deployed
BASIC_AUTH=user:pass ./scripts/smoke_api.sh https://...
```

19 checks: health, create, read, eight refusals, and the real lifecycle
(background run stops at G0 → approve → advances to G1). Exits non-zero
on first failure, so it can gate a deploy. **This is the post-deploy
verification for Step 3** — same script, different URL.

**3. Interactive docs — click-to-test in a browser.**

`GET /docs` (Swagger UI) · `GET /redoc` · `GET /openapi.json`

Swagger UI has a "Try it out" button per endpoint, including file
upload. This is the fastest way for someone else to explore the API
without writing a client. Turn it off in production once there are real
customers.

**Front-end development without a backend:** a runner built with
`dry_run=True` advances stages with no API key and no model calls.

## Local development

```bash
uv pip install -e ".[server]"
ORBIT8_JOBS_ROOT=./jobs python -m orbit8.server      # :8000, /docs
```

Env: `ORBIT8_JOBS_ROOT` · `ORBIT8_CORS_ORIGINS` (comma-separated;
defaults to `http://localhost:3000`) · `ORBIT8_MAX_UPLOAD_BYTES` ·
`ORBIT8_PROVIDER` · `ORBIT8_MODEL` · `ORBIT8_PORT`

A runner built with `dry_run=True` advances stages with **no API key and
no model calls** — use it for front-end development.
