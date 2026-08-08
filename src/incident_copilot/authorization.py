"""Proof-carrying authorization: why this action is permitted, in a form that can be
recomputed and revoked.

The rest of the policy layer already decides *whether* the agent may act, in Python.
What it never produced was an artifact answering *why* -- the decision lived as a few
keys on a state dict, readable only by the gate that wrote them. "The code decided"
is the correct architecture and a bad answer to a skeptical reader, because it is
indistinguishable from "trust me" unless the reasoning is somewhere they can check.

So every authorization is issued as a proof: the predicates that had to hold, where
in DataHub each one was observed, the exact targets and actions they permit, and a
content hash over all of it. Two runs reaching the same conclusion from the same
evidence produce the same `authorization_id` -- that is what makes it a proof rather
than a receipt. Nothing here is signed; a signature would prove who issued it, which
is not the question. The question is whether the stated grounds are true, and that is
answered by re-reading DataHub, not by cryptography.

The part that matters operationally:

**A proof is not permanent.** Its live predicates are concrete, re-checkable claims
about the graph -- "field `order_status_detail` is present on urn:li:dataset:(...)",
"the snowflake mirror still lacks it". They were true when the proof was issued.
Between issuing and acting, DataHub can move. So `recheck_authorization` re-reads
every live predicate at mutation time, and any predicate that flipped revokes exactly
the targets it was grounding. The root-cause predicate grounds the whole
authorization; a mirror predicate grounds only that mirror.

That is the difference between an agent changing its mind and an agent losing
authority. The model is not consulted, and its confidence is irrelevant: the ground
the permission rested on stopped being true, so the permission stopped existing.

One rule inherited deliberately from `mirror_audit._field_status` and `revalidate.py`:
**being unable to read a predicate never revokes it.** An unconfirmed absence is not
evidence of absence, and a transient GMS hiccup must not silently widen or narrow
authority. Unreadable holds the previous verdict and says so.

What this module is deliberately NOT
------------------------------------
It is not a claim verifier. It does not decide whether anything the agent *said* is
true -- verifying an agent's assertions against a catalog is a real and separate
problem, and one other tools address directly and more thoroughly. `revalidate.py`
does a narrow version of that here, and its only job is to decide which evidence is
still allowed to count.

This module starts after that question is answered. Its subject is not a sentence,
it is a *mutation*: may this specific tool write to this specific URN at this
specific moment. That framing is what produces the three properties below, none of
which a truth-verifier needs and none of which a human-approval checkpoint would
leave meaningful:

  target scope     authority names exact URNs, not a run or a claim, because the
                   thing being authorized is a write to one entity
  continuity       authority is re-established at the instant of action rather than
                   granted once, because the gap between deciding and acting is where
                   a catalog moves
  revocability     the grounds can stop holding, and when they do the permission
                   disappears without anyone deciding it should

Nothing here removes a human from a loop they were in; the agent this governs never
had one. That is the case worth governing, and it is why the answer has to be code.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .mirror_audit import _field_status

# Bumped whenever the *meaning* of a proof changes -- which predicates are required,
# what a tier authorizes, how targets are derived. It is part of the hashed core, so
# a proof issued under different rules can never collide with one issued under these.
POLICY_VERSION = "policy-v1"

# Predicate kinds. The first two are live: they name a field on a URN and are
# re-read from DataHub's schemaMetadata aspect at mutation time. The last two are
# derived from evidence already scored by decision.py and are recorded for legibility
# rather than re-checked -- re-reading them would just re-run arithmetic on the same
# inputs, which cannot change on its own.
FIELD_PRESENT = "schema_field_present"
FIELD_ABSENT = "schema_field_absent"
CONFIDENCE_AT_LEAST = "confidence_at_least"
ROOT_CAUSE_CONFIRMED = "root_cause_confirmed"

LIVE_KINDS = frozenset({FIELD_PRESENT, FIELD_ABSENT})

# The aspect every live predicate is observed against. Named explicitly on each
# predicate so a reader can go look at the same place in DataHub the code did.
SCHEMA_ASPECT = "schemaMetadata"

# Confidence levels that clear the bar for acting at all. Mirrors compute_severity's
# `confidence_level == "low" -> no_action` rule; stated here as a predicate so the
# refusal proof can name the threshold it failed instead of just reporting no_action.
ACTING_CONFIDENCE = frozenset({"medium", "high"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _predicate(
    pid: str,
    kind: str,
    *,
    holds: bool,
    statement: str,
    observed: str,
    authorizes: list[str],
    urn: str | None = None,
    field: str | None = None,
    source_tool: str | None = None,
    unverifiable: bool = False,
) -> dict[str, Any]:
    """One checkable claim the authorization rests on.

    `authorizes` is the list of entity URNs this predicate is load-bearing for. It is
    what makes revocation precise instead of all-or-nothing: a mirror whose schema
    caught up should stop being a legal target without invalidating the root-cause
    finding that has nothing to do with it.
    """
    return {
        "id": pid,
        "kind": kind,
        "statement": statement,
        "urn": urn,
        "aspect": SCHEMA_ASPECT if kind in LIVE_KINDS else None,
        "field": field,
        "source_tool": source_tool,
        "observed": observed,
        "holds": holds,
        # `holds=False` means two very different things and the difference decides
        # whether anything may happen. A predicate DataHub actively contradicts is a
        # reason to refuse. A predicate DataHub could not answer is not -- the same
        # rule `mirror_audit._field_status` and `revalidate.py` already follow, for
        # the same reason: an unconfirmed absence is not evidence of absence.
        "unverifiable": unverifiable,
        "live": kind in LIVE_KINDS,
        "authorizes": sorted(authorizes),
        "checked_at": _now(),
    }


def _core(
    severity: str | None,
    decision: str,
    predicates: list[dict],
    targets: dict[str, list[str]],
    permitted_tags: list[str],
) -> dict[str, Any]:
    """The decisive content of a proof -- everything the decision actually turned on,
    and nothing that merely describes it.

    Deliberately excludes timestamps, free text and observed values: the same evidence
    reaching the same verdict about the same entities must hash identically across
    runs, or the id is a nonce with extra steps. `holds` is included because a proof
    that reached ALLOW over a different set of true predicates is a different proof.
    """
    return {
        "policy_version": POLICY_VERSION,
        "decision": decision,
        "severity": severity,
        "predicates": sorted(
            (
                {
                    "id": p["id"],
                    "kind": p["kind"],
                    "urn": p["urn"],
                    "field": p["field"],
                    "holds": p["holds"],
                    "authorizes": p["authorizes"],
                }
                for p in predicates
            ),
            key=lambda p: p["id"],
        ),
        "authorized_targets": {tool: sorted(urns) for tool, urns in sorted(targets.items())},
        "permitted_tags": sorted(permitted_tags),
    }


def proof_hash(core: dict) -> str:
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def authorization_id(core: dict) -> str:
    """Short, stable, and derived from the grounds -- not a random id.

    Two investigations that independently establish the same facts about the same
    entities land on the same AUTH-…, and a judge can verify that by running the
    scenario twice. A uuid would look the same on screen and prove nothing.
    """
    return f"AUTH-{proof_hash(core)[:12]}"


# Which tags each tier permits, mirrored from decision.SEVERITY_INSTRUCTIONS and from
# the per-tag check in mcp_client's gate. Kept here as *data* so the proof can state
# the permitted set explicitly rather than describing it in prose.
FLAG_TAG = "urn:li:tag:incident-flagged"
HIGH_TAG = "urn:li:tag:incident-severity-high"

_TIER_TAGS: dict[str, list[str]] = {
    "no_action": [],
    "tag_only": [FLAG_TAG],
    "tag_and_note": [FLAG_TAG],
    "tag_note_escalated": [FLAG_TAG, HIGH_TAG],
}


async def issue_authorization(
    state: dict, list_schema_fields_tool=None, authorized_targets=None
) -> dict[str, Any]:
    """Build the proof for whatever `report_findings` just decided. Read-only.

    Called after severity is computed, so this never influences the decision -- it
    records it, grounds it, and makes it revocable. A `no_action` run gets a proof
    too, with decision DENY and the failed predicate named: a refusal that can point
    at the specific claim it could not establish is worth considerably more than one
    that reports a tier.

    The root-cause predicate is re-read from DataHub here rather than taken from the
    model's `changed_field_path`, so the proof's own foundation is a Python
    observation. That is one extra `list_schema_fields` call per investigation, and it
    is the call that makes the word "proof" defensible.
    """
    findings = state.get("findings")
    severity: str | None = state.get("severity")
    root: str | None = state.get("root_cause_urn")
    confidence: str | None = state.get("confidence_level")
    field: str | None = getattr(findings, "changed_field_path", None) if findings else None
    outcome = getattr(findings, "outcome", None) if findings else None
    drift = state.get("schema_drift") or {}

    predicates: list[dict[str, Any]] = []
    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"P{counter}"

    predicates.append(
        _predicate(
            next_id(),
            ROOT_CAUSE_CONFIRMED,
            holds=outcome == "root_cause_found" and bool(root),
            statement="A root cause was confirmed on an exact DataHub URN",
            observed=str(root or "none"),
            authorizes=[root] if root else [],
            urn=root,
        )
    )
    predicates.append(
        _predicate(
            next_id(),
            CONFIDENCE_AT_LEAST,
            holds=confidence in ACTING_CONFIDENCE,
            statement=(
                "Evidence confidence is at least MEDIUM "
                f"({'/'.join(str(x) for x in (state.get('checks_confirmed'), state.get('checks_total')))} checks)"
            ),
            observed=(confidence or "unreported").upper(),
            authorizes=[root] if root else [],
            urn=root,
        )
    )

    # The live grounding for the root cause: the field that explains the symptom is
    # actually on the entity, right now, per DataHub's own schema aspect.
    if root and field and list_schema_fields_tool is not None:
        try:
            status = await _field_status(list_schema_fields_tool, root, field)
        except Exception:  # noqa: BLE001 -- issuing the proof must never take down the
            # mandatory report_findings checkpoint. An unreadable predicate is recorded
            # as unreadable, which does not hold, which is the conservative outcome.
            status = "unreadable"
        predicates.append(
            _predicate(
                next_id(),
                FIELD_PRESENT,
                holds=status == "current",
                unverifiable=status == "unreadable",
                statement=f"Field `{field}` is present on the root-cause entity",
                observed=status,
                authorizes=[root],
                urn=root,
                field=field,
                source_tool="list_schema_fields",
            )
        )

    # Each proven-stale mirror grounds its own authorization and nothing else. This
    # is the narrow-revocation case: a mirror that gets independently updated between
    # the proof being issued and the tag being written is no longer a thing this run
    # proved anything about, and tagging it would be acting on a finding that has
    # since been fixed.
    for mirror in drift.get("mirrors_stale", []):
        mirror_urn = mirror.get("urn")
        if not mirror_urn:
            continue
        predicates.append(
            _predicate(
                next_id(),
                FIELD_ABSENT,
                holds=True,
                statement=(
                    f"Mirror on {mirror.get('platform', 'unknown')} is running stale "
                    f"schema -- `{drift.get('checked_field')}` absent"
                ),
                observed=mirror.get("status", "stale"),
                authorizes=[mirror_urn],
                urn=mirror_urn,
                field=drift.get("checked_field"),
                source_tool="list_schema_fields",
            )
        )

    targets = (
        {name: sorted(authorized_targets(name, state)) for name in ("add_tags", "update_description")}
        if authorized_targets is not None
        else {"add_tags": [], "update_description": []}
    )
    # A tier that authorizes nothing authorizes nothing to no entity. Stated rather
    # than implied, so a DENY proof does not display a target list a reader could
    # mistake for permission.
    if severity in (None, "no_action"):
        targets = {"add_tags": [], "update_description": []}
    elif severity == "tag_only":
        targets = {**targets, "update_description": []}

    # A proof only says ALLOW when the tier permits something *and* nothing DataHub
    # can actually answer contradicts the grounds. Contradicted, not merely
    # unanswered: a schema the graph refused to serve leaves the tier's decision
    # standing, because being unable to look is not a finding.
    contradicted = [p["id"] for p in predicates if not p["holds"] and not p["unverifiable"]]
    decision = "ALLOW" if severity not in (None, "no_action") and not contradicted else "DENY"
    permitted_tags = _TIER_TAGS.get(severity or "no_action", [])
    if decision == "DENY":
        targets = {"add_tags": [], "update_description": []}
    core = _core(severity, decision, predicates, targets, permitted_tags)

    failed = contradicted
    return {
        "authorization_id": authorization_id(core),
        "proof_hash": proof_hash(core),
        "policy_version": POLICY_VERSION,
        "issued_at": _now(),
        "decision": decision,
        "severity": severity,
        "confidence": {
            "level": confidence,
            "confirmed": state.get("checks_confirmed"),
            "total": state.get("checks_total"),
        },
        "predicates": predicates,
        "failed_predicates": failed,
        "evidence": _evidence_grounding(state, predicates),
        "authorized_targets": targets,
        "permitted_tags": permitted_tags,
        "revoked_targets": [],
        "revoked_by": [],
        "core": core,
    }


def _evidence_grounding(state: dict, predicates: list[dict]) -> list[dict[str, Any]]:
    """Each evidence check, with where it came from -- a fresh tool call this run, a
    named prior investigation, or a live predicate in this proof.

    `E3 = lineage confirmed` is an assertion. `E3, inherited from INC-20260806-091500,
    grounded by P3 (schemaMetadata on urn:li:dataset:(…))` is a thing a reader can go
    and check. Imported here rather than re-derived so the labels can never drift from
    the ones the policy layer actually scored.
    """
    from .decision import EVIDENCE_LABELS

    findings = state.get("findings")
    inherited = set(getattr(findings, "inherited_evidence", None) or ())
    sources = state.get("inheritance_sources") or {}
    live = next((p["id"] for p in predicates if p["kind"] == FIELD_PRESENT), None)

    return [
        {
            "key": key,
            "label": label,
            "confirmed": bool(getattr(findings, key, False)) if findings else False,
            "inherited": key in inherited and bool(getattr(findings, key, False)),
            "source": sources.get(key) if key in inherited else "this run",
            # Only the two schema-derived checks are grounded by the live predicate;
            # the lineage checks are established by get_lineage calls that this proof
            # does not re-run, and claiming otherwise would be fabricated provenance.
            "grounded_by": live
            if key in ("evidence_recent_schema_change", "evidence_field_matches_symptom")
            else None,
        }
        for key, label in EVIDENCE_LABELS
    ]


async def recheck_authorization(proof: dict, list_schema_fields_tool=None) -> dict[str, Any]:
    """Re-read every live predicate and return the proof as it stands *now*.

    Returns a copy with `revoked_targets`, `revoked_by`, a recomputed hash, and the
    predicates carrying their fresh observations. The original is never mutated, so a
    caller can show both the issued proof and the current one side by side -- which is
    the whole demonstration: same authorization id, different verdict, and a named
    predicate explaining the difference.

    Unreadable is not a flip. If DataHub cannot answer, the predicate keeps its
    previous verdict and is marked `recheck: "unverifiable"`.
    """
    predicates = [dict(p) for p in proof.get("predicates", [])]
    if list_schema_fields_tool is None:
        return {**proof, "predicates": predicates, "rechecked_at": _now()}

    revoked_by: list[str] = []
    revoked_targets: set[str] = set()

    for predicate in predicates:
        if not predicate.get("live") or not predicate.get("urn") or not predicate.get("field"):
            continue
        try:
            status = await _field_status(
                list_schema_fields_tool, predicate["urn"], predicate["field"]
            )
        except Exception as exc:  # noqa: BLE001 -- a failed re-read must never widen
            # or narrow authority; it is reported and the prior verdict stands.
            predicate["recheck"] = "unverifiable"
            predicate["recheck_detail"] = str(exc)
            continue

        if status == "unreadable":
            predicate["recheck"] = "unverifiable"
            predicate["recheck_detail"] = "no readable schema for this entity"
            continue

        holds_now = status == "current" if predicate["kind"] == FIELD_PRESENT else status == "stale"
        predicate["observed_now"] = status
        if holds_now == predicate["holds"]:
            predicate["recheck"] = "confirmed"
            continue

        predicate["recheck"] = "flipped"
        predicate["holds"] = holds_now
        predicate["recheck_detail"] = (
            f"`{predicate['field']}` on {predicate['urn']} read as '{status}'; the proof "
            f"was issued on '{predicate['observed']}'"
        )
        revoked_by.append(predicate["id"])
        revoked_targets |= set(predicate["authorizes"])

    targets = {
        tool: [urn for urn in urns if urn not in revoked_targets]
        for tool, urns in (proof.get("authorized_targets") or {}).items()
    }
    decision = proof.get("decision")
    if decision == "ALLOW" and not any(targets.values()):
        decision = "REVOKED"

    core = _core(
        proof.get("severity"),
        decision,
        predicates,
        targets,
        proof.get("permitted_tags") or [],
    )
    return {
        **proof,
        "predicates": predicates,
        "authorized_targets": targets,
        "revoked_targets": sorted(revoked_targets),
        "revoked_by": revoked_by,
        "decision": decision,
        "rechecked_at": _now(),
        # The issued id is kept as the identity of the decision; the hash moves,
        # because the grounds moved. That divergence *is* the finding.
        "current_hash": proof_hash(core),
        "hash_changed": proof_hash(core) != proof.get("proof_hash"),
    }


def revocation_message(tool_name: str, rechecked: dict, attempted: list[str]) -> str:
    """The refusal text the gate returns when a proof no longer covers the attempt.

    Phrased around the predicate rather than the model, because that is what actually
    changed. An agent reading this learns that the world moved, not that it was
    naughty.
    """
    flipped = [
        p for p in rechecked.get("predicates", []) if p.get("recheck") == "flipped"
    ]
    detail = "; ".join(f"{p['id']} ({p.get('recheck_detail', '')})" for p in flipped)
    revoked = [urn for urn in attempted if urn in set(rechecked.get("revoked_targets") or ())]
    return (
        f"Blocked: authorization {rechecked.get('authorization_id')} no longer holds. "
        f"{len(flipped)} grounding predicate(s) changed in DataHub since it was issued: "
        f"{detail}. Revoked for: {', '.join(revoked) or 'all targets'}. "
        f"{tool_name} is refused until a fresh investigation re-establishes the evidence."
    )
