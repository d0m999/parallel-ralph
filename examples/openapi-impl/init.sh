#!/bin/bash
# init.sh — copy the openapi-impl baseline into ./.ralph
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$REPO_ROOT/examples/openapi-impl/baseline"
DEST="$REPO_ROOT/.ralph"

# .ralph/scripts/ ships in the repo — only consider non-script content as "occupied"
if [ -e "$DEST/prd.json" ] || [ -d "$DEST/stories" ] || [ -d "$DEST/state" ]; then
  echo "FATAL: $DEST already has prd.json / stories/ / state/. Refusing to overwrite."
  exit 1
fi

mkdir -p "$DEST/state" "$DEST/stories"
cp "$SRC/prd.json" "$DEST/prd.json"
cp "$SRC/progress.txt" "$DEST/progress.txt"
cp "$SRC/stories/manifest.json" "$DEST/stories/manifest.json"
cp "$SRC/stories/batch-001.jsonl" "$DEST/stories/batch-001.jsonl"
cp "$SRC/stories/batch-002.jsonl" "$DEST/stories/batch-002.jsonl"

: > "$DEST/state/verdicts.jsonl"
echo "[]" > "$DEST/state/seen_task_ids.json"

cat > "$DEST/PROMPT.md" <<'EOF'
# Code-impl operator (openapi-impl example)

You are the ralph operator for the openapi-impl example. The locked story
id is in .ralph/current_story.json. For the locked story:

1. Read prd.json and find the story by id.
2. Edit the files listed in `modifies` / `creates` to implement the
   endpoint.
3. Run the story's `acceptanceGate.command` (a pytest invocation).
4. If pytest passes, flip the locked story's `passes` field to true and
   emit `<promise>YIELD</promise>` (or `<promise>COMPLETE</promise>` if
   it was the last pending story).

Do not edit any file outside the locked story's modifies/creates list.
EOF

echo "✓ initialized $DEST from $SRC"
