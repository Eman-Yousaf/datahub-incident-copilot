#!/bin/sh
# Keeps the public demo alive without a human watching it.
#
# DataHub's quickstart compose file ships `restart: no` on every container
# except mysql, and OpenSearch on this VM dies roughly every ~26 hours --
# `OutOfMemoryError: unable to create native thread ... process/resource limits
# reached`, a thread/pid ceiling rather than RAM or disk. With no restart policy
# it exits and stays exited. It has already sat dead for two days once.
#
# The failure is invisible from outside: Caddy and uvicorn are untouched, so the
# demo URL keeps answering HTTP 200 while every investigation it runs comes back
# with zero evidence. Curling the site proves the web server is up, not that the
# agent can see anything.
#
# Judging is online and we don't know when anyone will look, so "check docker ps
# beforehand" is not a control. This runs from cron.
#
# ---------------------------------------------------------------------------
# Probe the path the judge actually takes, not a path that resembles it
# ---------------------------------------------------------------------------
#
# This script used to decide "is search alive?" by POSTing to GMS's RestLi
# endpoint, `/entities?action=search`. That was the wrong probe, and wrong in
# the specific way that mattered: the outage this file exists to catch presents
# as the GraphQL `searchAcrossEntities` resolver failing with
# `search_phase_execution_exception: all shards failed` **while the RestLi
# endpoint keeps answering normally** -- that divergence was observed directly
# during development, and it is why a GMS restart (not just an OpenSearch
# restart) is part of the repair below. The agent's `search` tool goes through
# GraphQL. So the old probe was the one query that survived the failure it was
# written to detect, and a broken stack would have been reported healthy.
#
# So the primary probe is now the demo's own `/api/status`, fetched over the
# public URL. One request covers every layer between a judge and the graph:
#
#     DNS -> TLS/Caddy -> uvicorn -> FastAPI -> GraphQL -> GMS -> OpenSearch
#
# and it asserts on real numbers (`datasets > 0`), not on HTTP 200, because 200
# is exactly what the invisible failure returns.
#
# Everything below the primary probe exists only to localise a failure, so the
# repair matches the layer that actually broke:
#
#   public /api/status ok ............................ nothing to do
#   local  /api/status ok, public not ................ edge (Caddy/TLS/DNS)
#   GraphQL ok, app not .............................. web tier (uvicorn)
#   GraphQL not ok ................................... DataHub (the old path)
#
# Two things are also re-asserted every tick, both idempotent:
#
#   1. `restart=unless-stopped` on the containers, so Docker itself revives
#      OpenSearch immediately instead of waiting for the next tick. Re-applied
#      every run on purpose: `datahub docker quickstart` recreates containers
#      from the compose file and resets the policy to `no`, so a reseed before
#      recording would otherwise silently undo this.
#
#   2. `number_of_replicas: 0` after any OpenSearch repair -- a single-node
#      cluster can never assign replicas, and indices created after seed time
#      come back with the default of 1.
#
# What this CANNOT fix, so don't mistake a quiet log for safety: the Azure VM
# being deallocated or out of credit, the disk filling up, or a Let's Encrypt
# renewal failure. Those exit 1 with STILL BROKEN and need a person.
#
# Install:  crontab -e  ->  */5 * * * * /home/azureuser/datahub-watchdog.sh
#           and, for the web-tier restart to work from cron, one sudoers line:
#           azureuser ALL=(root) NOPASSWD: /bin/systemctl restart incident-copilot-web.service, /bin/systemctl restart caddy
# Log:      /home/azureuser/watchdog.log

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

OPENSEARCH=datahub-opensearch-1
GMS=datahub-datahub-gms-quickstart-1
CONTAINERS="$OPENSEARCH $GMS datahub-frontend-quickstart-1 datahub-kafka-broker-1 datahub-mysql-1 datahub-datahub-actions-quickstart-1"

PUBLIC_URL=${PUBLIC_URL:-https://incident-copilot-demo.centralindia.cloudapp.azure.com}
WEB_PORT=${WEB_PORT:-8000}
WEB_SERVICE=${WEB_SERVICE:-incident-copilot-web.service}

LOG=/home/azureuser/watchdog.log
LOCK=/tmp/datahub-watchdog.lock

# Match how the app authenticates to GMS (`datahub_api._headers`): a bearer
# token when one is configured, nothing otherwise. cron starts with an empty
# environment, so read it out of the app's own .env rather than assuming the
# quickstart has metadata-service auth disabled -- a probe that 401s would be
# indistinguishable from an outage and would restart a healthy stack every five
# minutes.
ENV_FILE=${ENV_FILE:-/home/azureuser/incident-copilot/.env}
GMS_TOKEN=${DATAHUB_GMS_TOKEN:-}
if [ -z "$GMS_TOKEN" ] && [ -f "$ENV_FILE" ]; then
    GMS_TOKEN=$(sed -n 's/^DATAHUB_GMS_TOKEN=//p' "$ENV_FILE" 2>/dev/null | head -1 | tr -d "\"' ")
fi

log() {
    echo "$(date -Is) $*" >>"$LOG"
}

# Serialize: a repair takes minutes and cron fires every 5, so overlapping runs
# would otherwise restart GMS out from under each other.
if [ -z "$WATCHDOG_LOCKED" ]; then
    WATCHDOG_LOCKED=1
    export WATCHDOG_LOCKED
    exec flock -n "$LOCK" "$0" "$@"
fi

# Reseeding looks exactly like an outage from here: right after
# `datahub docker quickstart` the containers are up and old enough to pass the
# warm-up guard below, but search legitimately returns 0 entities until
# seed_data.py finishes loading the datapack. Restarting GMS in the middle of
# that would fight the reseed, so back off while one is underway. `touch
# /home/azureuser/watchdog.pause` covers any other maintenance; delete it after.
if [ -f /home/azureuser/watchdog.pause ]; then
    exit 0
fi
if pgrep -f 'seed_dat[a]\.py' >/dev/null 2>&1; then
    log "skip: seed_data.py is running"
    exit 0
fi

# A missing container means someone is mid-`datahub docker nuke` / quickstart.
# Repairing during a deliberate reseed would fight the person doing it.
for c in $CONTAINERS; do
    if ! docker inspect "$c" >/dev/null 2>&1; then
        log "skip: $c does not exist (reseed in progress?)"
        exit 0
    fi
done

for c in $CONTAINERS; do
    policy=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c" 2>/dev/null)
    if [ "$policy" != "unless-stopped" ]; then
        if docker update --restart unless-stopped "$c" >/dev/null 2>&1; then
            log "set restart=unless-stopped on $c (was '$policy')"
        else
            log "WARN: could not set restart policy on $c"
        fi
    fi
done

# Give a freshly-started stack time to finish coming up before judging it broken
# -- GMS takes minutes to become useful after a reseed, and a probe failure
# there is expected, not an outage.
started=$(docker inspect -f '{{.State.StartedAt}}' "$OPENSEARCH" 2>/dev/null)
started_epoch=$(date -d "$started" +%s 2>/dev/null || echo 0)
now_epoch=$(date +%s)
if [ "$started_epoch" -gt 0 ] && [ $((now_epoch - started_epoch)) -lt 600 ]; then
    exit 0
fi

# --------------------------------------------------------------------------- #
# Probes. Each prints a dataset count on success and nothing on failure, so the
# caller can treat "no number" and "zero" identically -- a catalog with zero
# datasets is as useless to a judge as one that refused to answer.
# --------------------------------------------------------------------------- #

# The whole stack, exactly as a reviewer meets it.
app_probe() {
    curl -s -m 25 "$1/api/status" 2>/dev/null |
        grep -o '"datasets":[0-9]*' | head -1 | cut -d: -f2
}

# The GraphQL resolver the agent's own `search` tool goes through. This is the
# one that fails during the OpenSearch outage; the RestLi endpoint does not.
gql_probe() {
    if [ -n "$GMS_TOKEN" ]; then
        set -- -H "Authorization: Bearer $GMS_TOKEN"
    else
        set --
    fi
    curl -s -m 25 -X POST 'http://localhost:8080/api/graphql' \
        -H 'Content-Type: application/json' "$@" \
        -d '{"query":"{ searchAcrossEntities(input: {types: [DATASET], query: \"*\", count: 0}) { total } }"}' \
        2>/dev/null | grep -o '"total":[0-9]*' | head -1 | cut -d: -f2
}

alive() {
    [ -n "$1" ] && [ "$1" -gt 0 ] 2>/dev/null
}

restart_unit() {
    if sudo -n systemctl restart "$1" >/dev/null 2>&1; then
        log "  restarted $1"
        return 0
    fi
    log "  WARN: could not restart $1 (needs a NOPASSWD sudoers line -- see header)"
    return 1
}

found=$(app_probe "$PUBLIC_URL")
if alive "$found"; then
    exit 0
fi

# One retry before acting: a single DNS or TLS blip is not an outage, and the
# repairs below are more disruptive than a five-minute wait would have been.
sleep 5
found=$(app_probe "$PUBLIC_URL")
if alive "$found"; then
    exit 0
fi

log "PUBLIC PROBE FAILED (datasets='$found') -- localising"

# --------------------------------------------------------------------------- #
# Layer 1: is only the public edge broken?
# --------------------------------------------------------------------------- #
local_found=$(app_probe "http://localhost:$WEB_PORT")
if alive "$local_found"; then
    log "  app is healthy on localhost:$WEB_PORT -- the edge (Caddy/TLS/DNS) is the fault"
    restart_unit caddy
    sleep 15
    found=$(app_probe "$PUBLIC_URL")
    if alive "$found"; then
        log "  RECOVERED via caddy restart -- $found datasets"
        exit 0
    fi
    log "  STILL BROKEN at the edge -- certificate renewal, DNS, or the Azure NSG. Needs a human."
    exit 1
fi

# --------------------------------------------------------------------------- #
# Layer 2: is DataHub fine and only the web tier down?
# --------------------------------------------------------------------------- #
gql_found=$(gql_probe)
if alive "$gql_found"; then
    log "  DataHub is healthy ($gql_found datasets over GraphQL) -- the web tier is the fault"
    restart_unit "$WEB_SERVICE"
    sleep 15
    found=$(app_probe "$PUBLIC_URL")
    if alive "$found"; then
        log "  RECOVERED via $WEB_SERVICE restart -- $found datasets"
        exit 0
    fi
    log "  STILL BROKEN after restarting $WEB_SERVICE. Needs a human."
    exit 1
fi

# --------------------------------------------------------------------------- #
# Layer 3: DataHub itself. The original repair, unchanged in substance.
# --------------------------------------------------------------------------- #
log "  GraphQL search is down (total='$gql_found') -- repairing DataHub"

if [ "$(docker inspect -f '{{.State.Running}}' "$OPENSEARCH" 2>/dev/null)" != "true" ]; then
    log "  $OPENSEARCH is down, starting it"
    docker start "$OPENSEARCH" >/dev/null 2>&1 || log "  WARN: docker start failed"
fi

i=0
while [ $i -lt 24 ]; do
    if curl -s -m 5 -o /dev/null http://localhost:9200/_cluster/health 2>/dev/null; then
        break
    fi
    i=$((i + 1))
    sleep 5
done

# A single-node cluster can never assign replicas, and searches against
# under-replicated shards fail intermittently. seed_data.py sets this at seed
# time, but indices created later come back with the default of 1.
curl -s -m 10 -X PUT 'http://localhost:9200/_all/_settings' \
    -H 'Content-Type: application/json' \
    -d '{"index":{"number_of_replicas":0}}' >/dev/null 2>&1

# GMS caches index connections and keeps failing after OpenSearch comes back
# underneath it, so it has to be restarted *afterwards*. That ordering is the
# whole reason this script exists rather than just a restart policy.
log "  restarting $GMS (it holds stale index connections across an OpenSearch restart)"
docker restart "$GMS" >/dev/null 2>&1 || log "  WARN: docker restart failed"

i=0
while [ $i -lt 36 ]; do
    gql_found=$(gql_probe)
    if alive "$gql_found"; then
        log "  DataHub recovered -- GraphQL returning $gql_found datasets"
        # The web tier holds its own connections; bounce it so the demo does not
        # keep serving errors from a stack that is now healthy underneath it.
        restart_unit "$WEB_SERVICE"
        sleep 15
        found=$(app_probe "$PUBLIC_URL")
        if alive "$found"; then
            log "  RECOVERED end to end -- $found datasets over the public URL"
            exit 0
        fi
        log "  DataHub is up but the public URL is not. Needs a human."
        exit 1
    fi
    i=$((i + 1))
    sleep 10
done

log "  STILL BROKEN after repair (GraphQL total='$gql_found') -- needs a human"
exit 1
