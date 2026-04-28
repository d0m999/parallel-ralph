#!/usr/bin/env python3
"""Audit N rendered ralph shards for set-theoretic correctness.

Checks:
    1. each shard's task count matches what its manifest declares
    2. pairwise disjoint (any two shards share no task_ids)
    3. union == baseline input set (read from baseline manifest's
       per-batch input files, not hardcoded)
    4. every batch jsonl is either a symlink to baseline or a real file
       (the latter is produced by redistribute_remaining.py)
    5. shard prd.json userStories ↔ shard manifest batches BATCH-id sets
       are equal

Exit 0 on all-pass; exit 1 with diagnostics on any failure.

Usage:
    python3 scripts_4x/audit_shards.py --num-shards 4
"""

from __future__ import annotations

import argparse
import json
import string
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = REPO_ROOT / ".ralph"
SHARD_LETTERS = list(string.ascii_lowercase)


def fail(msgs: list[str]) -> None:
    for m in msgs:
        print(f"FAIL: {m}", file=sys.stderr)
    sys.exit(1)


def read_json(p: Path):
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def shard_root(shard_id: str) -> Path:
    return REPO_ROOT / f".ralph-shard-{shard_id}"


def load_shard_manifest(shard_id: str) -> dict:
    p = shard_root(shard_id) / "stories" / "manifest.json"
    if not p.exists():
        sys.exit(
            f"FATAL: shard {shard_id} manifest {p} not found — "
            f"did render_shards.py run?"
        )
    return read_json(p)


def load_shard_prd(shard_id: str) -> dict:
    p = shard_root(shard_id) / "prd.json"
    if not p.exists():
        sys.exit(f"FATAL: shard {shard_id} prd {p} not found")
    return read_json(p)


def collect_baseline_task_ids() -> set[str]:
    """Read baseline manifest's per-batch input files and union all task_ids."""
    manifest_p = BASELINE_ROOT / "stories" / "manifest.json"
    if not manifest_p.exists():
        sys.exit(f"FATAL: baseline manifest {manifest_p} not found")
    manifest = read_json(manifest_p)
    ids: set[str] = set()
    for batch in manifest["batches"]:
        input_path = REPO_ROOT / batch["input_file"]
        if not input_path.exists():
            sys.exit(f"FATAL: baseline batch input {input_path} not found")
        with input_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                ids.add(rec["task_id"])
    expected = manifest.get("total_tasks")
    if isinstance(expected, int) and len(ids) != expected:
        sys.exit(
            f"FATAL: baseline collected {len(ids)} unique task_ids, "
            f"manifest claims total_tasks={expected}"
        )
    return ids


def collect_shard_task_ids(shard_id: str) -> set[str]:
    manifest = load_shard_manifest(shard_id)
    ids: set[str] = set()
    dups: list[str] = []
    for batch in manifest["batches"]:
        for tid in batch["task_ids"]:
            if tid in ids:
                dups.append(tid)
            ids.add(tid)
    if dups:
        sys.exit(
            f"FATAL: shard {shard_id} manifest has internal duplicate task_ids "
            f"({len(dups)} dup): {dups[:3]}..."
        )
    return ids


def verify_symlinks(shard_id: str) -> list[str]:
    manifest = load_shard_manifest(shard_id)
    bad: list[str] = []
    for batch in manifest["batches"]:
        link = REPO_ROOT / batch["input_file"]
        if link.is_symlink():
            target_abs = link.resolve()
            expected_filename = link.name
            expected_target = (
                REPO_ROOT / ".ralph" / "stories" / expected_filename
            ).resolve()
            if target_abs != expected_target:
                bad.append(
                    f"shard {shard_id}: {link} → {target_abs}, expected → {expected_target}"
                )
            if not target_abs.exists():
                bad.append(
                    f"shard {shard_id}: symlink {link} points to missing {target_abs}"
                )
        elif link.is_file():
            pass  # real file written by redistribute_remaining.py — existence is sufficient
        else:
            bad.append(f"shard {shard_id}: {link} not found (neither symlink nor file)")
    return bad


def verify_prd_manifest_consistency(shard_id: str) -> list[str]:
    manifest = load_shard_manifest(shard_id)
    prd = load_shard_prd(shard_id)
    manifest_ids = {b["story_id"] for b in manifest["batches"]}
    prd_ids = {s["id"] for s in prd["userStories"]}
    issues: list[str] = []
    if manifest_ids != prd_ids:
        missing_in_prd = manifest_ids - prd_ids
        extra_in_prd = prd_ids - manifest_ids
        if missing_in_prd:
            issues.append(
                f"shard {shard_id}: manifest has, prd does not: "
                f"{sorted(missing_in_prd)[:3]}..."
            )
        if extra_in_prd:
            issues.append(
                f"shard {shard_id}: prd has, manifest does not: "
                f"{sorted(extra_in_prd)[:3]}..."
            )
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-shards", type=int, required=True)
    args = parser.parse_args()

    if args.num_shards < 2 or args.num_shards > len(SHARD_LETTERS):
        sys.exit(
            f"FATAL: --num-shards must be in [2, {len(SHARD_LETTERS)}], got {args.num_shards}"
        )
    shard_ids = SHARD_LETTERS[: args.num_shards]
    print(f"[audit_shards] num_shards={args.num_shards}, shards={shard_ids}")

    failures: list[str] = []
    shard_id_sets: dict[str, set[str]] = {}

    # 1. Per-shard count check (vs that shard's own manifest.total_tasks)
    for sid in shard_ids:
        ids = collect_shard_task_ids(sid)
        shard_id_sets[sid] = ids
        manifest = load_shard_manifest(sid)
        want = manifest.get("total_tasks", -1)
        actual = len(ids)
        if actual != want:
            failures.append(f"(1) shard {sid}: task count {actual} != manifest.total_tasks {want}")
        else:
            print(f"  ✓ (1) shard {sid}: {actual} tasks (matches manifest.total_tasks)")

    # 2. Pairwise disjoint
    for i, a in enumerate(shard_ids):
        for b in shard_ids[i + 1 :]:
            inter = shard_id_sets[a] & shard_id_sets[b]
            if inter:
                failures.append(
                    f"(2) shard {a} ∩ shard {b}: {len(inter)} duplicate task_ids "
                    f"(e.g. {next(iter(inter))})"
                )
            else:
                print(f"  ✓ (2) shard {a} ∩ shard {b} = ∅")

    # 3. union == baseline
    baseline_ids = collect_baseline_task_ids()
    union: set[str] = set()
    for sid in shard_ids:
        union |= shard_id_sets[sid]
    missing = baseline_ids - union
    extra = union - baseline_ids
    if missing or extra:
        failures.append(
            f"(3) union vs baseline: missing {len(missing)} "
            f"(e.g. {next(iter(missing), 'n/a')}), "
            f"extra {len(extra)} (e.g. {next(iter(extra), 'n/a')})"
        )
    else:
        print(f"  ✓ (3) union of shards = baseline input set ({len(baseline_ids)} tasks)")

    # 4. Symlinks
    for sid in shard_ids:
        bad = verify_symlinks(sid)
        if bad:
            failures.extend([f"(4) {b}" for b in bad])
        else:
            print(f"  ✓ (4) shard {sid}: all batch symlinks point at baseline")

    # 5. prd ↔ manifest BATCH-id consistency
    for sid in shard_ids:
        issues = verify_prd_manifest_consistency(sid)
        if issues:
            failures.extend([f"(5) {i}" for i in issues])
        else:
            print(f"  ✓ (5) shard {sid}: prd.json ↔ manifest.json BATCH-id sets agree")

    if failures:
        print("")
        fail(failures)

    print("")
    print(f"✅ all audits passed for {args.num_shards}-shard layout")


if __name__ == "__main__":
    main()
