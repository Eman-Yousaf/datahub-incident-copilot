"""Tests for the web UI's live decision panel.

Run: `python tests/test_panel_snapshot.py` (no live DataHub needed). Exits non-zero
on failure.

The panel's whole claim is that it shows the *same* state the write-back gate acts
on, rather than a prettier retelling of the agent's narration. That claim is only
worth anything if two things hold, so both are asserted here:

1. When the gate refuses a mutation, the refusal reaches the panel. A dashboard
   that quietly drops blocked attempts would show a run as clean while the code was
   busy stopping it -- the exact discrepancy the gate exists to surface.
2. The panel never invents a value. Before `report_findings` runs there is no
   confidence and no severity, and the correct rendering of that is "locked and
   pending", not a zero or an optimistic default.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from incident_copilot.decision import build_report_findings_tool  # noqa: E402
from incident_copilot.mcp_client import _gate_mutation_tool, _wrap_with_provenance  # noqa: E402
from incident_copilot.memory import EvidenceItem, InvestigationCard  # noqa: E402
from incident_copilot.panel import snapshot  # noqa: E402

ROOT = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD)"
UNRELATED = "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.customers,PROD)"
FLAG = "urn:li:tag:incident-flagged"

results: list[bool] = []


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    results.append(bool(condition))


class FakeTool:
    def __init__(self, name):
        self.name = name
        self.response_format = "content_and_artifact"

        async def coroutine(*args, **kwargs):
            return ("success: true", None)

        self.coroutine = coroutine


def gated(name, state):
    return _gate_mutation_tool(FakeTool(name), state, get_entities_tool=None)


async def main():
    # ---- empty state: nothing known yet -------------------------------------
    state: dict = {"trigger": "order counts look wrong"}
    snap = snapshot(state)

    check("empty state renders without a findings object", snap["trigger"] == "order counts look wrong")
    check("confidence is unreported, not zero", snap["confidence"]["reported"] is False)
    check("confidence level is absent rather than defaulted", snap["confidence"]["level"] is None)
    check("gate reads locked before report_findings", snap["write_back"]["locked"] is True)
    check(
        "no tool is unlocked before report_findings",
        all(not t["unlocked"] for t in snap["write_back"]["tools"]),
    )
    check("all four evidence checks are listed while pending", len(snap["evidence"]) == 4)
    check(
        "no evidence check claims confirmation yet",
        all(not item["confirmed"] for item in snap["evidence"]),
    )
    check("no phase is marked done on an untouched run", all(not p["done"] for p in snap["phases"]))

    # ---- a blocked mutation must surface on the panel ------------------------
    blocked_state: dict = {"trigger": "t", "severity": "tag_only", "root_cause_urn": ROOT}
    await gated("add_tags", blocked_state).coroutine(tag_urns=[FLAG], entity_urns=[UNRELATED])
    snap = snapshot(blocked_state)
    stages = [event["stage"] for event in snap["write_back"]["events"]]

    check("an attempted mutation is recorded as proposed", "proposed" in stages)
    check("the gate's refusal reaches the panel", "blocked" in stages)
    check(
        "the refusal carries the reason the gate gave",
        any("Not authorized" in (e.get("reason") or "") for e in snap["write_back"]["events"]),
    )
    check("a blocked attempt is not counted as an action taken", snap["write_back"]["actions_taken"] == [])

    # ---- an allowed mutation records its whole lifecycle ---------------------
    allowed_state: dict = {"trigger": "t", "severity": "tag_only", "root_cause_urn": ROOT}
    await gated("add_tags", allowed_state).coroutine(tag_urns=[FLAG], entity_urns=[ROOT])
    snap = snapshot(allowed_state)
    stages = [event["stage"] for event in snap["write_back"]["events"]]

    check("an authorized mutation is recorded as allowed", "allowed" in stages)
    check("...and then as applied", "applied" in stages)
    check("...in that order", stages.index("allowed") < stages.index("applied"))
    check("the applied mutation appears in actions taken", len(snap["write_back"]["actions_taken"]) == 1)

    # ---- after report_findings, the panel mirrors the computed policy --------
    reported: dict = {"trigger": "t"}
    report = build_report_findings_tool(reported)
    await report.coroutine(
        outcome="root_cause_found",
        root_cause_urn=ROOT,
        root_cause_summary="a new field appeared",
        evidence_recent_schema_change=True,
        evidence_field_matches_symptom=True,
        evidence_lineage_confirms_path=False,
        evidence_downstream_confirmed=False,
        affected_dataset_count=0,
        affected_dashboard_count=0,
    )
    snap = snapshot(reported)

    check(
        "confidence on the panel equals the confidence the policy computed",
        (snap["confidence"]["confirmed"], snap["confidence"]["total"])
        == (reported["checks_confirmed"], reported["checks_total"]),
    )
    check(
        "severity on the panel is the severity the gate will enforce",
        snap["write_back"]["severity"] == reported["severity"],
    )
    check("2/4 is MEDIUM, so the gate is not locked", snap["write_back"]["locked"] is False)
    check(
        "update_description stays refused at this tier",
        not next(t for t in snap["write_back"]["tools"] if t["name"] == "update_description")["unlocked"],
    )
    check(
        "authorized targets name the root cause and nothing else",
        snap["write_back"]["authorized_targets"]["update_description"] == [ROOT],
    )
    check("the findings phase is now marked done", next(p for p in snap["phases"] if p["key"] == "findings")["done"])

    # ---- a low-confidence run must render as a refusal -----------------------
    refused: dict = {"trigger": "t"}
    await build_report_findings_tool(refused).coroutine(
        outcome="inconclusive",
        root_cause_summary="nothing conclusive",
        evidence_recent_schema_change=False,
        evidence_field_matches_symptom=False,
        evidence_lineage_confirms_path=False,
        evidence_downstream_confirmed=False,
    )
    snap = snapshot(refused)

    check("an inconclusive run reads as locked", snap["write_back"]["locked"] is True)
    check("...at severity no_action", snap["write_back"]["severity"] == "no_action")
    check(
        "...with no authorized target to write to",
        snap["write_back"]["authorized_targets"]["add_tags"] == [],
    )

    # ---- unbacked inheritance is shown, not silently dropped -----------------
    lying: dict = {"trigger": "t", "prior_cards": []}
    await build_report_findings_tool(lying).coroutine(
        outcome="root_cause_found",
        root_cause_urn=ROOT,
        root_cause_summary="claims to have inherited work nobody did",
        evidence_recent_schema_change=True,
        evidence_field_matches_symptom=True,
        evidence_lineage_confirms_path=True,
        evidence_downstream_confirmed=True,
        inherited_evidence=["evidence_lineage_confirms_path"],
    )
    snap = snapshot(lying)

    check(
        "a rejected inheritance claim is visible on the panel",
        len(snap["memory"]["rejected"]) == 1,
    )
    check(
        "the demoted check renders as unconfirmed",
        not next(e for e in snap["evidence"] if e["key"] == "evidence_lineage_confirms_path")["confirmed"],
    )

    # ---- a genuinely-backed inheritance is attributed to its source card ------
    older = InvestigationCard(
        incident_id="INC-20260101-000000",
        timestamp="2026-01-01T00:00:00+00:00",
        trigger="t",
        evidence=[
            EvidenceItem(
                key="evidence_lineage_confirms_path",
                label="Lineage path confirmed via get_lineage",
                confirmed=True,
            )
        ],
    )
    newer = InvestigationCard(
        incident_id="INC-20260202-000000",
        timestamp="2026-02-02T00:00:00+00:00",
        trigger="t",
        evidence=[
            EvidenceItem(
                key="evidence_lineage_confirms_path",
                label="Lineage path confirmed via get_lineage",
                confirmed=True,
            )
        ],
    )
    continued: dict = {"trigger": "t", "prior_cards": [older, newer]}
    await build_report_findings_tool(continued).coroutine(
        outcome="root_cause_found",
        root_cause_urn=ROOT,
        root_cause_summary="continues an earlier run",
        evidence_recent_schema_change=True,
        evidence_field_matches_symptom=True,
        evidence_lineage_confirms_path=True,
        evidence_downstream_confirmed=False,
        inherited_evidence=["evidence_lineage_confirms_path"],
    )
    snap = snapshot(continued)
    lineage_check = next(e for e in snap["evidence"] if e["key"] == "evidence_lineage_confirms_path")

    check("a backed inheritance claim survives", lineage_check["confirmed"] and lineage_check["inherited"])
    check(
        "the inherited check names the card that proved it",
        lineage_check["source"] == "INC-20260202-000000",
    )
    check(
        "attribution picks the most recent card, not just any",
        lineage_check["source"] != "INC-20260101-000000",
    )
    check(
        "a check proved this run carries no borrowed attribution",
        next(e for e in snap["evidence"] if e["key"] == "evidence_recent_schema_change")["source"] is None,
    )

    knowledge = snap["knowledge"]
    check("knowledge counts what this run proved itself", knowledge["proved_here"] == 2)
    check("knowledge counts what it reused instead of re-running", knowledge["reused"] == 1)
    check(
        "knowledge counts everything the next run can inherit",
        knowledge["available_next_run"] == 3,
    )
    check("knowledge reports the card as unstored until it really is", knowledge["stored"] is False)

    # ---- DataHub call traffic is counted, not estimated ----------------------
    counted: dict = {"trigger": "t"}
    tool = FakeTool("get_lineage")
    _wrap_with_provenance(tool, counted)
    await tool.coroutine()
    await tool.coroutine()
    snap = snapshot(counted)

    check("every DataHub tool call is counted", snap["datahub_calls"]["total"] == 2)
    check(
        "...and broken down by tool",
        snap["datahub_calls"]["by_tool"] == [{"tool": "get_lineage", "count": 2}],
    )

    # ---- the authorization proof reaches the UI, both as issued and as ruled --
    # The panel renders these two objects side by side; if the snapshot ever stopped
    # carrying one of them the revocation would silently disappear from the screen
    # while still happening in the gate, which is the exact class of divergence this
    # module exists to prevent.
    check("no authorization reported before the policy layer runs", snapshot({})["authorization"] is None)

    from incident_copilot.authorization import issue_authorization, recheck_authorization

    class _Schema:
        name = "list_schema_fields"

        def __init__(self):
            self.has = True

        async def coroutine(self, urn, keywords, limit, offset):
            return (json.dumps({"fields": [{"fieldPath": "f"}] if self.has else []}), None)

    class _F:
        outcome = "root_cause_found"
        changed_field_path = "f"
        inherited_evidence: list = []
        evidence_recent_schema_change = True
        evidence_field_matches_symptom = True
        evidence_lineage_confirms_path = True
        evidence_downstream_confirmed = True
        continues_incident_id = None
        root_cause_summary = "s"
        affected_dataset_count = 1
        affected_dashboard_count = 0
        platforms_affected: list = []
        business_criticality = "medium"

    schema = _Schema()
    st = {
        "severity": "tag_and_note",
        "root_cause_urn": "urn:li:dataset:(urn:li:dataPlatform:dbt,x,PROD)",
        "confidence_level": "high",
        "checks_confirmed": 4,
        "checks_total": 4,
        "findings": _F(),
    }
    st["authorization"] = await issue_authorization(st, schema, None)
    auth = snapshot(st)["authorization"]
    check("the panel carries the authorization id", auth["id"].startswith("AUTH-"))
    check("...its decision", auth["decision"] == "ALLOW")
    check("...and the grounds, with the aspect each was observed on", any(
        p["live"] and p["aspect"] == "schemaMetadata" for p in auth["predicates"]
    ))
    check("no recheck until a write is attempted", auth["recheck"] is None)

    schema.has = False
    st["authorization_recheck"] = await recheck_authorization(st["authorization"], schema)
    auth = snapshot(st)["authorization"]
    check("a revocation reaches the panel", auth["recheck"]["revoked_targets"] == [st["root_cause_urn"]])
    check("...naming the predicate that flipped", auth["recheck"]["revoked_by"] != [])
    check("...while the issued proof stays visible for comparison", auth["decision"] == "ALLOW")

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
