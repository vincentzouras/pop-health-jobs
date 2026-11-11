#!/usr/bin/env bash
# Deploy watcher/job_watch.py from the GitHub repo onto this Pi.
#
# The Pi runs a copy at ~/.hermes/scripts/job_watch.py, which is what the Hermes cron
# invokes. Copying by hand is how the two drift, so this pulls main, runs the self test
# on the candidate file and only then installs it. Any failure leaves the running copy
# untouched.
#
#   deploy-job-watch.sh            pull, verify, install (silent when nothing changed)
#   deploy-job-watch.sh --force    install even when the hash is unchanged
#
# Exit codes: 0 ok/no-op, 1 git problem, 2 selftest failed or install mismatch.
set -euo pipefail

REPO="${JOB_WATCH_REPO:-$HOME/.hermes/pop-health-jobs}"
SRC="$REPO/watcher/job_watch.py"
DEST_DIR="$HOME/.hermes/scripts"
DEST="$DEST_DIR/job_watch.py"
PY="${JOB_WATCH_PY:-$HOME/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3}"
FORCE="${1:-}"

say() { printf '%s\n' "$*"; }

[ -x "$PY" ] || { say "FATAL: python not found at $PY"; exit 1; }
[ -d "$REPO/.git" ] || { say "FATAL: no git checkout at $REPO"; exit 1; }

# 1. Update the checkout. --ff-only so a diverged clone fails loudly instead of merging.
if ! git -C "$REPO" fetch --quiet origin main; then
  say "FAILED: git fetch (network or remote problem)"
  exit 1
fi
LOCAL_HEAD=$(git -C "$REPO" rev-parse --short HEAD)
git -C "$REPO" merge --ff-only --quiet origin/main || {
  say "FAILED: $REPO has diverged from origin/main (local $LOCAL_HEAD) - needs a human"
  exit 1
}
NEW_HEAD=$(git -C "$REPO" rev-parse --short HEAD)

# Keep the status helper in step with the repo too (it is small, stateless, and has no state).
if [ -f "$REPO/deploy/status-job-watch.sh" ]; then
  mkdir -p "$HOME/.hermes/bin"
  cp "$REPO/deploy/status-job-watch.sh" "$HOME/.hermes/bin/status-job-watch.sh"
  chmod 775 "$HOME/.hermes/bin/status-job-watch.sh"
fi
# Retired helpers: the scan gate and the employer hunt are gone, so a stale copy on the Pi would
# be a script that talks to an API that no longer exists.
rm -f "$DEST_DIR/gate-job-watch.sh" "$DEST_DIR/hunt-employers.sh"

NEW_MD5=$(md5sum "$SRC" | cut -d' ' -f1)
CUR_MD5=$([ -f "$DEST" ] && md5sum "$DEST" | cut -d' ' -f1 || echo none)

if [ "$NEW_MD5" = "$CUR_MD5" ] && [ "$FORCE" != "--force" ]; then
  # Silent: this runs from cron, and empty stdout means "nothing to report".
  exit 0
fi

# 2. Prove the candidate works before it is installed.
if ! "$PY" "$SRC" --selftest >/tmp/job-watch-selftest.log 2>&1; then
  say "FAILED: selftest failed for $NEW_HEAD - NOT installed (running copy unchanged)"
  tail -5 /tmp/job-watch-selftest.log
  exit 2
fi
SELFTEST=$(tail -1 /tmp/job-watch-selftest.log)

# 3. Install atomically, keeping the last five rollback copies.
mkdir -p "$DEST_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
if [ -f "$DEST" ]; then
  cp "$DEST" "$DEST_DIR/job_watch.py.bak-$STAMP"
  ls -1t "$DEST_DIR"/job_watch.py.bak-* 2>/dev/null | tail -n +6 | xargs -r rm --
fi
cp "$SRC" "$DEST.new"
chmod 775 "$DEST.new"
mv "$DEST.new" "$DEST"

# 4. Read it back: what is installed must hash to what we tested.
INST_MD5=$(md5sum "$DEST" | cut -d' ' -f1)
if [ "$INST_MD5" != "$NEW_MD5" ]; then
  say "FAILED: installed hash $INST_MD5 != tested $NEW_MD5"
  exit 2
fi

say "deployed $NEW_HEAD -> ~/.hermes/scripts/job_watch.py (${NEW_MD5:0:12})"
say "$SELFTEST"
[ "$CUR_MD5" = "none" ] || say "previous ${CUR_MD5:0:12}, rollback job_watch.py.bak-$STAMP"
