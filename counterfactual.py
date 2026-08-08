"""The counterfactual: an authorization that stops holding because DataHub changed.

Everything the rest of this project enforces is a refusal *before* acting. This
script demonstrates the harder property -- a permission that was correctly granted,
against evidence that was genuinely true, being withdrawn because the ground it
rested on moved while the agent was still working.

    python counterfactual.py

Six steps, against a live DataHub:

    1. Issue an authorization from the real graph      (real reads)
    2. Perform the write it permits                    (real mutation + read-back)
    3. Remove the field that grounded it               (real schema emit)
    4. Re-check the same authorization                 (real reads)
    5. Attempt the identical write again               -> REFUSED
    6. Restore the field                               (always, even on failure)

What is real and what is staged, stated plainly rather than left for a reader to
work out: steps 1-6 all run against a live DataHub through the project's own code --
`issue_authorization`, `recheck_authorization` and `_gate_mutation_tool` are imported,
not reimplemented, and every predicate is read out of the catalog at the moment it is
evaluated. What is staged is only the *input*: instead of spending 90 seconds watching
an LLM investigate, the script asserts the conclusion a real run reaches ("root cause
is order_details, via order_status_detail, at tag_and_note") and hands it to the same
policy layer. The web app demonstrates the other half live. Splitting them this way
keeps each half checkable; a single unrepeatable take of both proves less.

Nothing here depends on the LLM, which is the point: the refusal in step 5 happens
with no model in the process at all.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from datahub.emitter.mcp import MetadataChangeProposalWrapper  # noqa: E402
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig  # noqa: E402
from datahub.metadata.schema_classes import SchemaMetadataClass  # noqa: E402

from incident_copilot.authorization import (  # noqa: E402
    issue_authorization,
    recheck_authorization,
)
from incident_copilot.mcp_client import (  # noqa: E402
    _authorized_targets,
    _gate_mutation_tool,
    datahub_tools,
)

load_dotenv()

ROOT = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,"
    "b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD)"
)
FIELD = "order_status_detail"
FLAG = "urn:li:tag:incident-flagged"

BAR = "─" * 78


def step(number: int, title: str) -> None:
    print(f"\n{BAR}\n  STEP {number} — {title}\n{BAR}")


def show_proof(proof: dict, *, heading: str) -> None:
    print(f"\n  {heading}")
    print(f"    authorization : {proof['authorization_id']}  ({proof['policy_version']})")
    print(f"    decision      : {proof['decision']}")
    print(f"    severity      : {proof['severity']}")
    print(f"    proof hash    : {proof.get('current_hash') or proof['proof_hash']}")
    print("    grounds:")
    for predicate in proof["predicates"]:
        mark = "✓" if predicate["holds"] else "✗"
        line = f"      {predicate['id']} [{mark}] {predicate['statement']}"
        if predicate["live"]:
            line += f"\n           observed '{predicate['observed']}'"
            if predicate.get("observed_now") and predicate["observed_now"] != predicate["observed"]:
                line += f" -> now '{predicate['observed_now']}'"
            line += f"\n           {predicate['aspect']} on {predicate['urn']}"
        print(line)
    targets = proof.get("authorized_targets") or {}
    print("    authorized targets:")
    for tool, urns in targets.items():
        print(f"      {tool}: {', '.join(urns) if urns else '(none)'}")


class _Findings:
    """The conclusion a real investigation reaches on the `clean-one-hop` scenario.

    Asserted rather than derived, and deliberately not dressed up as anything else --
    see the module docstring. The policy layer downstream of this treats it exactly
    as it treats the model's own `report_findings` payload, including re-reading the
    field against DataHub instead of believing the claim.
    """

    outcome = "root_cause_found"
    changed_field_path = FIELD
    inherited_evidence: list[str] = []
    evidence_recent_schema_change = True
    evidence_field_matches_symptom = True
    evidence_lineage_confirms_path = True
    evidence_downstream_confirmed = True


def investigation_state() -> dict:
    return {
        "severity": "tag_and_note",
        "root_cause_urn": ROOT,
        "confidence_level": "high",
        "checks_confirmed": 4,
        "checks_total": 4,
        "findings": _Findings(),
    }


def graph_client() -> DataHubGraph:
    return DataHubGraph(
        DatahubClientConfig(
            server=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
            token=os.environ.get("DATAHUB_GMS_TOKEN") or None,
        )
    )


def remove_field(graph: DataHubGraph, urn: str, field_path: str) -> SchemaMetadataClass:
    """Drop one field from an entity's schemaMetadata. Returns the aspect as it was,
    so step 6 can put it back byte-for-byte rather than reconstructing an approximation.
    """
    current = graph.get_aspect(urn, aspect_type=SchemaMetadataClass)
    if current is None:
        raise RuntimeError(f"no schemaMetadata on {urn} — run `python seed_data.py` first")
    original = graph.get_aspect(urn, aspect_type=SchemaMetadataClass)
    current.fields = [f for f in current.fields if f.fieldPath != field_path]
    graph.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=current))
    return original


async def settle(tool, urn: str, field: str, want: bool, tries: int = 12) -> bool:
    """Wait until MCP reads the schema the way we just wrote it.

    GMS acknowledges a schemaMetadata emit before every read path reflects it, and a
    demo that raced that window would look like the gate misfiring. Polling the same
    tool the gate uses means we wait on exactly the read that matters.
    """
    from incident_copilot.mirror_audit import _field_status

    for _ in range(tries):
        status = await _field_status(tool, urn, field)
        if (status == "current") is want:
            return True
        await asyncio.sleep(2)
    return False


async def main(keep: bool) -> int:
    graph = graph_client()
    original: SchemaMetadataClass | None = None

    async with datahub_tools("counterfactual") as (tools, _state):
        by_name = {t.name: t for t in tools}
        schema_tool = by_name["list_schema_fields"]
        get_entities = by_name.get("get_entities")
        add_tags = by_name["add_tags"]

        state = investigation_state()

        try:
            # ---------------------------------------------------------- #
            step(1, "Issue an authorization from the live graph")
            print(
                "\n  An investigation has concluded. The policy layer now decides what,\n"
                "  if anything, that conclusion permits — and grounds it in predicates it\n"
                "  reads out of DataHub itself rather than taking from the model."
            )
            proof = await issue_authorization(state, schema_tool, _authorized_targets)
            state["authorization"] = proof
            show_proof(proof, heading="ISSUED")
            if proof["decision"] != "ALLOW":
                print(
                    "\n  The authorization did not come back ALLOW, which means the graph is\n"
                    "  not in the expected starting state. Run `python seed_data.py` first."
                )
                return 1

            # ---------------------------------------------------------- #
            step(2, "Perform the write this authorization permits")
            gated = _gate_mutation_tool(add_tags, state, get_entities, schema_tool)
            result = await gated.coroutine(tag_urns=[FLAG], entity_urns=[ROOT])
            message = result[0] if isinstance(result, tuple) else result
            print(f"\n  add_tags -> {str(message)[:400]}")
            if str(message).startswith("Blocked:"):
                print("\n  Unexpected: the baseline write was refused. Stopping.")
                return 1
            print(
                "\n  Written, and re-read from DataHub to prove it landed — not a bare\n"
                "  success:true. This is the authorization being exercised legitimately."
            )

            # ---------------------------------------------------------- #
            step(3, "Reality changes")
            print(
                f"\n  Removing `{FIELD}` from the root-cause entity's schemaMetadata.\n"
                "  Nobody tells the agent. Its evidence checklist is untouched, its\n"
                "  severity tier is unchanged, and no model is consulted."
            )
            original = remove_field(graph, ROOT, FIELD)
            print("  emitted. waiting for the catalog to reflect it...")
            if not await settle(schema_tool, ROOT, FIELD, want=False):
                print("\n  The change did not settle in time; aborting rather than showing a race.")
                return 1
            print(f"  DataHub now reports `{FIELD}` absent from {ROOT}")

            # ---------------------------------------------------------- #
            step(4, "Re-check the same authorization")
            rechecked = await recheck_authorization(proof, schema_tool)
            show_proof(rechecked, heading="RE-CHECKED")
            print(f"\n    revoked by    : {', '.join(rechecked['revoked_by']) or '(nothing)'}")
            print(f"    revoked targets: {', '.join(rechecked['revoked_targets']) or '(none)'}")
            print(f"    hash changed  : {rechecked['hash_changed']}")
            print(
                f"\n  Same authorization id ({rechecked['authorization_id']}), different\n"
                "  verdict. The id is derived from the grounds, so it stays comparable while\n"
                "  the hash moves — which is what makes 'this permission changed' a\n"
                "  checkable statement rather than a new, unrelated decision."
            )

            # ---------------------------------------------------------- #
            step(5, "Attempt the identical write again")
            gated = _gate_mutation_tool(add_tags, state, get_entities, schema_tool)
            result = await gated.coroutine(tag_urns=[FLAG], entity_urns=[ROOT])
            message = result[0] if isinstance(result, tuple) else result
            print(f"\n  add_tags -> {message}")
            blocked = str(message).startswith("Blocked:")
            print(
                "\n  The agent did not change its mind. It was never asked.\n"
                "  The evidence changed, so the authority changed."
                if blocked
                else "\n  UNEXPECTED: the write was not blocked."
            )
            if not blocked:
                return 1

        finally:
            # ---------------------------------------------------------- #
            if original is not None and not keep:
                step(6, "Restore")
                graph.emit(MetadataChangeProposalWrapper(entityUrn=ROOT, aspect=original))
                await settle(schema_tool, ROOT, FIELD, want=True)
                print(f"\n  `{FIELD}` restored on {ROOT}. The graph is back where it started.")
            elif original is not None:
                step(6, "Restore skipped (--keep)")
                print(f"\n  `{FIELD}` is still removed. Run `python seed_data.py` to restore it.")

    print(f"\n{BAR}\n  Authorization issued, exercised, revoked, and refused — all in code.\n{BAR}\n")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the field removed instead of restoring it (for inspecting DataHub afterwards)",
    )
    raise SystemExit(asyncio.run(main(parser.parse_args().keep)))
