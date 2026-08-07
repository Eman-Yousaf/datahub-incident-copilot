# Demo video script (target: under 3:00)

Working notes for recording the required demo video -- a shot list so recording is a
planned take rather than improvised.

**The story is not "watch an agent walk a lineage graph."** Plenty of agents do that. The
story is: *this one knows when not to act, explains why, learns from every investigation,
and never starts from scratch.* Everything below is ordered to land that, and to land it
before a judge stops watching.

**Before recording**: full clean baseline --
`docker container prune -f && datahub docker nuke && datahub docker quickstart &&
python seed_data.py`. Do not record against an instance that has had prior test
write-backs applied: earlier runs' tags and appended notes show up as pre-existing
catalog state and quietly destroy the "watch this happen live" framing. The reseed is
also what makes run 1 genuinely start from an empty memory.

Recording two runs back to back is the whole point -- **do not skip run 1 to save time.**
Run 1 is the one nobody else's demo has.

## 0:00-0:20 -- Hook

Lead with the refusal, not the capability:

> "Most incident agents give you an answer whether or not they have one. This one
> refuses -- and then it writes down exactly what it would need to be sure, so the next
> investigation doesn't start from zero."

## 0:20-1:10 -- Run 1: the refusal

Run the CLI against the incident report. Let it play close to real time; the narration is
already good prose, so point at it rather than reading it aloud.

Beats to call out as they scroll past:
- `recall_prior_investigations` fires **first**, and comes back empty -- this is a cold
  start, and the agent says so.
- It investigates for real, then hits the evidence checkpoint: **2 of 4 checks confirmed →
  LOW → severity `no_action`.**
- `add_tags` and `update_description` **do not run.** Say plainly: this is not the model
  choosing to be cautious, it is a gate in Python the model cannot reach.
- But the run is not empty -- an Investigation Card is written to DataHub naming the two
  checks that were missing.

The line to land here: **a refusal that leaves a record is worth more than a guess.**

## 1:10-2:05 -- Run 2: it continues instead of restarting

Same command, same code, same prompt. Nothing changed except that memory now exists.

- `recall_prior_investigations` returns run 1's card.
- The agent **skips the two checks already confirmed** and spends its calls only on what
  was missing. Call this out explicitly -- it is the "never starts from scratch" claim
  made visible.
- Watch for the schema-drift finding folded into `report_findings`' own output --
  **all three cross-platform mirrors of the table -- snowflake, looker, powerbi -- are
  running stale schema.** This is the beat worth slowing down for: DataHub's lineage
  shows those as connected and has no way to tell you they disagree on shape. It's not
  the agent's choice whether to check this -- `report_findings` runs the audit itself
  the moment it has a confirmed root cause and field name, so this beat can't get
  skipped on the take. Those mirrors keep producing the symptom even after the root
  cause is fixed.
- 4/4 → HIGH → `tag_note_escalated` → tags and an incident note are written, then
  **re-read back out of DataHub** to prove they actually landed.

If the run is long, speed up the middle and return to real time for the drift finding and
the write-back. Those are the two moments that sell it.

## 2:05-2:35 -- DataHub UI: proof it was real

Browser to the `order_details` dataset page (http://localhost:9002):
- the `incident-flagged` / `incident-severity-high` tags now on the entity
- the appended note with the agent's actual finding
- **the Investigation Cards themselves**, as `document` entities linked to the asset --
  run 2's card citing run 1's

That last one is the shot that proves the memory is durable catalog metadata a human can
read, not chat history that disappears when the process exits.

## 2:35-2:55 -- Close

> "Four things the model is never allowed to decide: its own confidence, its own severity,
> whether a write is permitted, and which entity that write may touch. All four are plain
> Python. The agent investigates; the code decides what may be done about it."

Optionally, the honest one-liner -- it tends to land well with engineers:

> "The gate caught a bug in itself: the first time the agent tried to tag something it
> wasn't authorized to, the block crashed the run instead of blocking it. That's in the
> commit history."

## 2:55-3:00 -- End card

Repo: github.com/Eman-Yousaf/datahub-incident-copilot
Live: incident-copilot-demo.centralindia.cloudapp.azure.com

## Recording notes

- Terminal font large enough to read on a phone.
- Do a silent dry run first so pacing lines up with what's on screen.
- Redirect stdout and stderr **separately** if capturing (`> out.log 2> err.log`) -- the
  MCP server's stderr logging is multi-KB of GraphQL and will bury the narration.
- Keep it under 3:00. The brief requires it, and judges rarely watch past it.
