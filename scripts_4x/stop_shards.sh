#!/bin/bash
# scripts_4x/stop_shards.sh — 优雅停止 N 路 ralph harness
#
# Usage:
#   ./scripts_4x/stop_shards.sh                # 默认全停 (a b c d)
#   ./scripts_4x/stop_shards.sh c d            # 只停 c+d (DEGRADE 路径)
#
# 流程: 读 ${ROOT}/run.pid → SIGTERM → wait 5s → SIGKILL fallback → 清 .instance + run.pid
#
# 注意: ralph.sh 的 trap 'rm -f $INSTANCE_LOCK $STORY_LOCK_FILE' EXIT 会在 SIGTERM 后自清,
# 但 run.pid 是 run_shards.sh 写的, 这里负责清.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [ "$#" -eq 0 ]; then
  SHARDS=(a b c d)
else
  SHARDS=("$@")
fi

GRACE_SEC="${GRACE_SEC:-5}"

echo "[stop_shards] stopping ${#SHARDS[@]} shard(s): ${SHARDS[*]} (grace=${GRACE_SEC}s)"

stopped=0
already_dead=0
killed_hard=0

for SHARD in "${SHARDS[@]}"; do
  ROOT=".ralph-shard-${SHARD}"
  pidfile="${ROOT}/run.pid"
  instance="${ROOT}/.instance"

  if [ ! -f "$pidfile" ]; then
    if [ -f "$instance" ]; then
      # 没 run.pid 但有 .instance — 取 .instance 里的 PID
      pid=$(cut -d: -f1 "$instance" 2>/dev/null || echo "")
    else
      echo "[stop_shards] shard-${SHARD}: no pidfile, skip"
      continue
    fi
  else
    pid=$(cat "$pidfile" 2>/dev/null || echo "")
  fi

  if [ -z "$pid" ] || ! [[ "$pid" =~ ^[0-9]+$ ]]; then
    echo "[stop_shards] shard-${SHARD}: pid '$pid' invalid, cleaning lock files only"
    rm -f "$pidfile" "$instance"
    already_dead=$((already_dead + 1))
    continue
  fi

  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[stop_shards] shard-${SHARD}: pid=$pid already dead, cleaning lock files"
    rm -f "$pidfile" "$instance"
    already_dead=$((already_dead + 1))
    continue
  fi

  echo "[stop_shards] shard-${SHARD}: SIGTERM pid=$pid"
  kill -TERM "$pid" 2>/dev/null || true

  # wait grace period
  i=0
  while [ "$i" -lt "$GRACE_SEC" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 1
    i=$((i + 1))
  done

  if kill -0 "$pid" 2>/dev/null; then
    echo "[stop_shards]   pid=$pid still alive after ${GRACE_SEC}s, SIGKILL"
    kill -KILL "$pid" 2>/dev/null || true
    killed_hard=$((killed_hard + 1))
  else
    echo "[stop_shards]   pid=$pid stopped gracefully"
    stopped=$((stopped + 1))
  fi

  rm -f "$pidfile" "$instance"
done

echo ""
echo "[stop_shards] summary: ${stopped} TERM-stop, ${killed_hard} KILL-stop, ${already_dead} already-dead"
