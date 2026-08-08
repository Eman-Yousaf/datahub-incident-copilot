"""Turns the agent's self-reported evidence into a confidence level and a severity
tier, both computed in plain Python -- not asked of the LLM as free-floating numbers.

The agent still does all the actual reasoning (deciding what to check, interpreting
what it finds); this module only decides how much that reasoning is worth acting on.
Concretely: `report_findings` is a tool the agent must call once per investigation,
with a checklist of which evidence items it actually confirmed via earlier tool calls.
Confidence is `checked / total` on that checklist, bucketed into low/medium/high --
never a fabricated precise probability, and always traceable back to which checks
passed. Severity is a deterministic function of that confidence plus blast-radius
size/spread, per SEVERITY below -- a judge (or the code) can reconstruct why any given
severity was chosen without having to trust the model's word for it.

Write-back tools are gated on the result (see mcp_client.py's `_gate_mutation_tool`):
a "no_action" severity -- low confidence, or an inconclusive investigation -- makes
add_tags/update_description refuse to run, regardless of what the agent tries. That's
the enforced "do not act" path: uncertainty routes to a human-review recommendation
instead of an autonomous write, and that routing can't be talked around by the model.

The gate covers *acting on the data* -- tagging and annotating catalog entities. It
deliberately does not cover *recording what was learned*: `report_findings` always
builds an Investigation Card (see memory.py) and that card is always written back,
refusals included. A run that refuses to act still produces the most useful artifact
it can -- an explicit record of what was checked, what was missing, and exactly what
evidence would make action safe next time -- and every field on it is derived here in
Python from the agent's evidence, never authored freehand by the model.
"""

from datetime import datetime, timezone
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from .memory import (
    REQUIRED_BEFORE_RETRY,
    EvidenceItem,
    InvestigationCard,
    confirmed_evidence_keys,
    confirmed_evidence_sources,
    new_incident_id,
)
from .mirror_audit import audit_schema_drift
from .revalidate import conflicted_ids, revalidate_prior_knowledge

SeverityTier = Literal["no_action", "tag_only", "tag_and_note", "tag_note_escalated"]

# What each tier actually authorizes -- shown to the agent so it knows exactly what
# it may do next, and used by mcp_client.py's gate to enforce the same thing in code.
SEVERITY_INSTRUCTIONS: dict[SeverityTier, str] = {
    "no_action": (
        "Confidence is too low (or the investigation is inconclusive) to act "
        "autonomously. Do NOT call add_tags or update_description -- they will be "
        "blocked. End your summary recommending human review instead."
    ),
    "tag_only": (
        "You may call add_tags with ONLY 'urn:li:tag:incident-flagged' on the exact "
        "root-cause URN. Do not call update_description or add the severity-high tag "
        "-- both will be blocked at this tier."
    ),
    "tag_and_note": (
        "You may call add_tags with 'urn:li:tag:incident-flagged' and "
        "update_description(operation='append'), both on the exact root-cause URN. "
        "The severity-high tag will be blocked at this tier."
    ),
    "tag_note_escalated": (
        "You may call add_tags with BOTH 'urn:li:tag:incident-flagged' and "
        "'urn:li:tag:incident-severity-high', and update_description(operation="
        "'append'), all on the exact root-cause URN. If report_findings' automatic "
        "cross-platform check found stale mirrors, add_tags (not update_description) "
        "may also target those exact mirror URNs -- any other entity is blocked."
    ),
}

EVIDENCE_LABELS = (
    ("evidence_recent_schema_change", "Recent schema change confirmed on the exact URN"),
    ("evidence_field_matches_symptom", "Changed field plausibly matches the reported symptom"),
    ("evidence_lineage_confirms_path", "Lineage path confirmed via get_lineage"),
    ("evidence_downstream_confirmed", "Downstream impact confirmed via get_lineage"),
)


class Findings(BaseModel):
    outcome: Literal["root_cause_found", "inconclusive"] = Field(
        description="Whether a root cause was confirmed via a direct tool call on its "
        "exact URN, or the investigation is stopping without one."
    )
    root_cause_urn: str | None = Field(
        default=None,
        description="The exact URN where SIGNAL was confirmed. Required if outcome is "
        "'root_cause_found'. Must be a real URN an earlier tool call returned, never "
        "invented.",
    )
    root_cause_summary: str = Field(
        description="One or two sentences: what changed and why it plausibly explains "
        "the symptom, or why nothing conclusive was found."
    )
    evidence_recent_schema_change: bool = Field(
        description="TRUE only if a tool call on the root-cause URN itself showed a "
        "field with a lastModified timestamp noticeably more recent than its siblings."
    )
    evidence_field_matches_symptom: bool = Field(
        description="TRUE only if the changed field's own name/description plausibly "
        "explains the SPECIFIC symptom in the incident report, not just 'something "
        "changed'."
    )
    evidence_lineage_confirms_path: bool = Field(
        description="TRUE only if get_lineage actually returned a path connecting the "
        "root cause to the entity named in the incident report."
    )
    evidence_downstream_confirmed: bool = Field(
        description="TRUE only if get_lineage(upstream=False) on the root-cause URN "
        "has actually been called (whether it returned zero consumers or many -- "
        "FALSE only means 'not yet called', not 'found nothing')."
    )
    affected_dataset_count: int = Field(
        default=0,
        description="Count of downstream DATASET-typed entities from your blast-radius "
        "get_lineage call. 0 if none, or if outcome is inconclusive.",
    )
    affected_dashboard_count: int = Field(
        default=0,
        description="Count of downstream DASHBOARD/CHART-typed entities from your "
        "blast-radius get_lineage call. 0 if none, or if outcome is inconclusive.",
    )
    platforms_affected: list[str] = Field(
        default_factory=list,
        description="Distinct platform names (e.g. ['snowflake','looker']) seen among "
        "the downstream entities. Empty if none.",
    )
    business_criticality: Literal["low", "medium", "high"] = Field(
        default="medium",
        description="Your judgment of how business-critical the affected assets are, "
        "based on what you've actually seen (customer-facing dashboards, executive "
        "reporting, names/domains indicating importance). Default 'medium' if you "
        "have no specific signal either way -- don't guess 'high' without a reason "
        "you can point to.",
    )
    hypotheses_tested: list[str] = Field(
        default_factory=list,
        description="Short descriptions of the explanations you actually investigated "
        "this run, e.g. 'recent schema change on order_details introduced a new status "
        "value'. One line each.",
    )
    hypotheses_rejected: list[str] = Field(
        default_factory=list,
        description="Of those, the ones a tool call actually ruled OUT, with the reason, "
        "e.g. 'upstream promotions table changed -- rejected, no field modified in the "
        "incident window'. These are recorded so a future investigation does not waste "
        "calls re-testing them; only list a hypothesis here if you genuinely disproved "
        "it, not if you merely didn't get to it.",
    )
    inherited_evidence: list[str] = Field(
        default_factory=list,
        description="Evidence check names (exactly as spelled in this schema, e.g. "
        "'evidence_lineage_confirms_path') that you carried forward from a prior "
        "Investigation Card returned by recall_prior_investigations, rather than "
        "re-confirming with your own tool call this run. Leave empty if you confirmed "
        "everything yourself. Every claim here is verified against the cards recall "
        "actually returned: if none of them confirms the check, the claim is rejected "
        "and the check is counted as UNCONFIRMED, which lowers your confidence. Only "
        "list a check you genuinely saw marked confirmed in a recalled card.",
    )
    continues_incident_id: str | None = Field(
        default=None,
        description="If recall_prior_investigations returned a card you are continuing, "
        "its incident_id (e.g. 'INC-20260803-141522'). None if this is a fresh "
        "investigation with no usable prior card.",
    )
    changed_field_path: str | None = Field(
        default=None,
        description="The exact fieldPath you confirmed in step 2 SIGNAL as the field "
        "that changed and explains the symptom -- e.g. 'order_status_detail'. Set "
        "this whenever outcome is 'root_cause_found' and SIGNAL was confirmed via a "
        "schema field this run (the normal case): it automatically triggers a "
        "cross-platform check for same-entity mirrors on other platforms running "
        "stale schema. Leave it None only when the root cause came purely from a "
        "prior Investigation Card with no fresh field name of your own this run, or "
        "when outcome is 'inconclusive'.",
    )


_EVIDENCE_KEYS = frozenset(name for name, _ in EVIDENCE_LABELS)


def validate_inheritance(findings: Findings, state: dict) -> list[str]:
    """Check every "I carried this forward" claim against the cards actually recalled,
    and demote the ones nothing backs. Mutates `findings` in place; returns a
    human-readable list of what was rejected.

    This closes the one hole the rest of the policy layer would otherwise leave open.
    Confidence is `checks confirmed / 4`, and a check counts as confirmed either
    because a tool call proved it this run or because a prior card already proved it.
    The second half is the model's word alone -- so an agent that wanted to act could
    reach HIGH confidence by asserting inheritance for checks no investigation ever
    ran. Here that assertion is verified against `state["prior_cards"]`, which recall
    populated in Python from real stored payloads. A claim with nothing behind it
    doesn't just lose its inherited flag: the check goes back to unconfirmed, so it
    lowers confidence and can pull severity down to no_action, exactly as if the
    agent had admitted it never checked. Same reasoning as gating write-back in code
    rather than in the prompt -- an honesty instruction the model usually follows is
    not a control.
    """
    all_prior: list[InvestigationCard] = state.get("prior_cards") or []
    # A card whose claim live DataHub contradicts is withdrawn as evidence before
    # any of this runs (see revalidate.py). It stays visible on the new card and in
    # the UI -- the withdrawal is a finding, not a silent filter -- but it can no
    # longer back an inheritance claim, so a run leaning on stale memory falls back
    # to what it proved itself.
    conflicted = set(state.get("conflicted_incident_ids") or ())
    prior = [card for card in all_prior if card.incident_id not in conflicted]

    backed = confirmed_evidence_keys(prior)
    stale_backed = confirmed_evidence_keys(
        [card for card in all_prior if card.incident_id in conflicted]
    )
    sources = confirmed_evidence_sources(prior)
    recalled_ids = {card.incident_id for card in prior}

    dropped: list[str] = []
    kept: list[str] = []
    # Which prior card each accepted claim rests on. Recorded for display only --
    # the accept/reject decision above is unchanged -- so the UI can attribute a
    # skipped check to a specific stored investigation instead of asserting a
    # vague "inherited".
    attribution: dict[str, str] = {}
    for key in dict.fromkeys(findings.inherited_evidence or []):
        if key not in _EVIDENCE_KEYS:
            dropped.append(f"`{key}` — not one of the four evidence checks")
            continue
        if key in backed:
            kept.append(key)
            attribution[key] = sources.get(key, "")
            continue
        if key in stale_backed:
            dropped.append(
                f"`{key}` — the only card confirming this check no longer matches live "
                "DataHub, so it was withdrawn and the check reset to unconfirmed"
            )
        else:
            dropped.append(
                f"`{key}` — no recalled Investigation Card confirms this check, so it was "
                "reset to unconfirmed"
            )
        setattr(findings, key, False)

    findings.inherited_evidence = kept
    state["inheritance_sources"] = attribution

    if findings.continues_incident_id and findings.continues_incident_id not in recalled_ids:
        dropped.append(
            f"`continues_incident_id={findings.continues_incident_id}` — that card was "
            "not among the ones recall returned this run"
        )
        findings.continues_incident_id = None

    return dropped


def compute_confidence(findings: Findings) -> tuple[str, int, int]:
    """Confidence is purely `checks passed / checks total` on the 4-item evidence
    checklist -- a heuristic bucket (low/medium/high), not a fabricated percentage.
    An inconclusive outcome is always low confidence regardless of the checklist,
    since there's no confirmed root cause for evidence to be evidence *of*.
    """
    checks = [getattr(findings, name) for name, _ in EVIDENCE_LABELS]
    checked, total = sum(checks), len(checks)
    if findings.outcome == "inconclusive":
        return "low", checked, total
    ratio = checked / total
    level = "high" if ratio >= 0.75 else "medium" if ratio >= 0.5 else "low"
    return level, checked, total


def compute_severity(
    confidence_level: str, findings: Findings, schema_drift: dict | None = None
) -> SeverityTier:
    """Severity = f(confidence, affected datasets, affected dashboards, business
    criticality, confirmed schema drift) -- a plain function, not a judgment call the
    model makes freely. Low confidence (or inconclusive) always routes to no_action:
    uncertainty means a human reviews it, the agent doesn't act on a guess.

    `schema_drift` is `state["schema_drift"]`, populated automatically inside
    `report_findings` (see `audit_schema_drift` in mirror_audit.py) whenever a root
    cause and a freshly-confirmed field name are both available -- None if there was
    nothing to audit this run (inconclusive outcome, or no fresh field name). Two or
    more confirmed-stale mirrors is a real, code-verified signal of ongoing risk
    beyond the blast-radius count alone, so it's one more OR-condition on the same
    escalation tier. Every existing condition and return path here is unchanged; the
    default `None` makes this a no-op whenever there was nothing to check.
    """
    if findings.outcome == "inconclusive" or confidence_level == "low":
        return "no_action"
    total_affected = findings.affected_dataset_count + findings.affected_dashboard_count
    spans_multiple_platforms = len(set(findings.platforms_affected)) > 1
    if total_affected == 0:
        return "tag_only"
    if confidence_level == "medium":
        # Acts, but doesn't escalate on medium confidence even with a big blast
        # radius -- not fully sure it's even the right root cause yet.
        return "tag_and_note"
    stale_mirrors = len((schema_drift or {}).get("mirrors_stale", []))
    if (
        spans_multiple_platforms
        or total_affected >= 10
        or findings.business_criticality == "high"
        or stale_mirrors >= 2
    ):
        return "tag_note_escalated"
    return "tag_and_note"


def _refusal_reason(findings: Findings, confidence_level: str, checked: int, total: int) -> str:
    """Why the policy withheld action, stated in terms of the checks themselves.
    Derived, not written by the model -- so the reason on the card always matches the
    arithmetic that actually produced the refusal.
    """
    if findings.outcome == "inconclusive":
        return (
            "No root cause was established, so there is nothing to act on. "
            f"{findings.root_cause_summary}".strip()
        )
    return (
        f"Only {checked} of {total} evidence checks were confirmed, which is "
        f"{confidence_level.upper()} confidence. Autonomous write-back requires at "
        "least medium confidence, so the write-back tools were blocked and this is "
        "routed to human review instead."
    )


def build_card(state: dict) -> InvestigationCard:
    """Assemble the durable Investigation Card from what actually happened this run.

    Every field is derived: evidence from the agent's checklist, confidence/severity
    from the functions above, the refusal reason from the arithmetic, the required-
    before-retry list from exactly which checks came back false, provenance from the
    tools that really got called, and actions_taken from mutations that really
    succeeded (recorded by the gate in mcp_client.py, not claimed by the model).

    `findings.inherited_evidence` has already been through `validate_inheritance` by
    the time this runs, so anything still flagged inherited here is backed by a card
    that really was recalled.
    """
    findings: Findings = state["findings"]
    confidence_level: str = state["confidence_level"]
    checked: int = state["checks_confirmed"]
    total: int = state["checks_total"]
    severity: SeverityTier = state["severity"]
    inherited = set(findings.inherited_evidence or [])
    schema_drift = state.get("schema_drift") or {}
    mirrors_checked = schema_drift.get("mirrors_checked", [])
    mirrors_stale = schema_drift.get("mirrors_stale", [])

    decision = "REFUSAL" if severity == "no_action" else "ACTION"
    evidence = [
        EvidenceItem(
            key=name,
            label=label,
            confirmed=getattr(findings, name),
            inherited=name in inherited and getattr(findings, name),
        )
        for name, label in EVIDENCE_LABELS
    ]

    return InvestigationCard(
        incident_id=state.setdefault("incident_id", new_incident_id()),
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        trigger=state.get("trigger", ""),
        subject_urn=state.get("subject_urn"),
        root_cause_urn=findings.root_cause_urn,
        root_cause_summary=findings.root_cause_summary,
        root_cause_field=findings.changed_field_path,
        outcome=findings.outcome,
        evidence=evidence,
        hypotheses_tested=findings.hypotheses_tested,
        hypotheses_rejected=findings.hypotheses_rejected,
        confidence_level=confidence_level,
        checks_confirmed=checked,
        checks_total=total,
        severity=severity,
        decision=decision,
        refusal_reason=(
            _refusal_reason(findings, confidence_level, checked, total)
            if decision == "REFUSAL"
            else ""
        ),
        required_before_retry=(
            [REQUIRED_BEFORE_RETRY[name] for name, _ in EVIDENCE_LABELS if not getattr(findings, name)]
            if decision == "REFUSAL"
            else []
        ),
        provenance=sorted(state.get("tools_used", set())),
        actions_taken=list(state.get("actions_taken", [])),
        continues_incident_id=findings.continues_incident_id,
        reused_checks=len([name for name in inherited if getattr(findings, name, False)]),
        datahub_calls=len(state.get("datahub_calls") or []),
        dropped_inheritance=list(state.get("dropped_inheritance", [])),
        schema_drift_field=schema_drift.get("checked_field"),
        schema_drift_mirrors_checked=len(mirrors_checked),
        schema_drift_mirrors_stale=len(mirrors_stale),
        schema_drift_stale_platforms=[m.get("platform", "unknown") for m in mirrors_stale],
    )


def build_report_findings_tool(state: dict, get_lineage_tool=None, list_schema_fields_tool=None):
    """Returns the report_findings tool, bound to `state` (a plain dict shared with
    mcp_client.py's mutation-tool gate). Calling this tool is how the agent's
    self-reported evidence becomes the code-computed severity that gates write-back.

    Also runs the cross-platform schema-drift audit itself, whenever there's enough
    to run it on. This used to be a separate tool (`check_schema_drift`) the agent
    could choose to call or skip -- but a finding this central to the pitch can't
    depend on the model remembering to ask for it. `get_lineage_tool`/
    `list_schema_fields_tool` are the raw MCP tools `audit_schema_drift` needs; if
    either is unavailable, the audit is silently skipped rather than failing the
    mandatory checkpoint.
    """

    @tool(args_schema=Findings)
    async def report_findings(**kwargs) -> str:
        """Call this exactly once, after you've either confirmed a root cause or
        concluded inconclusive, and BEFORE any add_tags/update_description call.
        Reports your evidence checklist honestly (only mark an item TRUE if a tool
        call actually confirmed it). Confidence and the write-back tier you're
        authorized to use are computed from your answers, not decided by you --
        add_tags/update_description will refuse to run until you've called this. If
        you confirmed a root cause via a specific schema field this run (the normal
        case), report it in changed_field_path -- this automatically checks whether
        same-entity mirrors on other platforms are running stale schema."""
        findings = Findings(**kwargs)

        # Re-test stored knowledge against the graph as it is now, BEFORE anything
        # is allowed to inherit from it. A card can be entirely genuine and still
        # describe a state DataHub has since moved on from, and a stale finding
        # buying confidence in the present is the specific way memory makes an
        # agent worse rather than better.
        prior_cards = state.get("prior_cards") or []
        if prior_cards:
            validation = await revalidate_prior_knowledge(list_schema_fields_tool, prior_cards)
            state["prior_validation"] = validation
            state["conflicted_incident_ids"] = conflicted_ids(validation)

        # Verify inheritance claims BEFORE the arithmetic runs, so an unbacked claim
        # can't buy confidence it didn't earn.
        dropped = validate_inheritance(findings, state)

        schema_drift: dict | None = None
        if (
            findings.outcome == "root_cause_found"
            and findings.root_cause_urn
            and findings.changed_field_path
            and get_lineage_tool is not None
            and list_schema_fields_tool is not None
        ):
            try:
                schema_drift = await audit_schema_drift(
                    get_lineage_tool,
                    list_schema_fields_tool,
                    findings.root_cause_urn,
                    findings.changed_field_path,
                )
            except Exception:  # noqa: BLE001 -- the audit is best-effort; a failure
                # here must never take down the mandatory report_findings checkpoint.
                schema_drift = None
            state["schema_drift"] = schema_drift

        confidence_level, checked, total = compute_confidence(findings)
        severity = compute_severity(confidence_level, findings, schema_drift)
        state["severity"] = severity
        state["root_cause_urn"] = findings.root_cause_urn
        # Everything build_card needs, captured at the moment the policy ran rather
        # than re-asked of the model later, when it may have drifted.
        state["findings"] = findings
        state["confidence_level"] = confidence_level
        state["checks_confirmed"] = checked
        state["checks_total"] = total
        state["dropped_inheritance"] = dropped

        checklist = "\n".join(
            f"{'✓' if getattr(findings, name) else '✗'} {label}"
            for name, label in EVIDENCE_LABELS
        )
        rejected = (
            "Inheritance claims rejected (nothing recalled backs them; the checks were "
            "reset to unconfirmed):\n" + "\n".join(f"  - {claim}" for claim in dropped) + "\n"
            if dropped
            else ""
        )
        validation_rows = state.get("prior_validation") or []
        revalidation = (
            "Prior knowledge re-tested against live DataHub:\n"
            + "\n".join(
                f"  - {row['incident_id']}: {row['verdict'].upper()} -- {row['detail']}"
                for row in validation_rows
            )
            + "\n"
            if validation_rows
            else ""
        )

        mirrors_checked = (schema_drift or {}).get("mirrors_checked", [])
        mirrors_stale = (schema_drift or {}).get("mirrors_stale", [])
        if schema_drift is None:
            drift_line = (
                "Schema drift: not checked (no freshly-confirmed root-cause field "
                "this run to check mirrors against).\n"
            )
        elif not mirrors_checked:
            drift_line = (
                "Schema drift: checked -- no same-entity mirrors found on other "
                "platforms.\n"
            )
        else:
            mirror_lines = "\n".join(
                f"  - {m['platform']} ({m['urn']}): {m['status']}" for m in mirrors_checked
            )
            drift_line = (
                f"Schema drift on `{schema_drift.get('checked_field')}` -- checked "
                f"{len(mirrors_checked)} cross-platform mirror(s):\n{mirror_lines}\n"
                + (
                    f"  {len(mirrors_stale)} of {len(mirrors_checked)} running STALE "
                    "schema -- they will keep producing the same symptom even after "
                    "the root cause is fixed, until independently updated.\n"
                    if mirrors_stale
                    else "  All mirrors current.\n"
                )
            )

        return (
            f"{revalidation}"
            f"{rejected}"
            f"Root-cause confidence: {confidence_level.upper()} "
            f"({checked}/{total} evidence checks confirmed)\n"
            f"Evidence:\n{checklist}\n"
            f"{drift_line}"
            f"Severity = f(confidence={confidence_level}, "
            f"affected_datasets={findings.affected_dataset_count}, "
            f"affected_dashboards={findings.affected_dashboard_count}, "
            f"business_criticality={findings.business_criticality}, "
            f"stale_mirrors={len(mirrors_stale)}) "
            f"= {severity}\n"
            f"Authorized action: {SEVERITY_INSTRUCTIONS[severity]}\n"
            "Then call write_investigation_card (always -- it is never blocked, and a "
            "refusal is exactly the case where recording what you learned matters most)."
        )

    return report_findings
