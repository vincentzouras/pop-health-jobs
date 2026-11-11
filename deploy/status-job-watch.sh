#!/usr/bin/env bash
# One-shot health/consistency check for the job-watch pipeline on the Pi.
#
# Answers "is what runs here the same as what is in the repo, and is the board being fed
# by it?" Prints a PASS/FAIL line per check and exits 1 if any hard check fails.
#
#   status-job-watch.sh
set -uo pipefail

REPO="${JOB_WATCH_REPO:-$HOME/.hermes/pop-health-jobs}"
DEST="$HOME/.hermes/scripts/job_watch.py"
PY="${JOB_WATCH_PY:-$HOME/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3}"
STATE="$HOME/.hermes/job-watches/job-alerts-state.json"
HERMES="$HOME/.hermes/hermes-agent"
CRON_ID="${JOB_WATCH_CRON_ID:-8e7a83cfdc37}"

fails=0
ok()   { printf '  PASS  %-26s %s\n' "$1" "$2"; }
bad()  { printf '  FAIL  %-26s %s\n' "$1" "$2"; fails=$((fails+1)); }
warn() { printf '  WARN  %-26s %s\n' "$1" "$2"; }
info() { printf '  ----  %-26s %s\n' "$1" "$2"; }

# board credentials live in the shared env file, same as job_watch.py reads
set -a
[ -f "$HOME/.hermes/.env" ] && . "$HOME/.hermes/.env"
set +a
WEB_URL="${JOB_BOARD_URL:-https://pop-health-jobs.vercel.app}"

printf '\njob-watch status  (%s)\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# 1. installed file vs repo HEAD
if [ -f "$DEST" ] && [ -d "$REPO/.git" ]; then
  INST=$(md5sum "$DEST" | cut -d' ' -f1)
  HEAD=$(git -C "$REPO" rev-parse --short HEAD)
  git -C "$REPO" fetch --quiet origin main 2>/dev/null
  BEHIND=$(git -C "$REPO" rev-list --count HEAD..origin/main 2>/dev/null || echo "?")
  SRC_MD5=$(git -C "$REPO" show origin/main:watcher/job_watch.py 2>/dev/null | md5sum | cut -d' ' -f1)
  if [ "$INST" = "$SRC_MD5" ]; then
    ok "install vs origin/main" "identical (${INST:0:12}, $HEAD)"
  else
    bad "install vs origin/main" "installed ${INST:0:12} != origin/main ${SRC_MD5:0:12} (behind $BEHIND commit(s))"
  fi
  info "local clone" "$REPO @ $HEAD"
else
  bad "install vs origin/main" "missing $DEST or $REPO"
fi

# 2. the installed copy passes its own tests
if out=$("$PY" "$DEST" --selftest 2>&1 | tail -1); then
  case "$out" in
    *"0 title failures"*"0 location failures"*"0 boundary failures"*"0 lock failures"*)
      ok "selftest" "$out" ;;
    *) bad "selftest" "$out" ;;
  esac
else
  bad "selftest" "crashed"
fi

# 3. schedule
if [ -x "$HERMES/hermes" ]; then
  LIST=$("$HERMES/hermes" cron list 2>/dev/null || true)
  if printf '%s' "$LIST" | grep -A2 "$CRON_ID" | grep -q '\[active\]'; then
    ok "cron $CRON_ID" "active, 0 8 * * *"
  else
    bad "cron $CRON_ID" "not active"
  fi
  if printf '%s' "$LIST" | grep -q "deploy-job-watch"; then
    ok "cron auto-deploy" "scheduled"
  else
    bad "cron auto-deploy" "not scheduled (drift possible)"
  fi
  # Fail loudly if something is still scheduled that this repo no longer ships.
  if printf '%s' "$LIST" | grep -q "hunt-employers.sh"; then
    bad "cron employer hunt" "still scheduled - the hunt was removed, delete that cron"
  else
    ok "cron employer hunt" "absent (removed by design)"
  fi
else
  bad "cron" "hermes not found at $HERMES"
fi

# 4. state freshness
if [ -f "$STATE" ]; then
  AGE_H=$(( ( $(date +%s) - $(stat -c %Y "$STATE") ) / 3600 ))
  COUNTS=$("$PY" - "$STATE" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print("%d seen, %d locations, %d runs" % (len(d.get("seen", {})), len(d.get("locations", {})), d.get("runs", 0)))
PYEOF
)
  if [ "$AGE_H" -le 36 ]; then ok "state freshness" "${AGE_H}h old ($COUNTS)"; else bad "state freshness" "${AGE_H}h old - no recent run?"; fi
else
  bad "state freshness" "missing $STATE"
fi

# 4b. no leftovers from the removed employer hunt
EXTRA_DIR="$HOME/.hermes/job-watches/sources.d"
if [ -d "$EXTRA_DIR" ] && [ -n "$(ls -1 "$EXTRA_DIR"/*.json 2>/dev/null)" ]; then
  warn "sources.d leftover" "the hunt was removed; $(ls -1 "$EXTRA_DIR"/*.json 2>/dev/null | wc -l) file(s) are now ignored"
else
  info "sources.d" "absent (employers come from SOURCES in job_watch.py)"
fi

# 5. board reachable and reporting the same scanner build
if command -v curl >/dev/null; then
  BOARD=$(curl -s -m 20 "$WEB_URL/api/jobs" || true)
  if [ -z "$BOARD" ]; then
    bad "board api" "$WEB_URL/api/jobs unreachable"
  else
    PARSED=$(printf '%s' "$BOARD" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
m=d.get("meta",{})
print("%s|%s|%s|%s" % (m.get("lastIngest"), m.get("scannerVersion") or "none",
                       len(d.get("active",[])), len(d.get("archive",[]))))')
    LAST=${PARSED%%|*}; REST=${PARSED#*|}; SCAN=${REST%%|*}; REST=${REST#*|}
    ok "board api" "lastIngest=$LAST active=${REST%%|*} archive=${REST##*|}"
    if [ "$SCAN" = "none" ]; then
      info "board scanner" "not reported yet (first run after this change will set it)"
    elif [ -f "$DEST" ] && [ "${SCAN}" = "$(md5sum "$DEST" | cut -c1-12)" ]; then
      ok "board scanner" "matches installed build ($SCAN)"
    else
      info "board scanner" "board says $SCAN, installed is $(md5sum "$DEST" | cut -c1-12) - expected until the next run"
    fi
  fi
  SUPPRESSED=$(curl -s -m 20 "$WEB_URL/api/suppressed" | "$PY" -c 'import json,sys
try: print(len(json.load(sys.stdin).get("suppressed", [])))
except Exception: print("?")')
  info "suppressed feed" "$SUPPRESSED job(s) the Pi should skip"
else
  bad "board api" "curl missing"
fi

# 6. browser + credentials
# The browser exits after 2 minutes idle by design (BROWSER_IDLE_TIMEOUT_MS), so "not running"
# is the normal resting state and only service-level trouble is a failure.
SVC=$(systemctl --user is-active camofox.service 2>/dev/null || true)
CJ=$(curl -s -m 8 http://127.0.0.1:9377/health 2>/dev/null || true)
case "$SVC" in
  active) ;;
  *) bad "camofox service" "${SVC:-not running} - start it with: systemctl --user start camofox" ;;
esac
case "$CJ" in
  "")
    [ "$SVC" = "active" ] && bad "camofox" "no response on 127.0.0.1:9377 despite an active service"
    ;;
  *'"browserRunning":true'*) ok "camofox" "browser up" ;;
  *browser_rss_pressure*)
    # A page blew past the 450 MB cap. The run revives it, but it is worth knowing about.
    warn "camofox" "last browser was killed for RSS pressure; revives on the next run" ;;
  *) warn "camofox" "browser idle (starts on demand); service is active" ;;
esac
[ -n "${JOB_BOARD_TOKEN:-}" ] && ok "JOB_BOARD_TOKEN" "set" || bad "JOB_BOARD_TOKEN" "missing in ~/.hermes/.env"
[ -n "${JOB_BOARD_URL:-}" ] && info "JOB_BOARD_URL" "$JOB_BOARD_URL" || info "JOB_BOARD_URL" "default (production)"

printf '\n%s\n\n' "$([ "$fails" -eq 0 ] && echo 'all checks passed' || echo "$fails check(s) failed")"
[ "$fails" -eq 0 ]
