# sample-jsonl example

A minimal "data-fill" demo that exercises the full parallel-ralph harness
(JsonlSchemaGate, streaming verdict append helper, shard rendering, write
boundary hook). Each story is a small batch of short text snippets; the
operator subagent classifies each as `positive` / `negative` / `neutral`.

## Layout

```
examples/sample-jsonl/
├── baseline/                 ← gets copied into ./.ralph by init.sh
│   ├── prd.json              ← acceptance.default_gate uses jsonl_schema
│   ├── progress.txt
│   └── stories/
│       ├── manifest.json
│       ├── batch-001.jsonl   ← 4 task lines
│       └── batch-002.jsonl   ← 4 task lines
└── init.sh                   ← copies baseline → .ralph
```

## Running it

```bash
# 1. Initialise .ralph from the baseline
./examples/sample-jsonl/init.sh

# 2a. Single-process
./ralph.sh

# 2b. 2-shard parallel
python3 scripts_4x/render_shards.py --num-shards 2
python3 scripts_4x/audit_shards.py --num-shards 2
./scripts_4x/run_shards.sh a b
./scripts_4x/dashboard.sh a b
# When all shards complete:
python3 scripts_4x/merge_shards.py --num-shards 2 --out-dir eval_results
```

The acceptance gate requires:
- exactly 4 verdicts per story
- `task_id` set matches batch
- `schema_version == "sentiment-v1"`
- ≥ 90% rows with `reason` length ≥ 80 chars
- ≥ 2 distinct `qa` values (or 1 if `n_tasks ≤ 5`)

See `prd.json` `acceptance.default_gate` for the full schema.
