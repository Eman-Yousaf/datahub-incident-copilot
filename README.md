# Incident Copilot

An agent that investigates a data-quality incident by walking DataHub's lineage graph
live, narrating its reasoning step-by-step ("checking upstream... found a schema change
on orders.status 6h ago... tracing downstream... found 2 dashboards + 1 ML model at
risk... tagging them now"), then writes its findings back into DataHub so the next person
(or agent) inherits the investigation.

Built for the [DataHub Agent Hackathon](https://datahub.devpost.com/), Track 1 ("Agents
That Do Real Work").

## Status

Early scaffolding — see `TODO` markers in each module. Not yet functional end-to-end.

## Architecture

- `seed_data.py` — loads DataHub's real showcase-ecommerce datapack into a local
  quickstart instance, and locks the incident trigger points used for the demo
- `src/incident_copilot/mcp_client.py` — connects to the DataHub MCP server
- `src/incident_copilot/agent.py` — the ReAct agent loop (LangChain + Groq) bound to
  DataHub's MCP tools (read + mutation); the agent decides its own investigation path,
  it does not follow a fixed script
- `src/incident_copilot/narrate.py` — live, first-person narration of the agent's actual
  tool calls and reasoning as they happen
- `src/incident_copilot/writeback.py` — write-back helpers (tags / notes / structured
  properties) the agent chooses between based on what it finds
- `cli.py` — entry point: `python cli.py "our revenue dashboard looks wrong"`
- `examples/` — sample recorded investigation output

## Setup

Requires Docker (for DataHub's local quickstart), Python 3.11+, and a Groq API key.

```bash
uv sync
cp .env.example .env   # fill in GROQ_API_KEY, DATAHUB_GMS_TOKEN if needed
datahub docker quickstart
python seed_data.py
python cli.py "our revenue dashboard looks wrong"
```

## License

Apache 2.0 — see [LICENSE](./LICENSE).
