# parallel-ralph

[![test](https://github.com/OWNER/parallel-ralph/actions/workflows/test.yml/badge.svg)](https://github.com/OWNER/parallel-ralph/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

> 中文版：[README_ZH.md](./README_ZH.md)

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

## Setup

**Prerequisites**

- Python **3.10+**
- [Claude Code](https://claude.com/claude-code) installed and authenticated
  (`claude` on `$PATH`; `npm install -g @anthropic-ai/claude-code`)
- A git repository for your project (the loop commits + branches)
- macOS or Linux (the shell tooling assumes BSD / GNU coreutils)

The harness itself is **pure stdlib** — no third-party Python deps at runtime.
`pytest` and `ruff` are dev-only.

**Install**

```bash
git clone https://github.com/OWNER/parallel-ralph.git
cd parallel-ralph
pip install -e '.[dev]'      # only needed if you want to run pytest / ruff
pytest -q                    # smoke test (should report 55 passed)
```

**Try a demo before bringing your own task**

```bash
./examples/sample-jsonl/init.sh   # JsonlSchemaGate demo (sentiment labeling)
./ralph.sh                         # single-process loop

# or the code-implementation demo (CommandGate runs pytest):
./examples/openapi-impl/init.sh
./ralph.sh
```

---

## Workflow

### Conceptual model

The harness is a **per-story loop**:

1. ralph.sh picks the highest-priority story with `passes: false` from
   `prd.json` and locks it into `current_story.json`.
2. A fresh Claude Code instance is dispatched with a prompt that points at
   the locked story plus three plumbing commands:
   `prepare` → (subagent does the actual work) → `validate` → `finalize`.
3. `validate` runs the configured acceptance gate (JSONL schema check,
   `pytest`, etc.). On PASS, `finalize` flips `passes: true`, appends a line
   to `progress.txt`, and emits `<promise>YIELD</promise>` (or
   `<promise>COMPLETE</promise>` if no stories remain).
4. The loop repeats with a clean context. Memory between iterations: git
   history, `progress.txt`, `prd.json` (which stories are done).

### Single-process

Use this when:
- you have ≤ a few dozen short stories,
- or you're prototyping — make it work single-process first.

```bash
# 0. Prepare your baseline (or run an example's init.sh)
./examples/sample-jsonl/init.sh

# 1. Run the loop
./ralph.sh                                    # default: 200 iters, 15 retries/story
./ralph.sh 50 5 1200                          # max_iters=50, max_retries=5, agent_timeout=1200s
```

`ralph.sh` keeps running until either every story has `passes: true` (it
prints `<promise>COMPLETE</promise>` and exits) or `max_iterations` is
reached.

### N-shard parallel

Use this when stories are independent enough that N Claude Code instances
can work concurrently without stepping on each other:

- **Labeling tasks**: each shard owns a contiguous slice of `task_ids`.
- **Code-implementation tasks**: each shard owns a disjoint set of file
  paths (use git worktrees if conflicts are real).

```bash
# 1. Initialise the baseline once
./examples/sample-jsonl/init.sh

# 2. Render N shard trees from .ralph/
python3 scripts_4x/render_shards.py --num-shards 4
# → creates .ralph-shard-{a,b,c,d} with prd.json, manifest, batch symlinks

# 3. Verify the 5 invariants (count, disjoint, union, symlinks, prd↔manifest)
python3 scripts_4x/audit_shards.py --num-shards 4

# 4. Launch 4 shards in the background (staggered by 30s by default)
./scripts_4x/run_shards.sh a b c d
# → spawns ./ralph.sh --shard-root .ralph-shard-X, logs to .ralph-shard-X/run.log

# 5. Watch progress (re-run any time; writes nothing)
./scripts_4x/dashboard.sh a b c d
watch -n 30 ./scripts_4x/dashboard.sh a b c d   # auto-refresh

# 6. Optional: cause-classifying monitor (auto-restarts rate-limit / dirty-tree)
python3 scripts_4x/monitor_shards.py --shards a b c d

# 7. When all shards report passes=true, merge + audit
python3 scripts_4x/merge_shards.py --num-shards 4 --out-dir eval_results
```

Tunable `run_shards.sh` env vars: `LAUNCH_DELAY=30`, `MAX_ITER=200`, `MAX_RETRIES=15`.

---

## Configuration

### prd.json

Authoritative task list. Each story has `id`, `passes`, `priority`,
optional `acceptanceGate`, plus whatever metadata the operator prompt
references (`title`, `modifies`, `creates`, `acceptanceCriteria`, etc.).

```json
{
  "branchName": "main",
  "userStories": [
    {
      "id": "BATCH-001",
      "passes": false,
      "priority": 1,
      "acceptanceGate": {
        "type": "command",
        "command": "pytest tests/test_endpoint_users.py -q"
      }
    },
    { "id": "BATCH-002", "passes": false, "priority": 2 }
  ],
  "acceptance": {
    "max_attempts": 3,
    "default_gate": {
      "type": "jsonl_schema",
      "schema_version": "judge-v1",
      "verdict_schema": {
        "required_fields": ["task_id", "qa", "reason"],
        "id_field": "task_id",
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
named in the story (or the project-wide `acceptance.default_gate`),
passing the story dict + shard root.

### manifest.json (labeling-mode only)

For JSONL/labeling tasks, the manifest declares the BATCH → task_ids mapping:

```json
{
  "schema_version": "sentiment-v1",
  "batch_size": 4,
  "n_batches": 2,
  "total_tasks": 8,
  "batches": [
    { "story_id": "BATCH-001", "input_file": ".ralph/stories/batch-001.jsonl",
      "n_tasks": 4, "task_ids": ["s_001","s_002","s_003","s_004"] },
    { "story_id": "BATCH-002", "input_file": ".ralph/stories/batch-002.jsonl",
      "n_tasks": 4, "task_ids": ["s_005","s_006","s_007","s_008"] }
  ]
}
```

`n_batches` and `total_tasks` are authoritative; `audit_shards.py` and
`merge_shards.py` cross-check every per-shard fact against them.

### Sharding configuration

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

## Critical concepts

**Fresh context per iteration.** Every story is implemented by a brand-new
Claude Code instance. Memory between iterations is git history,
`progress.txt`, and `prd.json` — nothing else. If a piece of context must
survive, write it to `progress.txt` or commit it.

**Right-sized stories.** A story has to fit in one context window. Too
big → the LLM runs out of context before finalize, the gate fails, the
loop retries, and the failure repeats. Split until each story is one
unit of work (one file pair, one batch of N tasks, one DB migration).

**Disjoint write scope is what makes parallelism safe.** The N shards
are guaranteed to be disjoint at the *task_id / batch* level by
`render_shards.py`+`audit_shards.py`. The write-boundary hook
(`scripts_4x/hooks/deny_outside_shard.py`) extends that guarantee to the
*filesystem* level: a subagent in shard `a` cannot write into
`.ralph-shard-b/`, `.ralph/`, or arbitrary `*.py` files. Hook is gated by
`RALPH_SHARD_ROOT`, so single-process mode pays no cost.

**Streaming-only writes.** Every verdict row goes to disk via
`append_verdict.py`, which validates schema + schema_version + reason
length, then `fsync`s. Crash at minute 25 cannot lose minute 1. *Never*
let a subagent accumulate verdicts in memory and bulk-write at the end —
that's the failure mode this rule was built to prevent.

**Promise token protocol.** Every iteration's stdout must end with
`<promise>YIELD</promise>` (more stories left), `<promise>COMPLETE</promise>`
(all done), or `<promise>VIOLATION</promise>` (max_attempts exceeded).
The loop driver cross-checks the token against `prd.json` so the agent
can't fake completion.

**Auto-recover.** If `attempts > 0` and the whole batch is already in
`seen_task_ids` but the gate keeps failing, `prepare` drops verdicts +
seen for that batch and redoes it. Built to handle the "subagent wrote
everything but the gate failed on a downstream check" loop.

**5-gate set-theoretic shard audit.** `audit_shards.py` enforces:
(1) per-shard count == manifest.total_tasks; (2) shard *i* ∩ shard *j* = ∅;
(3) ⋃ shards = baseline input set; (4) every batch symlink in a shard
points back at the baseline; (5) per-shard `prd.json` BATCH ids match
that shard's `manifest.json`. Fail-stop on any drift.

---

## Operations / debugging

### Reading the dashboard

```bash
./scripts_4x/dashboard.sh a b c d
```

Per-shard line: ASCII progress bar (passes / total), 429 (rate-limit) hit
count from `run.log`, recent reason average length from `verdicts.jsonl`,
PID alive/dead. The dashboard writes nothing — safe to leave under `watch`.

### Where to look when something is off

| Symptom | First file to read |
|---------|-------------------|
| "Did this story pass?" | `<ROOT>/prd.json` (`passes`) and `<ROOT>/progress.txt` |
| "What did the agent actually write?" | `<ROOT>/state/verdicts.jsonl` |
| "What did the loop driver do?" | `<ROOT>/loop.log` |
| "What did the Claude Code agent print?" | `<ROOT>/run.log` |
| "Which story is locked right now?" | `<ROOT>/current_story.json` |
| "Did a shard hit the boundary hook?" | `<ROOT>/run.log` (look for `exit 2`) |

`<ROOT>` is `.ralph` in single-process mode and `.ralph-shard-X` in
sharded mode.

### When you hit a rate limit

`monitor_shards.py` will detect 429s, sleep `--rate-limit-wait-sec`, and
auto-restart. Manual recovery:

```bash
./scripts_4x/stop_shards.sh                    # SIGTERM all shards
./scripts_4x/recover_shards_after_limit.sh     # reset retry counters,
                                               # restore priorities, relaunch
```

### When a shard dies (DEGRADE)

Stop the dead shard(s) cleanly, drain unseen tasks to survivors, relaunch:

```bash
# 4-shard → 2-shard: c+d die, drain to a+b
./scripts_4x/stop_shards.sh c d
python3 scripts_4x/redistribute_remaining.py \
        --from-shards c,d --keep-shards a,b --target keep
python3 scripts_4x/audit_shards.py --num-shards 2
./scripts_4x/run_shards.sh a b

# Full stop → fall back to single-process baseline
./scripts_4x/stop_shards.sh
python3 scripts_4x/redistribute_remaining.py \
        --from-shards a,b,c,d --target baseline
./ralph.sh
```

`redistribute_remaining.py` refuses to run while any source/target shard
is alive — it prevents concurrent RMW on `prd.json` / `manifest.json`.

### When the dirty-tree gate halts the loop

`ralph.sh` refuses to run a new iteration if the working tree has
uncommitted changes outside the shard's own state directory (single-process
mode looks at the whole tree; shard mode is shard-aware). This stops the
loop from quietly committing leftover edits. Resolve manually:

```bash
git status
# either commit the change, stash it, or revert — then rerun
```

### Stopping cleanly

```bash
./scripts_4x/stop_shards.sh              # SIGTERM all (default a b c d)
./scripts_4x/stop_shards.sh c d          # only c+d
GRACE_SEC=10 ./scripts_4x/stop_shards.sh # longer grace before SIGKILL
```

---

## Extending: write your own acceptance gate

`acceptance.py` is the single source of truth. Add a new gate type by:

1. Implementing the `Gate` protocol (a class with `validate(story, root) -> GateResult`).
2. Registering it in `_BUILTIN_GATES` keyed by `type` string.
3. Reference it from `prd.json` via `acceptanceGate.type` (per-story) or
   `acceptance.default_gate.type` (project-wide).

```python
# .ralph/scripts/acceptance.py (sketch)
class MyGate:
    def __init__(self, config: dict):
        self.threshold = config["threshold"]

    def validate(self, story: dict, root: Path) -> GateResult:
        ...
        return GateResult(
            passed=ok,
            failures=[] if ok else ["why it failed"],
            diagnostics={"score": score},
        )

_BUILTIN_GATES = {
    "jsonl_schema": JsonlSchemaGate,
    "command": CommandGate,
    "composite": CompositeGate,
    "my_gate": MyGate,        # ← register here
}
```

Tests for new gates live in `tests/test_acceptance_gates.py`. Aim to
cover both PASS and FAIL paths.

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
