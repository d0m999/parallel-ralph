#!/bin/bash
# scripts_4x/dashboard.sh — read-only N-shard ralph progress dashboard.
#
# Per-shard line: ASCII progress bar + 429 count + recent reason avg-length
# + PID status. Writes nothing; safe to run repeatedly while shards execute.
#
# Usage:
#   ./scripts_4x/dashboard.sh                # default 4 shards (a b c d)
#   ./scripts_4x/dashboard.sh a b            # 2 shards (e.g. post-DEGRADE)
#   watch -n 30 ./scripts_4x/dashboard.sh    # auto-refresh every 30s

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [ "$#" -eq 0 ]; then
  SHARDS=(a b c d)
else
  SHARDS=("$@")
fi

# Bar width
BAR_WIDTH=20

print_bar() {
  local done=$1
  local total=$2
  if [ "$total" -le 0 ]; then total=1; fi
  local filled=$((done * BAR_WIDTH / total))
  local empty=$((BAR_WIDTH - filled))
  printf "["
  printf "%0.s#" $(seq 1 $filled 2>/dev/null) 2>/dev/null || true
  printf "%0.s." $(seq 1 $empty 2>/dev/null) 2>/dev/null || true
  printf "]"
}

NOW=$(date '+%Y-%m-%d %H:%M:%S %Z')
echo "═══════════════════════════════════════════════════════════════════════════════"
echo "  parallel-ralph dashboard · ${NOW}"
echo "═══════════════════════════════════════════════════════════════════════════════"
printf "%-8s %-22s %6s %6s %5s %5s %5s\n" "shard" "progress" "done" "total" "429" "avg-c" "pid"
echo "───────────────────────────────────────────────────────────────────────────────"

total_done=0
total_total=0
total_429=0

for SHARD in "${SHARDS[@]}"; do
  ROOT=".ralph-shard-${SHARD}"

  if [ ! -d "${ROOT}" ]; then
    printf "%-8s %-22s %6s %6s %5s %5s %5s\n" "$SHARD" "(not rendered)" "-" "-" "-" "-" "-"
    continue
  fi

  # done = passes=true 数量; total = 总 story 数
  done_total=$(python3 -c "
import json
try:
    p = json.load(open('${ROOT}/prd.json'))
    done = sum(1 for s in p['userStories'] if s.get('passes'))
    total = len(p['userStories'])
    print(f'{done} {total}')
except Exception as e:
    print('? ?')
")
  done=$(echo "$done_total" | awk '{print $1}')
  total=$(echo "$done_total" | awk '{print $2}')

  # 429 / overloaded count from run.log + loop.log
  err_429=0
  for log in "${ROOT}/run.log" "${ROOT}/loop.log"; do
    if [ -f "$log" ]; then
      c=$(grep -cE '429|rate_limit_exceeded|overloaded_error' "$log" 2>/dev/null || echo 0)
      err_429=$((err_429 + c))
    fi
  done

  # avg reason length (sampled from last 50 verdicts)
  # The reason field name is read from prd.acceptance.default_gate.verdict_schema.reason_field
  avg_c="-"
  if [ -f "${ROOT}/state/verdicts.jsonl" ] && [ -f "${ROOT}/prd.json" ]; then
    avg_c=$(python3 -c "
import json
try:
    prd = json.load(open('${ROOT}/prd.json'))
    accept = prd.get('acceptance', {}).get('default_gate', {})
    schema = accept.get('verdict_schema', {})
    reason_field = schema.get('reason_field', 'reason')
    vs = []
    with open('${ROOT}/state/verdicts.jsonl') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            v = json.loads(line)
            r = v.get(reason_field, '')
            if isinstance(r, str):
                vs.append(len(r))
    if not vs:
        print('-')
    else:
        sample = vs[-50:] if len(vs) > 50 else vs
        print(int(sum(sample) / len(sample)))
except Exception:
    print('-')
")
  fi

  # PID 状态
  pid_status="dead"
  if [ -f "${ROOT}/run.pid" ]; then
    pid=$(cat "${ROOT}/run.pid" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      pid_status="$pid"
    else
      pid_status="dead"
    fi
  fi

  bar=$(print_bar "${done:-0}" "${total:-1}")
  printf "%-8s %-22s %6s %6s %5s %5s %5s\n" \
    "$SHARD" "$bar" "${done:-?}" "${total:-?}" "${err_429:-0}" "${avg_c:--}" "${pid_status}"

  if [[ "$done" =~ ^[0-9]+$ ]] && [[ "$total" =~ ^[0-9]+$ ]]; then
    total_done=$((total_done + done))
    total_total=$((total_total + total))
  fi
  if [[ "$err_429" =~ ^[0-9]+$ ]]; then
    total_429=$((total_429 + err_429))
  fi
done

echo "───────────────────────────────────────────────────────────────────────────────"
total_bar=$(print_bar "$total_done" "$total_total")
printf "%-8s %-22s %6s %6s %5s\n" "TOTAL" "$total_bar" "$total_done" "$total_total" "$total_429"
echo "═══════════════════════════════════════════════════════════════════════════════"

# Note: any rate-limit / DEGRADE policy thresholds are project-specific and
# should be added to your fork's dashboard tail.
