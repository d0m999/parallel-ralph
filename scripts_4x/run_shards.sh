#!/bin/bash
# scripts_4x/run_shards.sh — launch N ralph shard processes in the background.
#
# Usage:
#   ./scripts_4x/run_shards.sh                # default 4 shards (a b c d)
#   ./scripts_4x/run_shards.sh a b            # 2 shards
#   ./scripts_4x/run_shards.sh a b c          # 3 shards
#
# Env:
#   LAUNCH_DELAY=30    # stagger seconds between shard launches
#   MAX_ITER=200       # max iterations per shard
#   MAX_RETRIES=15     # max retries per story (ralph.sh arg 2)
#
# Stale-lock recovery: a leftover .instance pointing at a dead PID is
# cleared automatically before the new shard starts.
#
# Boundaries:
#   - never modifies git state (commits are single-source from the main session)
#   - never writes into other shard directories
#   - never writes into .ralph/ baseline (owned by the main session)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# --- Args ---
if [ "$#" -eq 0 ]; then
  SHARDS=(a b c d)
else
  SHARDS=("$@")
fi

LAUNCH_DELAY="${LAUNCH_DELAY:-30}"
MAX_ITER="${MAX_ITER:-200}"
MAX_RETRIES="${MAX_RETRIES:-15}"

# --- Sanity ---
if [ ! -x "./ralph.sh" ]; then
  echo "FATAL: ./ralph.sh not found or not executable in $REPO_ROOT" >&2
  exit 1
fi

for s in "${SHARDS[@]}"; do
  if [[ ! "$s" =~ ^[a-z]$ ]]; then
    echo "FATAL: shard id '$s' must be a single lowercase letter" >&2
    exit 1
  fi
done

echo "[run_shards] launching ${#SHARDS[@]} shard(s): ${SHARDS[*]}"
echo "[run_shards] LAUNCH_DELAY=${LAUNCH_DELAY}s, MAX_ITER=${MAX_ITER}, MAX_RETRIES=${MAX_RETRIES}"

LAST_IDX=$((${#SHARDS[@]} - 1))
for i in "${!SHARDS[@]}"; do
  SHARD="${SHARDS[$i]}"
  ROOT=".ralph-shard-${SHARD}"

  if [ ! -d "${ROOT}" ]; then
    echo "FATAL: ${ROOT} 不存在 — 先跑 'python3 scripts_4x/render_shards.py --num-shards N'" >&2
    exit 1
  fi
  if [ ! -f "${ROOT}/PROMPT.md" ]; then
    echo "FATAL: ${ROOT}/PROMPT.md 不存在 — render_shards.py 没完整跑?" >&2
    exit 1
  fi

  # === C4 修复: stale-lock 强夺 ===
  if [ -f "${ROOT}/.instance" ]; then
    pid=$(cut -d: -f1 "${ROOT}/.instance" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "[run_shards] shard-${SHARD} already running (pid=$pid), skipping"
      continue
    fi
    echo "[run_shards] shard-${SHARD}: stale lock (pid=${pid:-?} dead), clearing"
    rm -f "${ROOT}/.instance" "${ROOT}/run.pid"
  elif [ -f "${ROOT}/run.pid" ]; then
    # 没 .instance 但有 run.pid → 也清掉
    pid=$(cat "${ROOT}/run.pid" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "[run_shards] shard-${SHARD} already running (pid=$pid via run.pid), skipping"
      continue
    fi
    echo "[run_shards] shard-${SHARD}: stale run.pid (pid=${pid:-?} dead), clearing"
    rm -f "${ROOT}/run.pid"
  fi

  echo "[run_shards] starting shard-${SHARD} (root=${ROOT}, max_iter=${MAX_ITER}, max_retries=${MAX_RETRIES})"
  # nohup + setsid 解耦终端; 输出到 ${ROOT}/run.log (gitignored per .gitignore §10.2)
  nohup ./ralph.sh --shard-root "${ROOT}" "$MAX_ITER" "$MAX_RETRIES" \
    > "${ROOT}/run.log" 2>&1 &
  shard_pid=$!
  # 写 PID 文件 (供 stop_shards.sh / 重启 watchdog 用)
  echo "$shard_pid" > "${ROOT}/run.pid"
  echo "[run_shards]   pid=$shard_pid (logging to ${ROOT}/run.log)"

  # 错峰: 最后一个 shard 不 sleep
  if [ "$i" -lt "$LAST_IDX" ]; then
    sleep "$LAUNCH_DELAY"
  fi
done

echo ""
echo "✅ ${#SHARDS[@]} shard(s) launched"
echo ""
echo "Monitor: tail -f .ralph-shard-{${SHARDS[*]// /,}}/run.log"
echo "Status:  for s in ${SHARDS[*]}; do echo \"--- shard-\$s ---\"; tail -5 .ralph-shard-\$s/run.log; done"
echo "Stop:    ./scripts_4x/stop_shards.sh ${SHARDS[*]}"
