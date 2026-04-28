#!/usr/bin/env python3
"""Stream-append one verdict to <ROOT>/state/verdicts.jsonl + RMW seen_task_ids.json.

Used by the subagent: pipe a single-line JSON verdict to stdin, this helper
validates schema, appends with fsync, and updates seen set atomically.

Per-task usage (mandatory streaming pattern):

    # Default ROOT=.ralph (single-process baseline, backwards compatible)
    echo '{"task_id":"...", ...}' | python3 .ralph/scripts/append_verdict.py

    # Shard mode: explicitly target a shard root
    echo '{"task_id":"...", ...}' | \\
      python3 .ralph/scripts/append_verdict.py --shard-root .ralph-shard-a

PROHIBITED: accumulating verdicts in Python lists/dicts before bulk-writing.
The streaming pattern ensures a crash never costs more than the in-flight
verdict.

Schema enforcement comes from prd.json's `acceptance.default_gate.verdict_schema`
plus `acceptance.default_gate.schema_version` — see
`.ralph/scripts/acceptance.py` for the format.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance import load_default_verdict_schema  # noqa: E402


def fail(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stream-append one verdict to <ROOT>/state/verdicts.jsonl"
    )
    parser.add_argument(
        "--shard-root",
        type=Path,
        default=Path(".ralph"),
        help="ralph root (default .ralph; shard mode: .ralph-shard-X)",
    )
    args = parser.parse_args()

    root = args.shard_root
    verdicts_path = root / "state" / "verdicts.jsonl"
    seen_path = root / "state" / "seen_task_ids.json"
    state_dir = root / "state"
    prd_path = root / "prd.json"

    if not state_dir.exists():
        fail(
            f"state dir {state_dir} not found — "
            f"is shard-root={root} rendered? "
            f"(default mode uses .ralph; shard mode requires render_shards.py first)"
        )
    if not prd_path.exists():
        fail(f"prd.json not found at {prd_path}")

    with prd_path.open(encoding="utf-8") as f:
        prd = json.load(f)
    schema_version, vschema = load_default_verdict_schema(prd)

    raw = sys.stdin.read().strip()
    if not raw:
        fail("stdin is empty; pipe a single-line verdict JSON")

    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"stdin is not valid JSON: {e}")

    if not isinstance(verdict, dict):
        fail("verdict must be a JSON object")

    required = set(vschema.required_fields) | {"schema_version"}
    missing = required - verdict.keys()
    if missing:
        fail(f"missing required fields: {sorted(missing)}")

    if verdict["schema_version"] != schema_version:
        fail(
            f"schema_version must be '{schema_version}', "
            f"got '{verdict['schema_version']}'"
        )

    qa = verdict.get(vschema.qa_field)
    if qa not in vschema.valid_qa:
        fail(
            f"{vschema.qa_field} must be one of {list(vschema.valid_qa)}, "
            f"got '{qa}'"
        )

    reason = verdict.get(vschema.reason_field)
    if not isinstance(reason, str):
        fail(f"{vschema.reason_field} must be a string")
    if len(reason) < vschema.min_reason_chars:
        fail(
            f"{vschema.reason_field} length {len(reason)} < {vschema.min_reason_chars} "
            f"(reason-length-ratio gate requires ≥{vschema.reason_long_ratio_min:.0%} "
            f"of rows to clear; this row would lower the ratio)"
        )

    task_id = verdict.get(vschema.id_field)
    if not isinstance(task_id, str) or not task_id:
        fail(f"{vschema.id_field} must be a non-empty string")

    seen: list[str] = []
    if seen_path.exists():
        try:
            seen = json.load(seen_path.open())
        except json.JSONDecodeError as e:
            fail(f"{seen_path} is corrupt: {e}")
    if task_id in seen:
        fail(
            f"{vschema.id_field} '{task_id}' already in {seen_path} — "
            f"refuse to double-write (idempotency gate)"
        )

    line = json.dumps(verdict, ensure_ascii=False)
    with verdicts_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())

    new_seen = sorted({*seen, task_id})
    tmp = seen_path.with_suffix(f".json.tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(new_seen, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, seen_path)

    total = sum(1 for _ in verdicts_path.open())
    print(json.dumps({
        "appended": True,
        vschema.id_field: task_id,
        "verdicts_total": total,
        "reason_chars": len(reason),
        "shard_root": str(root),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
