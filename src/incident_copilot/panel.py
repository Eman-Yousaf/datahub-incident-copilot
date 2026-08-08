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
    return [
        {
            "key": key,
            "label": label,
            "confirmed": bool(getattr(findings, key, False)) if findings else False,
            "inherited": key in inherited and bool(getattr(findings, key, False)),
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
        "card_urn": state.get("card_urn"),
        "tools_used": sorted(state.get("tools_used") or ()),
    }
