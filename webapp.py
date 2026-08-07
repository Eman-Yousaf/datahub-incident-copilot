"""Public-facing web demo: runs the agent against the 3 locked incident scenarios and
streams its live narration to a browser via Server-Sent Events.

Scope is intentionally narrow -- fixed scenario buttons, not free-form text input --
to bound both cost (each run is a real multi-turn Azure OpenAI conversation against a
live DataHub instance with mutation tools enabled) and abuse surface, since this is
meant to sit behind a public URL during hackathon judging. Reuses the exact same
agent/tool code path as cli.py; only the transport (SSE instead of stdout) differs.
"""

import asyncio
import time

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from incident_copilot.agent import build_agent
from incident_copilot.mcp_client import datahub_tools
from incident_copilot.narrate import format_new_messages

load_dotenv()

app = FastAPI()

SCENARIOS = {
    "clean-one-hop": {
        "label": "Clean one-hop",
        "prompt": "Order count numbers on our dashboards look wrong -- we seem to be "
        "undercounting backordered orders",
    },
    "ambiguous-multi-parent": {
        "label": "Ambiguous multi-parent",
        "prompt": "Promotion attribution numbers on our sales reports look off -- "
        "certain promotion types seem to be undercounted",
    },
    "low-severity": {
        "label": "Low severity",
        "prompt": "The order_details replica in Snowflake looks out of sync with the "
        "main table -- can you check what's going on?",
    },
}

_run_lock = asyncio.Lock()
_last_run_finished_at = 0.0
_MIN_SECONDS_BETWEEN_RUNS = 45


def _sse(text: str) -> str:
    # SSE requires every line of a multi-line message to carry its own "data:" prefix;
    # the browser's EventSource reassembles them (joined by \n) into one event.data.
    return "\n".join(f"data: {line}" for line in text.split("\n")) + "\n\n"


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


@app.get("/api/investigate/{scenario}")
async def investigate(scenario: str) -> StreamingResponse:
    if scenario not in SCENARIOS:
        raise HTTPException(404, "unknown scenario")

    if _run_lock.locked():

        async def busy():
            yield _sse("[Another investigation is already running -- try again in a moment]")
            yield "event: done\ndata: done\n\n"

        return StreamingResponse(busy(), media_type="text/event-stream")

    async def stream():
        global _last_run_finished_at
        async with _run_lock:
            wait = _MIN_SECONDS_BETWEEN_RUNS - (time.monotonic() - _last_run_finished_at)
            if wait > 0:
                yield _sse(f"[Cooling down {wait:.0f}s before the next run...]")
                await asyncio.sleep(wait)

            prompt = SCENARIOS[scenario]["prompt"]
            yield _sse(f"Incident Copilot investigating: {prompt!r}")
            seen = 0
            try:
                async with datahub_tools(prompt) as tools:
                    agent = build_agent(tools)
                    async for state in agent.astream(
                        {"messages": [{"role": "user", "content": prompt}]},
                        config={"recursion_limit": 50},
                        stream_mode="values",
                    ):
                        lines, seen = format_new_messages(state["messages"], seen)
                        for blank_before, line in lines:
                            yield _sse(("\n" + line) if blank_before else line)
            except Exception as exc:  # noqa: BLE001 -- surface any failure to the viewer
                yield _sse(f"[Investigation failed: {exc}]")
            finally:
                _last_run_finished_at = time.monotonic()
                yield "event: done\ndata: done\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


_SCENARIO_BUTTONS = "\n".join(
    f'<button onclick="run(\'{key}\')">{meta["label"]}</button>'
    for key, meta in SCENARIOS.items()
)

INDEX_HTML = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Incident Copilot -- live demo</title>
<style>
  body {{ background: #0b0f14; color: #d7e0ea; font-family: ui-monospace, Menlo, Consolas, monospace;
         max-width: 900px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.3rem; font-weight: 600; }}
  p.sub {{ color: #8a9bb0; margin-top: -0.5rem; }}
  button {{ background: #1b2a3a; color: #d7e0ea; border: 1px solid #33465c; border-radius: 6px;
           padding: 0.5rem 1rem; margin: 0.25rem 0.5rem 1rem 0; cursor: pointer; font-family: inherit; }}
  button:hover {{ background: #24374b; }}
  button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  pre {{ background: #05080b; border: 1px solid #1b2a3a; border-radius: 8px; padding: 1rem;
        white-space: pre-wrap; word-wrap: break-word; min-height: 200px; max-height: 70vh;
        overflow-y: auto; font-size: 0.85rem; line-height: 1.5; }}
  a {{ color: #6ea8fe; }}
</style>
</head>
<body>
<h1>Incident Copilot</h1>
<p class="sub">A live agent investigating real DataHub lineage -- pick a scenario, watch it reason.
Read more on <a href="https://github.com/Eman-Yousaf/datahub-incident-copilot" target="_blank">GitHub</a>.</p>
{_SCENARIO_BUTTONS}
<pre id="log">Pick a scenario above to start an investigation.</pre>
<script>
let es = null;
function run(scenario) {{
  document.querySelectorAll("button").forEach(b => b.disabled = true);
  const log = document.getElementById("log");
  log.textContent = "";
  if (es) es.close();
  es = new EventSource("/api/investigate/" + scenario);
  es.onmessage = (e) => {{
    log.textContent += e.data + "\\n";
    log.scrollTop = log.scrollHeight;
  }};
  es.addEventListener("done", () => {{
    es.close();
    document.querySelectorAll("button").forEach(b => b.disabled = false);
  }});
  es.onerror = () => {{
    log.textContent += "\\n[connection closed]\\n";
    es.close();
    document.querySelectorAll("button").forEach(b => b.disabled = false);
  }};
}}
</script>
</body>
</html>
"""
