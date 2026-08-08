#!/bin/sh
# Keeps the public demo's search path alive without a human watching it.
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
# Two independent, idempotent things happen per tick:
#
#   1. Re-assert `restart=unless-stopped` on the containers, so Docker itself
#      revives OpenSearch immediately instead of waiting for the next tick.
#      Re-applied every run on purpose: `datahub docker quickstart` recreates
#      containers from the compose file and resets the policy back to `no`, so a
#      reseed before recording would otherwise silently undo this.
#
#   2. Probe search end-to-end and repair it. Step 1 alone is not enough --
#      GMS caches index connections and keeps failing after OpenSearch comes
#      back underneath it, so it has to be restarted *afterwards*. That ordering
#      is the whole reason this script exists rather than just a restart policy.
#
# Install:  crontab -e  ->  */5 * * * * /home/azureuser/datahub-watchdog.sh
# Log:      /home/azureuser/watchdog.log

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

OPENSEARCH=datahub-opensearch-1
GMS=datahub-datahub-gms-quickstart-1
CONTAINERS="$OPENSEARCH $GMS datahub-frontend-quickstart-1 datahub-kafka-broker-1 datahub-mysql-1 datahub-datahub-actions-quickstart-1"
LOG=/home/azureuser/watchdog.log
LOCK=/tmp/datahub-watchdog.lock

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

# The probe deliberately goes through GMS rather than straight to OpenSearch:
# it exercises the same path the agent's `search` tool uses, so it catches the
# stale-connection failure mode that a green cluster health would hide.
probe() {
    curl -s -m 25 -X POST 'http://localhost:8080/entities?action=search' \
        -H 'Content-Type: application/json' \
        -H 'X-RestLi-Protocol-Version: 2.0.0' \
        -d '{"input":"order","entity":"dataset","start":0,"count":1}' 2>/dev/null |
        grep -o '"numEntities":[0-9]*' | head -1 | cut -d: -f2
}

found=$(probe)
if [ -n "$found" ] && [ "$found" -gt 0 ] 2>/dev/null; then
    exit 0
fi

log "PROBE FAILED (numEntities='$found') -- repairing"

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

log "  restarting $GMS (it holds stale index connections across an OpenSearch restart)"
docker restart "$GMS" >/dev/null 2>&1 || log "  WARN: docker restart failed"

i=0
while [ $i -lt 36 ]; do
    found=$(probe)
    if [ -n "$found" ] && [ "$found" -gt 0 ] 2>/dev/null; then
        log "  RECOVERED -- search returning $found entities"
        exit 0
    fi
    i=$((i + 1))
    sleep 10
done

log "  STILL BROKEN after repair (numEntities='$found') -- needs a human"
exit 1
