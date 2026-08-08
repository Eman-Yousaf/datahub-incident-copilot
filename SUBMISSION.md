# Incident Copilot — Hackathon Submission

Source for the DataHub Agent Hackathon submission form (Track 1: "Agents That Do Real
Work"). Kept in the repo so the text and the code never drift apart.

**One sentence**: Incident Copilot is a trust-aware incident investigation agent that
gathers evidence, refuses unsafe actions through deterministic policy, writes structured
operational knowledge back into DataHub, and enables future investigations to continue
instead of starting from scratch.

**Live demo**: https://incident-copilot-demo.centralindia.cloudapp.azure.com — the whole
product runs in a browser: no CLI, no setup, no second DataHub tab. **Repo**:
https://github.com/Eman-Yousaf/datahub-incident-copilot.

The demo is an application, not a log viewer. A command center over the real catalog; an
incident list; a live investigation workspace where the evidence checklist, the confidence
arithmetic and the **write-back gate** resolve on screen as the agent works; the stored
Investigation Cards read back out of DataHub; the real lineage graph (interactive, and every
edge is a relationship DataHub actually traversed — none are inferred from layout); an
entity explorer; and a live system-status page.

The investigation panel reads the same `decision_state` object the mutation gate reads, not
the narration text beside it. That distinction is the point: a panel reconstructed from what
the model *said* could disagree with what the code *computed*, and catching exactly that
disagreement is why the policy lives in Python. So when the gate refuses a write, the refusal
appears on screen with the reason the code gave — `PROPOSED → BLOCKED`, verbatim.

Nothing on screen is rescaled or invented. Confidence renders as `confirmed / 4` and the
level it maps to; there is no percentage anywhere, because the evidence checklist doesn't
have that precision to give.

## The problem

Two problems, actually, and the second is the one nobody solves.

When a dashboard number looks wrong, the person who notices isn't the person who can
explain why — that takes someone tracing lineage backward, checking recent schema
changes, and working out who else is affected. An agent can do that walk.

But an agent that does the walk still leaves you with the harder problem: **the
investigation evaporates.** Next week the same symptom resurfaces and the next run — human
or agent — starts from zero. Worse, when the evidence is thin, an autonomous agent either
guesses and writes something wrong into your catalog, or gives up and produces nothing at
all. Neither outcome is operational knowledge.

Incident Copilot makes uncertainty productive: it refuses to act on thin evidence *by
policy enforced in code*, and it records the refusal as a structured, durable artifact
inside DataHub that the next investigation reads and continues from.

## Features

- **Persistent investigation memory, stored in DataHub itself.** Every run — including a
  refusal — writes an **Investigation Card** as a `document` entity linked to the assets it
  concerned: incident id, trigger, evidence confirmed, evidence missing, hypotheses tested,
  hypotheses rejected, confidence, severity, the decision (ACTION or REFUSAL), the exact
  refusal reason, what evidence would make action safe, and the provenance of every
  conclusion. Not chat memory — durable catalog metadata a human sees on the dataset page.
- **Replay avoidance: the next investigation continues, it doesn't restart.** Before
  anything else the agent calls `recall_prior_investigations`, inherits the checks already
  confirmed, skips the hypotheses already disproven, and spends its tool calls only on
  what's missing or may have changed. Verified live: a second run explicitly continued a
  stored card rather than re-deriving it.
- **A refusal is a first-class outcome, not a failure.** The severity gate blocks *acting
  on the data*; it never blocks *recording what was learned*. The lower the confidence, the
  more valuable the card — it's what lets the next run skip straight to the missing evidence
  instead of rediscovering the same dead end.
- **The LLM never decides whether writes are allowed. Python does.** Confidence is
  `confirmed / total` on a 4-item evidence checklist. Severity is
  `f(confidence, affected datasets, affected dashboards, business criticality, confirmed
  stale mirrors)` — the same plain function every run, in `decision.py`. Low confidence or
  an inconclusive outcome makes `add_tags`/`update_description` refuse to run at the code
  level, and routes to human review. Not a prompt instruction the model usually follows: a
  control it cannot reach.
- **Nor which entity a write may touch.** Severity answered *how much* the agent may do and
  never *to what* — a gap found by reading this project's own live trace, where a run tagged
  a mirror table alongside the real root cause while the authorization text it had just been
  handed said "the exact root-cause URN". That rule had only ever lived in the prompt.
  `_authorized_targets` now derives the permitted set in Python: the confirmed root cause,
  plus — for tagging only — mirrors the drift check *proved* stale. Everything else is
  refused before the tool runs.
- **Finds what the lineage graph structurally cannot tell you — automatically, not on the
  model's initiative.** DataHub's lineage is topologically honest but schema-blind: an edge
  asserts that two datasets are connected, never that they agree on shape.
  `report_findings` runs this check itself, in code, the instant it has a confirmed root
  cause and the field that changed — it used to be a tool the agent could choose to call or
  skip, but a finding this central to the pitch can't depend on the model remembering to
  ask for it. It walks two hops in both directions, name-matches the same real-world table
  across platforms, and confirms field-by-field whether each mirror actually picked up the
  change. On DataHub's own showcase-ecommerce datapack, all three mirrors of the dbt
  `order_details` model — snowflake, looker, powerbi — were running stale schema, and two
  of the three are only reachable at 2 hops. They keep producing the same symptom after the
  root cause is fixed. The agent supplies only the field name; the verdict is established
  by real tool calls in code, never asserted.
- **Prior knowledge is a hypothesis, not truth — and the graph gets a veto.** A stored
  card can be entirely genuine and still be out of date. An investigation that correctly
  proved `order_status_detail` was the cause last week is a true record *of last week*; if
  that field has since been reverted or the table rebuilt, the card describes a world that
  no longer exists. Inheriting it would let a stale finding buy confidence in the present —
  the specific way an agent with memory becomes worse than one without. So every recalled
  card naming a concrete claim (a field on a specific URN) has that claim **re-tested
  against live DataHub** before the confidence arithmetic runs: `confirmed`, `conflict`, or
  `unverifiable`. Only a conflict withdraws the card — an absence you couldn't confirm is
  not evidence of absence. And a withdrawn card doesn't just lose a badge: its checks stop
  backing inheritance entirely, so the run falls back to what it proved itself. Verified end
  to end: a run inheriting two checks from a contradicted card drops to 0/4 LOW and refuses
  to act **despite a 25-entity blast radius across two platforms**.
- **Inherited evidence is verified, not taken on faith.** A run that claims it carried a
  check forward from a prior card has that claim checked against the cards recall actually
  returned. An unbacked claim resets the check to unconfirmed — lowering confidence, and
  potentially pulling severity down to `no_action`. The rejection is recorded on the card.
  This closed a real hole found during validation, where a run claimed to inherit a check
  from prior cards that had confirmed nothing.
- **Cards are built by code, not authored by the model.** The agent supplies evidence;
  confidence, severity, the refusal reason and the required-before-retry list are all
  derived. Identical evidence always produces an identical card — a judge can reconstruct
  why any conclusion was reached without trusting the narrative.
- **Recall is deterministic too.** Finding prior cards is Python: list documents, grep for
  the card marker, decode the exact embedded JSON payload, score relevance arithmetically
  against the incident text. The model never gets to decide which past investigation it
  "feels" related to, so the same incident always inherits the same evidence.
- **Investigates by walking DataHub's real lineage graph live** — not a canned report
  generator. Given a plain-English report, the agent resolves the right entity, inspects
  its schema for a recent symptom-matching change, and walks lineage up/down as needed,
  deciding its own path. Validated across 3 distinct scenarios with divergent tool traces.
- **Write-back is verified, not trusted.** Every successful mutation re-fetches the entity
  and shows the tag or note present, rather than a bare `success: true`. The card write-back
  re-reads itself from DataHub the same way.
- **Live, first-person narration**, so a human watches the investigation unfold rather than
  reading a report afterward.
- **Anti-hallucination guardrails**: never fabricates a URN, never claims something
  "changed recently" without a tool call confirming it on that exact URN, and reports
  inconclusive rather than guessing.

## Functionality

Run `python cli.py "our revenue dashboard looks wrong"` against a running DataHub instance.
The agent:

1. **Recalls prior investigations** of this incident from DataHub, and inherits what they
   established: confirmed evidence to reuse, hypotheses already disproven, and the specific
   evidence a previous run said was still missing.
2. Searches DataHub for the entity the report is about, preferring the warehouse/transform
   layer (dbt/SQL) over thin BI-tool passthroughs as its anchor.
3. Inspects that entity's schema for a field that changed more recently than its siblings
   and plausibly explains the symptom — skipping checks a recalled card already confirmed.
4. If nothing is found, walks upstream lineage (capped at 3 hops), reasoning about which
   branch to prioritize when a node has several parents.
5. Once a root cause is confirmed, computes downstream blast radius via lineage.
6. Optionally checks cross-platform schema drift: whether same-entity mirrors on other
   platforms still carry the field that changed, or will keep producing the symptom after
   the root cause is fixed.
7. Reports findings through a required checkpoint: an evidence checklist becomes a
   confidence level becomes a computed severity tier. Inheritance claims are validated here,
   before the arithmetic — code, not the model, decides what happens next.
8. Writes back only what that tier authorizes, and only to the entities code confirmed
   something about — **or refuses outright** — then verifies any mutation actually landed by
   re-reading the entity.
9. **Writes the Investigation Card back into DataHub**, always, act or refuse, linked to the
   affected assets and to the prior cards it continued.
10. Prints a structured summary: root cause, confidence/severity, blast radius, what was
    written back, and which checks it was able to skip because a previous run had done them.

## Technologies

Implementation details — the substance of the project is the trust and memory layer built
on top of these.

- **DataHub OSS** (`v1.5.0.6`, Docker quickstart) as the metadata/lineage source of truth,
  *and* as the durable store for investigation knowledge (`document` entities)
- **DataHub MCP Server** (`mcp-server-datahub`) exposing search, lineage, schema, document
  read/write, and mutation tools over MCP
- **LangGraph** (`create_react_agent`) for the agent loop, bound to the MCP tools via
  `langchain-mcp-adapters`
- **Azure OpenAI** (`gpt-5-nano`) as the LLM
- **Python** (`uv` for dependency management)

## Data used

DataHub's own real **showcase-ecommerce reference datapack** — ~1,300 entities with genuine
cross-platform lineage (Postgres → S3 → Snowflake → dbt → Looker/Tableau/PowerBI, plus
Spark ETL jobs), rather than a hand-built synthetic graph. The datapack has no
naturally-occurring recent schema-change event to serve as an incident trigger, so 3
timestamped field additions are overlaid onto 3 real entities (`seed_data.py`) to create 3
locked, reproducible scenarios of increasing difficulty — the underlying lineage graph is
entirely real DataHub reference data. Investigation Cards are written back into that same
instance as first-class catalog documents.

## Open-source contribution

Building this surfaced a real, reproducible gap in `mcp-server-datahub` itself: its
`search` tool's filter docs don't warn that `entity_type = report` isn't valid, or that
PowerBI/Tableau/Looker report-style artifacts are actually indexed as
`entity_type = dataset` — the agent hit this directly (an LLM guessed `report`, got zero
results, burned retries before recovering). Filed as a docs fix upstream:
[acryldata/mcp-server-datahub#155](https://github.com/acryldata/mcp-server-datahub/pull/155).

A second, sharper bug surfaced during validation: the server exposes `sort_by`/`sort_order`
on `search`, and a model naturally fills in `sort_by="relevance"` — which DataHub OSS has no
field to sort on, so OpenSearch rejects every such query with `query_shard_exception: No
mapping found for [relevance]` → `all shards failed` (HTTP 400). It presents as a
search-backend outage while actually being an unvalidated-argument problem. Worked around in
this repo by stripping the arguments in code (`_wrap_search_sort_compat` in `mcp_client.py`);
filed as a behavioral fix upstream:
[acryldata/mcp-server-datahub#198](https://github.com/acryldata/mcp-server-datahub/pull/198)
— normalizes `sort_by="relevance"` to no-op rather than forwarding it to the backend.
