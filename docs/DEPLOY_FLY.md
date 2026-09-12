# Step 3 — Deploy the API to Fly.io

Everything in the repo is written and verified. What remains needs your
Fly credentials, so the `fly` commands below are yours to run.

**Acceptance test:** `BASIC_AUTH=... ./scripts/smoke_api.sh https://orbit8-agent.fly.dev`
returns **19 passed, 0 failed**.

---

## Prerequisites

```bash
brew install flyctl      # not currently installed on this machine
fly auth login           # run with a leading ! in Claude Code
```

Docker is **not** needed locally — Fly builds remotely.

---

## Deploy

### 1. Create the app (does not deploy yet)

```bash
fly launch --no-deploy --name orbit8-agent --region sjc
```

Answer **no** to "copy config to a new app?" — `fly.toml` is already
written. If the name is taken, pick another and change `app` in
`fly.toml`.

### 2. Create the volume

The artifact tree lives here. `derive()` reads it on every request, so
**losing this volume loses the jobs** — state is the volume, not the
machine.

```bash
fly volumes create orbit8_data --region sjc --size 10
```

### 3. Set secrets

Never in `fly.toml` — the repo is public.

```bash
fly secrets set ORBIT8_BASIC_AUTH='alice:LONG_RANDOM,bob:LONG_RANDOM'
fly secrets set DEEPSEEK_API_KEY='sk-...'
fly secrets set ORBIT8_CORS_ORIGINS='https://orbit8-web.vercel.app'
```

Generate passwords with `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.

`ORBIT8_CORS_ORIGINS` can wait until the web app exists; it defaults to
`http://localhost:3000`. Update it when you know the Vercel URL,
including any preview domains.

### 4. Deploy

```bash
fly deploy
```

### 5. Verify

```bash
curl https://orbit8-agent.fly.dev/health
BASIC_AUTH='alice:PASSWORD' ./scripts/smoke_api.sh https://orbit8-agent.fly.dev
```

The script creates a real job, runs it to gate G0, approves it, and
confirms it advances to G1. **19 passed, 0 failed** means the deployment
works end to end. It exits non-zero otherwise, so it can gate a release.

---

## What the config does, and why

| Choice | Reason |
|---|---|
| `min_machines_running = 1`, `auto_stop_machines = false` | A stopped machine kills an in-flight two-hour job. Idle cost is the price of not losing work. |
| One machine only | A Fly volume attaches to exactly one machine. Scaling out needs the object-storage backend first. |
| `--workers 1` | Run-state is in memory; a second worker would answer `/jobs/{id}` from a different view. Concurrency comes from the runner's threads. |
| Non-root user | The sandbox confines generated adapters; the process spawning them should not be root either. |
| `ORBIT8_ENV=production` | Turns on the fail-closed auth check and turns off `/docs`. |
| Health check unauthenticated | Fly's probe runs before secrets are available, and `/health` leaks nothing. |

**Fail closed:** with `ORBIT8_ENV=production` and no `ORBIT8_BASIC_AUTH`,
the app **refuses to boot**. Forgetting the secret is the likeliest way
to publish an open API that spends tokens, so it stops the deploy rather
than serving anonymously. If `fly deploy` fails with
`ORBIT8_BASIC_AUTH is required`, that guard fired — set the secret.

---

## Operating it

```bash
fly logs                       # tail
fly status                     # machine + volume health
fly ssh console                # shell in the container
fly ssh console -C "ls /data/jobs"
fly secrets list               # names only, never values
fly scale vm shared-cpu-4x     # if jobs are CPU-bound
```

**Back up the volume.** Fly takes daily snapshots, but they are not a
tested restore. Before anything real:

```bash
fly ssh console -C "tar czf - /data/jobs" > backup-$(date +%F).tgz
```

---

## Known limits of this deployment

Carried forward deliberately, not oversights:

| Limit | Consequence | Fix lands in |
|---|---|---|
| **Shared Basic auth** | No per-user audit; a leaked password means rotating for everyone | Step 5 |
| **`tenant_id` not enforced** | A label, not a boundary — any authenticated user can read any job by id | Before external customers |
| **No token cap** | The verifier is ~80% of LLM calls and uncapped; a large upload is a cost incident | Step 5 — the real blocker before non-authors run big files |
| **Single machine** | Deploy or crash interrupts a running job (artifacts survive; `POST /jobs/{id}/run` resumes) | Object storage phase |
| **No cancel endpoint** | A runaway job needs `fly ssh console` | Step 5 |

The first and third are the ones to watch during the 5-person beta.

---

## If something breaks

**`fly deploy` fails with `ORBIT8_BASIC_AUTH is required`** — the
fail-closed guard. Run the `fly secrets set` above.

**Health check failing** — `fly logs`. Usually the volume is not mounted:
`fly volumes list` should show `orbit8_data` attached.

**Job stuck in `waiting_gate`** — expected. A gate needs a human;
`POST /jobs/{id}/approve`.

**Job shows `run_state: idle` mid-lifecycle** — the machine restarted.
The artifacts survived; `POST /jobs/{id}/run` resumes from where
`derive()` says the job is.

**CORS errors in the browser** — `ORBIT8_CORS_ORIGINS` must list the
exact Vercel origin, scheme included. Preview deploys get their own
domains.
