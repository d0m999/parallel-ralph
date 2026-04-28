#!/usr/bin/env python3
"""Merge N shard verdicts.jsonl into a single eval_results/ artifact + audit.

Pipeline:
    1. read each .ralph-shard-X/state/verdicts.jsonl, accumulate
    2. per-shard count check vs that shard's manifest.total_tasks
    3. dedup_check: total verdict count + uniqueness of id field
    4. baseline alignment: verdict id set == baseline manifest input set
    5. random-sample schema audit: REQUIRED_FIELDS / qa-field ∈ valid /
       reason ≥ min_chars / schema_version == configured
    6. write merged JSONL in baseline batch order

Optional:
    --partial PATH    if you have an additional already-merged JSONL prefix
                      (e.g. an earlier partial run with a different
                      schema_version), it is concatenated before the merged
                      shard verdicts and the combined output is written too.

Exit 0 = all-pass + write outputs; exit 1 = any failure (no files written).

Usage:
    python3 scripts_4x/merge_shards.py --num-shards 4 --out-dir eval_results
    python3 scripts_4x/merge_shards.py --num-shards 4 --dry-run
    python3 scripts_4x/merge_shards.py --num-shards 4 \\
            --partial eval_results/older_partial.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import string
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".ralph" / "scripts"))
from acceptance import VerdictSchema, load_default_verdict_schema  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = REPO_ROOT / ".ralph"
SHARD_LETTERS = list(string.ascii_lowercase)

SAMPLE_SIZE = 50
SAMPLE_SEED = 42


def fail(msgs: list[str]) -> None:
    for m in msgs:
        print(f"FAIL: {m}", file=sys.stderr)
    sys.exit(1)


def read_json(p: Path):
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        sys.exit(f"FATAL: {p} not found")
    out: list[dict] = []
    with p.open(encoding="utf-8") as f:
        for ln_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                sys.exit(f"FATAL: {p} line {ln_no}: malformed JSON: {e}")
            if rec.get("__meta__"):
                continue
            out.append(rec)
    return out


def collect_baseline_ids_in_order(baseline_manifest: dict) -> list[str]:
    out: list[str] = []
    for batch in baseline_manifest["batches"]:
        input_path = REPO_ROOT / batch["input_file"]
        if not input_path.exists():
            sys.exit(f"FATAL: baseline batch input {input_path} not found")
        with input_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                out.append(rec["task_id"])
    return out


def sample_audit(
    verdicts: list[dict],
    schema_version: str,
    schema: VerdictSchema,
) -> list[str]:
    rng = random.Random(SAMPLE_SEED)
    n = min(SAMPLE_SIZE, len(verdicts))
    sample = rng.sample(verdicts, n) if n else []
    failures: list[str] = []
    required = set(schema.required_fields) | {"schema_version"}
    valid_qa = set(schema.valid_qa)
    for i, v in enumerate(sample):
        tid = v.get(schema.id_field, f"<row-{i}>")
        missing = required - set(v.keys())
        if missing:
            failures.append(f"sample[{i}] {tid}: missing fields {sorted(missing)}")
            continue
        if v.get("schema_version") != schema_version:
            failures.append(
                f"sample[{i}] {tid}: schema_version='{v.get('schema_version')}' "
                f"!= '{schema_version}'"
            )
        if v.get(schema.qa_field) not in valid_qa:
            failures.append(
                f"sample[{i}] {tid}: {schema.qa_field}='{v.get(schema.qa_field)}' "
                f"not in {sorted(valid_qa)}"
            )
        reason = v.get(schema.reason_field, "")
        if not isinstance(reason, str) or len(reason) < schema.min_reason_chars:
            failures.append(
                f"sample[{i}] {tid}: {schema.reason_field} length "
                f"{len(reason) if isinstance(reason, str) else '<not-str>'} "
                f"< {schema.min_reason_chars}"
            )
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-shards", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true", help="audit only, no writes")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "eval_results",
        help="output directory for merged JSONL (default: eval_results/)",
    )
    parser.add_argument(
        "--out-name",
        type=str,
        default=None,
        help="output filename stem (default: derived from baseline manifest)",
    )
    parser.add_argument(
        "--partial",
        type=Path,
        default=None,
        help=(
            "Optional pre-existing JSONL with verdicts of a different "
            "schema_version; concatenated before the merged shard output. "
            "Triggers a second 'combined' file."
        ),
    )
    parser.add_argument(
        "--date",
        type=str,
        default=datetime.now().strftime("%Y%m%d"),
        help="filename date suffix (default: today YYYYMMDD)",
    )
    args = parser.parse_args()

    if args.num_shards < 2 or args.num_shards > len(SHARD_LETTERS):
        sys.exit(f"FATAL: --num-shards must be in [2, {len(SHARD_LETTERS)}]")
    shard_ids = SHARD_LETTERS[: args.num_shards]
    print(f"[merge_shards] num_shards={args.num_shards}, shards={shard_ids}, dry_run={args.dry_run}")

    baseline_manifest_p = BASELINE_ROOT / "stories" / "manifest.json"
    if not baseline_manifest_p.exists():
        sys.exit(f"FATAL: baseline manifest {baseline_manifest_p} not found")
    baseline_manifest = read_json(baseline_manifest_p)
    baseline_total = baseline_manifest.get("total_tasks", -1)

    baseline_prd = read_json(BASELINE_ROOT / "prd.json")
    schema_version, schema = load_default_verdict_schema(baseline_prd)

    failures: list[str] = []
    all_verdicts: list[dict] = []

    # 1+2: read each shard, count vs manifest
    for sid in shard_ids:
        p = REPO_ROOT / f".ralph-shard-{sid}" / "state" / "verdicts.jsonl"
        manifest_p = REPO_ROOT / f".ralph-shard-{sid}" / "stories" / "manifest.json"
        verdicts = load_jsonl(p)
        shard_manifest = read_json(manifest_p)
        want = shard_manifest["total_tasks"]
        actual = len(verdicts)
        if actual != want:
            failures.append(
                f"(2) shard {sid}: {actual} verdicts != expected {want} "
                f"(from manifest)"
            )
        else:
            print(f"  ✓ (2) shard {sid}: {actual} verdicts (matches manifest)")
        all_verdicts.extend(verdicts)

    # 3: dedup_check on id field
    id_field = schema.id_field
    ids = [v.get(id_field, "") for v in all_verdicts]
    if isinstance(baseline_total, int) and len(ids) != baseline_total:
        failures.append(
            f"(3a) total verdicts {len(ids)} != baseline.total_tasks {baseline_total}"
        )
    else:
        print(f"  ✓ (3a) total verdicts = {len(ids)}")
    if len(set(ids)) != len(ids):
        seen: dict[str, int] = {}
        dups: list[str] = []
        for tid in ids:
            seen[tid] = seen.get(tid, 0) + 1
            if seen[tid] == 2:
                dups.append(tid)
                if len(dups) >= 3:
                    break
        dup_count = len(ids) - len(set(ids))
        failures.append(f"(3b) {dup_count} duplicate {id_field}s (e.g. {dups})")
    else:
        print(f"  ✓ (3b) all {id_field} values unique")

    # 4: baseline alignment
    baseline_order = collect_baseline_ids_in_order(baseline_manifest)
    baseline_ids = set(baseline_order)
    actual_set = set(ids)
    missing = baseline_ids - actual_set
    extra = actual_set - baseline_ids
    if missing or extra:
        failures.append(
            f"(4) baseline mismatch: missing {len(missing)} "
            f"(e.g. {next(iter(missing), 'n/a')}), "
            f"extra {len(extra)} (e.g. {next(iter(extra), 'n/a')})"
        )
    else:
        print("  ✓ (4) verdict ids ↔ baseline input set agree")

    # 5: random-sample schema audit
    sample_failures = sample_audit(all_verdicts, schema_version, schema)
    if sample_failures:
        failures.extend([f"(5) {sf}" for sf in sample_failures])
    else:
        print(
            f"  ✓ (5) random sample (n={SAMPLE_SIZE}, seed={SAMPLE_SEED}) "
            f"schema audit pass"
        )

    if failures:
        print("")
        fail(failures)

    if args.dry_run:
        print("")
        print("✅ all 5 audit layers passed — dry-run, no files written")
        return

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.out_name or baseline_manifest.get("project") or "merged"
    merged_p = out_dir / f"{stem}_merged_{args.date}.jsonl"

    by_id = {v[id_field]: v for v in all_verdicts}
    with merged_p.open("w", encoding="utf-8") as f:
        for tid in baseline_order:
            f.write(json.dumps(by_id[tid], ensure_ascii=False) + "\n")
    print(f"  → wrote {merged_p} ({len(baseline_order)} verdicts)")

    if args.partial:
        if not args.partial.exists():
            sys.exit(f"FATAL: --partial {args.partial} not found")
        prefix = load_jsonl(args.partial)
        combined_p = out_dir / f"{stem}_combined_{args.date}.jsonl"
        with combined_p.open("w", encoding="utf-8") as f:
            for v in prefix:
                f.write(json.dumps(v, ensure_ascii=False) + "\n")
            for tid in baseline_order:
                f.write(json.dumps(by_id[tid], ensure_ascii=False) + "\n")
        print(
            f"  → wrote {combined_p} ({len(prefix)} prefix + "
            f"{len(baseline_order)} merged)"
        )

    print("")
    print(f"✅ merge complete — 5 audit layers passed, output under {out_dir}")


if __name__ == "__main__":
    main()
