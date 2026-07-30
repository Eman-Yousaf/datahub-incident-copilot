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
"""

from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

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
        "'append'), all on the exact root-cause URN."
    ),
}

_EVIDENCE_LABELS = (
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


def compute_confidence(findings: Findings) -> tuple[str, int, int]:
    """Confidence is purely `checks passed / checks total` on the 4-item evidence
    checklist -- a heuristic bucket (low/medium/high), not a fabricated percentage.
    An inconclusive outcome is always low confidence regardless of the checklist,
    since there's no confirmed root cause for evidence to be evidence *of*.
    """
    checks = [getattr(findings, name) for name, _ in _EVIDENCE_LABELS]
    checked, total = sum(checks), len(checks)
    if findings.outcome == "inconclusive":
        return "low", checked, total
    ratio = checked / total
    level = "high" if ratio >= 0.75 else "medium" if ratio >= 0.5 else "low"
    return level, checked, total


def compute_severity(confidence_level: str, findings: Findings) -> SeverityTier:
    """Severity = f(confidence, affected datasets, affected dashboards, business
    criticality) -- a plain function, not a judgment call the model makes freely.
    Low confidence (or inconclusive) always routes to no_action: uncertainty means
    a human reviews it, the agent doesn't act on a guess.
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
    if spans_multiple_platforms or total_affected >= 10 or findings.business_criticality == "high":
        return "tag_note_escalated"
    return "tag_and_note"


def build_report_findings_tool(state: dict):
    """Returns the report_findings tool, bound to `state` (a plain dict shared with
    mcp_client.py's mutation-tool gate). Calling this tool is how the agent's
    self-reported evidence becomes the code-computed severity that gates write-back.
    """

    @tool(args_schema=Findings)
    def report_findings(**kwargs) -> str:
        """Call this exactly once, after you've either confirmed a root cause or
        concluded inconclusive, and BEFORE any add_tags/update_description call.
        Reports your evidence checklist honestly (only mark an item TRUE if a tool
        call actually confirmed it). Confidence and the write-back tier you're
        authorized to use are computed from your answers, not decided by you --
        add_tags/update_description will refuse to run until you've called this."""
        findings = Findings(**kwargs)
        confidence_level, checked, total = compute_confidence(findings)
        severity = compute_severity(confidence_level, findings)
        state["severity"] = severity
        state["root_cause_urn"] = findings.root_cause_urn

        checklist = "\n".join(
            f"{'✓' if getattr(findings, name) else '✗'} {label}"
            for name, label in _EVIDENCE_LABELS
        )
        return (
            f"Root-cause confidence: {confidence_level.upper()} "
            f"({checked}/{total} evidence checks confirmed)\n"
            f"Evidence:\n{checklist}\n"
            f"Severity = f(confidence={confidence_level}, "
            f"affected_datasets={findings.affected_dataset_count}, "
            f"affected_dashboards={findings.affected_dashboard_count}, "
            f"business_criticality={findings.business_criticality}) "
            f"= {severity}\n"
            f"Authorized action: {SEVERITY_INSTRUCTIONS[severity]}"
        )

    return report_findings
