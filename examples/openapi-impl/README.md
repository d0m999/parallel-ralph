# openapi-impl example

A minimal "code-implementation" demo: each story implements ONE REST
endpoint (handler module + pytest test file). The acceptance gate is a
`CommandGate` that runs the per-endpoint pytest.

This shows how parallel-ralph can drive parallel code implementation when
each story has a disjoint write scope (one handler file + one test file
per endpoint), gated by tests.

## Layout

```
examples/openapi-impl/
├── openapi.yaml            ← spec the stories implement against
├── baseline/               ← gets copied to ./.ralph/ for ralph to drive
│   ├── prd.json
│   ├── progress.txt
│   └── stories/
│       └── manifest.json   ← empty task_ids for code-impl mode
├── src/handlers/           ← what stories will write into
│   └── __init__.py
└── tests/                  ← what stories will write into (test files)
```

`prd.json` per story sets `acceptanceGate.type == "command"` so the gate
runs pytest directly. No `verdicts.jsonl` involvement at all.

## Running it

```bash
# 1. Initialise .ralph from the baseline
./examples/openapi-impl/init.sh

# 2. Single-process (each iteration: pick story → edit handler+test → pytest)
./ralph.sh

# 2-shard parallel works too if endpoints are disjoint:
python3 scripts_4x/render_shards.py --num-shards 2
./scripts_4x/run_shards.sh a b
```

The operator (per `.ralph/PROMPT.md`) reads the locked story's `modifies` /
`creates` list, edits those files, then runs:

```bash
pytest examples/openapi-impl/tests/test_<endpoint>.py -q
```

If the test passes, the operator flips `passes=true` in `prd.json` and
emits `<promise>YIELD</promise>` (or `<promise>COMPLETE</promise>` on the
last story).
