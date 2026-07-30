# Incident Copilot — Hackathon Submission

Source for the DataHub Agent Hackathon submission form (Track 1: "Agents That Do Real
Work"). Kept in the repo so the text and the code never drift apart.

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
5. Writes back to DataHub: a tag always, a descriptive note if the blast radius is
   non-trivial, and an escalated severity tag if it spans multiple platforms or many
   consumers.
6. Prints a structured final summary (root cause, blast radius, what was written back).

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
