# Demo video script (target: under 3:00)

Working notes for recording the required demo video. Not part of the submitted repo
content in spirit (delete or keep, your call) -- just a shot list so recording is a
quick, planned take rather than improvised.

**Before recording**: full clean baseline --
`datahub docker nuke && datahub docker quickstart && python seed_data.py` -- do not
record against an instance that's had prior test write-backs applied (they'll show up
as pre-existing tags/notes and undercut the "watch it happen live" framing). Use shape
(a), the clean-one-hop scenario -- it's the clearest single-hop story for someone
seeing this for the first time, and it also has an unedited real transcript already
saved at `examples/sample_incident_report.md` you can rehearse against.

## 0:00-0:15 -- Hook (talking head or voiceover over a title card)

"Incident Copilot investigates data-quality incidents by walking DataHub's real lineage
graph live -- not a canned report, an agent that decides its own investigation path and
writes what it finds back into the catalog."

## 0:15-1:45 -- Terminal: the live investigation (screen recording)

Run: `python cli.py "Order count numbers on our dashboards look wrong -- we seem to be
undercounting backordered orders"`

Let it run close to real-time so the narration is visibly live, not a fast-forwarded
log dump. Points to call out as they scroll by (either live voiceover or captions):
- The `search` call resolving to the real `order_details` dbt table -- this is DataHub's
  own showcase-ecommerce reference data, not synthetic
- `list_schema_fields` finding `order_status_detail`, described as a new "Backordered"
  sub-status -- the actual root-cause signal, confirmed on that exact entity
- `get_lineage` computing blast radius
- `add_tags` / `update_description` -- the write-back actually happening, live

If the full run is long, it's fine to speed up the middle (search/schema calls) and
slow back to real-time for the moment the signal is found and the write-back happens --
those are the two moments that sell "this is really deciding things."

## 1:45-2:30 -- Cut to DataHub UI (screen recording, browser)

Navigate to the `order_details` dataset page in DataHub's UI (http://localhost:9002).
Show:
- The `incident-flagged` tag now present on the entity
- The appended description/note with the agent's actual finding
- (Optional, if time allows) the lineage graph view, zoomed to show the real
  cross-platform fan-out (dbt -> Snowflake -> Looker/Tableau/PowerBI) that made this a
  meaningful graph to investigate, not a toy one

This is the proof-of-write-back shot -- confirms the tags/notes shown aren't just
console output, they actually landed in DataHub.

## 2:30-2:55 -- Close (talking head or voiceover over a final card)

One or two sentences: real agentic decisions (not a fixed pipeline -- different runs
take different paths depending on what's found), real DataHub data, real write-back.
Built for Track 1, "Agents That Do Real Work."

## 2:55-3:00 -- End card

Repo URL: github.com/Eman-Yousaf/datahub-incident-copilot

## Recording notes

- Terminal font size large enough to read on a phone screen
- If narrating live over the terminal run, do a silent dry run first so the pacing of
  what you say lines up with what's on screen -- the LLM's own narration text is
  already good prose, you're mostly just pointing at it, not repeating it verbatim
- Keep total under 3:00 -- the brief requires it, and judges are unlikely to watch past
  it anyway
