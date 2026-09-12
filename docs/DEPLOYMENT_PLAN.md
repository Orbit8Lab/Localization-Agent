# Deployment Plan: Orbit8 Agent as a Hosted Service

**Goal (1 month):** 5 team members upload a file in a browser and get a
bug report back, without touching a terminal.
**Direction (later):** multi-tenant commercial product.

This plan is written back-to-front on purpose: each step is testable
before the next one exists. Building the UI first means writing it
against an API you are still inventing.

---

## Why the agent cannot run on Vercel

Three properties of the system, not preferences:

| Property | Where | Conflicts with |
|---|---|---|
| Jobs run ~2 hours | LQA full run, 1,233 rows | Vercel function cap: 300s |
| State **is** the filesystem | `store.py` — `root / job_id`, `mkdir`, `write_text` | Ephemeral per-invocation FS |
| Sandbox needs a real process | `sandbox.py` — `subprocess.run`, `setrlimit`, `preexec_fn` | No POSIX process control |

So: **Vercel hosts the UI, a container host runs the agent.** This is a
deployment split, not a rewrite — §6 of the paper is right that the
web service is a presentation layer over `Job.derive()`.

---

## Repositories

Two. Not one, not three.

| Repo | Contents | Visibility | Deploys to |
|---|---|---|---|
| `Orbit8-Agent` (existing) | agent + `orbit8.server` API | **public** | Fly.io |
| `orbit8-web` (new) | Next.js UI | private | Vercel |

The API lives in the agent repo because it imports `orbit8` directly and
shares its dependencies and test suite. A third repo for shared types is
not worth it with one front end and one back end.

**Because `Orbit8-Agent` is public:** no client names in example
configs, no default credentials, no real glossaries in fixtures.

---

## Steps

### Step 1 — HTTP API over the existing agent (local)

Wrap, do not rewrite. `orbit8.server` imports `Job` and calls the same
methods the CLI does.

```
POST   /jobs                  multipart: file + intake fields -> {job_id}
GET    /jobs                  list
GET    /jobs/{id}             Job.derive() -> phase, action, gate, progress
POST   /jobs/{id}/approve     {gate, by, note} -> Job.approve()
GET    /jobs/{id}/artifacts   list produced artifacts
GET    /jobs/{id}/artifacts/{stage}/{name}   download
```

Done when: `curl -F file=@strings.xlsx localhost:8000/jobs` returns a
job id, and polling `/jobs/{id}` reports the stage advancing.

**Two decisions made here, cheap now and expensive later:**
- `tenant_id` threaded through job creation from day one (`IntakeBrief`
  already carries it, defaulting to `"default"`).
- Uploads go **directly to this API**, never proxied through Next.js
  (Vercel caps request bodies at 4.5MB; game exports exceed it).

### Step 2 — Background execution

`POST /jobs` must return in a second, not two hours. A background worker
drains a queue of pending jobs and calls `next_step()` in a loop until a
gate or completion.

No Redis or Celery yet: the artifact tree already *is* the job state, so
a crashed worker resumes by re-deriving. That is the derived-state design
paying off.

Done when: you can poll status while a job is mid-run.

### Step 3 — Deploy the API to Fly.io

One container, one persistent volume mounted at the job root, API keys
as secrets, HTTP basic auth, CORS enabled (Vercel and Fly are different
origins — enable it here, not in Step 4).

Done when: the Step 1 curl commands work against a public URL.

### Step 4 — Next.js UI on Vercel

Screens: upload, job list, job detail (stage + progress + why a gate is
blocked), gate approval, results download.

`Job.derive()` already returns `Stage(phase, action, gate, target,
detail)` — the UI mostly renders what the agent computes. The "what
stage am I in and what's next" query is already solved server-side.

Done when: a team member completes a job without a terminal.

### Step 5 — Harden for 5 users

Real login. **Per-job token cap** (the per-finding verifier is ~80% of
LLM calls and currently uncapped — a non-author running a large file is
a cost incident). Upload size limit. Error states in the UI.

---

## Deliberately deferred

| Deferred | Until | Why it is safe to wait |
|---|---|---|
| Object storage (S3/R2) | internal multi-project | A volume is fine for 5 users on one host |
| Tenant isolation | before any external customer | Non-negotiable then: a glossary leaking across studios is a breach, not a bug |
| Gate UI polish | after beta | Beta reveals which gates are actually used |
| Billing, SSO, self-serve | commercial phase | — |

**But:** keep `ArtifactStore` behind an interface and keep `tenant_id`
threaded from Step 1. Both are cheap now and a rewrite later.

---

## Known faults to fix before non-authors use this

From the paper's own Deployment section:

1. Per-finding verifier is ~80% of LLM calls and **uncapped**.
2. A validation failure discards a whole batch instead of retrying smaller.
3. Asset reachability is asserted only for the glossary, not every asset.

(1) is a Step 5 blocker. (2) and (3) are quality issues that can follow.
