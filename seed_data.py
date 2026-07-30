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

import requests
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    AuditStampClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StringTypeClass,
    TagPropertiesClass,
)

GMS_SERVER = "http://localhost:8080"
OPENSEARCH_SERVER = "http://localhost:9200"
ACTOR = "urn:li:corpuser:b2fd91.bryan@example.com"

# The agent's `add_tags` mutation tool refuses tag URNs that don't already exist as
# real Tag entities in DataHub -- these back the write-back decision (tag-only vs
# tag+note vs escalated) described in the plan's milestone 3/5.
TAGS = [
    {
        "urn": "urn:li:tag:incident-flagged",
        "name": "incident-flagged",
        "description": (
            "Applied by Incident Copilot to the entity identified as the root cause "
            "of an investigated data-quality incident."
        ),
    },
    {
        "urn": "urn:li:tag:incident-severity-high",
        "name": "incident-severity-high",
        "description": (
            "Applied by Incident Copilot when an incident's blast radius reaches "
            "multiple downstream consumers (dashboards, BI reports, ML models, etc.)."
        ),
    },
]

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


def ensure_tags(graph: DataHubGraph) -> None:
    for tag in TAGS:
        if graph.exists(tag["urn"]):
            print(f"[tags] '{tag['name']}' already exists, skipping")
            continue
        graph.emit(
            MetadataChangeProposalWrapper(
                entityUrn=tag["urn"],
                aspect=TagPropertiesClass(
                    name=tag["name"], description=tag["description"]
                ),
            )
        )
        print(f"[tags] created '{tag['name']}'")


def fix_opensearch_replicas() -> None:
    """The quickstart's OpenSearch is a single data node, so the default
    number_of_replicas=1 can never actually be satisfied -- every index sits at
    `yellow` health with unassigned replica shards, which intermittently makes
    `search` fail with "all shards failed" even though the primary shards are fine.
    Setting replicas to 0 (there's nothing to replicate to anyway) fixes this at the
    root instead of just retrying past it. Idempotent; safe to run every time.

    This function fixes *existing* indices, which only covers indices present at the
    moment it runs. It's not enough on its own: any index created afterward (dated
    ISM history indices roll over daily; a long-lived deployment like the public demo
    keeps running and creating new ones) starts fresh at OpenSearch's out-of-the-box
    default of 1 replica, silently drifting the cluster back to yellow -- confirmed
    happening on the live demo VM within a couple hours of a clean seed. The index
    template below closes that gap by making 0-replicas the default for every index
    *from creation time onward*, not just the ones that exist right now.
    """
    requests.put(
        f"{OPENSEARCH_SERVER}/_index_template/no-replicas-default",
        json={
            "index_patterns": ["*"],
            # Low priority: DataHub's own index templates (if they explicitly set
            # number_of_replicas) still win on conflict. This only fills the gap
            # where nothing else has an opinion, which is the actual failure mode
            # observed -- OpenSearch's built-in default of 1 applying by default.
            "priority": 0,
            "template": {"settings": {"number_of_replicas": 0}},
        },
        timeout=30,
    ).raise_for_status()

    for index in ("_all", ".opendistro-ism-config", ".opendistro-ism-managed-index-history-*"):
        resp = requests.put(
            f"{OPENSEARCH_SERVER}/{index}/_settings",
            json={"index": {"number_of_replicas": 0}},
            timeout=30,
        )
        # The dated ISM history index (created lazily, not always present yet on a
        # fresh volume) 404s if no index matches the wildcard -- harmless, it's an
        # internal OpenDistro bookkeeping index unrelated to DataHub's own search.
        if resp.status_code != 404:
            resp.raise_for_status()
    health = requests.get(f"{OPENSEARCH_SERVER}/_cluster/health", timeout=30).json()
    print(f"[opensearch] cluster health: {health['status']}, "
          f"{health['unassigned_shards']} unassigned shards")


def main() -> None:
    load_datapack()
    wait_for_datapack_to_settle()
    fix_opensearch_replicas()
    graph = DataHubGraph(DatahubClientConfig(server=GMS_SERVER))
    ensure_tags(graph)
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
