"""Regression tests for report_findings' automatic schema-drift audit.

`check_schema_drift` used to be a tool the agent could choose to call or skip (step
5b, "optional, at most once"). That made the demo's centerpiece finding -- three
cross-platform mirrors silently running stale schema -- dependent on the model
remembering to ask for it. Now `report_findings` runs the audit itself whenever it
has enough to run it on (a confirmed root cause and a freshly-confirmed field name),
so the finding can't be skipped by the model simply not calling a separate tool.

Run: `python tests/test_report_findings_drift.py` (no live DataHub needed).
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from incident_copilot.decision import build_report_findings_tool  # noqa: E402

ROOT = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD)"
MIRROR_STALE = "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details,PROD)"
MIRROR_CURRENT = "urn:li:dataset:(urn:li:dataPlatform:looker,b2fd91.order-entry-looker.view.order_details,PROD)"
FIELD = "order_status_detail"

BASE_FINDINGS = dict(
    outcome="root_cause_found",
    root_cause_urn=ROOT,
    root_cause_summary="x",
    evidence_recent_schema_change=True,
    evidence_field_matches_symptom=True,
    evidence_lineage_confirms_path=True,
    evidence_downstream_confirmed=True,
    affected_dataset_count=1,
    affected_dashboard_count=0,
)


class FakeLineageTool:
    """Real neighbor-shape response: MIRROR_STALE and MIRROR_CURRENT are same-name
    siblings of ROOT on other platforms, returned regardless of direction (good
    enough here -- the audit dedupes by URN either way)."""

    name = "get_lineage"

    async def coroutine(self, urn, upstream, max_hops, max_results):
        neighbors = [
            {"urn": MIRROR_STALE, "type": "DATASET", "name": "ORDER_DETAILS",
             "platform": {"name": "snowflake"}},
            {"urn": MIRROR_CURRENT, "type": "DATASET", "name": "order_details",
             "platform": {"name": "looker"}},
        ]
        bucket_key = "upstreams" if upstream else "downstreams"
        payload = {bucket_key: {"searchResults": [{"entity": n} for n in neighbors]}}
        return (json.dumps(payload), None)


class FakeSchemaFieldsTool:
    """MIRROR_STALE lacks FIELD; MIRROR_CURRENT has it."""

    name = "list_schema_fields"

    async def coroutine(self, urn, keywords, limit, offset):
        fields = [{"fieldPath": FIELD}] if urn == MIRROR_CURRENT else []
        return (json.dumps({"fields": fields}), None)


class BrokenSchemaFieldsTool:
    """A malformed-but-present response: `fields` is a string, not a list of dicts.
    Triggers a real exception inside mirror_audit's _field_status (its own try/except
    only wraps the tool call itself, not this line) -- the only realistic way to
    exercise decision.py's outer guard without a mocking library, consistent with
    this test suite's hand-rolled-fake style.
    """

    name = "list_schema_fields"

    async def coroutine(self, urn, keywords, limit, offset):
        return (json.dumps({"fields": "not-a-list"}), None)


results: list[bool] = []


def expect(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    results.append(cond)


async def main():
    # --- audit runs automatically and a stale mirror feeds severity ---
    state = {}
    tool = build_report_findings_tool(state, FakeLineageTool(), FakeSchemaFieldsTool())
    out = await tool.coroutine(**BASE_FINDINGS, changed_field_path=FIELD, platforms_affected=["dbt"])
    expect("audit populated state['schema_drift']", "schema_drift" in state)
    expect(
        "stale mirror identified",
        bool(state.get("schema_drift", {}).get("mirrors_stale"))
        and state["schema_drift"]["mirrors_stale"][0]["urn"] == MIRROR_STALE,
    )
    expect("current mirror not flagged stale", len(state["schema_drift"]["mirrors_stale"]) == 1)
    expect("drift line names the stale platform", "snowflake" in out and "STALE" in out)

    # --- no field name -> audit is skipped cleanly, not silently claimed ---
    state2 = {}
    tool2 = build_report_findings_tool(state2, FakeLineageTool(), FakeSchemaFieldsTool())
    out2 = await tool2.coroutine(**BASE_FINDINGS, changed_field_path=None, platforms_affected=["dbt"])
    expect("no field name -> schema_drift stays unset", "schema_drift" not in state2)
    expect("drift line explains why, doesn't claim a check happened", "not checked" in out2)

    # --- inconclusive outcome -> never audited, even if a field name leaks through ---
    state3 = {}
    tool3 = build_report_findings_tool(state3, FakeLineageTool(), FakeSchemaFieldsTool())
    inconclusive = dict(BASE_FINDINGS, outcome="inconclusive", root_cause_urn=None)
    await tool3.coroutine(**inconclusive, changed_field_path=FIELD, platforms_affected=[])
    expect("inconclusive outcome -> no audit despite a field name", "schema_drift" not in state3)

    # --- a genuine failure inside the audit must not crash the mandatory checkpoint ---
    state4 = {}
    tool4 = build_report_findings_tool(state4, FakeLineageTool(), BrokenSchemaFieldsTool())
    out4 = await tool4.coroutine(**BASE_FINDINGS, changed_field_path=FIELD, platforms_affected=["dbt"])
    expect("malformed tool response doesn't raise past report_findings", isinstance(out4, str))
    expect("failed audit recorded as schema_drift=None, not a stale claim", state4.get("schema_drift") is None)

    # --- server doesn't expose the tools (older DataHub) -> clean no-op, no crash ---
    state5 = {}
    tool5 = build_report_findings_tool(state5)
    await tool5.coroutine(**BASE_FINDINGS, changed_field_path=FIELD, platforms_affected=["dbt"])
    expect("tools unavailable -> audit cleanly skipped", "schema_drift" not in state5)

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


raise SystemExit(asyncio.run(main()))
