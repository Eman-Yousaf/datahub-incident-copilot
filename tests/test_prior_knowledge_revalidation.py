"""Tests for treating prior knowledge as a hypothesis rather than as truth.

Run: `python tests/test_prior_knowledge_revalidation.py` (no live DataHub needed).
Exits non-zero on failure.

The memory layer already refuses inheritance claims no recalled card confirms. This
covers the second, more dangerous hole: a card can be entirely genuine and still be
out of date. If the field it named has since disappeared, the card describes a world
that no longer exists, and letting it back an evidence check would let a stale
finding buy confidence in the present -- the exact way memory makes an agent worse
rather than better.

The asymmetry between `conflict` and `unverifiable` is the part worth protecting:
a contradicted card is withdrawn, but a card we merely *couldn't* check is not. An
absence you failed to confirm is not evidence of absence, so being unable to check
must never demote anything on its own.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from incident_copilot.decision import build_report_findings_tool  # noqa: E402
from incident_copilot.memory import EvidenceItem, InvestigationCard  # noqa: E402
from incident_copilot.revalidate import (  # noqa: E402
    checkable_claim,
    conflicted_ids,
    revalidate_prior_knowledge,
)

ROOT = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD)"
FIELD = "order_status_detail"

results: list[bool] = []


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    results.append(bool(condition))


class SchemaTool:
    """Returns whichever field list the test wants for a given URN."""

    def __init__(self, fields_by_urn):
        async def coroutine(urn, keywords=None, limit=10, offset=0):
            fields = fields_by_urn.get(urn)
            if fields is None:
                return ('{"fields": null}', None)
            listed = ", ".join(f'{{"fieldPath": "{f}"}}' for f in fields)
            return (f'{{"fields": [{listed}]}}', None)

        self.coroutine = coroutine


class ExplodingSchemaTool:
    def __init__(self):
        async def coroutine(**kwargs):
            raise RuntimeError("GMS unreachable")

        self.coroutine = coroutine


def card(incident_id, *, field=FIELD, urn=ROOT, drift_field=None, confirmed=True):
    return InvestigationCard(
        incident_id=incident_id,
        timestamp="2026-02-02T00:00:00+00:00",
        trigger="order counts look wrong",
        root_cause_urn=urn,
        root_cause_field=field,
        schema_drift_field=drift_field,
        evidence=[
            EvidenceItem(
                key="evidence_recent_schema_change",
                label="Recent schema change confirmed on the exact URN",
                confirmed=confirmed,
            ),
            EvidenceItem(
                key="evidence_field_matches_symptom",
                label="Changed field plausibly matches the reported symptom",
                confirmed=confirmed,
            ),
        ],
    )


async def main():
    # ---- the claim a card makes ---------------------------------------------
    check("a card naming a field and a URN has a checkable claim",
          checkable_claim(card("INC-1")) == (ROOT, FIELD))
    check("a card with no field has nothing to re-check",
          checkable_claim(card("INC-2", field=None)) is None)
    check("an older card falls back to its drift field rather than being skipped",
          checkable_claim(card("INC-3", field=None, drift_field=FIELD)) == (ROOT, FIELD))

    # ---- verdicts ------------------------------------------------------------
    still_there = SchemaTool({ROOT: [FIELD, "order_id"]})
    rows = await revalidate_prior_knowledge(still_there, [card("INC-A")])
    check("a field still present confirms the prior finding", rows[0]["verdict"] == "confirmed")

    gone = SchemaTool({ROOT: ["order_id", "customer_id"]})
    rows = await revalidate_prior_knowledge(gone, [card("INC-B")])
    check("a field that has since disappeared is a conflict", rows[0]["verdict"] == "conflict")
    check("...and the conflict says which field and why",
          FIELD in rows[0]["detail"] and "withdrawn" in rows[0]["detail"])

    unreadable = SchemaTool({})
    rows = await revalidate_prior_knowledge(unreadable, [card("INC-C")])
    check("an unreadable schema is unverifiable, never a conflict",
          rows[0]["verdict"] == "unverifiable")

    rows = await revalidate_prior_knowledge(ExplodingSchemaTool(), [card("INC-D")])
    check("a failing re-check degrades to unverifiable rather than raising",
          rows[0]["verdict"] == "unverifiable")

    rows = await revalidate_prior_knowledge(None, [card("INC-E")])
    check("no schema tool at all means unverifiable, not confirmed",
          rows[0]["verdict"] == "unverifiable")

    rows = await revalidate_prior_knowledge(still_there, [card("INC-F", field=None)])
    check("a card with no concrete claim is unverifiable", rows[0]["verdict"] == "unverifiable")

    mixed = await revalidate_prior_knowledge(gone, [card("INC-G"), card("INC-H", field=None)])
    check("only contradicted cards are marked conflicted", conflicted_ids(mixed) == {"INC-G"})

    # ---- end to end: a conflict must actually block the write-back -----------
    state = {"trigger": "order counts look wrong", "prior_cards": [card("INC-STALE")]}
    report = build_report_findings_tool(state, None, gone)
    text = await report.coroutine(
        outcome="root_cause_found",
        root_cause_urn=ROOT,
        root_cause_summary="leaning entirely on a stored finding",
        evidence_recent_schema_change=True,
        evidence_field_matches_symptom=True,
        evidence_lineage_confirms_path=False,
        evidence_downstream_confirmed=False,
        inherited_evidence=[
            "evidence_recent_schema_change",
            "evidence_field_matches_symptom",
        ],
        affected_dataset_count=20,
        affected_dashboard_count=5,
        platforms_affected=["snowflake", "looker"],
    )

    check("the conflicting card is reported to the agent, not hidden",
          "CONFLICT" in text and "INC-STALE" in text)
    check("both checks resting on the stale card were reset to unconfirmed",
          state["checks_confirmed"] == 0)
    check("confidence collapses to low", state["confidence_level"] == "low")
    check("severity drops to no_action despite a large blast radius",
          state["severity"] == "no_action")
    check("the withdrawal is recorded on the card, not silently applied",
          any("no longer matches live" in claim for claim in state["dropped_inheritance"]))

    # ---- the same run against a graph that still agrees ----------------------
    ok_state = {"trigger": "order counts look wrong", "prior_cards": [card("INC-FRESH")]}
    await build_report_findings_tool(ok_state, None, still_there).coroutine(
        outcome="root_cause_found",
        root_cause_urn=ROOT,
        root_cause_summary="the stored finding still matches the graph",
        evidence_recent_schema_change=True,
        evidence_field_matches_symptom=True,
        evidence_lineage_confirms_path=False,
        evidence_downstream_confirmed=False,
        inherited_evidence=[
            "evidence_recent_schema_change",
            "evidence_field_matches_symptom",
        ],
        affected_dataset_count=20,
        affected_dashboard_count=5,
        platforms_affected=["snowflake", "looker"],
    )
    check("a confirmed card still backs inheritance", ok_state["checks_confirmed"] == 2)
    check("...and the run is allowed to act", ok_state["severity"] != "no_action")
    check("...with nothing dropped", ok_state["dropped_inheritance"] == [])

    # ---- unverifiable must not demote ---------------------------------------
    unsure_state = {"trigger": "order counts look wrong", "prior_cards": [card("INC-UNSURE")]}
    await build_report_findings_tool(unsure_state, None, unreadable).coroutine(
        outcome="root_cause_found",
        root_cause_urn=ROOT,
        root_cause_summary="could not re-check the stored finding",
        evidence_recent_schema_change=True,
        evidence_field_matches_symptom=True,
        evidence_lineage_confirms_path=False,
        evidence_downstream_confirmed=False,
        inherited_evidence=["evidence_recent_schema_change"],
    )
    check("being unable to check does not withdraw the card",
          unsure_state["checks_confirmed"] == 2)

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
