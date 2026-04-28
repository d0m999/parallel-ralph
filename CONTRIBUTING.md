# Contributing to parallel-ralph

Thanks for your interest in contributing! This project is small, has a
single-purpose scope (run N disjoint ralph loops in parallel under Claude
Code), and aims to stay that way. Bug reports, doc fixes, and focused
feature PRs are all welcome.

## Ground rules

- **Pure stdlib at runtime.** No third-party dependencies in the harness
  itself. `pytest` and `ruff` are the only dev-time deps. Examples
  (`examples/openapi-impl/`) may have their own requirements; that's fine.
- **Shard determinism.** `render_shards.py` + `audit_shards.py` must keep
  the 5-layer correctness contract (count match, pairwise disjoint,
  union == baseline, batch symlinks ⇒ baseline, prd ↔ manifest agreement).
  If you change rendering, `audit_shards.py` must still pass.
- **Streaming-only writes.** Every verdict row hits disk via
  `append_verdict.py` (which `fsync`s). No bulk-writes, no in-memory
  accumulation, no bypassing the helper.
- **Acceptance gates are pluggable.** Add a new gate by implementing the
  `Gate` protocol in `.ralph/scripts/acceptance.py` and registering it in
  `_BUILTIN_GATES`. Don't fork the validator into a sibling script.
- **Tests first.** Anything that touches `acceptance.py`, `render_shards.py`,
  `audit_shards.py`, or the write-boundary hook needs a corresponding test.
  Target ≥ 80% coverage for changed modules.

## Dev setup

```bash
git clone https://github.com/OWNER/parallel-ralph.git
cd parallel-ralph
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

You should see `55 passed` (or higher, as the suite grows).

## Running the harness locally

The fastest way to exercise the full prepare → subagent → validate →
finalize chain without spawning a real LLM:

```bash
./examples/sample-jsonl/init.sh
echo '{"id":"BATCH-001"}' > .ralph/current_story.json
python3 .ralph/scripts/run_batch.py prepare
# pipe 4 synthetic verdicts through .ralph/scripts/append_verdict.py
python3 .ralph/scripts/run_batch.py validate
python3 .ralph/scripts/run_batch.py finalize
```

For sharded mode:

```bash
./examples/sample-jsonl/init.sh
python3 scripts_4x/render_shards.py --num-shards 2
python3 scripts_4x/audit_shards.py --num-shards 2
./scripts_4x/run_shards.sh a b
./scripts_4x/dashboard.sh a b
```

## Pull request workflow

1. Open an issue first for non-trivial changes — saves both of us time
   if scope/approach needs discussion.
2. Fork, branch off `main` with a descriptive name (`fix/audit-symlink-loop`,
   `feat/yaml-gate`, etc.).
3. Keep commits focused. One logical change per commit when reasonable.
4. Run the full suite locally:
   ```bash
   pytest -q
   ruff check .
   ```
5. Update `CHANGELOG.md` under `## [Unreleased]` with a one-line entry.
6. Open the PR with:
   - **What changed** — 1–3 bullets.
   - **Why** — link the issue or explain the motivation.
   - **Test plan** — how you verified it.

## Coding style

- 4-space indent, type hints on new public functions.
- `ruff` for lint. Run `ruff check . --fix` before pushing.
- Small files (200–400 lines typical, 800 max). High cohesion, low coupling.
- No silent error swallowing — surface failures explicitly.
- No hardcoded values — config via `prd.json`, env vars, or CLI flags.

## Reporting bugs

Open an issue with:
- Repro steps (ideally a minimal `prd.json` + `manifest.json`).
- Expected vs. actual behavior.
- Output of `python3 scripts_4x/audit_shards.py --num-shards N` if shard-related.
- Python version (`python3 --version`) and OS.

## Security issues

Please **don't** open a public issue for security vulnerabilities.
Email the maintainer privately first.

## License

By contributing, you agree your contributions will be licensed under the
[MIT License](./LICENSE).
