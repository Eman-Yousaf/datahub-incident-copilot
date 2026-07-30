# Incident Copilot

**"Order counts on our dashboards look wrong." Incident Copilot investigates — live, on
DataHub's real lineage graph — and writes what it finds back into the catalog.**

Point it at a one-line incident report. It searches DataHub for the entity involved,
narrates its own reasoning as it goes ("checking `order_status_detail` — description
mentions a new 'Backordered' sub-status, added recently — that's a plausible root
cause"), decides for itself whether to walk further upstream or stop, computes the real
downstream blast radius, and tags + annotates the responsible entity so the next person
(or agent) inherits the investigation instead of re-doing it. Not a fixed 5-step
pipeline with narration bolted on afterward — three different incident shapes take three
different, verifiably different paths through the same code (see
`examples/sample_incident_report.md` for an unedited transcript).

Built for the [DataHub Agent Hackathon](https://datahub.devpost.com/), Track 1 ("Agents
That Do Real Work").

## Status

Milestones 1-4 complete: DataHub's showcase-ecommerce datapack is seeded with 3 locked
incident-trigger scenarios, and the ReAct agent runs end-to-end against all 3 — resolving
the right entity, confirming a root-cause signal via a direct tool call before acting on
it, computing the correct blast radius, and choosing the correct write-back tier (tag-only
/ tag+note / tag+note+escalated) based on what it actually found. Live narration streams
as the investigation happens (`cli.py` + `narrate.py`) — a sample transcript is in
`examples/sample_incident_report.md`. Submission assembly and the demo video are still
ahead.

## Architecture

- `seed_data.py` — loads DataHub's real showcase-ecommerce datapack into a local
  quickstart instance, and locks the incident trigger points used for the demo
- `src/incident_copilot/mcp_client.py` — connects to the DataHub MCP server
- `src/incident_copilot/agent.py` — the ReAct agent loop (LangGraph + Azure OpenAI) bound to
  DataHub's MCP tools (read + mutation); the agent decides its own investigation path,
  it does not follow a fixed script
- `src/incident_copilot/narrate.py` — live, first-person narration of the agent's actual
  tool calls and reasoning as they happen
- `cli.py` — entry point: `python cli.py "our revenue dashboard looks wrong"`
- `examples/` — sample recorded investigation output

## Setup

Requires Docker (for DataHub's local quickstart), Python 3.11+, and an Azure OpenAI
deployment (any OpenAI-compatible chat model with tool calling works; swap the
`AzureChatOpenAI` import in `agent.py` if using a different provider).

```bash
uv sync
cp .env.example .env   # fill in AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY, DATAHUB_GMS_TOKEN if needed
datahub docker quickstart
python seed_data.py
python cli.py "our revenue dashboard looks wrong"
```

## License

Apache 2.0 — see [LICENSE](./LICENSE).
