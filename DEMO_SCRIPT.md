# Demo video script (target: under 3:00)

Working notes for recording the required demo video -- a shot list so recording is a
planned take rather than improvised.

**Record the web app, not the CLI.** Everything below happens at
`incident-copilot-demo.centralindia.cloudapp.azure.com`. A judge watching a terminal has
to take your word for what the code did; a judge watching the decision panel can see the
policy resolve. The CLI still works and is the same code path -- it's just the worse shot.

**The story is not "watch an agent walk a lineage graph."** Plenty of agents do that. The
story is: *it proves the cause against DataHub, refuses to act when the evidence is thin,
writes what it learned back into the catalog, and re-tests that memory before ever trusting
it again.* Everything below is ordered to land that before a judge stops watching.

## Before recording

Full clean baseline, on the VM:

```
docker container prune -f && datahub docker nuke && datahub docker quickstart
cd ~/incident-copilot && .venv/bin/python seed_data.py
sudo systemctl restart incident-copilot-web.service
```

Then wait ~3 minutes before testing anything -- DataHub's search index fills in slowly
after a reseed, and an early check looks like a broken app.

Why the reseed matters, beyond tidiness: earlier runs' tags and appended notes persist as
catalog state and quietly destroy the "watch this happen live" framing. It also resets
memory to exactly the two seeded prior investigations, which is what makes the beat in
1:00-1:40 fire.

**Check `docker ps` shows six containers** right before you record. OpenSearch on this VM
dies roughly every 26 hours; the watchdog cron restarts it, but a run that starts mid-repair
will look broken. `curl` returning 200 proves nothing -- the web server is fine either way.

**Record the memory run first.** Every investigation you run stores a new card, and recall
returns the two *most relevant* ones. After a couple of takes, your fresh cards outrank the
seeded history and the CONFLICT beat stops appearing. If you need another take, re-run
`seed_data.py` first.

## 0:00-0:20 -- Hook

Open on the Command Center. Don't narrate the nav.

> "Most incident agents give you an answer whether or not they have one. This one proves
> its answer against DataHub, refuses when it can't, and remembers what it learned --
> then refuses to trust that memory until it re-checks it."

Point at one number: **actions / refusals**. A refusal is a recorded outcome, not a failure.

## 0:20-1:00 -- Incident → investigation

Click **Order count discrepancy → Investigate**. Start the run.

Let the left pane scroll; don't read it aloud. Call out the right-hand panel instead, which
is the part nobody else has:

- the progress list ticking off real DataHub work
- **◈ DataHub calls** climbing -- every one is a real MCP call to GMS
- the four evidence checks filling in as they're confirmed

Say plainly: *the model supplies evidence. It never scores itself.*

## 1:00-1:40 -- The memory beat (the one to slow down for)

The purple banner appears before the agent touches anything: **prior verified knowledge
found in DataHub** -- two stored investigations, what each already established, what's
still missing.

Then the part worth pausing on. Both cards are re-tested against the live graph:

```
✓ confirmed   INC-20260806-091500   order_status_detail still present -- finding holds
⚠ conflict    INC-20260807-143000   order_status_code_v1 is gone -- withdrawn as evidence
```

The line to land:

> "A stored finding is a true record of when it was written -- not a standing fact. This
> one no longer matches the graph, so it's withdrawn. The agent doesn't get to trust its
> own memory."

Then show the consequence, which is the proof it isn't cosmetic: the withdrawn checks
**reset to unconfirmed**, confidence drops, and severity falls a tier. Verified live:
4/4 claimed → 2/4 allowed → HIGH → MEDIUM → `tag_note_escalated` → `tag_and_note`.

## 1:40-2:15 -- Gate, drift, write-back

- **Write-back gate**: `add_tags` / `update_description` shown permitted or refused, with
  the authorized target URNs. Say it once: *this is Python, not a prompt the model usually
  follows.*
- **Cross-platform mirrors**: snowflake / looker / powerbi chips. DataHub's lineage shows
  all three as connected and has no way to tell you they disagree on *shape*. Two of the
  three are only reachable at 2 hops. They keep producing the symptom after the root cause
  is fixed.
- Write-back events: `PROPOSED → ALLOWED → APPLIED → VERIFIED`, the last being a re-read
  out of DataHub rather than a bare `success: true`.

If you'd rather show the refusal instead, run **Replica out of sync** -- it lands at
1/4 LOW → `no_action` → gate `🔒 LOCKED`, both tools refused. Pick one; there isn't time
for both.

## 2:15-2:40 -- It compounds

The closing panel: **Every incident makes DataHub smarter** -- checks proved by this run,
checks not re-run, checks now established for the next one, DataHub calls made.

Then **Investigations** in the nav: every run ever completed, read back out of DataHub as
real `document` entities, refusals included, with `↩ continues INC-…` chains between them.

> "This isn't chat history. It's catalog metadata a human opens on the dataset page, and
> the next investigation reads it and continues."

Optional, if there's room: **Lineage** -- the real graph, 38 nodes, every edge a
relationship DataHub actually traversed.

## 2:40-2:55 -- Close

> "Four things the model is never allowed to decide: its own confidence, its own severity,
> whether a write is permitted, and which entity that write may touch. Plus one more --
> whether its own memory still holds. All five are plain Python."

The honest one-liner, if it fits -- it tends to land with engineers:

> "The gate caught a bug in itself: the first time the agent tried to tag something it
> wasn't authorized to, the block crashed the run instead of blocking it. It's in the
> commit history."

## 2:55-3:00 -- End card

Repo: github.com/Eman-Yousaf/datahub-incident-copilot
Live: incident-copilot-demo.centralindia.cloudapp.azure.com
Upstream PRs: acryldata/mcp-server-datahub #155, #198 (submitted, not merged)

## Recording notes

- **Don't navigate away mid-run.** Leaving the page closes the stream and cancels the
  investigation server-side.
- A newly stored card takes a few seconds to appear under Investigations -- that view reads
  the search index, which lags writes. Don't cut to it instantly.
- Browser at a readable zoom; the decision panel is the thing that must be legible on a
  phone, not the log.
- Do a silent dry run first so pacing lines up with what's actually on screen. Run length
  varies -- the agent genuinely decides its own path.
- Keep it under 3:00. The brief requires it, and judges rarely watch past it.
