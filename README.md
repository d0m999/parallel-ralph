# parallel-ralph

[![test](https://github.com/OWNER/parallel-ralph/actions/workflows/test.yml/badge.svg)](https://github.com/OWNER/parallel-ralph/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

A harness for running **N disjoint [ralph](https://ghuntley.com/ralph/) loops
in parallel** under [Claude Code](https://claude.com/claude-code), with:

- **Set-theoretic shard verification** — render N shards from a baseline
  manifest, audit pairwise disjoint + union = whole, fail-stop on any drift.
  Splits are derived (auto), supplied via `--splits "1-13,14-26,..."`, or
  loaded from a JSON config file.
- **Pluggable acceptance gates** — `JsonlSchemaGate` (the parameterized
  5-gate labeling check), `CommandGate` (any shell command, PASS iff exit
  0), and `CompositeGate` (AND-of). Per-story override via `acceptanceGate`
  in `prd.json`, or project-wide default via `acceptance.default_gate`.
- **Write-boundary `PreToolUse` hook** — env-var-gated
  (`RALPH_SHARD_ROOT`) enforcement that subagents cannot escape their shard
  directory. Project-specific deny prefixes via `RALPH_HARD_DENY_PREFIXES`.
  No-op in single-process mode.
- **`<promise>YIELD/COMPLETE/VIOLATION</promise>` token protocol** —
  cross-validated against `prd.json`, anti-cheat against false completion.
- **Streaming-only writes** — every verdict row fsync'd one at a time so a
  crash at minute 25 cannot lose minute 1's work. Schema enforced by
  `append_verdict.py`.
- **Graceful degradation (DEGRADE path)** — when a shard dies, drain its
  unseen tasks to surviving shards or fall back to single-process baseline.
- **Cause-classifying monitor** — auto-restart for rate-limit / dirty-tree
  failures; alert and stop on unclassified ones.
- **Atomic state hygiene** — temp+rename writes, PID + liveness instance
  lock, stale-lock reclamation, idempotent symlink rebuilds.

The harness was originally built to drive thousands of LLM-as-judge
classifications in parallel, but ralph itself is a **code-implementation
loop**: stories with disjoint write scope and deterministic acceptance
gates can be implemented by N parallel Claude Code shards. See the
[examples/openapi-impl/](examples/openapi-impl/) demo.

---

## Layout

```
parallel-ralph/
├── ralph.sh                                  # main single/sharded loop
├── .ralph/                                   # baseline state (gitignored)
│   └── scripts/
│       ├── acceptance.py                     # gate plug-in interface
│       ├── run_batch.py                      # prepare / validate / finalize
│       └── append_verdict.py                 # streaming append helper
├── scripts_4x/
│   ├── render_shards.py                      # split baseline → N shards
│   ├── audit_shards.py                       # 5-layer correctness audit
│   ├── merge_shards.py                       # merge shard outputs + audit
│   ├── monitor_shards.py                     # cause-classify + auto-restart
│   ├── redistribute_remaining.py             # DEGRADE path
│   ├── recover_shards_after_limit.sh         # post-rate-limit recovery
│   ├── run_shards.sh                         # launch N shards
│   ├── stop_shards.sh                        # graceful shutdown
│   ├── dashboard.sh                          # ASCII progress dashboard
│   ├── PROMPT.md.tmpl                        # operator prompt template
│   └── hooks/
│       └── deny_outside_shard.py             # PreToolUse write-boundary hook
├── examples/
│   ├── sample-jsonl/                         # JsonlSchemaGate demo
│   └── openapi-impl/                         # CommandGate demo
├── tests/                                    # 50+ unit/integration tests
├── LICENSE                                   # MIT
├── pyproject.toml                            # stdlib runtime, pytest dev
└── README.md
```

---

## Quick start

### Try it on the sample jsonl example

```bash
./examples/sample-jsonl/init.sh
./ralph.sh                                    # single-process
# or:
python3 scripts_4x/render_shards.py --num-shards 2
python3 scripts_4x/audit_shards.py --num-shards 2
./scripts_4x/run_shards.sh a b
./scripts_4x/dashboard.sh a b
```

### Try the code-implementation example

```bash
./examples/openapi-impl/init.sh
./ralph.sh
```

### Run the test suite

```bash
pip install -e '.[dev]'
pytest -q
```

---

## How a story is gated

`prd.json` may set a project-wide default plus per-story overrides:

```json
{
  "branchName": "main",
  "userStories": [
    {
      "id": "BATCH-001",
      "passes": false,
      "acceptanceGate": {
        "type": "command",
        "command": "pytest tests/test_endpoint_users.py -q"
      }
    },
    {
      "id": "BATCH-002",
      "passes": false
    }
  ],
  "acceptance": {
    "max_attempts": 3,
    "default_gate": {
      "type": "jsonl_schema",
      "schema_version": "judge-v1",
      "verdict_schema": {
        "required_fields": ["task_id", "qa", "reason"],
        "qa_field": "qa",
        "reason_field": "reason",
        "valid_qa": ["yes", "no", "uncertain"],
        "min_reason_chars": 150,
        "reason_long_ratio_min": 0.9,
        "distinct_qa_min": 2,
        "distinct_qa_min_small": 1,
        "small_batch_threshold": 33
      }
    }
  }
}
```

`run_batch.py validate` and `run_batch.py finalize` dispatch to the gate
named in the story (or the default), passing the story dict + shard root.
`acceptance.py` is the single source of truth — add a new gate type by
implementing the `Gate` protocol and registering it in `_BUILTIN_GATES`.

---

## Sharding configuration

The baseline manifest's `n_batches` and `total_tasks` are authoritative;
splits are not hardcoded.

```bash
# Auto-compute even split (last shard absorbs the remainder)
python3 scripts_4x/render_shards.py --num-shards 4

# Explicit ranges
python3 scripts_4x/render_shards.py --num-shards 4 \
        --splits "1-13,14-26,27-39,40-53"

# From a JSON config
python3 scripts_4x/render_shards.py --num-shards 4 \
        --splits-file shard-splits.json
```

`shard-splits.json`:
```json
{"splits": [[1,13],[14,26],[27,39],[40,53]]}
```

---

## Ralph for code vs. labeling

| Concern             | Labeling use case               | Code-implementation use case            |
|---------------------|----------------------------------|----------------------------------------|
| Per-story output    | append rows to `verdicts.jsonl` | `git commit` of code + test files       |
| Gate                | `JsonlSchemaGate`               | `CommandGate` running pytest/cargo test |
| Disjoint scope      | task_id partition               | file-path partition (hook enforces it)  |
| Shard isolation     | separate state dirs             | separate dirs **OR git worktrees**      |

The write-boundary hook enforces directory-level disjoint scope already;
story-level disjoint scope is the same idea applied to file paths. For
stories that genuinely conflict, run each shard in its own `git worktree`
and merge at the end.

---

## License

MIT — see [LICENSE](./LICENSE).

## Origin & credits

The harness in this repo was extracted (and sanitized) from a private
LLM-eval project. Many implementation details — the 5-gate acceptance
contract, the auto-recovery heuristic, the streaming-only write rule,
the calibrated subagent-serial-1×N finding — come from real failure
modes encountered there.

The underlying ralph loop pattern is from
[Geoffrey Huntley's "ralph"](https://ghuntley.com/ralph/) — credit there
for the original idea of "one PRD, one acceptance file, loop until done."
This harness was directly inspired by
[snarktank/ralph](https://github.com/snarktank/ralph), which provided a
concrete reference implementation of the loop we built on top of.
