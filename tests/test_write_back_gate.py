"""Regression tests for the write-back gate -- the control the whole project rests on.

Run: `python tests/test_write_back_gate.py` (needs the project's deps, not a live
DataHub). Exits non-zero on failure.

Two bugs motivated this file, both found by running the agent rather than reading it:

1. The gate authorized a *severity tier* but never checked *which entity* was being
   written to, so a run tagged a mirror table alongside the real root cause while its
   authorization text said "the exact root-cause URN".
2. Every refusal returned a bare string, but DataHub's MCP tools are declared
   `response_format='content_and_artifact'` -- so LangChain raised ValueError and the
   whole investigation died instead of the model reading the refusal. The gate's
   central promise was false end-to-end, and an earlier version of this harness missed
   it by using a fake tool that didn't declare `response_format`.

So `FakeTool` mirrors the real contract, and the shape of every return is asserted --
a refusal has to satisfy exactly the same contract a real result does.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from incident_copilot.mcp_client import _authorized_targets, _gate_mutation_tool  # noqa: E402

ROOT = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD)"
SNOW = "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details,PROD)"
LOOKER = "urn:li:dataset:(urn:li:dataPlatform:looker,b2fd91.order-entry-looker.view.order_details,PROD)"
UNRELATED = "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.customers,PROD)"

FLAG = "urn:li:tag:incident-flagged"
HIGH = "urn:li:tag:incident-severity-high"

ran: list[dict] = []


class FakeTool:
    """Stand-in for a DataHub MCP tool.

    `response_format` is not incidental: it is how the real tools are declared, and
    it is what turns a bare-string refusal into a run-ending ValueError.
    """

    def __init__(self, name, response_format="content_and_artifact"):
        self.name = name
        self.response_format = response_format

        async def coroutine(*args, **kwargs):
            ran.append({"tool": name, "kwargs": kwargs})
            return ("success: true", None) if response_format == "content_and_artifact" else "success: true"

        self.coroutine = coroutine


def gated(name, state, response_format="content_and_artifact"):
    return _gate_mutation_tool(FakeTool(name, response_format), state, get_entities_tool=None)


def drift(*stale_urns):
    records = [{"urn": u, "platform": "x", "status": "stale"} for u in stale_urns]
    return {"checked_field": "order_status_detail", "root_urn": ROOT,
            "mirrors_checked": records, "mirrors_stale": records}


results: list[bool] = []


def expect(label, got, blocked: bool, *, tuple_shape=True):
    """Assert both the decision and the return shape LangChain will enforce."""
    if tuple_shape and not (isinstance(got, tuple) and len(got) == 2 and isinstance(got[0], str)):
        print(f"FAIL  {label}\n        wrong shape {type(got)} -- LangChain raises ValueError here")
        results.append(False)
        return
    text = got[0] if isinstance(got, tuple) else got
    ok = text.startswith("Blocked:") == blocked
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        -> {text[:140]}")
    results.append(ok)


async def main():
    # --- the observed regression: a mirror tagged with no drift check to back it ---
    state = {"severity": "tag_note_escalated", "root_cause_urn": ROOT}
    before = len(ran)
    got = await gated("add_tags", state).coroutine(tag_urns=[FLAG], entity_urns=[ROOT, SNOW, ROOT])
    expect("mirror tagged, drift never checked -> blocked", got, True)
    print(f"{'PASS' if len(ran) == before else 'FAIL'}  ...and the real tool never ran")
    results.append(len(ran) == before)

    # --- but once code proved those mirrors stale, flagging them is earned ---
    state = {"severity": "tag_note_escalated", "root_cause_urn": ROOT, "schema_drift": drift(SNOW, LOOKER)}
    got = await gated("add_tags", state).coroutine(tag_urns=[FLAG], entity_urns=[ROOT, SNOW, LOOKER])
    expect("confirmed-stale mirrors tagged -> allowed", got, False)

    got = await gated("add_tags", state).coroutine(tag_urns=[FLAG], entity_urns=[UNRELATED])
    expect("unrelated entity tagged -> blocked", got, True)

    # --- the incident narrative stays on the root cause, even for a stale mirror ---
    got = await gated("update_description", state).coroutine(entity_urn=SNOW, operation="append", description="x")
    expect("note appended to stale mirror -> blocked", got, True)

    got = await gated("update_description", state).coroutine(entity_urn=ROOT, operation="append", description="x")
    expect("note appended to root cause -> allowed", got, False)

    # --- no confirmed root cause: nothing is authorized, whatever the tier says ---
    got = await gated("add_tags", {"severity": "tag_note_escalated", "root_cause_urn": None}).coroutine(
        tag_urns=[FLAG], entity_urns=[ROOT])
    expect("no root cause confirmed -> blocked", got, True)

    # --- the pre-existing severity gates still fire, and still return a usable shape ---
    got = await gated("add_tags", {}).coroutine(tag_urns=[FLAG], entity_urns=[ROOT])
    expect("report_findings never called -> blocked", got, True)

    got = await gated("add_tags", {"severity": "no_action", "root_cause_urn": ROOT}).coroutine(
        tag_urns=[FLAG], entity_urns=[ROOT])
    expect("no_action severity -> blocked", got, True)

    tag_only = {"severity": "tag_only", "root_cause_urn": ROOT}
    got = await gated("add_tags", tag_only).coroutine(tag_urns=[FLAG, HIGH], entity_urns=[ROOT])
    expect("severity-high tag at tag_only -> blocked", got, True)

    got = await gated("update_description", tag_only).coroutine(entity_urn=ROOT, operation="append", description="x")
    expect("update_description at tag_only -> blocked", got, True)

    got = await gated("add_tags", tag_only).coroutine(tag_urns=[FLAG], entity_urns=[ROOT])
    expect("tag_only on root cause -> allowed", got, False)

    # --- the shape helper is contract-driven, not hardcoded to tuples ---
    got = await gated("add_tags", {"severity": "no_action", "root_cause_urn": ROOT}, "content").coroutine(
        tag_urns=[FLAG], entity_urns=[ROOT])
    ok = isinstance(got, str) and got.startswith("Blocked:")
    print(f"{'PASS' if ok else 'FAIL'}  plain-string tool contract -> bare string refusal")
    results.append(ok)

    # --- _authorized_targets: mirrors widen tagging only, never the note ---
    state = {"root_cause_urn": ROOT, "schema_drift": drift(SNOW, LOOKER)}
    for label, actual, want in (
        ("add_tags = root + stale mirrors", _authorized_targets("add_tags", state), {ROOT, SNOW, LOOKER}),
        ("update_description = root only", _authorized_targets("update_description", state), {ROOT}),
    ):
        print(f"{'PASS' if actual == want else 'FAIL'}  _authorized_targets({label})")
        results.append(actual == want)

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


raise SystemExit(asyncio.run(main()))
