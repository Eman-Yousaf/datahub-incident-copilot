"""Persistent investigation memory: the Investigation Card.

Every investigation -- including one that refuses to act -- ends by writing a
structured knowledge artifact back into DataHub as a `document` entity, linked to
the assets it was about. The next investigation reads those cards first and
*continues* from them instead of starting over.

This is not chat memory. Nothing here persists in the agent's context between runs;
the durable state lives in DataHub itself, which is the point -- a human opening the
dataset page in DataHub sees the same card the next agent run will read.

Two design rules this module exists to enforce:

1. **The card is built by Python, not written by the LLM.** The model supplies
   evidence (which checks it actually confirmed, which hypotheses it tested and
   rejected); every derived field -- confidence, severity, the refusal reason, what
   evidence would unlock action later -- is computed here from that input. So a card
   can't claim a conclusion the evidence doesn't support, and two runs with identical
   evidence always produce identical cards.

2. **A refusal is knowledge, not a failure.** The severity gate blocks acting on the
   data; it never blocks recording what was learned. A card documenting *why* the
   agent refused, and exactly what evidence would make action safe, is the most
   valuable thing a low-confidence run can produce -- and it's what lets the next run
   skip straight to the missing piece.

The card is stored twice over in one document: as readable markdown (what a human or
a judge sees in DataHub's UI) wrapping a single-line JSON payload inside an HTML
comment (what `parse_card` reads back exactly, with no fuzzy text parsing). Free-text
fields are truncated on the way in so the payload stays comfortably inside the excerpt
window `grep_documents` returns when the next run fetches it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from .mcp_util import result_json

# Marker delimiting the machine-readable payload. Also the search term used to find
# our own cards among all the other `document` entities in the graph (the showcase
# datapack ships its own operational runbooks as documents, so "any document" is much
# too broad a net). Kept free of regex metacharacters -- grep_documents compiles it
# as an RE2 pattern.
CARD_MARKER = "INCIDENT-COPILOT-CARD-V1"

TITLE_PREFIX = "Incident Investigation Card"

# save_document's document_type enum. "Decision" is the honest label: the card's
# reason for existing is recording what the agent decided it was allowed to do.
DOCUMENT_TYPE = "Decision"

Decision = Literal["ACTION", "REFUSAL"]

# Bounds on free text copied into the JSON payload. A card carries ~8 short strings
# and 2 small lists; capping each keeps the whole payload near 1-2KB, well inside the
# context window grep_documents returns around a match.
_MAX_TEXT = 300
_MAX_LIST_ITEMS = 6


def _clip(text: str | None, limit: int = _MAX_TEXT) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _clip_list(items: list[str] | None) -> list[str]:
    return [_clip(item, 160) for item in (items or [])[:_MAX_LIST_ITEMS]]


# What each unconfirmed evidence check would take to confirm. Deterministic: the
# "required before retry" list on a refusal card is exactly the failed checks mapped
# through this table, never a free-form suggestion the model improvises.
REQUIRED_BEFORE_RETRY: dict[str, str] = {
    "evidence_recent_schema_change": (
        "Confirm a recently-modified field on the candidate URN itself "
        "(list_schema_fields on that exact URN)"
    ),
    "evidence_field_matches_symptom": (
        "Establish that the changed field plausibly explains the reported symptom, "
        "not just that something changed"
    ),
    "evidence_lineage_confirms_path": (
        "Confirm a lineage path from the candidate root cause to the entity named in "
        "the report (get_lineage upstream)"
    ),
    "evidence_downstream_confirmed": (
        "Measure the downstream blast radius from the root-cause URN "
        "(get_lineage upstream=False)"
    ),
}


class EvidenceItem(BaseModel):
    key: str
    label: str
    confirmed: bool
    # True when this check was carried forward from an earlier card rather than
    # re-confirmed in this run. Tracked so a card can never quietly launder inherited
    # evidence as fresh -- the provenance stays visible on every future read.
    inherited: bool = False


class InvestigationCard(BaseModel):
    """One investigation's durable record. Written on every run, act or refuse."""

    incident_id: str
    timestamp: str
    trigger: str

    subject_urn: str | None = None
    root_cause_urn: str | None = None
    root_cause_summary: str = ""
    outcome: str = "inconclusive"

    evidence: list[EvidenceItem] = Field(default_factory=list)
    hypotheses_tested: list[str] = Field(default_factory=list)
    hypotheses_rejected: list[str] = Field(default_factory=list)

    confidence_level: str = "low"
    checks_confirmed: int = 0
    checks_total: int = 0
    severity: str = "no_action"

    decision: Decision = "REFUSAL"
    refusal_reason: str = ""
    required_before_retry: list[str] = Field(default_factory=list)

    # Which MCP tools actually produced the conclusions above -- provenance a reader
    # can check, rather than taking the narrative on faith.
    provenance: list[str] = Field(default_factory=list)
    actions_taken: list[str] = Field(default_factory=list)

    # Set when this run continued an earlier card instead of starting cold.
    continues_incident_id: str | None = None
    reused_checks: int = 0

    # Inheritance claims the code refused to honour because no recalled card actually
    # confirmed them. Recorded rather than silently discarded: a run that tried to
    # carry forward evidence it never had is exactly the thing a reader wants to see.
    dropped_inheritance: list[str] = Field(default_factory=list)

    @property
    def missing_evidence(self) -> list[EvidenceItem]:
        return [item for item in self.evidence if not item.confirmed]

    @property
    def confirmed_evidence(self) -> list[EvidenceItem]:
        return [item for item in self.evidence if item.confirmed]


def confirmed_evidence_keys(cards: list[InvestigationCard]) -> set[str]:
    """The evidence checks a set of recalled cards genuinely established.

    This is the ground truth an inheritance claim is checked against: the agent may
    say it carried a check forward, but unless one of the cards actually returned by
    `recall_prior_investigations` marks that check confirmed, there is nothing to
    carry. Without this, "inherited" would be a hole straight through the evidence
    checklist -- the model could reach high confidence by asserting that some earlier
    run had already done the work.
    """
    return {
        item.key for card in cards for item in card.evidence if item.confirmed
    }


def new_incident_id(now: datetime | None = None) -> str:
    """Timestamp-based so ids sort chronologically and never collide across runs
    without needing to read every existing card first to find the next number."""
    now = now or datetime.now(timezone.utc)
    return f"INC-{now.strftime('%Y%m%d-%H%M%S')}"


def card_title(card: InvestigationCard) -> str:
    subject = card.root_cause_urn or card.subject_urn or "unresolved"
    # The URN tail is the human-recognisable part (the table name); the full URN is
    # in the body and in related_assets.
    tail = subject.rstrip(")").split(",")[-2] if "," in subject else subject
    return f"{TITLE_PREFIX} {card.incident_id} — {tail} [{card.decision}]"


def render_card(card: InvestigationCard) -> str:
    """Markdown for humans, with the exact machine payload embedded in an HTML
    comment so `parse_card` never has to re-derive structure from prose."""
    lines: list[str] = [
        f"# {TITLE_PREFIX} {card.incident_id}",
        "",
        f"**Decision:** {card.decision}  ",
        f"**Confidence:** {card.checks_confirmed} / {card.checks_total} "
        f"({card.confidence_level.upper()})  ",
        f"**Severity:** {card.severity}  ",
        f"**Recorded:** {card.timestamp}",
        "",
        f"**Trigger:** {card.trigger}",
        "",
    ]

    if card.continues_incident_id:
        lines += [
            f"> Continues investigation **{card.continues_incident_id}** — "
            f"{card.reused_checks} evidence check(s) reused rather than re-run.",
            "",
        ]

    if card.root_cause_urn:
        lines += [f"**Root cause:** `{card.root_cause_urn}`", "", card.root_cause_summary, ""]
    else:
        lines += [f"**Root cause:** not established. {card.root_cause_summary}", ""]

    lines += ["## Evidence", ""]
    for item in card.evidence:
        mark = "x" if item.confirmed else " "
        suffix = " _(inherited from prior investigation)_" if item.inherited else ""
        lines.append(f"- [{mark}] {item.label}{suffix}")
    lines.append("")

    if card.dropped_inheritance:
        lines += [
            "> **Unbacked inheritance rejected.** The agent claimed to carry forward "
            "evidence that no recalled card confirmed; those checks were reset to "
            "unconfirmed before confidence was computed:",
            "",
        ]
        lines += [f"> - {claim}" for claim in card.dropped_inheritance]
        lines.append("")

    if card.hypotheses_tested or card.hypotheses_rejected:
        lines += ["## Hypotheses", ""]
        for hypothesis in card.hypotheses_tested:
            lines.append(f"- Tested: {hypothesis}")
        for hypothesis in card.hypotheses_rejected:
            lines.append(f"- Rejected: {hypothesis}")
        lines.append("")

    if card.decision == "REFUSAL":
        lines += [
            "## Why no action was taken",
            "",
            card.refusal_reason,
            "",
            "### Required before action becomes safe",
            "",
        ]
        lines += [f"- {req}" for req in card.required_before_retry] or ["- (none recorded)"]
        lines.append("")
    else:
        lines += ["## Actions taken", ""]
        lines += [f"- {action}" for action in card.actions_taken] or ["- (none recorded)"]
        lines.append("")

    if card.provenance:
        lines += ["## Provenance", "", "Derived from: " + ", ".join(f"`{t}`" for t in card.provenance), ""]

    payload = json.dumps(card.model_dump(), separators=(",", ":"), ensure_ascii=False)
    lines += ["---", "", f"<!-- {CARD_MARKER} {payload} -->", ""]
    return "\n".join(lines)


_PAYLOAD_RE = re.compile(re.escape(CARD_MARKER) + r"\s*(\{.*?\})\s*-->", re.DOTALL)


def parse_card(text: str) -> InvestigationCard | None:
    """Recover a card from stored document content (or from a grep excerpt around
    the marker). Returns None rather than raising: a malformed or truncated card is
    a reason to investigate from scratch, not a reason to crash the run."""
    if not text:
        return None
    match = _PAYLOAD_RE.search(text)
    if not match:
        return None
    try:
        return InvestigationCard.model_validate(json.loads(match.group(1)))
    except Exception:  # noqa: BLE001 -- any malformed payload means "no usable card"
        return None


_STOPWORDS = {
    "the", "our", "are", "and", "for", "with", "look", "looks", "wrong", "seem",
    "seems", "off", "on", "in", "of", "to", "is", "it", "we", "some", "certain",
    "numbers", "number", "data", "issue", "problem", "report", "reports",
}


def report_tokens(text: str) -> set[str]:
    """Distinctive lowercase words from an incident report, used to score how
    related a stored card is to the incident now being investigated."""
    words = re.findall(r"[a-z_]{3,}", (text or "").lower())
    return {word for word in words if word not in _STOPWORDS}


def relevance(card: InvestigationCard, incident_report: str) -> float:
    """Jaccard-ish overlap between the current report and the card's own trigger
    plus root cause. Plain arithmetic on both sides so which prior card gets reused
    is reproducible, not a similarity judgment the model makes differently each run."""
    now = report_tokens(incident_report)
    if not now:
        return 0.0
    before = report_tokens(f"{card.trigger} {card.root_cause_urn or ''} {card.root_cause_summary}")
    if not before:
        return 0.0
    return len(now & before) / len(now)


def summarize_for_agent(cards: list[InvestigationCard]) -> str:
    """The digest handed to the agent at the start of a run: what's already known,
    what was already ruled out, and what specifically is still missing. Phrased as
    instructions about what NOT to redo, because that's the behaviour being bought."""
    if not cards:
        return (
            "No prior investigation found for this incident. Starting from scratch — "
            "investigate normally, and record a card at the end so the next run doesn't "
            "have to repeat this work."
        )

    out: list[str] = [f"Found {len(cards)} prior investigation(s) for this incident."]
    for card in cards:
        out += [
            "",
            f"── {card.incident_id} ({card.timestamp}) — decision: {card.decision}, "
            f"confidence {card.checks_confirmed}/{card.checks_total} ({card.confidence_level}), "
            f"severity {card.severity}",
            f"   Trigger: {card.trigger}",
        ]
        if card.root_cause_urn:
            out.append(f"   Root cause established: {card.root_cause_urn}")
            out.append(f"   {card.root_cause_summary}")
        confirmed = card.confirmed_evidence
        if confirmed:
            out.append("   ALREADY CONFIRMED — do not re-run these checks:")
            out += [f"     ✓ {item.label}" for item in confirmed]
        missing = card.missing_evidence
        if missing:
            out.append("   STILL MISSING — this is what you need to check:")
            out += [f"     ✗ {item.label}" for item in missing]
        if card.hypotheses_rejected:
            out.append("   ALREADY DISPROVEN — do not re-test these:")
            out += [f"     ✗ {hypothesis}" for hypothesis in card.hypotheses_rejected]
        if card.decision == "REFUSAL" and card.required_before_retry:
            out.append(f"   Previously refused because: {card.refusal_reason}")
            out.append("   Action becomes safe once you establish:")
            out += [f"     → {req}" for req in card.required_before_retry]

    out += [
        "",
        "Continue this investigation — do NOT start over. Re-confirm a prior finding only "
        "if you have a concrete reason to think it changed (e.g. the entity itself has "
        "been modified since that card was written). Spend your tool calls on the missing "
        "evidence above. When you call report_findings, list the checks you carried "
        "forward in `inherited_evidence` and set `continues_incident_id` to the card you "
        "continued, so the provenance stays honest.",
    ]
    return "\n".join(out)


# A stored card has to share at least this share of the current report's distinctive
# words to count as the same incident. Set deliberately high enough that an unrelated
# incident doesn't inherit someone else's conclusions -- inheriting wrong evidence is
# far worse than investigating from scratch.
RELEVANCE_THRESHOLD = 0.25

# How many prior cards to feed forward. More than a couple stops being useful context
# and starts being noise the model has to re-read every turn.
MAX_PRIOR_CARDS = 2


def build_recall_tool(state: dict, search_documents_tool, grep_documents_tool):
    """Returns `recall_prior_investigations`, the first thing the agent calls.

    Deliberately implemented in Python rather than left to the model: it finds every
    document in the graph, keeps only the ones carrying our card marker, decodes their
    exact JSON payloads, and scores relevance arithmetically. The model never gets to
    decide which prior investigation it "feels" related to -- it just receives the
    digest, so the same incident always inherits the same evidence.
    """

    @tool
    async def recall_prior_investigations(incident_report: str) -> str:
        """Call this FIRST, before any other tool, with the incident report text.
        Looks up Investigation Cards this agent stored in DataHub during previous
        investigations of the same incident and returns what was already established,
        what was already disproven, and what evidence is still missing. If a prior
        investigation exists you must continue it rather than repeating its work."""
        state["trigger"] = incident_report

        try:
            # The card marker isn't reliably tokenised by the search index, so rather
            # than trusting a keyword query to find our own cards, list the documents
            # (a small set in this graph -- the datapack ships 3 runbooks, plus our
            # cards) and let grep decide which actually carry a card payload. Exact,
            # and it can't miss a card because of analyzer behaviour.
            found = result_json(await search_documents_tool.coroutine(query="*", num_results=50))
            urns = [
                result["entity"]["urn"]
                for result in found.get("searchResults", [])
                if isinstance(result, dict) and result.get("entity", {}).get("urn")
            ]
            if not urns:
                return summarize_for_agent([])

            grepped = result_json(
                await grep_documents_tool.coroutine(
                    urns=urns,
                    pattern=CARD_MARKER,
                    context_chars=6000,
                    max_matches_per_doc=1,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- recall is best-effort by design
            # A memory lookup that fails must never take the investigation down with
            # it: the correct fallback is simply to investigate from scratch.
            return (
                f"Prior-investigation lookup was unavailable ({exc}). Proceed with a "
                "full investigation from scratch, and still record a card at the end."
            )

        scored: list[tuple[float, str, InvestigationCard]] = []
        for document in grepped.get("results", []):
            for match in document.get("matches", []):
                card = parse_card(match.get("excerpt", ""))
                if card is None:
                    continue
                score = relevance(card, incident_report)
                if score >= RELEVANCE_THRESHOLD:
                    scored.append((score, document.get("urn", ""), card))
                break

        scored.sort(key=lambda row: (row[0], row[2].timestamp), reverse=True)
        selected = scored[:MAX_PRIOR_CARDS]

        state["prior_cards"] = [card for _, _, card in selected]
        state["prior_card_urns"] = [urn for _, urn, _ in selected if urn]
        return summarize_for_agent(state["prior_cards"])

    return recall_prior_investigations


def build_write_card_tool(state: dict, save_document_tool, grep_documents_tool, card_builder):
    """Returns `write_investigation_card`, the last thing the agent calls.

    Notably NOT gated on severity. The severity gate exists to stop the agent acting
    on data it isn't sure about; recording what it learned is the opposite -- the
    lower the confidence, the more valuable the record, because that card is what lets
    the next run skip straight to the missing evidence instead of rediscovering the
    same dead end. `card_builder` is injected (decision.build_card) so this module
    stays free of any dependency on the policy layer that imports it.
    """

    @tool
    async def write_investigation_card() -> str:
        """Call this LAST, after report_findings and after any write-back you were
        authorized to do. Takes no arguments: the card is assembled in code from the
        evidence you already reported, the tools you actually called, and the
        mutations that actually succeeded. Always call it -- including when you were
        refused permission to act, since a recorded refusal is what lets the next
        investigation continue instead of starting over."""
        if "findings" not in state:
            return "Blocked: call report_findings first -- the card is built from its evidence."

        card = card_builder(state)
        related_assets = [
            urn for urn in (card.root_cause_urn, card.subject_urn) if urn
        ]
        try:
            saved = result_json(
                await save_document_tool.coroutine(
                    document_type=DOCUMENT_TYPE,
                    title=card_title(card),
                    content=render_card(card),
                    topics=["incident-copilot", "incident-investigation", card.decision.lower()],
                    related_assets=list(dict.fromkeys(related_assets)) or None,
                    related_documents=state.get("prior_card_urns") or None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return f"Investigation card could not be stored: {exc}"

        urn = saved.get("urn")
        if not saved.get("success") or not urn:
            return f"Investigation card was not stored: {saved.get('message', saved) or 'unknown error'}"

        state["card_urn"] = urn
        state["card"] = card

        # Same principle as the mutation gate's read-back: prove the card is really in
        # DataHub by fetching it again, rather than reporting a bare success:true.
        verified = ""
        try:
            check = result_json(
                await grep_documents_tool.coroutine(
                    urns=[urn], pattern=CARD_MARKER, context_chars=200, max_matches_per_doc=1
                )
            )
            if check.get("documents_with_matches"):
                verified = " [Verified: re-read from DataHub, card payload present]"
        except Exception:  # noqa: BLE001 -- verification is best-effort
            verified = " [Verification read-back failed]"

        inherited = (
            f" It continues {card.continues_incident_id}, reusing {card.reused_checks} "
            f"evidence check(s)."
            if card.continues_incident_id
            else ""
        )
        return (
            f"Investigation card {card.incident_id} stored in DataHub as {urn}, linked to "
            f"{len(related_assets)} asset(s), decision {card.decision}.{inherited}"
            f"{verified}\n\n{render_card(card)}"
        )

    return write_investigation_card
