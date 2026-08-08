# Demo video script (hard limit: 3:00)

Working notes for the required demo video — a shot list, so recording is a planned take
rather than an improvisation.

**The structural decision: lead with the revocation, don't build to it.**

Every submission in this hackathon climaxes at 2:30. Judges stop watching well before
that. So the first twenty seconds are the thing nothing else in the field can show —
a write that succeeds, then the identical write refused because DataHub moved underneath
the permission — and everything after it explains how that was possible.

Do not open with an architecture diagram. Do not open with a nav tour.

**Record the web app**, except for the cold-open, which is the terminal. A judge watching
a terminal has to take your word for what happened; a judge watching the decision panel
can see the policy resolve. The one exception is the counterfactual, where the terminal is
*better* — it makes clear no browser state is involved.

## Before recording

Full clean baseline, on the VM:

```
docker container prune -f && datahub docker nuke && datahub docker quickstart
cd ~/incident-copilot && .venv/bin/python seed_data.py
sudo systemctl restart incident-copilot-web.service
```

Wait ~3 minutes before testing anything — DataHub's search index fills in slowly after a
reseed, and an early check looks like a broken app.

**Check `docker ps` shows six containers** right before you record. OpenSearch on this VM
dies roughly every 26 hours; the watchdog cron restarts it, but a run that starts mid-repair
looks broken. `curl` returning 200 proves nothing — the web server answers either way.

**Record the memory run before any other take.** Every investigation stores a new card, and
recall returns the two *most relevant*. After a couple of takes your fresh cards outrank the
seeded history and the CONFLICT beat stops firing. If you need another take, re-run
`seed_data.py` first.

**Dry-run `python counterfactual.py` once** before recording it. It takes ~40s including the
settle waits, and you want to know its real pacing rather than discovering it on camera.

---

## 0:00–0:22 — Cold open: the authority disappears

Terminal, already scrolled to the STEP 4/5 output of `python counterfactual.py`. No preamble,
no logo.

> "This agent just wrote to a data catalog. It was allowed to, and here's the proof it was
> allowed to — the exact field, on the exact table, that justified it."

Point at `AUTH-…` and predicate `P3`.

> "Then that field was removed from DataHub. Nobody told the agent. It wasn't asked again,
> and no model was involved in what happens next."

Scroll to the refusal.

> "Same authorization. Same write. Refused — because the thing it rested on stopped being
> true. The agent didn't change its mind. The evidence changed, so the authority changed."

**This is the whole submission.** Everything after this is showing that it's real.

## 0:22–0:40 — What the problem actually is

Cut to the Command Center.

> "Most agents that touch a catalog get permission once and keep it. But catalogs move —
> a field gets reverted, a mirror gets repaired — and the justification quietly stops being
> true while the agent is still working. So here, permission is an artifact with grounds,
> and the grounds are re-read at the moment of the write."

Point at one number: **actions / refusals**. A refusal is a recorded outcome, not a failure.

## 0:40–1:15 — A real investigation, live

Click **Order count discrepancy → Investigate**.

Let the left pane scroll; don't read it aloud. Narrate the right panel:

- the progress list ticking off real DataHub work
- **◈ DataHub calls** climbing — every one a real MCP call to GMS
- the four evidence checks filling in as they're confirmed

Say plainly: *the model supplies evidence. It never scores itself.*

## 1:15–1:45 — The memory beat

The purple banner appears before the agent touches anything: prior investigations found in
DataHub, what each established, what's still missing.

Then both cards are re-tested against the live graph:

```
✓ confirmed   INC-20260806-091500   order_status_detail still present -- finding holds
⚠ conflict    INC-20260807-143000   order_status_code_v1 is gone -- withdrawn as evidence
```

> "A stored finding is a true record of when it was written, not a standing fact. This one
> no longer matches the graph, so it's withdrawn — and the agent falls back to what it can
> prove itself."

Show the consequence, which is what proves it isn't cosmetic: withdrawn checks reset to
unconfirmed, confidence drops, severity falls a tier. Verified live: 4/4 claimed → 2/4
allowed → HIGH → MEDIUM.

## 1:45–2:15 — The authorization panel

This is the beat the cold open promised to explain.

- **Authorization**: `AUTH-…`, decision, and the predicates — each showing the aspect
  (`schemaMetadata`), the tool that read it, the URN, and the observed value.
- Say it once: *the id is a hash of those grounds. Same evidence, same id, every run.*
- **Write-back gate**: authorized targets listed explicitly. Severity says how much;
  the target list says to what.
- Write-back events: `PROPOSED → ALLOWED → APPLIED → VERIFIED` — the last a re-read out of
  DataHub, not a bare `success: true`.

If there's room: **Cross-platform mirrors** — snowflake / looker / powerbi chips. Lineage
shows all three connected and cannot tell you they disagree on *shape*. Two of the three
are only reachable at 2 hops.

## 2:15–2:35 — Don't trust it, check it

Two commands, on screen, fast.

```
python verify_authorization.py      # recompute every stored authorization
```

> "Every card stores the grounds its id was computed from. This recomputes all of them
> straight out of the catalog. Edit one and it fails."

Then **Policy → Run the attacks**.

> "Thirteen hostile writes against the real gate — including two that move DataHub
> underneath a live authorization. Three are legitimate and must succeed: a gate that
> blocks everything proves nothing."

## 2:35–2:52 — It compounds

**Investigations** in the nav: every run ever completed, read back out of DataHub as real
`document` entities, refusals included, `↩ continues` chains between them, each carrying the
authorization it acted under.

> "Not chat history. Catalog metadata a human opens on the dataset page — and the next
> investigation reads it and continues."

## 2:52–3:00 — End card

> "Most agents tell you what happened. This one can prove why it was allowed to act —
> and notices when it isn't anymore."

```
github.com/Eman-Yousaf/datahub-incident-copilot
incident-copilot-demo.centralindia.cloudapp.azure.com
Upstream: acryldata/mcp-server-datahub #155, #198 (submitted, not merged)
```

---

## Backup plans

Assume something breaks; decide now rather than on camera.

| If this fails | Do this |
|---|---|
| VM down / OpenSearch dead | Cut to the pre-recorded fallback capture (record one *today*, before you need it) |
| Live investigation stalls mid-run | Cut to Investigations and narrate a stored card — same policy layer, already resolved |
| `counterfactual.py` races the settle window | It aborts rather than showing a race. Re-run; it's idempotent and restores the field in `finally` |
| Agent reaches a different conclusion than the take you planned | Say so out loud. It's a feature — the run table in the README shows three different outcomes from one prompt |

## Recording notes

- **Don't navigate away mid-run.** Leaving the page closes the stream and cancels the
  investigation server-side.
- A newly stored card takes a few seconds to appear under Investigations — that view reads
  the search index, which lags writes. Don't cut to it instantly.
- Browser at a readable zoom. The decision panel must be legible on a phone; the log doesn't
  have to be.
- Do one silent dry run so pacing matches what's actually on screen. Run length varies —
  the agent genuinely decides its own path.
- Keep it under 3:00. The rules require it and judges rarely watch past it.
