# How to report something — one page

**Everything goes to `Orbit8Lab/Localization-Agent` → Issues → New issue.**
Even if it looks like a website problem. Do not try to pick the right
repo; someone moves it if it needs moving.

**You do not choose a label, a severity, or a component.** Describe what
you saw. A triager sorts it within a day. If you are unsure whether
something is even a bug, file it anyway — see *Question* below.

---

## Which of the three forms

| Form | Use it when |
|---|---|
| **Bug report** | It did something other than what it promised. |
| **Feature or improvement** | It never promised this, and it should. |
| **Question** | You cannot tell whether it is wrong, or whether you are. |

When in doubt, **Bug report**. Being re-labelled costs nothing; a report
you did not file costs everything.

---

## Bug report — the five boxes

### Job ID *(optional, but the most useful thing in the report)*
Copy it from the console, under the game name:

```
SongOfKings
job-20260912-4599e0e7     ← this
En → Zh   tenant: default
```

With it, an engineer opens the same job and reads what actually
happened. Without it, they are guessing from a description. **Include it
whenever the problem happened inside a job.**

### What happened *(required)*
What you did, then what the system did. Plain sentences.

> I uploaded the SongOfKings export, waited for it to reach G1, and
> clicked **Approve G1**. The page went back to "Waiting on a gate" with
> no message.

Not: *"approval is broken."* That tells us the conclusion, not the
evidence — and the conclusion is sometimes wrong in an interesting way.

### What you expected instead *(required)*
> I expected the job to move on to translation.

This box catches the cases where the system was right and the
expectation was off. That is still worth knowing: if the console led you
to expect the wrong thing, the console has a problem.

### How much did this get in your way? *(required)*

| Option | Means |
|---|---|
| **I could not continue at all** | You were stopped. |
| **I found a way around it** | Annoying, not fatal. |
| **It was confusing, but I got there** | The system worked; explaining itself did not. |
| **It looked wrong but I am not sure** | See below. |

> **Pick the last one freely.** "It looked wrong but I am not sure" is
> often the most valuable report we get. A wrong number with no error
> message is our worst category of defect, precisely because nothing
> announces it. A crash is loud; a quiet wrong answer ships. If a count,
> a translation, or a status looked off and you could not say why, that
> is exactly the report we want — not a false alarm.

### Anything else *(optional)*
**Screenshots. Please.** Paste directly into the box. Also useful: a
number or line of text that looked wrong, the approximate time, and
whether it happened more than once.

---

## Feature or improvement — the three boxes

### What is hard right now *(required)*
**Describe the problem, not the solution.**

> I cannot tell which of my six jobs need something from me without
> opening each one.

leads somewhere better than *"add a filter."* The first invites a good
answer; the second pre-commits to one that may not be the best available.

### What you have in mind *(optional)*
Say it here if you have an idea. It will not be held against you.

### How often does this come up? *(required)*
Every job / Most jobs / Occasionally / Once so far. Answer honestly —
"once so far" is a real answer, and small friction hit every single job
often outranks a larger one hit rarely.

---

## Question — two boxes

Job ID (if it concerns one), and **What are you unsure about?**

Use this when you cannot tell whether the system is wrong or you are.
**Either answer is useful.** If the system is wrong, it is a bug. If it
is right but you could not tell, the system failed to explain itself —
which is also something we need to fix, just in a different place.

---

## Good report, in full

> **Job ID:** job-20260912-4599e0e7
>
> **What happened:** The job reached G1. I clicked Approve G1 and left
> the note "glossary reviewed". The amber box disappeared, then came back
> a few seconds later still saying "Waiting on G1". No error appeared.
> I tried twice.
>
> **What I expected:** The job to move past G1 and start the pilot
> translation.
>
> **How much did this get in your way:** I could not continue at all.
>
> **Anything else:** Screenshot attached. Around 14:20 today. The
> Approvals panel showed only G0, never G1.

What makes it good: a job ID, the exact sequence, what the screen said,
that it happened twice, and what was *absent* — no error message. That
last detail is often what identifies the cause.

---

## Two things worth repeating

1. **Report it even if you think it might be your fault.** Under-reporting
   is the failure we most want to avoid during testing. Nobody is
   evaluated on how many of their reports turn out to be real.

2. **You cannot break anything by filing an issue.** The worst outcome
   is a comment saying "this is working as intended, here is why" — and
   that comment usually means we owe you a clearer interface.
