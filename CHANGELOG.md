# Changelog

All notable changes to **parallel-ralph** are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-04-28

Initial public release. Extracted and sanitized from a private LLM-eval
project that drove thousands of LLM-as-judge classifications in parallel.

The ralph loop pattern itself comes from [Geoffrey Huntley's
"ralph"](https://ghuntley.com/ralph/); this harness was directly
inspired by [snarktank/ralph](https://github.com/snarktank/ralph), which
provided a concrete reference implementation of the loop we built on
top of.

### Added
- **Set-theoretic shard verification** — `render_shards.py` splits a baseline
  manifest into N disjoint shards (auto-split, `--splits "a-b,c-d,..."`,
  or `--splits-file shard-splits.json`); `audit_shards.py` enforces the
  5-layer correctness contract (count match, pairwise disjoint, union ==
  baseline, batch symlinks point at baseline, prd ↔ manifest BATCH-id
  agreement). Fail-stop on any drift.
- **Pluggable acceptance gates** (`.ralph/scripts/acceptance.py`):
  - `JsonlSchemaGate` — parameterized 5-gate labeling check
    (count / schema / valid_qa / reason length ratio / distinct_qa).
  - `CommandGate` — runs any shell command, PASS iff exit 0.
  - `CompositeGate` — AND-of multiple gates.
  - Per-story override via `acceptanceGate` in `prd.json`; project-wide
    default via `acceptance.default_gate`.
- **Streaming-only verdict writes** — `append_verdict.py` validates schema +
  schema_version + reason length, appends with `fsync`, and updates
  `seen_task_ids.json` atomically. A crash at minute 25 cannot lose
  minute 1's work.
- **Promise token protocol** — `<promise>YIELD/COMPLETE/VIOLATION</promise>`
  cross-validated against `prd.json`, anti-cheat against false completion.
- **Write-boundary `PreToolUse` hook**
  (`scripts_4x/hooks/deny_outside_shard.py`) — env-var-gated
  (`RALPH_SHARD_ROOT`) enforcement that subagents cannot write outside
  their shard directory. Project-specific deny prefixes via
  `RALPH_HARD_DENY_PREFIXES`. No-op in single-process mode.
- **Graceful degradation (DEGRADE path)**
  (`scripts_4x/redistribute_remaining.py`) — when a shard dies, drain its
  unseen tasks to surviving shards or fall back to single-process baseline.
- **Cause-classifying monitor** (`scripts_4x/monitor_shards.py`) —
  auto-restart for rate-limit / dirty-tree failures; alert and stop on
  unclassified ones.
- **Atomic state hygiene** — temp+rename writes, PID + liveness instance
  lock, stale-lock reclamation, idempotent symlink rebuilds.
- **Auto-recover** in `run_batch.py prepare` — if `attempts > 0` and the
  whole batch is already in `seen_task_ids`, drop verdicts + seen for this
  batch and redo (the prior attempt wrote everything but the gate failed).
- **Examples**:
  - `examples/sample-jsonl/` — JsonlSchemaGate demo with 2 batches × 4
    sentiment-classification tasks.
  - `examples/openapi-impl/` — CommandGate demo where each story is a
    code-implementation gated by `pytest`.
- **Test suite** — 55 unit/integration tests covering acceptance gates,
  shard rendering, shard auditing, write-boundary hook, and helper
  validation.
- **MIT license**, pure-stdlib runtime, `pytest` + `ruff` for dev.

[Unreleased]: https://github.com/d0m999/parallel-ralph/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/d0m999/parallel-ralph/releases/tag/v0.1.0
