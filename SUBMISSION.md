# Incident Copilot — Hackathon Submission

Source for the DataHub Agent Hackathon submission form (Track 1: "Agents That Do Real
Work"). Kept in the repo so the text and the code never drift apart.

**The problem**: when a dashboard number looks wrong, the person who notices usually
isn't the person who can explain why — that takes someone manually tracing lineage
backward through the warehouse, checking recent schema changes, and figuring out who
else is affected. Incident Copilot does that walk itself, live, on DataHub's real
context graph, and writes the answer back so the next person (or agent) doesn't have to
redo it.

**Live demo**: https://incident-copilot-demo.centralindia.cloudapp.azure.com — try it
directly in a browser, no setup needed. **Repo**:
https://github.com/Eman-Yousaf/datahub-incident-copilot.

## Features

- **Investigates data-quality incidents by walking DataHub's real lineage graph live** —
  not a canned report generator. Given a plain-English incident report (e.g. "order count
  numbers on our dashboards look wrong"), the agent resolves the right entity, inspects
  its schema for a recent, symptom-matching change, and walks lineage upstream/downstream
  as needed.
- **A real agent loop, not a fixed pipeline.** Built as a LangGraph ReAct agent bound
  directly to DataHub's MCP tools — the LLM decides at each step whether to keep walking
  lineage, which of several upstream parents to prioritize (using real signals: recency,
  query frequency), and when to stop. Validated across 3 distinct trigger scenarios with
  divergent tool-call traces, not a single scripted sequence with narration bolted on.
- **Live, first-person narration.** Prints the agent's own reasoning and each tool call/
  result as they happen, so a human watches the investigation unfold instead of reading a
  report after the fact.
- **Writes findings back into DataHub**, not just to a console: tags affected entities and
  appends an incident note, choosing between tag-only / tag+note / tag+note+escalated
  based on the actual computed blast radius — a genuinely conditional decision, verified
  to differ correctly across all 3 test scenarios.
- **Confidence scoring, shown as a heuristic, not a fabricated statistic.** Before any
  write-back, the agent reports a checklist of 4 evidence items it actually confirmed via
  tool calls (recent schema change, symptom match, lineage path, downstream impact).
  Confidence is `checked/total`, bucketed to low/medium/high — never a manufactured
  precise percentage — and the checklist is shown in full so a judge can see exactly why.
- **Severity is a deterministic function, not a free LLM judgment call.**
  `severity = f(confidence, affected datasets, affected dashboards, business
  criticality)` is computed in plain Python (`decision.py`), the same function every
  time — inspectable and reproducible, not something the model decides fresh each run.
- **A real "do not act" path, enforced in code.** If confidence comes back low (or the
  investigation is inconclusive), the write-back tools are *blocked at the code level* —
  not just discouraged by a prompt — and the agent is routed to recommend human review
  instead of guessing. Verified live: a run that hit a real backend search failure
  correctly produced 0/4 evidence, LOW confidence, and made zero write-back attempts.
- **Write-back is verified, not just trusted.** Every successful `add_tags`/
  `update_description` call automatically re-fetches the entity from DataHub and shows
  the result — the tag or note is visibly present in the response, not just a bare
  `success: true` a judge has to take on faith.
- **Anti-hallucination guardrails**: never fabricates a URN, never claims a node "changed
  recently" without a tool call confirming it on that exact URN, and reports "inconclusive"
  rather than guessing when evidence doesn't support a conclusion.

## Functionality

Run `python cli.py "our revenue dashboard looks wrong"` against a running DataHub
instance. The agent:

1. Searches DataHub for the entity the report is about, preferring the warehouse/
   transform layer (dbt/SQL) over thin BI-tool passthroughs (Looker/Tableau/PowerBI)
   as its investigation anchor.
2. Inspects that entity's schema fields for one that changed more recently than its
   siblings and plausibly explains the symptom.
3. If nothing is found, walks upstream lineage (capped at 3 hops) and repeats, reasoning
   about which branch to prioritize when a node has multiple parents.
4. Once a root cause is confirmed, computes downstream blast radius via lineage.
5. Reports its findings through a required checkpoint tool: an evidence checklist that
   becomes a confidence level, which becomes a computed severity tier — code, not the
   model, decides what it's authorized to do next.
6. Writes back to DataHub only what that tier authorizes (or nothing, if confidence was
   too low), then verifies the mutation actually landed by re-reading the entity.
7. Prints a structured final summary (root cause, confidence/severity, blast radius,
   what was written back).

## Technologies

- **DataHub OSS** (`v1.5.0.6`, Docker quickstart) as the metadata/lineage source of truth
- **DataHub MCP Server** (`mcp-server-datahub`) exposing DataHub's search, lineage, schema,
  and mutation (tag/description write-back) tools over MCP
- **LangGraph** (`create_react_agent`) for the ReAct agent loop, bound directly to the MCP
  tools via `langchain-mcp-adapters`
- **Azure OpenAI** (`gpt-5-nano`) as the LLM
- **Python** (`uv` for dependency management)

## Data used

DataHub's own real **showcase-ecommerce reference datapack** — ~1,300 entities with
genuine cross-platform lineage (Postgres → S3 → Snowflake → dbt → Looker/Tableau/PowerBI,
plus Spark ETL jobs), rather than a hand-built synthetic graph. The datapack has no
naturally-occurring recent schema-change event to serve as an incident trigger, so 3
timestamped field additions are overlaid onto 3 real entities (`seed_data.py`) to create
3 locked, reproducible incident scenarios of increasing difficulty — the underlying
lineage graph itself is entirely real DataHub reference data.

## Open-source contribution

Building this surfaced a real, reproducible gap in `mcp-server-datahub` itself: its
`search` tool's filter docs don't warn that `entity_type = report` isn't valid, or
that PowerBI/Tableau/Looker report-style artifacts are actually indexed as
`entity_type = dataset` — the agent hit this directly (an LLM guessed `report`, got
zero results, burned retries before recovering). Filed as a docs fix upstream:
[acryldata/mcp-server-datahub#155](https://github.com/acryldata/mcp-server-datahub/pull/155).
