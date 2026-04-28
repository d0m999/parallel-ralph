#!/bin/bash
# scripts_4x/recover_shards_after_limit.sh
# Resume shards after a rate-limit window:
#   - only touches stories with passes=false
#   - resets per-story retry counters
#   - restores priorities to the shard's intra-shard original order
#   - clears stale locks left by dead processes
#   - finally invokes run_shards.sh with the supplied shard list
#
# Usage:
#   ./scripts_4x/recover_shards_after_limit.sh             # default: a b c d
#   ./scripts_4x/recover_shards_after_limit.sh a b         # subset

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [ "$#" -eq 0 ]; then
  SHARDS=(a b c d)
else
  SHARDS=("$@")
fi

LAUNCH_DELAY="${LAUNCH_DELAY:-45}"
MAX_ITER="${MAX_ITER:-200}"
MAX_RETRIES="${MAX_RETRIES:-15}"

log() {
  printf '[recover_shards] %s\n' "$*"
}

pid_alive() {
  local pid="$1"
  [ -n "$pid" ] && [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

for SHARD in "${SHARDS[@]}"; do
  ROOT=".ralph-shard-${SHARD}"
  PRD="${ROOT}/prd.json"
  RETRIES_DIR="${ROOT}/.retries"
  INSTANCE="${ROOT}/.instance"
  RUN_PID="${ROOT}/run.pid"
  STORY_LOCK="${ROOT}/current_story.json"

  if [ ! -d "$ROOT" ]; then
    log "skip shard-${SHARD}: missing ${ROOT}"
    continue
  fi
  if [ ! -f "$PRD" ]; then
    log "skip shard-${SHARD}: missing ${PRD}"
    continue
  fi

  live_pid=""
  if [ -f "$INSTANCE" ]; then
    inst_pid="$(cut -d: -f1 "$INSTANCE" 2>/dev/null || true)"
    if pid_alive "${inst_pid:-}"; then
      live_pid="$inst_pid"
    fi
  fi
  if [ -z "$live_pid" ] && [ -f "$RUN_PID" ]; then
    run_pid="$(cat "$RUN_PID" 2>/dev/null || true)"
    if pid_alive "${run_pid:-}"; then
      live_pid="$run_pid"
    fi
  fi

  if [ -n "$live_pid" ]; then
    log "shard-${SHARD}: live pid=${live_pid}, skip recovery mutation"
    continue
  fi

  log "shard-${SHARD}: resetting pending story priorities and retries"
  python3 - "$PRD" "$RETRIES_DIR" <<'PY'
import json
import sys
from pathlib import Path

prd_path = Path(sys.argv[1])
retries_dir = Path(sys.argv[2])
data = json.loads(prd_path.read_text())

pending_ids = []
for idx, story in enumerate(data["userStories"], start=1):
    if not story.get("passes", False):
        story["priority"] = idx
        pending_ids.append(story["id"])

prd_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

for story_id in pending_ids:
    retry_file = retries_dir / story_id
    if retry_file.exists():
        retry_file.unlink()

print("pending:", " ".join(pending_ids) if pending_ids else "(none)")
PY

  if [ -f "$INSTANCE" ] || [ -f "$RUN_PID" ] || [ -f "$STORY_LOCK" ]; then
    log "shard-${SHARD}: clearing stale lock files"
    rm -f "$INSTANCE" "$RUN_PID" "$STORY_LOCK"
  fi
done

log "launching shards: ${SHARDS[*]}"
LAUNCH_DELAY="$LAUNCH_DELAY" MAX_ITER="$MAX_ITER" MAX_RETRIES="$MAX_RETRIES" \
  "$SCRIPT_DIR/run_shards.sh" "${SHARDS[@]}"
