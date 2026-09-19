# Triage guide

One intake point, two repos underneath. Reporters describe what
happened; **a triager assigns every label.** That split is deliberate: a
field an untrained reporter picks wrong is worse than one they did not
pick at all, because wrong metadata routes work to the wrong person and
nobody notices.

Triage is a person, once a day, ten minutes. Not a rota, not a meeting.

---

## For reporters (PMs and testers)

File everything at **`Orbit8Lab/Localization-Agent`**, even if it looks
like a website problem. Do not try to decide which repo. Triage moves it.

Include the **job ID** (`job-20260912-4599e0e7`, under the game name in
the console). With it, an engineer reproduces from the artifacts. Without
it, they are guessing.

Report anything that looked wrong even if you are not sure. "I could not
tell whether this was correct" is a real finding: if the system cannot
explain itself to you, that is a defect in the system, not in you.

---

## The labels

Four sets. A triaged issue carries exactly one from each of the first
three.

### `type:` — what kind of thing is this

| Label | Means |
|---|---|
| `type:bug` | It did something other than what it promised. |
| `type:feature` | It never promised this, and it should. |
| `type:content` | **Not code.** A glossary term or style rule is wrong. |
| `type:docs` | The system behaved correctly but explained itself badly. |
| `type:question` | Nobody is sure yet. Re-label once someone is. |

`type:content` matters more than it looks. "The translation used the
wrong word" is usually a **glossary decision**, not a code change — and
it should flow back as a glossary or style-rule update, the way a
post-editor's rejection does. Without this label those pile up in the
engineering queue and terminology decisions wait on sprint capacity.

### `layer:` — which part of the system

| Label | Means | Lives in |
|---|---|---|
| `layer:ui` | Console renders wrong, dead button, confusing label | `orbit8-web` |
| `layer:api` | Wrong status code, 500, auth, CORS, upload | `Localization-Agent` |
| `layer:agent` | Assistant said something false, used the wrong tool, looped | `Localization-Agent` |
| `layer:pipeline` | Translation or LQA output is wrong | `Localization-Agent` |
| `layer:infra` | Deploy, Fly, Vercel, secrets, volume | either |

**An issue may touch two layers.** Label both, then decide where the
*fix* goes and move the issue to that repo. Example: the console shows a
stale stage after chat approves a gate. `layer:ui` + `layer:api` — but
the fix is the API returning `job_changed`, so it lives in the agent
repo. If two separate fixes are genuinely needed, split into two issues
and link them; one issue in two sprints tracks badly.

### `sev:` — how bad

Ordered by how hard the problem is to *notice*, not by how loud it is.

| Label | Means | Why here |
|---|---|---|
| `sev:1-silent-wrong` | Wrong result with no error. A check that did not run. Tokens spent with nothing to show. | **The worst category.** A crash announces itself; this does not. "A check that does not run is indistinguishable from a check that passes." Cost incidents live here. |
| `sev:2-blocked` | Cannot complete the work. Job stuck, upload fails, gate will not clear. | Visible and total. |
| `sev:3-wrong-visible` | Bad output the user can see and work around. | Annoying, not dangerous. |
| `sev:4-friction` | Confusing, slow, ugly. Includes "I did not know what G1 meant." | Real, and the thing most likely to be dismissed. |

A PM saying "it looked wrong but I am not sure" is often `sev:1` — they
have spotted something silent. Ask before downgrading it.

### Workflow labels

`needs-triage` (auto-applied, removed when triaged) · `needs-info`
(waiting on the reporter) · `good-first-issue` · `blocked`

---

## Triage: five decisions

1. **Can I reproduce it?** If not and there is no job ID, ask —
   `needs-info`. Do not guess.
2. **Is it a bug at all?** Behaved-as-designed but confusing is
   `type:docs` or `type:feature`, not a bug. Say so kindly in a comment:
   a reporter told their report was invalid files fewer next time, and
   under-reporting is the failure mode that hurts most.
3. **Which layer?** Label all that apply.
4. **How bad?** Ask specifically whether anything was silently wrong
   before settling below `sev:1`.
5. **Which repo?** Where the fix goes, not where it was seen. Move with
   `gh issue transfer`. Then remove `needs-triage` and add it to the
   project board.

---

## Sprint

The org project board spans both repos, so an issue keeps its history
when it moves.

Suggested order, and it is not "sev:1 first, always": one `sev:1` can
outrank every `sev:2`, because silent wrongness spreads. But a board of
nothing but `sev:1` work with no `sev:4` fixes ships a correct system
nobody can use. Both matter.

**Untriaged issues do not enter a sprint.** The board is for work
someone has looked at.
