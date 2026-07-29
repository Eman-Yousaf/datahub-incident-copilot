"""Loads DataHub's real showcase-ecommerce datapack into the local quickstart, then
overlays the 3 locked incident triggers used for the robustness pass (milestone 6).

The datapack itself has no naturally-occurring recent schema-change event -- every
field across every entity was bulk-created in a single ingestion event (confirmed via
the timeline API: one ADD-all-fields event per entity, no prior history). So each of
the 3 triggers below adds exactly one new field to one real entity's schema, with a
`lastModified` audit stamp timestamped N hours before whenever this script runs. The
lineage graph, all other entities, and every other aspect stay completely genuine --
only these 3 schema versions are synthetic.

Trigger shapes (see plan milestone 2/6):
  - clean-one-hop:          order_details (dbt hub, 12 downstream BI consumers) --
                            root cause is on the very node the "dashboard looks wrong"
                            report names, no upstream walk needed.
  - ambiguous-multi-parent: promotions, one of order_details' 11 upstream parents --
                            the agent must walk up from order_details and reason about
                            which of the 11 parents is the actual cause.
  - low-severity:           order_details_replica (terminal leaf, 0 downstream) --
                            same-shape change, but blast radius is empty, so the
                            write-back choice should be tag-only, not tag+note.
"""

import subprocess
import time
from datetime import datetime, timezone

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    AuditStampClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StringTypeClass,
)

GMS_SERVER = "http://localhost:8080"
ACTOR = "urn:li:corpuser:b2fd91.bryan@example.com"

OVERLAYS = [
    {
        "name": "clean-one-hop",
        "urn": "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD)",
        "hours_ago": 6,
        "field_path": "order_status_detail",
        "description": (
            "Extended order status detail, added in a recent schema migration to "
            "support the new 'Backordered' sub-status. Legacy dashboards filtering on "
            "the original order_status codes (1/2/3) do not yet account for it, causing "
            "order counts to undercount backordered orders."
        ),
    },
    {
        "name": "ambiguous-multi-parent",
        "urn": "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.promotions,PROD)",
        "hours_ago": 5,
        "field_path": "promotion_type",
        "description": (
            "New promotion classification (e.g. 'seasonal', 'clearance', 'loyalty'), "
            "added recently. Promotion attribution reports built before this migration "
            "don't join on it, causing certain promotion types to be undercounted."
        ),
    },
    {
        "name": "low-severity",
        "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.order_entry_db.analytics.order_details_replica,PROD)",
        "hours_ago": 2,
        "field_path": "_replica_sync_batch_id",
        "description": (
            "Internal replication batch identifier added by the replica sync job. This "
            "table has no downstream consumers, so impact is contained to it alone."
        ),
    },
]


def load_datapack() -> None:
    """Loads the showcase-ecommerce datapack into the running quickstart (idempotent)."""
    subprocess.run(
        ["datahub", "docker", "ingest-sample-data", "--pack", "showcase-ecommerce"],
        check=True,
    )


def _aspect_row_count() -> int:
    result = subprocess.run(
        [
            "docker", "exec", "datahub-mysql-1",
            "mysql", "-u", "root", "-pdatahub", "-D", "datahub", "-N",
            "-e", "SELECT COUNT(*) FROM metadata_aspect_v2;",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def wait_for_datapack_to_settle(
    poll_seconds: int = 10, stable_checks: int = 3, timeout_seconds: int = 600
) -> None:
    """`ingest-sample-data` submits ~3800 MCPs in ASYNC_BATCH mode and its CLI process
    returns long before GMS finishes writing them (confirmed: the row count in
    metadata_aspect_v2 kept climbing for minutes after the subprocess exited). Applying
    an overlay while that queue is still draining lets a stale, fieldless rewrite of the
    same entity land afterwards and silently revert it. So: block until the primary
    aspect-store row count stops changing before touching any overlay.
    """
    last_count = -1
    stable = 0
    waited = 0
    while stable < stable_checks:
        time.sleep(poll_seconds)
        waited += poll_seconds
        count = _aspect_row_count()
        stable = stable + 1 if count == last_count else 0
        print(f"metadata_aspect_v2 rows: {count} (stable checks: {stable}/{stable_checks})")
        last_count = count
        if waited >= timeout_seconds:
            raise RuntimeError("timed out waiting for datapack ingestion to settle")


def _has_field(graph: DataHubGraph, urn: str, field_path: str) -> bool:
    current = graph.get_aspect(urn, aspect_type=SchemaMetadataClass)
    return current is not None and any(f.fieldPath == field_path for f in current.fields)


def _emit_overlay(graph: DataHubGraph, overlay: dict) -> None:
    urn = overlay["urn"]
    current = graph.get_aspect(urn, aspect_type=SchemaMetadataClass)
    if current is None:
        raise RuntimeError(f"no schemaMetadata found for {urn} -- run the datapack load first")

    recent_ms = int(
        (datetime.now(timezone.utc).timestamp() - overlay["hours_ago"] * 3600) * 1000
    )
    audit_stamp = AuditStampClass(time=recent_ms, actor=ACTOR)

    current.fields.append(
        SchemaFieldClass(
            fieldPath=overlay["field_path"],
            type=SchemaFieldDataTypeClass(type=StringTypeClass()),
            nativeDataType="VARCHAR",
            nullable=True,
            description=overlay["description"],
            lastModified=audit_stamp,
        )
    )
    current.lastModified = audit_stamp

    graph.emit(MetadataChangeProposalWrapper(entityUrn=urn, aspect=current))


def apply_overlay(graph: DataHubGraph, overlay: dict) -> None:
    """Adds the overlay field, then confirms it survives.

    `ingest-sample-data` submits its ~3800 MCPs in ASYNC_BATCH mode and returns before
    GMS has finished draining that queue. If this overlay is applied while the datapack's
    own (fieldless-change) schemaMetadata for the same entity is still in flight behind it,
    that stale write can land afterwards and silently revert the field we just added. So:
    verify the field is still present a few seconds later, and re-apply if it was clobbered.
    """
    urn = overlay["urn"]
    field_path = overlay["field_path"]

    if _has_field(graph, urn, field_path):
        print(f"[{overlay['name']}] '{field_path}' already present on {urn}, skipping")
        return

    _emit_overlay(graph, overlay)
    for attempt in range(6):
        time.sleep(5)
        if _has_field(graph, urn, field_path):
            print(
                f"[{overlay['name']}] added '{field_path}' to {urn}, "
                f"timestamped {overlay['hours_ago']}h ago (confirmed after {attempt + 1} check(s))"
            )
            return
        print(f"[{overlay['name']}] overlay was clobbered by in-flight ingestion, re-applying")
        _emit_overlay(graph, overlay)

    raise RuntimeError(f"[{overlay['name']}] could not stabilize overlay on {urn}")


def main() -> None:
    load_datapack()
    wait_for_datapack_to_settle()
    graph = DataHubGraph(DatahubClientConfig(server=GMS_SERVER))
    for overlay in OVERLAYS:
        apply_overlay(graph, overlay)

    # Belt-and-suspenders: re-verify everything once more after a final delay, in case
    # any stragglers from the datapack's async queue landed after the settle-wait above.
    time.sleep(20)
    for overlay in OVERLAYS:
        if not _has_field(graph, overlay["urn"], overlay["field_path"]):
            print(f"[{overlay['name']}] lost after final settle, re-applying")
            apply_overlay(graph, overlay)


if __name__ == "__main__":
    main()
