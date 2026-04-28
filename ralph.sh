#!/bin/bash
# Ralph loop harness.
# Reads <RALPH_DIR>/prd.json, locks one story per iteration, runs claude --print,
# verifies structural diff (only the locked story may flip passes false→true).
# Usage:
#   ./ralph.sh [max_iterations] [max_retries_per_story] [agent_timeout]
#   ./ralph.sh --shard-root .ralph-shard-a [max_iterations] ...
#
# Single-process default: RALPH_DIR=$SCRIPT_DIR/.ralph (backwards compatible).
# Sharded mode: --shard-root .ralph-shard-{a,b,...}
# In shard mode the dirty-tree gate is shard-aware so concurrent sibling shards
# do not trigger a FATAL.
#
# Env:
#   RALPH_CLAUDE_MODEL   model name passed to `claude --model` (default: opus)
#   RALPH_CLAUDE_BIN     claude executable (default: claude)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- CLI flag parsing: --shard-root must come before positional args ---
SHARD_ROOT_OVERRIDE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --shard-root)
      [ -z "${2:-}" ] && { echo "FATAL: --shard-root needs a value" >&2; exit 1; }
      SHARD_ROOT_OVERRIDE="$2"
      shift 2
      ;;
    --shard-root=*)
      SHARD_ROOT_OVERRIDE="${1#*=}"
      shift
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "FATAL: unknown flag: $1" >&2
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

if [ -n "$SHARD_ROOT_OVERRIDE" ]; then
  # Resolve to absolute path (allow both .ralph-shard-a and absolute /path/to/shard)
  if [[ "$SHARD_ROOT_OVERRIDE" = /* ]]; then
    RALPH_DIR="$SHARD_ROOT_OVERRIDE"
  else
    RALPH_DIR="$SCRIPT_DIR/$SHARD_ROOT_OVERRIDE"
  fi
else
  RALPH_DIR="$SCRIPT_DIR/.ralph"
fi

# Relative shard path for git status filtering (e.g. ".ralph-shard-a" or ".ralph")
SHARD_REL="${RALPH_DIR#$SCRIPT_DIR/}"

# Export shard root for child processes (PreToolUse hook activation;
# scripts_4x/hooks/deny_outside_shard.py reads $RALPH_SHARD_ROOT to enforce write boundary).
# Default mode (.ralph) does NOT export — hook stays no-op for single-process baseline.
if [ "$SHARD_REL" != ".ralph" ]; then
  export RALPH_SHARD_ROOT="$SHARD_REL"
fi

PRD_FILE="$RALPH_DIR/prd.json"
PROGRESS_FILE="$RALPH_DIR/progress.txt"
PROMPT_FILE="$RALPH_DIR/PROMPT.md"
LOG_FILE="$RALPH_DIR/loop.log"
ARCHIVE_DIR="$RALPH_DIR/archive"
LAST_BRANCH_FILE="$RALPH_DIR/.last-branch"
STORY_LOCK_FILE="$RALPH_DIR/current_story.json"
RETRY_DIR="$RALPH_DIR/.retries"
INSTANCE_LOCK="$RALPH_DIR/.instance"

mkdir -p "$RALPH_DIR" "$ARCHIVE_DIR" "$RETRY_DIR"

# --- Helper: shard-aware dirty-tree probe ---
# In default mode (.ralph), only ignore dirty under .ralph-shard-*/ (other shards may run concurrently).
# In shard mode, ignore dirty under .ralph/ AND any .ralph-shard-*/ (all harness state is expected dirty during run).
shard_aware_dirty() {
  if [ "$SHARD_REL" = ".ralph" ]; then
    git status --porcelain | grep -vE '^.. \.ralph-shard-[a-z]+/' || true
  else
    git status --porcelain | grep -vE '^.. (\.ralph/|\.ralph-shard-[a-z]+/)' || true
  fi
}

# --- Helper: numeric guard (refuse to feed non-int into $(())) ---
assert_nonneg_int() {
  local label="$1"
  local value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "FATAL: $label is not a non-negative integer: '$value'" | tee -a "$LOG_FILE"
    exit 3
  fi
}

# --- Helper: atomic state write (temp + mv on same fs) ---
atomic_write() {
  local target="$1"
  local content="$2"
  local tmp="$target.tmp.$$"
  printf "%s" "$content" > "$tmp"
  mv "$tmp" "$target"
}

# --- Instance lock (PID + liveness probe) ---
if [ -f "$INSTANCE_LOCK" ]; then
  other_pid=$(cut -d: -f1 "$INSTANCE_LOCK" 2>/dev/null || echo "")
  if [ -n "$other_pid" ] && kill -0 "$other_pid" 2>/dev/null; then
    echo "FATAL: another ralph.sh instance is alive (pid=$other_pid)."
    echo "  If this is wrong, rm $INSTANCE_LOCK and retry."
    exit 1
  fi
  echo "Stale instance lock found (pid=$other_pid not alive). Reclaiming." | tee -a "$LOG_FILE"
fi
atomic_write "$INSTANCE_LOCK" "$$:$(date +%s)"
trap 'rm -f "$INSTANCE_LOCK" "$STORY_LOCK_FILE"' EXIT

# --- Read target branch from prd.json (no hard-code) ---
if [ ! -f "$PRD_FILE" ]; then
  echo "FATAL: $PRD_FILE not found. Run /ralph-init first."
  exit 1
fi
TARGET_BRANCH=$(python3 -c "
import json
print(json.load(open('$PRD_FILE')).get('branchName',''))
")
if [ -z "$TARGET_BRANCH" ]; then
  echo "FATAL: branchName missing from prd.json"
  exit 1
fi

# --- Hard gate: must be on target branch with clean tree (shard-aware) ---
CURRENT_GIT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_GIT_BRANCH" != "$TARGET_BRANCH" ]; then
  echo "FATAL: ralph.sh expects branch '$TARGET_BRANCH'."
  echo "  Current: $CURRENT_GIT_BRANCH"
  echo "  Run: git checkout $TARGET_BRANCH && ./ralph.sh"
  exit 1
fi
DIRTY_OUTSIDE=$(shard_aware_dirty)
if [ -n "$DIRTY_OUTSIDE" ]; then
  echo "FATAL: working tree is dirty outside ralph scope (shard_rel=$SHARD_REL)."
  echo "$DIRTY_OUTSIDE"
  echo "  Commit or stash these files before running ralph."
  exit 1
fi

MAX_ITERATIONS=${1:-143}
MAX_RETRIES_PER_STORY=${2:-3}
DEFAULT_AGENT_TIMEOUT=${3:-1800}
assert_nonneg_int "MAX_ITERATIONS" "$MAX_ITERATIONS"
assert_nonneg_int "MAX_RETRIES_PER_STORY" "$MAX_RETRIES_PER_STORY"
assert_nonneg_int "DEFAULT_AGENT_TIMEOUT" "$DEFAULT_AGENT_TIMEOUT"
ITERATION=0

prd_query() {
  python3 -c "
import json, sys
d = json.load(open('$PRD_FILE'))
$1
" || { echo "FATAL: prd.json parse failed" | tee -a "$LOG_FILE"; exit 2; }
}

# --- Archive previous run if branchName changed since last run ---
if [ -f "$LAST_BRANCH_FILE" ]; then
  LAST_BRANCH=$(cat "$LAST_BRANCH_FILE" 2>/dev/null || echo "")
  if [ -n "$LAST_BRANCH" ] && [ "$LAST_BRANCH" != "$TARGET_BRANCH" ]; then
    DATE=$(date +%Y-%m-%d)
    FOLDER_NAME=$(echo "$LAST_BRANCH" | sed 's|/|-|g')
    ARCHIVE_FOLDER="$ARCHIVE_DIR/$DATE-$FOLDER_NAME"
    echo "Archiving previous run: $LAST_BRANCH -> $ARCHIVE_FOLDER"
    mkdir -p "$ARCHIVE_FOLDER"
    [ -f "$PRD_FILE" ] && cp "$PRD_FILE" "$ARCHIVE_FOLDER/"
    [ -f "$PROGRESS_FILE" ] && cp "$PROGRESS_FILE" "$ARCHIVE_FOLDER/"
  fi
fi
atomic_write "$LAST_BRANCH_FILE" "$TARGET_BRANCH"

# Initialize progress file if missing (should already exist from /ralph-init)
if [ ! -s "$PROGRESS_FILE" ]; then
  echo "WARNING: $PROGRESS_FILE missing — re-init with empty Codebase Patterns"
  atomic_write "$PROGRESS_FILE" "## Codebase Patterns
(empty — populate via /ralph-init or manual seed)

## Completed Stories

## Current Blockers
"
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') Ralph Loop starting (max=$MAX_ITERATIONS, branch=$TARGET_BRANCH)" | tee -a "$LOG_FILE"

while [ "$ITERATION" -lt "$MAX_ITERATIONS" ]; do
  ITERATION=$((ITERATION + 1))
  echo ""
  echo "==============================================================="
  echo "  Ralph Loop iteration $ITERATION of $MAX_ITERATIONS"
  echo "  $(date '+%Y-%m-%d %H:%M:%S')"
  echo "==============================================================="

  # --- Pick next pending story ---
  NEXT_STORY_ID=$(prd_query "
stories = d['userStories']
pending = [s for s in stories if not s['passes']]
if not pending:
    print('ALL_DONE')
else:
    pending.sort(key=lambda s: s['priority'])
    print(pending[0]['id'])
")

  if [ "$NEXT_STORY_ID" = "ALL_DONE" ]; then
    echo ""
    echo "All stories COMPLETE!"
    echo "$(date '+%Y-%m-%d %H:%M:%S') ALL COMPLETE after $ITERATION iterations" | tee -a "$LOG_FILE"
    break
  fi
  echo "  Next story: $NEXT_STORY_ID"

  # --- Per-story retry tracker (file-based, bash 3.x safe) ---
  RETRY_FILE="$RETRY_DIR/$NEXT_STORY_ID"
  PREV_COUNT=0
  if [ -f "$RETRY_FILE" ]; then
    raw=$(cat "$RETRY_FILE")
    if [[ "$raw" =~ ^[0-9]+$ ]]; then
      PREV_COUNT="$raw"
    else
      echo "  WARNING: retry file corrupt for $NEXT_STORY_ID (got '$raw') — resetting to 0"
      PREV_COUNT=0
    fi
  fi
  assert_nonneg_int "PREV_COUNT($NEXT_STORY_ID)" "$PREV_COUNT"
  CURR_COUNT=$((PREV_COUNT + 1))
  atomic_write "$RETRY_FILE" "$CURR_COUNT"

  if [ "$CURR_COUNT" -gt "$MAX_RETRIES_PER_STORY" ]; then
    echo "CIRCUIT BREAKER: $NEXT_STORY_ID failed $MAX_RETRIES_PER_STORY times. Skipping." | tee -a "$LOG_FILE"
    python3 -c "
import json
d = json.load(open('$PRD_FILE'))
for s in d['userStories']:
    if s['id'] == '$NEXT_STORY_ID':
        s['priority'] = 99999
        break
json.dump(d, open('$PRD_FILE', 'w'), indent=2, ensure_ascii=False)
"
    continue
  fi
  echo "  Attempt $CURR_COUNT/$MAX_RETRIES_PER_STORY for $NEXT_STORY_ID"

  # --- Per-story timeoutSeconds (read from prd.json, default fallback) ---
  AGENT_TIMEOUT=$(python3 -c "
import json
d = json.load(open('$PRD_FILE'))
for s in d['userStories']:
    if s['id'] == '$NEXT_STORY_ID':
        v = s.get('timeoutSeconds', $DEFAULT_AGENT_TIMEOUT)
        print(v if isinstance(v, int) and v > 0 else $DEFAULT_AGENT_TIMEOUT)
        break
else:
    print($DEFAULT_AGENT_TIMEOUT)
")
  assert_nonneg_int "AGENT_TIMEOUT($NEXT_STORY_ID)" "$AGENT_TIMEOUT"
  echo "  Per-story timeout: ${AGENT_TIMEOUT}s"

  # --- Story lock (atomic write) ---
  LOCK_JSON=$(python3 -c "
import json
from datetime import datetime, timezone
print(json.dumps({
    'id': '$NEXT_STORY_ID',
    'iteration': $ITERATION,
    'attempt': $CURR_COUNT,
    'locked_at': datetime.now(timezone.utc).isoformat(),
}, indent=2))
")
  atomic_write "$STORY_LOCK_FILE" "$LOCK_JSON"
  echo "  Story lock written: $STORY_LOCK_FILE"

  # --- Multi-file snapshot via git stash (covers all modifies, not just one) ---
  STASH_REF=""
  if [ -n "$(git status --porcelain)" ]; then
    # Should not happen — we asserted clean tree at startup and after each commit.
    echo "  WARNING: tree dirty before iteration — stashing for safety"
  fi
  # Pre-iteration commit hash for rollback reference
  PRE_HEAD=$(git rev-parse HEAD)
  echo "  Pre-iteration HEAD: $PRE_HEAD"

  # --- Snapshot prd.json passes state ---
  BEFORE_PRD_SNAPSHOT=$(python3 -c "
import json
d = json.load(open('$PRD_FILE'))
for s in d['userStories']:
    print(f'{s[\"id\"]}:{s[\"passes\"]}')
")

  # --- Re-assert branch (defense vs cross-iteration drift) ---
  CURRENT_GIT_BRANCH=$(git branch --show-current)
  if [ "$CURRENT_GIT_BRANCH" != "$TARGET_BRANCH" ]; then
    echo "FATAL: no longer on $TARGET_BRANCH (now on $CURRENT_GIT_BRANCH). Stopping." | tee -a "$LOG_FILE"
    break
  fi

  # --- Launch claude (background + manual poll for macOS-safe timeout) ---
  echo "$(date '+%Y-%m-%d %H:%M:%S') Starting iteration $ITERATION (story=$NEXT_STORY_ID, timeout=${AGENT_TIMEOUT}s)" >> "$LOG_FILE"

  ITER_OUTPUT_FILE=$(mktemp)
  CLAUDE_EXIT_CODE=0
  TIMED_OUT=""

  CLAUDE_BIN="${RALPH_CLAUDE_BIN:-claude}"
  CLAUDE_MODEL="${RALPH_CLAUDE_MODEL:-opus}"
  (unset CLAUDECODE; "$CLAUDE_BIN" --dangerously-skip-permissions --print --model "$CLAUDE_MODEL" < "$PROMPT_FILE" > "$ITER_OUTPUT_FILE" 2>&1) &
  CLAUDE_PID=$!

  WAITED=0
  while kill -0 "$CLAUDE_PID" 2>/dev/null; do
    if [ "$WAITED" -ge "$AGENT_TIMEOUT" ]; then
      TIMED_OUT="true"
      echo "" | tee -a "$LOG_FILE"
      echo "TIMEOUT: agent exceeded ${AGENT_TIMEOUT}s, killing PID $CLAUDE_PID" | tee -a "$LOG_FILE"
      kill "$CLAUDE_PID" 2>/dev/null || true
      sleep 3
      pkill -P "$CLAUDE_PID" 2>/dev/null || true
      kill -9 "$CLAUDE_PID" 2>/dev/null || true
      break
    fi
    sleep 10
    WAITED=$((WAITED + 10))
  done
  set +e
  wait "$CLAUDE_PID" 2>/dev/null
  CLAUDE_EXIT_CODE=$?
  set -e
  assert_nonneg_int "CLAUDE_EXIT_CODE" "$CLAUDE_EXIT_CODE"

  OUTPUT=$(cat "$ITER_OUTPUT_FILE" 2>/dev/null || echo "")
  cat "$ITER_OUTPUT_FILE" >> "$LOG_FILE" 2>/dev/null || true
  cat "$ITER_OUTPUT_FILE" >&2 2>/dev/null || true
  rm -f "$ITER_OUTPUT_FILE"

  if [ -n "$TIMED_OUT" ]; then
    CLAUDE_EXIT_CODE=124
  fi

  echo "$(date '+%Y-%m-%d %H:%M:%S') Iteration $ITERATION finished (exit=$CLAUDE_EXIT_CODE, timeout=${TIMED_OUT:-none})" >> "$LOG_FILE"

  if [ "$CLAUDE_EXIT_CODE" -eq 124 ]; then
    echo "  TIMEOUT — will rollback uncommitted changes and retry" | tee -a "$LOG_FILE"
  elif [ "$CLAUDE_EXIT_CODE" -ne 0 ]; then
    echo "WARNING: Claude exited with code $CLAUDE_EXIT_CODE" | tee -a "$LOG_FILE"
  fi

  # --- Branch escape recovery (downgraded from FATAL: agent error, not harness error) ---
  POST_BRANCH=$(git branch --show-current)
  if [ "$POST_BRANCH" != "$TARGET_BRANCH" ]; then
    DIRTY_COUNT=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    echo "  WARNING: agent escaped to '$POST_BRANCH' (dirty=$DIRTY_COUNT) — recovering to $TARGET_BRANCH" | tee -a "$LOG_FILE"
    git checkout --force "$TARGET_BRANCH" --quiet 2>/dev/null || git checkout "$TARGET_BRANCH" --quiet 2>/dev/null || true
    echo "  Recovered. Iteration work lost — will retry." | tee -a "$LOG_FILE"
  fi

  # --- Structural diff: only locked story's passes may have flipped false->true ---
  AFTER_PRD_SNAPSHOT=$(python3 -c "
import json
d = json.load(open('$PRD_FILE'))
for s in d['userStories']:
    print(f'{s[\"id\"]}:{s[\"passes\"]}')
")

  VALIDATION_RESULT=$(python3 -c "
before = dict(line.split(':',1) for line in '''$BEFORE_PRD_SNAPSHOT'''.strip().split('\n') if ':' in line)
after  = dict(line.split(':',1) for line in '''$AFTER_PRD_SNAPSHOT'''.strip().split('\n') if ':' in line)
locked = '$NEXT_STORY_ID'
changed = [sid for sid in after if before.get(sid) != after[sid]]
if not changed:
    print('NO_CHANGE')
elif changed == [locked] and after[locked] == 'True':
    print('OK')
elif changed == [locked] and after[locked] == 'False':
    print('OK_NO_PASS')
else:
    print(f'VIOLATION:changed={changed},locked={locked}')
")

  case "$VALIDATION_RESULT" in
    OK)
      echo "  Validation: PASS — story $NEXT_STORY_ID completed (passes flipped false->true on locked story only)."
      echo "$(date '+%Y-%m-%d %H:%M:%S') VALIDATED: $NEXT_STORY_ID passed" >> "$LOG_FILE"
      rm -f "$RETRY_FILE"
      ;;
    OK_NO_PASS)
      echo "  Validation: $NEXT_STORY_ID attempted but passes still false (will retry; current attempt $CURR_COUNT/$MAX_RETRIES_PER_STORY)."
      echo "$(date '+%Y-%m-%d %H:%M:%S') NOT_PASSED: $NEXT_STORY_ID" >> "$LOG_FILE"
      ;;
    NO_CHANGE)
      echo "  Validation: no prd.json changes detected this iteration."
      echo "$(date '+%Y-%m-%d %H:%M:%S') NO_CHANGE in iteration $ITERATION" >> "$LOG_FILE"
      ;;
    VIOLATION:*)
      echo "VIOLATION: agent flipped passes on unexpected stories — self-healing prd.json + git reset" | tee -a "$LOG_FILE"
      echo "  Detail: $VALIDATION_RESULT" | tee -a "$LOG_FILE"
      echo "  Locked story was: $NEXT_STORY_ID" | tee -a "$LOG_FILE"
      python3 -c "
import json
d = json.load(open('$PRD_FILE'))
before = dict(line.split(':',1) for line in '''$BEFORE_PRD_SNAPSHOT'''.strip().split('\n') if ':' in line)
healed = []
for s in d['userStories']:
    if s['id'] != '$NEXT_STORY_ID':
        original = before.get(s['id'], 'False') == 'True'
        if s['passes'] != original:
            healed.append(s['id'])
            s['passes'] = original
json.dump(d, open('$PRD_FILE', 'w'), indent=2, ensure_ascii=False)
if healed:
    print(f'  Healed {len(healed)} stories: {healed}')
" | tee -a "$LOG_FILE"
      LOCKED_PASSED=$(python3 -c "
import json
d = json.load(open('$PRD_FILE'))
for s in d['userStories']:
    if s['id'] == '$NEXT_STORY_ID':
        print('yes' if s['passes'] else 'no')
        break
")
      if [ "$LOCKED_PASSED" = "yes" ]; then
        echo "  Locked story $NEXT_STORY_ID legitimately passed — keeping flip." | tee -a "$LOG_FILE"
      else
        echo "  Locked story $NEXT_STORY_ID did not pass — leaving prd.json healed only." | tee -a "$LOG_FILE"
      fi
      ;;
  esac

  # --- COMPLETE signal: cross-validate against prd.json before exiting ---
  if echo "$OUTPUT" | grep -q "<promise>COMPLETE</promise>"; then
    ACTUALLY_DONE=$(prd_query "
pending = [s for s in d['userStories'] if not s['passes']]
print('yes' if not pending else 'no')
")
    if [ "$ACTUALLY_DONE" = "yes" ]; then
      echo ""
      echo "Ralph completed all stories (verified against prd.json)."
      echo "$(date '+%Y-%m-%d %H:%M:%S') ALL COMPLETE (verified) after $ITERATION iterations" | tee -a "$LOG_FILE"
      break
    else
      echo "WARNING: agent claimed COMPLETE but prd.json has pending stories. Continuing." | tee -a "$LOG_FILE"
    fi
  fi

  if echo "$OUTPUT" | grep -q "<promise>YIELD</promise>"; then
    echo "  YIELD signal received, continuing to next iteration."
    echo "$(date '+%Y-%m-%d %H:%M:%S') YIELD: $NEXT_STORY_ID" >> "$LOG_FILE"
  fi

  # --- Rollback uncommitted changes only (preserve commits agent made) ---
  # Default mode: full cleanup (operator session commits between iterations).
  # Shard mode: legitimate state writes (state/verdicts.jsonl, seen_task_ids.json,
  # prd.json, progress.txt, loop.log) live INSIDE shard root and must NOT be cleaned.
  # Only files OUTSIDE all ralph dirs get cleaned (defense vs subagent escape).
  POST_HEAD=$(git rev-parse HEAD)
  if [ "$POST_HEAD" = "$PRE_HEAD" ] && [ -n "$(git status --porcelain)" ]; then
    if [ "$SHARD_REL" = ".ralph" ]; then
      echo "  Cleaning uncommitted changes (no commit was made this iteration)" | tee -a "$LOG_FILE"
      git checkout -- . 2>/dev/null || true
      git clean -fd -- . 2>/dev/null || true
    else
      # Shard mode: revert ONLY files outside any ralph dir (subagent escape defense)
      OUTSIDE_DIRTY=$(shard_aware_dirty)
      if [ -n "$OUTSIDE_DIRTY" ]; then
        echo "  Shard mode: subagent escaped — reverting files outside ralph scope" | tee -a "$LOG_FILE"
        echo "$OUTSIDE_DIRTY" | tee -a "$LOG_FILE"
        # Per-path revert (safer than blanket `git checkout -- .`)
        while IFS= read -r line; do
          [ -z "$line" ] && continue
          path=$(echo "$line" | awk '{$1=""; sub(/^ /,""); print}')
          [ -z "$path" ] && continue
          # Try checkout (revert tracked) then rm (untracked)
          git checkout -- "$path" 2>/dev/null || rm -f "$path" 2>/dev/null || true
        done <<< "$OUTSIDE_DIRTY"
      else
        DIRTY_COUNT=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
        echo "  Shard mode: dirty count=$DIRTY_COUNT (expected, all inside ralph scope)" >> "$LOG_FILE"
      fi
    fi
  fi

  rm -f "$STORY_LOCK_FILE"

  if [ "$ITERATION" -lt "$MAX_ITERATIONS" ]; then
    echo "Cooling down 10s before next iteration..."
    sleep 10
  fi
done

# --- Final summary ---
echo ""
echo "==============================================================="
echo "  Ralph Loop Summary"
echo "==============================================================="
python3 -c "
import json, sys
d = json.load(open('$PRD_FILE'))
stories = d.get('userStories', [])
if not stories:
    print('  No stories found in prd.json')
    sys.exit(0)
for s in stories:
    status = 'PASS' if s['passes'] else 'TODO'
    print(f'  [{status}] {s[\"id\"]}: {s[\"title\"]}')
passed = sum(1 for s in stories if s['passes'])
total = len(stories)
pct = (passed/total*100) if total else 0
print(f'\n  Progress: {passed}/{total} ({pct:.0f}%)')
"
echo "  Iterations used: $ITERATION / $MAX_ITERATIONS"
echo "  Branch: $TARGET_BRANCH"
echo "  Log: $LOG_FILE"
echo "  Progress notes: $PROGRESS_FILE"

python3 -c "
import json, sys
d = json.load(open('$PRD_FILE'))
pending = [s for s in d['userStories'] if not s['passes']]
sys.exit(1 if pending else 0)
"
