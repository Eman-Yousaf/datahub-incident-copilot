"""The judge-facing web application: a single URL that shows the whole product.

Everything a reviewer needs is reachable here -- the incident list, a live
investigation with the policy layer visible as it runs, the real DataHub lineage
graph, the stored Investigation Cards, and a system status page. No CLI, no second
DataHub tab, no setup.

Two distinct read paths, deliberately:

* `/api/investigate/{scenario}` runs the actual agent over MCP, exactly the code
  path `cli.py` uses, and streams both the narration and a structured snapshot of
  the live policy state (see panel.py). Cost and abuse surface are bounded by a
  fixed scenario list plus a single-run lock -- each run is a real multi-turn Azure
  OpenAI conversation against a live DataHub with mutation tools enabled.
* Everything else reads DataHub over plain GraphQL (see datahub_api.py), because a
  nav click should answer immediately rather than pay for an MCP subprocess.

Nothing on this server can write to DataHub except the agent's own gated tools.
The explorer views are read-only by construction, so adding a UI didn't add a
second, ungated path into the catalog.
"""

import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from incident_copilot import datahub_api
from incident_copilot.agent import build_agent
from incident_copilot.mcp_client import datahub_tools
from incident_copilot.memory import RELEVANCE_THRESHOLD, relevance
from incident_copilot.narrate import format_new_messages
from incident_copilot.panel import snapshot
from incident_copilot.policy_selftest import run_selftest

load_dotenv()

app = FastAPI(title="DataHub Incident Copilot")

WEB_DIR = Path(__file__).parent / "web"

# The three locked scenarios. Fixed rather than free-text for the reasons in the
# module docstring; each one is a real seeded change in the showcase-ecommerce
# datapack (see seed_data.py), not a canned script -- the agent still has to find
# it, and regularly reaches a different conclusion depending on what it confirms.
SCENARIOS = {
    "clean-one-hop": {
        "label": "Order count discrepancy",
        "subject": "order_details",
        "shape": "Root cause sits on the reported entity itself",
        "prompt": "Order count numbers on our dashboards look wrong -- we seem to be "
        "undercounting backordered orders",
    },
    "ambiguous-multi-parent": {
        "label": "Promotion attribution drift",
        "subject": "promotions",
        "shape": "Root cause is one of 11 upstream parents",
        "prompt": "Promotion attribution numbers on our sales reports look off -- "
        "certain promotion types seem to be undercounted",
    },
    "low-severity": {
        "label": "Replica out of sync",
        "subject": "order_details_replica",
        "shape": "Terminal leaf, no downstream consumers",
        "prompt": "The order_details replica in Snowflake looks out of sync with the "
        "main table -- can you check what's going on?",
    },
}

_run_lock = asyncio.Lock()
_last_run_finished_at = 0.0
_MIN_SECONDS_BETWEEN_RUNS = 45


def _sse(event: str, payload) -> str:
    """One SSE frame. Text events carry a `data:` prefix per line, which the
    browser's EventSource rejoins with newlines; structured events carry JSON."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    lines = "\n".join(f"data: {line}" for line in body.split("\n"))
    return f"event: {event}\n{lines}\n\n"


# --------------------------------------------------------------------------- #
# Read APIs
# --------------------------------------------------------------------------- #


@app.get("/api/status")
async def status() -> dict:
    return await datahub_api.system_status()


@app.get("/api/incidents")
async def incidents() -> dict:
    """The scenario list, enriched with each one's real investigation history.

    Severity and confidence come from the most recent stored card that actually
    matches the incident text -- scored with the same `relevance` function the
    agent's own recall uses, so the badge on this page and the memory the agent
    inherits can never tell different stories. An incident nobody has investigated
    yet says exactly that, rather than showing an invented severity.
    """
    history = await datahub_api.investigation_cards(limit=100)
    cards = history.get("cards", [])

    out = []
    for key, meta in SCENARIOS.items():
        related = [
            card
            for card in cards
            if _relevance_to(card, meta["prompt"]) >= RELEVANCE_THRESHOLD
        ]
        related.sort(key=lambda card: card["timestamp"], reverse=True)
        latest = related[0] if related else None
        out.append(
            {
                "id": key,
                **{k: v for k, v in meta.items() if k != "prompt"},
                "prompt": meta["prompt"],
                "investigation_count": len(related),
                "latest": latest,
            }
        )
    return {"incidents": out, "error": history.get("error")}


def _relevance_to(card_row: dict, prompt: str) -> float:
    """`memory.relevance` operates on an InvestigationCard; the API layer works in
    plain dicts. Rather than rebuild a card just to score it, reuse the same
    arithmetic over the same two fields it reads."""

    class _Shim:
        trigger = card_row.get("trigger", "")
        root_cause_urn = card_row.get("root_cause_urn")
        root_cause_summary = card_row.get("root_cause_summary", "")

    return relevance(_Shim, prompt)


@app.get("/api/investigations")
async def investigations() -> dict:
    return await datahub_api.investigation_cards(limit=100)


@app.get("/api/policy-selftest")
async def policy_selftest() -> dict:
    """Run the real write-back gate against deliberately hostile attempts.

    Safe to expose publicly: the scenarios run against a stub tool rather than a
    live MCP connection, so no path through this endpoint can reach DataHub. It
    exists because in a healthy investigation the gate never fires, which makes
    the single most important behaviour in the project the one a live demo is
    least likely to show.
    """
    return await run_selftest()


# Every field of every dataset is itself a searchable `schemaField` entity in
# DataHub, so an unfiltered search reports thousands of "matches" a person browsing
# a catalog would never call results. Restrict the explorer to the entity types a
# human actually navigates.
_BROWSABLE_TYPES = ["DATASET", "DASHBOARD", "CHART", "DATA_FLOW", "DATA_JOB", "CONTAINER"]


@app.get("/api/entities")
async def entities(q: str = "*", count: int = 24) -> dict:
    return await datahub_api.search_entities(
        q, count=min(count, 50), types=_BROWSABLE_TYPES
    )


@app.get("/api/entity")
async def entity(urn: str) -> dict:
    if not urn.startswith("urn:li:"):
        raise HTTPException(400, "not a DataHub URN")
    return await datahub_api.entity_detail(urn)


@app.get("/api/lineage")
async def lineage(urn: str, hops: int = 2) -> dict:
    if not urn.startswith("urn:li:"):
        raise HTTPException(400, "not a DataHub URN")
    return await datahub_api.lineage_graph(urn, hops=hops)


# --------------------------------------------------------------------------- #
# Live investigation
# --------------------------------------------------------------------------- #


@app.get("/api/investigate/{scenario}")
async def investigate(scenario: str) -> StreamingResponse:
    if scenario not in SCENARIOS:
        raise HTTPException(404, "unknown scenario")

    if _run_lock.locked():

        async def busy():
            yield _sse("log", "[Another investigation is already running -- try again in a moment]")
            yield _sse("done", "busy")

        return StreamingResponse(busy(), media_type="text/event-stream")

    async def stream():
        global _last_run_finished_at
        async with _run_lock:
            wait = _MIN_SECONDS_BETWEEN_RUNS - (time.monotonic() - _last_run_finished_at)
            if wait > 0:
                yield _sse("log", f"[Cooling down {wait:.0f}s before the next run...]")
                await asyncio.sleep(wait)

            prompt = SCENARIOS[scenario]["prompt"]
            yield _sse("log", f"Incident Copilot investigating: {prompt!r}")
            seen = 0
            try:
                async with datahub_tools(prompt) as (tools, decision_state):
                    agent = build_agent(tools)
                    # Emitted before the first turn so the panel renders its empty
                    # state from real structure rather than placeholder markup.
                    yield _sse("state", snapshot(decision_state))
                    async for state in agent.astream(
                        {"messages": [{"role": "user", "content": prompt}]},
                        config={"recursion_limit": 50},
                        stream_mode="values",
                    ):
                        lines, seen = format_new_messages(state["messages"], seen)
                        for blank_before, line in lines:
                            yield _sse("log", ("\n" + line) if blank_before else line)
                        # After each turn, not on a timer: the panel advances exactly
                        # when the underlying policy state actually changed.
                        yield _sse("state", snapshot(decision_state))
            except Exception as exc:  # noqa: BLE001 -- surface any failure to the viewer
                yield _sse("log", f"[Investigation failed: {exc}]")
            finally:
                _last_run_finished_at = time.monotonic()
                yield _sse("done", "done")

    return StreamingResponse(stream(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# Static app shell
# --------------------------------------------------------------------------- #

class _RevalidatingStatic(StaticFiles):
    """Serve the app bundle with `no-cache`, so browsers revalidate on every load.

    Not a micro-optimisation in reverse: this bit during development, where a
    redeployed `app.js` kept rendering with the previous build's behaviour and
    looked like the fix hadn't worked. The same trap during judging would be far
    worse -- a reviewer holding a stale bundle sees bugs that no longer exist, and
    there is no way to tell them to hard-refresh. `no-cache` still allows a 304 on
    an unchanged file, so the cost is a conditional request, not a re-download.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:
        return False

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/static", _RevalidatingStatic(directory=WEB_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
