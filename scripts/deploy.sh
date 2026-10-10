#!/usr/bin/env bash
# Deploy main to production: test gate, quiet-moment switch, first-cycles
# check, automatic rollback.
#
#   scripts/deploy.sh            # deploy the checked-out main
#   scripts/deploy.sh --dry-run  # build + test the candidate only (any branch)
#
# 1. Preflight: a clean checkout of main that matches origin/main.
# 2. Build a candidate image (rtjobs-scraper:candidate-<sha>); production keeps
#    running rtjobs-scraper:latest untouched until the switch.
# 3. Gate, inside the candidate (the exact environment production gets, with
#    networking off): the full offline suite, the scrapling contract check,
#    and Chrome's version against the pinned base.
# 4. Wait until no cycle is running and the next tick is at least 20s away
#    (a switch mid-cycle interrupts it), then retag latest and recreate.
# 5. Watch the next VERIFY_CYCLES cycles: roll back to the previous image if
#    any board fails in all of them, a run hits the scrapling contract or a
#    config error, a worker is not running, or the cycles never finish.
#    A board that was already stuck on a login before the switch (waiting for
#    your Indeed code / checkpoint solve) is reported as unverified instead:
#    its failures say nothing about the new image.
set -euo pipefail
cd "$(dirname "$0")/.."

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
VERIFY_CYCLES=2
log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*"; }
die() { log "ABORT: $*"; exit 1; }

exec 9>/tmp/rtjobs-deploy.lock
flock -n 9 || die "another deploy is running"

# --- 1. Preflight -----------------------------------------------------------
branch=$(git rev-parse --abbrev-ref HEAD)
[ -z "$(git status --porcelain)" ] || die "uncommitted changes in $(pwd)"
if [ "$DRY" = 0 ]; then
    [ "$branch" = main ] || die "the checkout is on '$branch'; production deploys main only"
    git fetch -q origin main
    [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "main differs from origin/main: push or pull first"
fi
sha=$(git rev-parse --short HEAD)
stamp=$(date +%Y%m%d-%H%M)
candidate="rtjobs-scraper:candidate-$sha"
base=$(sed -n 's/^ARG CHROME_BASE=//p' Dockerfile)
[ -n "$base" ] || die "Dockerfile has no ARG CHROME_BASE"
docker image inspect "$base" >/dev/null 2>&1 \
    || die "Chrome base image $base is missing: run scripts/build_chrome_base.sh"

# --- 2. Build ---------------------------------------------------------------
log "Building $candidate from $branch@$sha"
docker build -q -t "$candidate" . >/dev/null

# --- 3. Gate ----------------------------------------------------------------
log "Gate: offline suite inside the candidate (network off)"
gate_log=$(mktemp /tmp/rtjobs-gate-XXXXXX.log)
if ! docker run --rm --init --network none --entrypoint python "$candidate" scripts/verify_offline.py >"$gate_log" 2>&1; then
    tail -n 25 "$gate_log"
    die "offline suite failed (full log: $gate_log); production untouched"
fi
grep -q "All offline checks passed." "$gate_log" || die "offline suite did not report success ($gate_log)"
log "Gate: $(grep -c '^PASS' "$gate_log") checks passed"
docker run --rm --init --network none -e PYTHON_DOTENV_DISABLED=1 -e TELEGRAM_TOKEN=gate -e TELEGRAM_CHAT_ID=1 \
    -e TELEGRAM_TEST_ID=2 -e DATA_DIR=/tmp --entrypoint python "$candidate" \
    -c "from core.browser import verify_scrapling_contract as check; check()" \
    || die "scrapling contract check failed in the candidate; production untouched"
chrome=$(docker run --rm --entrypoint google-chrome-stable "$candidate" --version | awk '{print $3}')
[ "rtjobs-chrome-base:$chrome" = "$base" ] || die "candidate has Chrome $chrome, Dockerfile pins $base"
log "Gate: scrapling contract ok, Chrome $chrome"

if [ "$DRY" = 1 ]; then
    log "Dry run: $candidate passed the gate; production untouched."
    exit 0
fi

# --- 4. Switch at a quiet moment --------------------------------------------
interval=$(( $(sed -n 's/^SCRAPE_INTERVAL_MINUTES=//p' .env 2>/dev/null | tail -n 1 || true) + 0 ))
[ "$interval" -gt 0 ] 2>/dev/null || interval=3
interval=$(( interval * 60 ))
log "Waiting for a quiet moment (no cycle running, next tick >= 20s away)"
waited=0
while :; do
    if docker ps --format '{{.Names}}' | grep -qx rtjobs; then
        waited=$((waited + 2))
        # A board waiting for a person keeps the container up for its whole
        # wait (up to 10 min); switching now would end that wait.
        [ "$waited" = 60 ] && log "Still running after 60s: probably a board waiting for a login code or checkpoint; waiting for it to finish."
        sleep 2; continue
    fi
    left=$(( interval - $(date +%s) % interval ))
    if [ "$left" -ge 20 ]; then break; fi
    sleep $(( left + 3 ))
done
previous=$(docker image inspect rtjobs-scraper:latest --format '{{.Id}}')
docker tag "$previous" "rtjobs-scraper:rollback-$stamp"
switched=$(date -u '+%Y-%m-%d %H:%M:%S')
docker tag "$candidate" rtjobs-scraper:latest
log "Switching to $candidate (previous image kept as rtjobs-scraper:rollback-$stamp)"
docker compose --profile enrichment up -d >/dev/null 2>&1

rollback() {
    log "ROLLING BACK: $1"
    docker tag "rtjobs-scraper:rollback-$stamp" rtjobs-scraper:latest
    docker compose --profile enrichment up -d >/dev/null 2>&1
    log "Rolled back to rtjobs-scraper:rollback-$stamp; $candidate kept for inspection."
    exit 1
}

# --- 5. Watch the first cycles ----------------------------------------------
deadline=$(( $(date +%s) + VERIFY_CYCLES * interval + 420 ))
log "Watching the next $VERIFY_CYCLES cycles"
while :; do
    for worker in enrichment delivery monitor; do
        docker compose ps --status running --services 2>/dev/null | grep -qx "$worker" \
            || rollback "the $worker worker is not running"
    done
    verdict=$(docker compose exec -T enrichment python - "$switched" "$VERIFY_CYCLES" <<'PY' 2>&1 || true
import sqlite3, sys
since, needed = sys.argv[1], int(sys.argv[2])
c = sqlite3.connect(f"file:{sys.argv[3] if len(sys.argv) > 3 else '/data/rtjobs.db'}?mode=ro", uri=True)
cycles = c.execute("SELECT id FROM scrape_batches WHERE started_at >= ? AND finished_at IS NOT NULL"
                   " ORDER BY id LIMIT ?", (since, needed)).fetchall()
LOGIN = {"login_failed", "session_expired", "signed_in", "running"}  # running = still waiting for you
runs = {}
for (batch,) in cycles:
    for source, status, error in c.execute("SELECT source, status, COALESCE(error, '') FROM runs WHERE batch_id=?", (batch,)):
        if "scrapling contract changed" in error or status == "config_error":
            print(f"FAIL {source} {status}: {error[:200]}"); sys.exit()
        runs.setdefault(source, []).append(status)
if len(cycles) < needed:
    print(f"WAIT {len(cycles)}/{needed} cycles finished"); sys.exit()
def stuck_before(source):
    row = c.execute("SELECT status FROM runs WHERE source=? AND started_at < ? AND status != 'running'"
                    " ORDER BY id DESC LIMIT 1", (source, since)).fetchone()
    return bool(row) and row[0] in LOGIN
broken, unverified = [], []
for source, statuses in runs.items():
    if "ok" in statuses or "signed_in" in statuses:
        continue
    (unverified if set(statuses) <= LOGIN and stuck_before(source) else broken).append(
        f"{source} {'/'.join(statuses)}")
note = f" (unverified, login already pending before the switch: {', '.join(unverified)})" if unverified else ""
print(("FAIL boards never ok: " + ", ".join(broken)) if broken else
      "PASS " + "; ".join(f"{source} {'/'.join(statuses)}" for source, statuses in sorted(runs.items())) + note)
PY
)
    case "$verdict" in
        PASS*) log "Verified: ${verdict#PASS }"; break ;;
        FAIL*) rollback "${verdict#FAIL }" ;;
    esac
    [ "$(date +%s)" -lt "$deadline" ] || rollback "cycles did not finish in time (${verdict:-no answer})"
    sleep 10
done
docker tag "$candidate" "rtjobs-scraper:production-$sha-$stamp"
log "Deployed $sha as rtjobs-scraper:production-$sha-$stamp. Rollback target: rtjobs-scraper:rollback-$stamp."
