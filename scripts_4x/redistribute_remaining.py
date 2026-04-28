#!/usr/bin/env python3
"""DEGRADE path: migrate unseen tasks from drained shards to surviving ones.

Spec:
    --from-shards a,b,c,d           shard ids to drain (must be already stopped)
    --keep-shards a,b               surviving shard ids (when --target=keep)
    --target {keep|baseline}        where the unseen tasks go
    --batch-size 50                 new BATCH slice size

Hard constraints:
    1. Already-written verdicts in from_shards are kept (later merge_shards.py
       merges across all shard verdicts).
    2. New BATCH ids start at max(target.BATCH-id) + 1 to avoid collisions.
    3. Migration refuses to run while any from_shard or keep_shard is alive
       (avoids concurrent RMW on prd/manifest).
    4. After migration, audit_shards.py should be re-run.

Exit 0 = migration done; exit 1 on conflict / live process / path collision.

Usage:
    # 4-shard → 2-shard DEGRADE: stop c+d, keep a+b
    ./scripts_4x/stop_shards.sh c d
    python3 scripts_4x/redistribute_remaining.py \\
            --from-shards c,d --keep-shards a,b --target keep
    ./scripts_4x/run_shards.sh a b

    # full stop → fall back to baseline single process
    ./scripts_4x/stop_shards.sh
    python3 scripts_4x/redistribute_remaining.py \\
            --from-shards a,b,c,d --target baseline
    ./ralph.sh
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".ralph" / "scripts"))
from acceptance import load_default_verdict_schema  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = REPO_ROOT / ".ralph"
DEFAULT_BATCH_SIZE = 50


def fail(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"[redistribute] {msg}")


def shard_root(sid: str) -> Path:
    return REPO_ROOT / f".ralph-shard-{sid}"


def parse_shard_list(s: str) -> list[str]:
    out = [tok.strip() for tok in s.split(",") if tok.strip()]
    for x in out:
        if not (len(x) == 1 and x.isalpha() and x.islower()):
            fail(f"shard id '{x}' must be a single lowercase letter")
    return out


def assert_shard_dead(sid: str) -> None:
    instance = shard_root(sid) / ".instance"
    if not instance.exists():
        return
    try:
        pid_str = instance.read_text().split(":", 1)[0].strip()
        pid = int(pid_str)
    except (ValueError, OSError):
        info(f"shard {sid}: .instance unparseable ({instance}), treating as dead lock")
        return
    if pid <= 0:
        return
    try:
        os.kill(pid, 0)
        fail(
            f"shard {sid}: pid={pid} still alive, refuse to migrate.\n"
            f"  Run: ./scripts_4x/stop_shards.sh {sid}"
        )
    except ProcessLookupError:
        info(f"shard {sid}: pid={pid} dead, lock is stale (stop_shards.sh will clean it)")


def read_json(p: Path):
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(p: Path, data) -> None:
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(p)


def collect_unseen_tasks_from_shard(sid: str, id_field: str) -> list[dict]:
    manifest_p = shard_root(sid) / "stories" / "manifest.json"
    if not manifest_p.exists():
        fail(f"shard {sid}: manifest {manifest_p} not found")
    manifest = read_json(manifest_p)

    seen_p = shard_root(sid) / "state" / "seen_task_ids.json"
    seen: set[str] = set()
    if seen_p.exists():
        try:
            seen = set(read_json(seen_p))
        except json.JSONDecodeError:
            fail(f"shard {sid}: {seen_p} corrupt")

    unseen: list[dict] = []
    for batch in manifest["batches"]:
        input_file = REPO_ROOT / batch["input_file"]
        if not input_file.exists():
            fail(f"shard {sid}: batch input {input_file} missing (broken symlink?)")
        with input_file.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                task = json.loads(line)
                if task[id_field] not in seen:
                    unseen.append(task)
    return unseen


def next_batch_index(target_root: Path) -> int:
    manifest_p = target_root / "stories" / "manifest.json"
    if not manifest_p.exists():
        fail(f"target manifest {manifest_p} not found")
    manifest = read_json(manifest_p)
    max_idx = 0
    for b in manifest["batches"]:
        sid = b["story_id"]
        idx = int(sid.split("-")[1])
        if idx > max_idx:
            max_idx = idx
    return max_idx + 1


def split_to_batches(tasks: list[dict], batch_size: int) -> list[list[dict]]:
    return [tasks[i : i + batch_size] for i in range(0, len(tasks), batch_size)]


def write_batch_jsonl(target_root: Path, batch_idx: int, batch_tasks: list[dict]) -> Path:
    stories_dir = target_root / "stories"
    stories_dir.mkdir(parents=True, exist_ok=True)
    p = stories_dir / f"batch-{batch_idx:03d}.jsonl"
    if p.exists() or p.is_symlink():
        fail(f"target {p} already exists; refuse to overwrite (duplicate migration?)")
    with p.open("w", encoding="utf-8") as f:
        for t in batch_tasks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    return p


def append_to_target_manifest(
    target_root: Path,
    new_batches: list[tuple[int, list[dict], Path]],
    id_field: str,
) -> None:
    manifest_p = target_root / "stories" / "manifest.json"
    manifest = read_json(manifest_p)
    for idx, tasks, jsonl_path in new_batches:
        rel_input = jsonl_path.relative_to(REPO_ROOT)
        manifest["batches"].append(
            {
                "story_id": f"BATCH-{idx:03d}",
                "input_file": str(rel_input),
                "n_tasks": len(tasks),
                "task_ids": [t[id_field] for t in tasks],
            }
        )
    manifest["n_batches"] = len(manifest["batches"])
    manifest["total_tasks"] = sum(b["n_tasks"] for b in manifest["batches"])
    if "redistribute_log" not in manifest:
        manifest["redistribute_log"] = []
    manifest["redistribute_log"].append(
        {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "added_batches": [f"BATCH-{idx:03d}" for idx, _, _ in new_batches],
            "added_tasks": sum(len(t) for _, t, _ in new_batches),
        }
    )
    atomic_write_json(manifest_p, manifest)


def append_to_target_prd(
    target_root: Path,
    new_batches: list[tuple[int, list[dict], Path]],
) -> None:
    prd_p = target_root / "prd.json"
    prd = read_json(prd_p)
    max_priority = max((s.get("priority", 0) for s in prd["userStories"]), default=0)
    for idx, tasks, jsonl_path in new_batches:
        max_priority += 1
        rel_input = str(jsonl_path.relative_to(REPO_ROOT))
        prd["userStories"].append(
            {
                "id": f"BATCH-{idx:03d}",
                "title": f"BATCH-{idx:03d} · {len(tasks)} tasks (redistribute)",
                "description": (
                    f"Read {rel_input} ({len(tasks)} task lines), produce a verdict per "
                    f"task per the configured acceptance gate. Origin: "
                    f"redistribute_remaining.py migration from a drained shard."
                ),
                "storyType": "data-fill",
                "entryCount": len(tasks),
                "modifies": [
                    f"{target_root.name}/state/verdicts.jsonl",
                    f"{target_root.name}/state/seen_task_ids.json",
                    f"{target_root.name}/prd.json",
                    f"{target_root.name}/progress.txt",
                ],
                "creates": [],
                "acceptanceCriteria": [
                    f"verdicts.jsonl contains exactly {len(tasks)} rows for this batch",
                    "verdict task_ids set == manifest.batches[].task_ids set",
                    "schema_version equals the project default",
                    "reason length ratio meets the configured threshold",
                ],
                "priority": max_priority,
                "passes": False,
                "notes": "Injected by redistribute_remaining.py",
                "attempts": 0,
                "timeoutSeconds": 2400,
            }
        )
    atomic_write_json(prd_p, prd)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--from-shards",
        type=str,
        required=True,
        help="comma-separated shard ids to drain (e.g. 'c,d')",
    )
    parser.add_argument(
        "--keep-shards",
        type=str,
        default="",
        help="comma-separated shard ids to keep (only with --target=keep)",
    )
    parser.add_argument("--target", choices=["keep", "baseline"], required=True)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--dry-run", action="store_true", help="compute migration plan, write nothing"
    )
    args = parser.parse_args()

    from_shards = parse_shard_list(args.from_shards)
    keep_shards: list[str] = []
    if args.target == "keep":
        if not args.keep_shards:
            fail("--target=keep requires --keep-shards")
        keep_shards = parse_shard_list(args.keep_shards)
        overlap = set(from_shards) & set(keep_shards)
        if overlap:
            fail(f"--from-shards and --keep-shards must not overlap (clash: {sorted(overlap)})")

    info(
        f"DEGRADE plan: from={from_shards}, keep={keep_shards or '(N/A)'}, "
        f"target={args.target}, batch_size={args.batch_size}"
    )

    # Read default id field from baseline prd
    baseline_prd = read_json(BASELINE_ROOT / "prd.json")
    _, vschema = load_default_verdict_schema(baseline_prd)
    id_field = vschema.id_field

    for sid in from_shards:
        assert_shard_dead(sid)
    info("✓ all from_shards confirmed dead")

    for sid in keep_shards:
        assert_shard_dead(sid)
    if keep_shards:
        info("✓ all keep_shards confirmed dead (safe to mutate prd/manifest)")

    pooled: list[dict] = []
    seen_ids: set[str] = set()
    per_shard_drain: dict[str, int] = {}
    for sid in from_shards:
        unseen = collect_unseen_tasks_from_shard(sid, id_field)
        per_shard_drain[sid] = len(unseen)
        for t in unseen:
            if t[id_field] in seen_ids:
                continue
            seen_ids.add(t[id_field])
            pooled.append(t)
    info(f"unseen per from_shard: {per_shard_drain} → pooled {len(pooled)} tasks total")

    if not pooled:
        info("no unseen tasks to redistribute, exiting")
        return

    batches = split_to_batches(pooled, args.batch_size)
    info(f"split into {len(batches)} new batches (last batch {len(batches[-1])} tasks)")

    if args.target == "baseline":
        targets = [BASELINE_ROOT]
    else:
        targets = [shard_root(s) for s in keep_shards]

    plan: dict[Path, list[list[dict]]] = {t: [] for t in targets}
    for i, batch_tasks in enumerate(batches):
        target = targets[i % len(targets)]
        plan[target].append(batch_tasks)

    info("distribution plan:")
    for target, batch_groups in plan.items():
        info(
            f"  → {target.relative_to(REPO_ROOT)}: {len(batch_groups)} new batches, "
            f"{sum(len(b) for b in batch_groups)} tasks"
        )

    if args.dry_run:
        info("dry-run: no writes")
        return

    for target_root, batch_groups in plan.items():
        if not batch_groups:
            continue
        next_idx = next_batch_index(target_root)
        new_batches: list[tuple[int, list[dict], Path]] = []
        for batch_tasks in batch_groups:
            jsonl_path = write_batch_jsonl(target_root, next_idx, batch_tasks)
            new_batches.append((next_idx, batch_tasks, jsonl_path))
            next_idx += 1
        append_to_target_manifest(target_root, new_batches, id_field)
        append_to_target_prd(target_root, new_batches)
        info(
            f"  ✓ {target_root.relative_to(REPO_ROOT)}: appended "
            f"{[f'BATCH-{idx:03d}' for idx, _, _ in new_batches]}"
        )

    info("")
    info(f"✅ redistribute complete — {len(pooled)} tasks across {len(batches)} new batches")
    info("")
    info("Next steps:")
    if args.target == "baseline":
        info("  ./ralph.sh                                                 # resume baseline")
    else:
        info(f"  ./scripts_4x/run_shards.sh {' '.join(keep_shards)}                       # resume keep_shards")
    info(
        "  python3 scripts_4x/audit_shards.py --num-shards N           # audit (N = keep count)"
    )


if __name__ == "__main__":
    main()
