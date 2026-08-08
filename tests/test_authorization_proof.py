"""Adversarial tests for the authorization proof layer (authorization.py).

The claim this file exists to attack: an autonomous mutation is permitted only while
the specific DataHub facts that justified it are still true, and the permission names
the exact entities it covers. Every scenario below runs against the real
`_gate_mutation_tool` and the real `issue_authorization` / `recheck_authorization` --
imported, never reimplemented. Only the tool underneath the gate is a stub, so no
test can reach a live catalog regardless of how the gate rules.

Run: `python tests/test_authorization_proof.py` (no live DataHub needed).
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from incident_copilot.authorization import (  # noqa: E402
    authorization_id,
    issue_authorization,
    recheck_authorization,
)
from incident_copilot.mcp_client import _authorized_targets, _gate_mutation_tool  # noqa: E402

ROOT = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD)"
MIRROR = "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details,PROD)"
UNRELATED = "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.customers,PROD)"
FIELD = "order_status_detail"
FLAG = "urn:li:tag:incident-flagged"

results: list[bool] = []


def expect(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    results.append(bool(cond))


class SchemaTool:
    """A controllable stand-in for `list_schema_fields`.

    `present` is the set of (urn, field) pairs the graph currently has. Mutating it
    between issuing a proof and re-checking it is how these tests move DataHub
    underneath an authorization -- the same thing `counterfactual.py` does for real
    against a live instance.
    """

    name = "list_schema_fields"

    def __init__(self, present: set, unreadable: set | None = None):
        self.present = set(present)
        self.unreadable = set(unreadable or ())

    async def coroutine(self, urn, keywords, limit, offset):
        if urn in self.unreadable:
            return (json.dumps({}), None)
        field = keywords[0] if keywords else None
        fields = [{"fieldPath": field}] if (urn, field) in self.present else []
        return (json.dumps({"fields": fields}), None)


class StubMutation:
    """Same contract the real DataHub MCP mutation tools declare -- a refusal that
    returns a bare string instead of a tuple raises inside LangChain's tool runner
    and kills the run, so the stub must not be more forgiving than reality."""

    def __init__(self, name, log):
        self.name = name
        self.response_format = "content_and_artifact"

        async def coroutine(*args, **kwargs):
            log.append(kwargs)
            return ("success: true", None)

        self.coroutine = coroutine


class Findings:
    """The fields `issue_authorization` reads off `state["findings"]`."""

    def __init__(self, outcome="root_cause_found", field=FIELD, inherited=None):
        self.outcome = outcome
        self.changed_field_path = field
        self.inherited_evidence = inherited or []
        self.evidence_recent_schema_change = True
        self.evidence_field_matches_symptom = True
        self.evidence_lineage_confirms_path = True
        self.evidence_downstream_confirmed = True
        # The rest of what `build_card` reads. Present so the card round-trip below
        # exercises the real builder rather than a reduced stand-in for it.
        self.root_cause_urn = ROOT
        self.root_cause_summary = "order_status_detail was added and the reports never picked it up"
        self.hypotheses_tested = []
        self.hypotheses_rejected = []
        self.continues_incident_id = None
        self.affected_dataset_count = 1
        self.affected_dashboard_count = 0
        self.platforms_affected = ["dbt"]
        self.business_criticality = "medium"


def state_for(severity, *, confidence="high", checks=4, stale=None, findings=None) -> dict:
    state = {
        "severity": severity,
        "root_cause_urn": ROOT,
        "confidence_level": confidence,
        "checks_confirmed": checks,
        "checks_total": 4,
        "findings": findings or Findings(),
    }
    if stale:
        state["schema_drift"] = {
            "checked_field": FIELD,
            "mirrors_stale": [{"urn": u, "platform": "snowflake", "status": "stale"} for u in stale],
        }
    return state


async def attempt(tool_name, state, schema_tool, **kwargs):
    """Run one mutation attempt through the real gate. Returns (message, reached)."""
    log: list = []
    gated = _gate_mutation_tool(StubMutation(tool_name, log), state, None, schema_tool)
    result = await gated.coroutine(**kwargs)
    message = result[0] if isinstance(result, tuple) else result
    return message, bool(log)


async def main() -> int:
    # ---------------------------------------------------------------- #
    # 1. Insufficient evidence -> the proof itself denies
    # ---------------------------------------------------------------- #
    graph = SchemaTool(present={(ROOT, FIELD)})
    low = state_for("no_action", confidence="low", checks=1)
    low["authorization"] = await issue_authorization(low, graph, _authorized_targets)
    expect("insufficient evidence -> proof decision DENY", low["authorization"]["decision"] == "DENY")
    expect(
        "...and the DENY proof authorizes no target at all",
        low["authorization"]["authorized_targets"] == {"add_tags": [], "update_description": []},
    )
    msg, reached = await attempt("add_tags", low, graph, tag_urns=[FLAG], entity_urns=[ROOT])
    expect("...and the gate blocks the write", msg.startswith("Blocked:") and not reached)

    # ---------------------------------------------------------------- #
    # 2. Evidence becomes sufficient -> ALLOW, and the write really runs
    # ---------------------------------------------------------------- #
    high = state_for("tag_and_note")
    high["authorization"] = await issue_authorization(high, graph, _authorized_targets)
    proof = high["authorization"]
    expect("sufficient evidence -> proof decision ALLOW", proof["decision"] == "ALLOW")
    expect("...grounded on a live predicate read from DataHub", any(p["live"] for p in proof["predicates"]))
    expect(
        "...naming the aspect it was observed on",
        all(p["aspect"] == "schemaMetadata" for p in proof["predicates"] if p["live"]),
    )
    expect("...and the root cause is an authorized target", ROOT in proof["authorized_targets"]["add_tags"])
    msg, reached = await attempt("add_tags", high, graph, tag_urns=[FLAG], entity_urns=[ROOT])
    expect("authorized target -> mutation reaches the tool", reached and not str(msg).startswith("Blocked:"))

    # ---------------------------------------------------------------- #
    # 3. A target outside the proof is refused regardless of tier
    # ---------------------------------------------------------------- #
    msg, reached = await attempt("add_tags", high, graph, tag_urns=[FLAG], entity_urns=[UNRELATED])
    expect("target outside authorized scope -> blocked", msg.startswith("Blocked:") and not reached)

    # ---------------------------------------------------------------- #
    # 4. THE COUNTERFACTUAL: the grounding evidence changes in DataHub,
    #    so the authorization is revoked and the same write is refused.
    # ---------------------------------------------------------------- #
    live = state_for("tag_and_note")
    live["authorization"] = await issue_authorization(live, graph, _authorized_targets)
    before = live["authorization"]
    msg, reached = await attempt("add_tags", live, graph, tag_urns=[FLAG], entity_urns=[ROOT])
    expect("baseline: the write is permitted while the field is present", reached)

    graph.present.discard((ROOT, FIELD))  # reality moves

    after = await recheck_authorization(before, graph)
    expect("field removed -> a grounding predicate flips", after["revoked_by"] != [])
    expect("...the root cause is revoked", ROOT in after["revoked_targets"])
    expect("...the decision becomes REVOKED", after["decision"] == "REVOKED")
    expect("...and the proof hash no longer matches what was issued", after["hash_changed"])
    expect(
        "...while the authorization id is unchanged, so the two are comparable",
        after["authorization_id"] == before["authorization_id"],
    )
    msg, reached = await attempt("add_tags", live, graph, tag_urns=[FLAG], entity_urns=[ROOT])
    expect("...the identical write is now blocked", msg.startswith("Blocked:") and not reached)
    expect("...and the refusal names the predicate, not the model", "predicate" in msg)

    # ---------------------------------------------------------------- #
    # 5. Revocation is scoped: a mirror that gets fixed loses its own
    #    authorization without touching the root cause.
    # ---------------------------------------------------------------- #
    graph2 = SchemaTool(present={(ROOT, FIELD)})
    scoped = state_for("tag_note_escalated", stale=[MIRROR])
    scoped["authorization"] = await issue_authorization(scoped, graph2, _authorized_targets)
    expect(
        "a proven-stale mirror is an authorized add_tags target",
        MIRROR in scoped["authorization"]["authorized_targets"]["add_tags"],
    )

    graph2.present.add((MIRROR, FIELD))  # the mirror catches up

    narrowed = await recheck_authorization(scoped["authorization"], graph2)
    expect("mirror caught up -> mirror revoked", MIRROR in narrowed["revoked_targets"])
    expect("...root cause NOT revoked by an unrelated predicate", ROOT not in narrowed["revoked_targets"])
    expect("...so the authorization still stands overall", narrowed["decision"] == "ALLOW")
    msg, reached = await attempt("add_tags", scoped, graph2, tag_urns=[FLAG], entity_urns=[MIRROR])
    expect("...tagging the fixed mirror is blocked", msg.startswith("Blocked:") and not reached)
    msg, reached = await attempt("add_tags", scoped, graph2, tag_urns=[FLAG], entity_urns=[ROOT])
    expect("...tagging the root cause still succeeds", reached)

    # ---------------------------------------------------------------- #
    # 6. Fail-safe: unable to read is not the same as contradicted.
    # ---------------------------------------------------------------- #
    blind = SchemaTool(present={(ROOT, FIELD)}, unreadable={ROOT})
    unread = state_for("tag_and_note")
    unread["authorization"] = await issue_authorization(unread, blind, _authorized_targets)
    expect(
        "unreadable predicate at issue time does not deny",
        unread["authorization"]["decision"] == "ALLOW",
    )
    expect(
        "...and is recorded as unverifiable rather than false",
        any(p["unverifiable"] for p in unread["authorization"]["predicates"] if p["live"]),
    )

    readable = SchemaTool(present={(ROOT, FIELD)})
    ok = state_for("tag_and_note")
    ok["authorization"] = await issue_authorization(ok, readable, _authorized_targets)
    readable.unreadable.add(ROOT)
    held = await recheck_authorization(ok["authorization"], readable)
    expect("unreadable at recheck time does not revoke", held["revoked_targets"] == [])
    expect("...and says so explicitly", any(p.get("recheck") == "unverifiable" for p in held["predicates"]))

    # ---------------------------------------------------------------- #
    # 7. A contradicted ground denies even at an acting tier -- the code's
    #    read of DataHub outranks the model's evidence checklist.
    # ---------------------------------------------------------------- #
    empty = SchemaTool(present=set())
    lying = state_for("tag_note_escalated")
    lying["authorization"] = await issue_authorization(lying, empty, _authorized_targets)
    expect(
        "model claims a field DataHub does not have -> DENY despite a top tier",
        lying["authorization"]["decision"] == "DENY",
    )
    msg, reached = await attempt("add_tags", lying, empty, tag_urns=[FLAG], entity_urns=[ROOT])
    expect("...and the write is blocked", msg.startswith("Blocked:") and not reached)

    # ---------------------------------------------------------------- #
    # 8. The id is a function of the grounds, not a nonce.
    # ---------------------------------------------------------------- #
    g = SchemaTool(present={(ROOT, FIELD)})
    a = await issue_authorization(state_for("tag_and_note"), g, _authorized_targets)
    b = await issue_authorization(state_for("tag_and_note"), g, _authorized_targets)
    expect("same grounds -> same authorization id", a["authorization_id"] == b["authorization_id"])
    expect("...and the same proof hash", a["proof_hash"] == b["proof_hash"])
    c = await issue_authorization(state_for("tag_only"), g, _authorized_targets)
    expect("different tier -> different id", c["authorization_id"] != a["authorization_id"])
    d = await issue_authorization(
        state_for("tag_note_escalated", stale=[MIRROR]), g, _authorized_targets
    )
    expect("different authorized target set -> different id", d["authorization_id"] != a["authorization_id"])
    expect(
        "the id is reproducible from the recorded core alone",
        authorization_id(a["core"]) == a["authorization_id"],
    )

    # ---------------------------------------------------------------- #
    # 9. Backwards compatibility: no schema tool -> the older gate
    #    behaviour is preserved rather than failing closed by accident.
    # ---------------------------------------------------------------- #
    legacy = state_for("tag_and_note")
    legacy["authorization"] = await issue_authorization(legacy, None, _authorized_targets)
    log: list = []
    gated = _gate_mutation_tool(StubMutation("add_tags", log), legacy, None)
    out = await gated.coroutine(tag_urns=[FLAG], entity_urns=[ROOT])
    expect("no live schema tool -> proof issues without a live predicate", legacy["authorization"]["decision"] == "ALLOW")
    expect("...and the gate still permits the earned write", bool(log) and not str(out[0]).startswith("Blocked:"))

    # ---------------------------------------------------------------- #
    # 10. The proof survives the round trip into DataHub and back, and an
    #     independent reader can recompute the id from the card alone.
    # ---------------------------------------------------------------- #
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from verify_authorization import verify  # noqa: E402

    from incident_copilot.decision import build_card  # noqa: E402
    from incident_copilot.memory import parse_card, render_card  # noqa: E402

    g2 = SchemaTool(present={(ROOT, FIELD)})
    stored = state_for("tag_and_note")
    stored["authorization"] = await issue_authorization(stored, g2, _authorized_targets)
    stored.update(trigger="t", incident_id="INC-20260808-120000", tools_used={"search"})
    card = build_card(stored)
    expect("the card carries the authorization id", card.authorization_id == stored["authorization"]["authorization_id"])
    expect("...and the grounds it was computed from", bool(card.authorization_core))

    reread = parse_card(render_card(card))
    expect("the card round-trips through its stored markdown", reread is not None)
    expect("...preserving the proof core exactly", reread.authorization_core == card.authorization_core)
    ok, detail = verify(reread)
    expect(f"an independent reader recomputes the same id ({detail})", ok)

    # A card whose grounds were edited must fail verification -- otherwise the
    # verifier is decoration.
    tampered = parse_card(render_card(card))
    tampered.authorization_core["severity"] = "tag_note_escalated"
    bad, _ = verify(tampered)
    expect("...and a tampered proof core fails to verify", not bad)

    # A revoked run keeps a verifiable identity: the id and hash describe the proof
    # as issued, while the decision records what the gate ruled.
    g2.present.discard((ROOT, FIELD))
    stored["authorization_recheck"] = await recheck_authorization(stored["authorization"], g2)
    revoked_card = build_card(stored)
    expect("a revoked run records the REVOKED decision", revoked_card.authorization_decision == "REVOKED")
    expect("...and names what lost authorization", ROOT in revoked_card.authorization_revoked_targets)
    ok2, _ = verify(parse_card(render_card(revoked_card)))
    expect("...while still verifying against its issued grounds", ok2)

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


raise SystemExit(asyncio.run(main()))
