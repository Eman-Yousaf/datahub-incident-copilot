"""Turns the live `decision_state` dict into a JSON snapshot the web UI renders.

The web UI's investigation panel exists to show the machinery a judge would
otherwise have to take on faith: which evidence checks are confirmed, the
arithmetic behind the confidence level, the severity that arithmetic produced, and
whether write-back is locked or open. All of that has to come from the *same dict*
`mcp_client._gate_mutation_tool` reads when it decides to block a mutation.

The tempting shortcut is to parse it out of the narration text the agent is already
streaming. That would be a lie dressed as a dashboard: the panel would show what the
model *said*, while the gate acts on what the code *computed*, and the two are
allowed to disagree -- catching exactly that disagreement is why the gate exists.
So the UI reads the state itself, and this module's only job is to make it
JSON-safe.

Nothing here computes policy. If a number isn't in the state dict already, it
doesn't belong on screen.
"""

from __future__ import annotations

from typing import Any

from .decision import EVIDENCE_LABELS, SEVERITY_INSTRUCTIONS

# Write-back tools and the severity tiers that authorize them, mirrored from
# mcp_client so the panel can show a lock *before* any mutation is attempted.
# Kept as a display concern only -- the enforcing copy stays in mcp_client.
_WRITE_TOOLS = ("add_tags", "update_description")


def _phases(state: dict) -> list[dict[str, Any]]:
    """The investigation's progress, derived from tools that actually ran.

    Every phase is keyed off observed evidence -- a name in `tools_used` (recorded
    by the provenance wrapper when the coroutine really executed), or a key the
    policy layer only sets once it has run. None of it is a script playing out on
    a timer, which is the thing that would make this a fake progress bar.
    """
    used: set[str] = set(state.get("tools_used") or ())
    drift = state.get("schema_drift")

    return [
        {
            # Keyed off `prior_cards` rather than the tool name: the provenance
            # wrapper is applied to the MCP tools before the memory and policy
            # tools are appended, so recall never appears in `tools_used`. The
            # key it writes is the honest signal that it ran -- and it is set
            # even when the answer was "no prior investigation", which is a
            # completed recall, not a skipped one.
            "key": "recall",
            "label": "Prior investigations recalled",
            "done": "prior_cards" in state,
            "detail": f"{len(state.get('prior_cards') or [])} card(s) found",
        },
        {
            "key": "resolve",
            "label": "Entity resolved in DataHub",
            "done": bool(used & {"search", "get_entities"}),
        },
        {
            "key": "signal",
            "label": "Schema inspected for a recent change",
            "done": "list_schema_fields" in used,
        },
        {
            "key": "lineage",
            "label": "Lineage traversed",
            "done": "get_lineage" in used,
        },
        {
            "key": "drift",
            "label": "Cross-platform mirrors audited",
            "done": drift is not None,
            "detail": (
                f"{len(drift.get('mirrors_stale', []))} of "
                f"{len(drift.get('mirrors_checked', []))} stale"
                if drift
                else ""
            ),
        },
        {
            "key": "findings",
            "label": "Evidence reported to the policy layer",
            "done": "findings" in state,
        },
        {
            # The decision is made the moment severity exists, not when a
            # mutation happens. A run correctly refused at `no_action` performs
            # no write and must still read as decided -- marking it incomplete
            # would frame the refusal as an unfinished investigation, which is
            # the exact misreading this project exists to correct.
            "key": "writeback",
            "label": "Write-back decided",
            "done": state.get("severity") is not None,
            "detail": state.get("severity") or "",
        },
        {
            "key": "card",
            "label": "Investigation Card stored in DataHub",
            "done": bool(state.get("card_urn")),
        },
    ]


def _evidence(state: dict) -> list[dict[str, Any]]:
    """The four checks, with their real labels, shown as pending until
    `report_findings` has actually run."""
    findings = state.get("findings")
    inherited = set(getattr(findings, "inherited_evidence", None) or ())
    sources = state.get("inheritance_sources") or {}
    return [
        {
            "key": key,
            "label": label,
            "confirmed": bool(getattr(findings, key, False)) if findings else False,
            "inherited": key in inherited and bool(getattr(findings, key, False)),
            # Which stored investigation proved it, when it wasn't proved here.
            # An attributable claim can be checked; a bare "inherited" can't.
            "source": sources.get(key) if key in inherited else None,
            "reported": findings is not None,
        }
        for key, label in EVIDENCE_LABELS
    ]


def _write_back(state: dict) -> dict[str, Any]:
    """Whether each mutation tool is currently open, and every gate decision so far.

    `severity is None` means `report_findings` hasn't run, which is itself a lock:
    the gate refuses every mutation until it has. That's shown as locked rather
    than as "unknown", because refusing is exactly what the code would do.
    """
    severity = state.get("severity")
    allowed_by_tier = {
        "add_tags": {"tag_only", "tag_and_note", "tag_note_escalated"},
        "update_description": {"tag_and_note", "tag_note_escalated"},
    }
    root = state.get("root_cause_urn")
    stale = [
        mirror.get("urn")
        for mirror in (state.get("schema_drift") or {}).get("mirrors_stale", [])
        if mirror.get("urn")
    ]

    return {
        "locked": severity in (None, "no_action"),
        "severity": severity,
        "authorization": SEVERITY_INSTRUCTIONS.get(severity) if severity else None,
        "tools": [
            {
                "name": name,
                "unlocked": severity in allowed_by_tier[name] if severity else False,
            }
            for name in _WRITE_TOOLS
        ],
        # Exactly what `_authorized_targets` would permit right now -- the panel's
        # answer to "to what?", which severity alone never answered.
        "authorized_targets": {
            "add_tags": ([root] if root else []) + stale,
            "update_description": [root] if root else [],
        },
        "events": list(state.get("writeback_events") or []),
        "actions_taken": list(state.get("actions_taken") or []),
    }


def _drift(state: dict) -> dict[str, Any] | None:
    drift = state.get("schema_drift")
    if not drift:
        return None
    return {
        "field": drift.get("checked_field"),
        "root_urn": drift.get("root_urn"),
        "mirrors": drift.get("mirrors_checked", []),
        "stale": drift.get("mirrors_stale", []),
    }


def snapshot(state: dict) -> dict[str, Any]:
    """A complete, JSON-safe view of where the investigation stands right now."""
    findings = state.get("findings")
    prior = state.get("prior_cards") or []

    return {
        "trigger": state.get("trigger", ""),
        "incident_id": state.get("incident_id"),
        "phases": _phases(state),
        "memory": {
            "prior_cards": [
                {
                    "incident_id": card.incident_id,
                    "timestamp": card.timestamp,
                    "decision": card.decision,
                    "confidence": f"{card.checks_confirmed}/{card.checks_total}",
                    "severity": card.severity,
                    "confirmed": [item.label for item in card.confirmed_evidence],
                    "missing": [item.label for item in card.missing_evidence],
                }
                for card in prior
            ],
            # Prior knowledge re-tested against the live graph. Surfaced as its own
            # list because a CONFLICT is the most important thing this panel can
            # report: the agent declining to trust its own memory.
            "validation": list(state.get("prior_validation") or []),
            "continues": getattr(findings, "continues_incident_id", None),
            "reused_checks": len(
                [
                    key
                    for key in (getattr(findings, "inherited_evidence", None) or [])
                    if getattr(findings, key, False)
                ]
            ),
            # Claims the code refused to honour. Shown deliberately: a run that
            # tried to inherit evidence nothing backs is the most interesting
            # thing the panel can display.
            "rejected": list(state.get("dropped_inheritance") or []),
        },
        "evidence": _evidence(state),
        "confidence": {
            "level": state.get("confidence_level"),
            "confirmed": state.get("checks_confirmed"),
            "total": state.get("checks_total"),
            "reported": findings is not None,
        },
        "root_cause": {
            "urn": state.get("root_cause_urn"),
            "summary": getattr(findings, "root_cause_summary", "") if findings else "",
            "outcome": getattr(findings, "outcome", None) if findings else None,
            "field": getattr(findings, "changed_field_path", None) if findings else None,
        },
        "blast_radius": {
            "datasets": getattr(findings, "affected_dataset_count", 0) if findings else 0,
            "dashboards": getattr(findings, "affected_dashboard_count", 0) if findings else 0,
            "platforms": list(getattr(findings, "platforms_affected", []) or []) if findings else [],
            "criticality": getattr(findings, "business_criticality", None) if findings else None,
        },
        "schema_drift": _drift(state),
        "write_back": _write_back(state),
        "authorization": _authorization(state),
        "card_urn": state.get("card_urn"),
        "tools_used": sorted(state.get("tools_used") or ()),
        # Real DataHub API traffic, not a decorative counter: every entry is one
        # MCP call that reached GMS this run.
        "datahub_calls": {
            "total": len(state.get("datahub_calls") or []),
            "by_tool": _call_breakdown(state),
        },
        "knowledge": _knowledge(state),
    }


def _authorization(state: dict) -> dict[str, Any] | None:
    """The authorization proof as issued, and -- once a write has been attempted --
    as it re-read at the moment of action.

    Both are surfaced rather than only the latest, because the interesting event is
    the *difference* between them: same authorization id, a named predicate that
    stopped holding, and a target list that got shorter. Collapsing them to one
    current view would hide exactly the thing worth showing.

    Nothing is recomputed here. `proof` is the object the gate consulted, and
    `recheck` is the object it consulted at write time; if the panel disagreed with
    either, the panel would be the bug.
    """
    proof = state.get("authorization")
    if not proof:
        return None
    recheck = state.get("authorization_recheck")
    return {
        "id": proof.get("authorization_id"),
        "policy_version": proof.get("policy_version"),
        "hash": proof.get("proof_hash"),
        "issued_at": proof.get("issued_at"),
        "decision": proof.get("decision"),
        "severity": proof.get("severity"),
        "predicates": proof.get("predicates") or [],
        "failed_predicates": proof.get("failed_predicates") or [],
        "evidence": proof.get("evidence") or [],
        "authorized_targets": proof.get("authorized_targets") or {},
        "permitted_tags": proof.get("permitted_tags") or [],
        "recheck": (
            {
                "at": recheck.get("rechecked_at"),
                "decision": recheck.get("decision"),
                "revoked_targets": recheck.get("revoked_targets") or [],
                "revoked_by": recheck.get("revoked_by") or [],
                "hash": recheck.get("current_hash"),
                "hash_changed": recheck.get("hash_changed"),
                "predicates": recheck.get("predicates") or [],
                "authorized_targets": recheck.get("authorized_targets") or {},
            }
            if recheck
            else None
        ),
    }


def _call_breakdown(state: dict) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for name in state.get("datahub_calls") or []:
        counts[name] = counts.get(name, 0) + 1
    return [
        {"tool": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda row: -row[1])
    ]


def _knowledge(state: dict) -> dict[str, Any]:
    """What this run leaves behind for the next one.

    The closing claim of the whole project is that an investigation compounds
    rather than evaporating, so it should be stated in countable terms: how many
    checks this run proved itself, how many it didn't have to redo, and how many
    the next investigation of this incident will find already established. All
    three are read off the same evidence the policy layer scored -- no separate
    bookkeeping that could drift away from it.
    """
    findings = state.get("findings")
    if findings is None:
        return {"stored": False, "proved_here": 0, "reused": 0, "available_next_run": 0}

    inherited = set(findings.inherited_evidence or [])
    confirmed = [key for key, _ in EVIDENCE_LABELS if getattr(findings, key, False)]
    return {
        "stored": bool(state.get("card_urn")),
        "card_urn": state.get("card_urn"),
        "incident_id": state.get("incident_id"),
        "proved_here": len([key for key in confirmed if key not in inherited]),
        "reused": len([key for key in confirmed if key in inherited]),
        # A refusal stores a card too, so even a run that proved nothing still
        # records what was missing -- which is the thing that lets the next run
        # skip straight to it.
        "available_next_run": len(confirmed),
        "continues": findings.continues_incident_id,
    }
