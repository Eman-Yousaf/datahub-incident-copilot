"""Guard the two shipped artifacts that fail *silently* when they're wrong.

Run: `python tests/test_shipped_scripts.py`. Exits non-zero on failure.

`test_web_bundle.py` covers the browser bundle for this reason already: a JS syntax
error blanks every view at once while the server still answers 200. The operations
watchdog has the same property and worse consequences. It runs from cron with its
output going to a log nobody reads, so a shell syntax error, a renamed container or
a probe pointed at the wrong endpoint doesn't announce itself -- it just means the
demo quietly stops being kept alive, and the first person to notice is a judge
looking at a stack that has been dead for a day.

The probe assertions below are not style checks. They pin a real bug: the watchdog
originally decided "is search alive?" by POSTing to GMS's RestLi
`/entities?action=search`, but the outage it exists to catch presents as the GraphQL
`searchAcrossEntities` resolver failing with `all shards failed` **while that RestLi
endpoint keeps answering normally** -- observed directly during development. The
agent's `search` tool goes through GraphQL. So the watchdog was probing the one query
that survived the failure it was written to detect, and would have reported a broken
stack as healthy.
"""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHDOG = ROOT / "ops" / "datahub-watchdog.sh"

results: list[bool] = []


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    results.append(bool(condition))


def main() -> int:
    check("ops/datahub-watchdog.sh exists", WATCHDOG.is_file())
    if not WATCHDOG.is_file():
        print(f"\n{sum(results)}/{len(results)} passed")
        return 1

    source = WATCHDOG.read_text(encoding="utf-8")
    # Assert about what the script *does*, not what it says. The header documents
    # the RestLi probe as the bug it used to have, and a naive substring check
    # over the whole file fails on that explanation -- which would make the only
    # way to pass be deleting the reason the fix exists.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )

    shell = shutil.which("sh") or shutil.which("dash") or shutil.which("bash")
    if shell:
        proc = subprocess.run([shell, "-n", str(WATCHDOG)], capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stderr.strip())
        check("watchdog parses as POSIX sh (sh -n)", proc.returncode == 0)
    else:
        print("SKIP  no shell available -- cannot parse-check the watchdog")

    # --- it probes what actually breaks ------------------------------------
    check(
        "probes the GraphQL resolver the agent's search tool uses",
        "searchAcrossEntities" in code and "/api/graphql" in code,
    )
    check(
        "does NOT rely on the RestLi endpoint that survives the outage",
        "action=search" not in code,
    )
    check(
        "probes the public URL end to end, not just localhost",
        "/api/status" in code and "PUBLIC_URL" in code,
    )
    check(
        "asserts on a real catalog count rather than HTTP 200",
        '"datasets":[0-9]*' in code,
    )

    # --- the repair ordering that is the whole reason it exists ------------
    opensearch_start = code.find("docker start")
    gms_restart = code.find("docker restart")
    check(
        "restarts GMS after OpenSearch (stale index connections)",
        opensearch_start != -1 and gms_restart != -1 and opensearch_start < gms_restart,
    )
    check(
        "re-asserts the restart policy quickstart resets",
        "--restart unless-stopped" in code,
    )
    check(
        "re-applies number_of_replicas=0 for indices created after seeding",
        "number_of_replicas" in code,
    )

    # --- it must not fight a deliberate reseed ------------------------------
    check("backs off while seed_data.py is running", "seed_dat" in code)
    check("honours a manual pause file", "watchdog.pause" in code)
    check("serializes overlapping cron ticks", "flock" in code)

    # --- and it must escalate rather than looping quietly -------------------
    check("exits non-zero when it cannot repair", "exit 1" in code)
    check("says so in the log when a human is needed", "needs a human" in code)

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
