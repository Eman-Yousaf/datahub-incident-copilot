"""Cross-platform schema-drift check: does a same-real-world-entity mirror on another
platform still have the field that changed on the confirmed root cause?

DataHub's lineage graph is topologically honest -- if two datasets are connected, that
connection is real. It has no concept of whether the connection is *schema-safe*: a
table on one platform can change shape while its lineage neighbor on another platform
(the same real-world table, synced/mirrored/rebuilt there) silently keeps the old
schema. The graph keeps showing them as connected either way -- an edge only asserts
connectivity, never parity.

Verified against DataHub's own showcase-ecommerce datapack: the dbt `order_details`
model has lineage neighbors on snowflake, looker, and powerbi that are all the same
real-world table, and none of them picked up a field added to the dbt model. Only the
snowflake copy is a direct (1-hop) sync; looker and powerbi read from that snowflake
copy rather than from dbt directly, so they only show up at 2 hops -- checking 1-hop
only would silently miss two of the three real, live-confirmed stale mirrors. DataHub's
lineage never surfaces any of this on its own.

`audit_schema_drift` is called automatically from decision.py's `report_findings` --
it is not a tool the agent chooses to call. It used to be (`check_schema_drift`,
callable "optionally, at most once"), but a finding this central to the pitch can't
depend on the model remembering to ask for it: same reasoning as every other gate in
this project (severity, write-back targets, inherited evidence) -- an instruction the
model usually follows is not a control. The agent's only remaining input is the field
name it confirmed via SIGNAL (`Findings.changed_field_path`); Python decides whether
and how the audit runs, and the agent never gets to claim mirrors are stale (or
current) on its own word.
"""

import re

from .mcp_util import result_json


def _normalized_leaf_name(raw: str) -> str:
    """Last dot-qualified segment of a name or URN, lowercased, alphanumerics only.

    Matches "order_details" (dbt) to "ORDER_DETAILS" (snowflake) to "order_details"
    (looker view) -- the same real-world table, spelled differently across platforms
    -- without knowing any platform's naming convention in advance. Same
    comma-splitting idiom `memory.py`'s `card_title` already uses for dataset URNs.
    """
    if not raw:
        return ""
    tail = raw.rstrip(")").split(",")[-2] if raw.startswith("urn:li:dataset:(") and "," in raw else raw
    leaf = tail.split(".")[-1] if "." in tail else tail
    return re.sub(r"[^a-z0-9]", "", leaf.lower())


async def _lineage_neighbors(get_lineage_tool, urn: str, upstream: bool) -> list[dict]:
    # max_hops=2, not 1: verified live that a real mirror can be a same-platform sync
    # away from a direct copy (e.g. a BI tool reads from a warehouse sync table, not
    # from the transform layer directly) -- 1 hop alone misses genuine mirrors.
    # Confirmed cheap: 2-hop searchAcrossLineage completes in well under a second on
    # a warm stack (same query the BLAST RADIUS step already relies on at this depth).
    try:
        result = result_json(
            await get_lineage_tool.coroutine(urn=urn, upstream=upstream, max_hops=2, max_results=30)
        )
    except Exception:  # noqa: BLE001 -- one failed direction shouldn't sink the audit
        return []
    bucket = result.get("upstreams" if upstream else "downstreams") or {}
    return [
        r["entity"]
        for r in bucket.get("searchResults", [])
        if isinstance(r, dict) and isinstance(r.get("entity"), dict) and r["entity"].get("urn")
    ]


async def _field_status(list_schema_fields_tool, mirror_urn: str, field_path: str) -> str:
    """"current" (field present), "stale" (schema readable, field absent), or
    "unreadable" (couldn't get a schema at all -- e.g. a non-tabular BI asset).
    Never counts "unreadable" as stale: an unconfirmed absence is not evidence.
    """
    try:
        result = result_json(
            await list_schema_fields_tool.coroutine(
                urn=mirror_urn, keywords=[field_path], limit=10, offset=0
            )
        )
    except Exception:  # noqa: BLE001
        return "unreadable"
    fields = result.get("fields")
    if fields is None:
        return "unreadable"
    # keywords can match on description text, not just fieldPath -- require an exact
    # fieldPath match, never trust "non-empty result" alone.
    return "current" if any(f.get("fieldPath") == field_path for f in fields) else "stale"


async def audit_schema_drift(
    get_lineage_tool, list_schema_fields_tool, entity_urn: str, field_path: str
) -> dict:
    """Find same-real-world-entity mirrors of `entity_urn` on OTHER platforms (via
    lineage within 2 hops, both directions) and confirm field-by-field whether each
    still has `field_path`. Read-only -- never mutates anything.

    Returns `{"checked_field", "root_urn", "mirrors_checked", "mirrors_stale"}`, the
    exact shape `state["schema_drift"]` holds. `mirrors_checked` is `[]` (not an
    error) when this entity simply has no name-matched sibling on another platform
    within 2 hops.
    """
    own_name = _normalized_leaf_name(entity_urn)
    neighbors = (
        await _lineage_neighbors(get_lineage_tool, entity_urn, upstream=True)
    ) + (
        await _lineage_neighbors(get_lineage_tool, entity_urn, upstream=False)
    )

    seen: dict[str, dict] = {}
    for entity in neighbors:
        urn = entity["urn"]
        if urn == entity_urn or urn in seen:
            continue
        entity_type = (entity.get("type") or "").upper()
        if entity_type and entity_type != "DATASET":
            continue
        if _normalized_leaf_name(entity.get("name") or urn) != own_name:
            continue
        seen[urn] = entity

    if not seen:
        return {
            "checked_field": field_path,
            "root_urn": entity_urn,
            "mirrors_checked": [],
            "mirrors_stale": [],
        }

    checked: list[dict] = []
    stale: list[dict] = []
    for urn, entity in seen.items():
        platform = (entity.get("platform") or {}).get("name", "unknown")
        status = await _field_status(list_schema_fields_tool, urn, field_path)
        record = {"urn": urn, "platform": platform, "status": status}
        checked.append(record)
        if status == "stale":
            stale.append(record)

    return {
        "checked_field": field_path,
        "root_urn": entity_urn,
        "mirrors_checked": checked,
        "mirrors_stale": stale,
    }
