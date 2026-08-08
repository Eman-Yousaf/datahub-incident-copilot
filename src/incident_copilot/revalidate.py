"""Re-check stored knowledge against the graph as it is *now*, before letting any of
it count as evidence.

The memory layer already refuses to let the agent inherit a check that no recalled
card confirms. That closes the "the model made it up" hole. It leaves a second one
open, and it's the more dangerous of the two: a card can be perfectly genuine and
still be **out of date**. An investigation from last week that correctly proved
`order_status_detail` was the root cause on a dbt model is a true record of last
week. If the field has since been reverted, renamed, or the table rebuilt, that
card now describes a world that no longer exists -- and inheriting it would let a
stale finding buy confidence in the present, which is exactly how an agent with
memory becomes worse than one without.

So prior knowledge is treated as a hypothesis, never as truth. Every recalled card
that names a concrete, checkable claim -- a field on a specific URN -- has that
claim re-tested against live DataHub before the confidence arithmetic runs:

    confirmed     the field is still there; the prior finding holds
    conflict      the schema reads fine and the field is gone; the card is stale
    unverifiable  the schema couldn't be read at all, or the card names no field

Only `conflict` withdraws a card's backing. `unverifiable` deliberately does not:
an absence you couldn't confirm is not evidence of absence -- the same rule
`mirror_audit._field_status` already follows when it refuses to call an unreadable
mirror stale. Being unable to check is a reason to stay where you are, not a reason
to demote.

A withdrawn card doesn't merely lose its badge. Its confirmed checks stop backing
inheritance at all, so a run resting on it falls back to what it proved itself --
which lowers confidence and can pull severity to `no_action`. The refusal is then
enforced by the same gate as every other refusal, in code, and the reason is
recorded on the new card. That is the point: the agent declines to act on its own
memory when the graph disagrees with it.
"""

from __future__ import annotations

from typing import Any

from .memory import InvestigationCard
from .mirror_audit import _field_status


def checkable_claim(card: InvestigationCard) -> tuple[str, str] | None:
    """The (urn, field) a card asserts, if it asserts one that can be re-tested.

    `root_cause_field` is recorded on cards written after this feature landed.
    `schema_drift_field` is the same field on older cards that happened to run the
    drift audit, so honouring it lets already-stored history be revalidated instead
    of being grandfathered in unchecked.
    """
    urn = card.root_cause_urn
    field = card.root_cause_field or card.schema_drift_field
    if not urn or not field:
        return None
    return urn, field


async def revalidate_prior_knowledge(
    list_schema_fields_tool, cards: list[InvestigationCard]
) -> list[dict[str, Any]]:
    """Re-test each card's concrete claim against live DataHub. Read-only.

    Returns one record per card, in the order given, so the UI can show the
    validation as part of the investigation rather than as a footnote.
    """
    if list_schema_fields_tool is None:
        return [
            {
                "incident_id": card.incident_id,
                "verdict": "unverifiable",
                "detail": "no schema tool available to re-check this card against DataHub",
                "urn": card.root_cause_urn,
                "field": None,
            }
            for card in cards
        ]

    out: list[dict[str, Any]] = []
    for card in cards:
        claim = checkable_claim(card)
        if claim is None:
            out.append(
                {
                    "incident_id": card.incident_id,
                    "verdict": "unverifiable",
                    "detail": "this card records no specific field to re-check",
                    "urn": card.root_cause_urn,
                    "field": None,
                }
            )
            continue

        urn, field = claim
        try:
            status = await _field_status(list_schema_fields_tool, urn, field)
        except Exception as exc:  # noqa: BLE001 -- revalidation is best-effort and
            # must never take down the mandatory report_findings checkpoint.
            out.append(
                {
                    "incident_id": card.incident_id,
                    "verdict": "unverifiable",
                    "detail": f"re-check failed: {exc}",
                    "urn": urn,
                    "field": field,
                }
            )
            continue

        if status == "current":
            verdict, detail = (
                "confirmed",
                f"`{field}` is still present on this entity, so the prior finding holds",
            )
        elif status == "stale":
            verdict, detail = (
                "conflict",
                f"`{field}` is no longer present on this entity -- the stored finding "
                "describes a state DataHub is no longer in, so it was withdrawn as evidence",
            )
        else:
            verdict, detail = (
                "unverifiable",
                f"could not read a schema for this entity to re-check `{field}`",
            )

        out.append(
            {
                "incident_id": card.incident_id,
                "verdict": verdict,
                "detail": detail,
                "urn": urn,
                "field": field,
            }
        )
    return out


def conflicted_ids(validation: list[dict[str, Any]]) -> set[str]:
    """Cards whose claim the live graph contradicts. These stop backing inheritance."""
    return {row["incident_id"] for row in validation if row.get("verdict") == "conflict"}
