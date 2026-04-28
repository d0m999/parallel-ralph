#!/bin/bash
# init.sh — copy the sample baseline into ./.ralph and prepare a clean state
#
# Run from the repo root:
#   ./examples/sample-jsonl/init.sh
#
# After this runs you can:
#   - single-process: ./ralph.sh
#   - 2-shard: python3 scripts_4x/render_shards.py --num-shards 2 \
#              && ./scripts_4x/run_shards.sh a b
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$REPO_ROOT/examples/sample-jsonl/baseline"
DEST="$REPO_ROOT/.ralph"

# .ralph/scripts/ ships in the repo — only consider non-script content as "occupied"
if [ -e "$DEST/prd.json" ] || [ -d "$DEST/stories" ] || [ -d "$DEST/state" ]; then
  echo "FATAL: $DEST already has prd.json / stories/ / state/. Refusing to overwrite."
  echo "  rm -rf $DEST/prd.json $DEST/stories $DEST/state  # if you really want to redo the example"
  exit 1
fi

mkdir -p "$DEST/state" "$DEST/stories"
cp "$SRC/prd.json" "$DEST/prd.json"
cp "$SRC/progress.txt" "$DEST/progress.txt"
cp "$SRC/stories/manifest.json" "$DEST/stories/manifest.json"
cp "$SRC/stories/batch-001.jsonl" "$DEST/stories/batch-001.jsonl"
cp "$SRC/stories/batch-002.jsonl" "$DEST/stories/batch-002.jsonl"

# Empty state files
: > "$DEST/state/verdicts.jsonl"
echo "[]" > "$DEST/state/seen_task_ids.json"

# Minimal PROMPT.md so ralph.sh can be invoked in single-process mode
cat > "$DEST/PROMPT.md" <<'EOF'
# Sentiment classification operator

You are the ralph operator for the sample-jsonl example. The locked story
id is in .ralph/current_story.json. Run:

  uv run python .ralph/scripts/run_batch.py prepare

Then dispatch a fresh subagent with the prompt at
.ralph/state/subagent_prompt.md. After the subagent finishes, run:

  uv run python .ralph/scripts/run_batch.py validate
  uv run python .ralph/scripts/run_batch.py finalize

Copy finalize's promise token verbatim to your final stdout.
EOF

echo "✓ initialized $DEST from $SRC"
echo "  Single-process:  ./ralph.sh"
echo "  2-shard:         python3 scripts_4x/render_shards.py --num-shards 2"
