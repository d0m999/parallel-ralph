#!/usr/bin/env python3
"""Render N parallel ralph shard directories from a .ralph baseline.

Splits the baseline manifest into N shards (any N ≥ 2). Each shard gets:

    .ralph-shard-{a,b,c,...}/
    ├── PROMPT.md                 ← scripts_4x/PROMPT.md.tmpl rendered
    ├── prd.json                  ← shard subset of baseline prd.json
    ├── progress.txt              ← initialized template
    ├── stories/
    │   ├── manifest.json         ← shard subset of baseline manifest
    │   └── batch-NNN.jsonl       ← symlink → ../../.ralph/stories/batch-NNN.jsonl
    └── state/
        ├── verdicts.jsonl        ← empty
        └── seen_task_ids.json    ← []

Re-run safety:
  default                          state non-empty OR git tracked → FATAL
  --refresh-template-only          rewrite PROMPT.md / prd.json / manifest only
  --force                          full wipe + reinit (dangerous)

Splits configuration:
  --splits "1-13,14-26,27-39,40-53"
                                   explicit per-shard inclusive batch ranges
                                   (1-based; range count must equal --num-shards)
  --splits-file path/to.json       same data from a JSON file:
                                   {"splits": [[1,13],[14,26],...]}
  no --splits                      auto-compute even split

The baseline's `n_batches` / `total_tasks` come from the baseline manifest;
they are not hardcoded.

Usage:
    python3 scripts_4x/render_shards.py --num-shards 4
    python3 scripts_4x/render_shards.py --num-shards 3 --refresh-template-only
    python3 scripts_4x/render_shards.py --num-shards 2 --force
    python3 scripts_4x/render_shards.py --num-shards 4 \\
            --splits "1-13,14-26,27-39,40-53"
"""

from __future__ import annotations

import argparse
import json
import os
import string
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ---- Constants ----------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = REPO_ROOT / ".ralph"
TMPL_FILE = REPO_ROOT / "scripts_4x" / "PROMPT.md.tmpl"
SHARD_LETTERS = list(string.ascii_lowercase)


@dataclass(frozen=True)
class ShardSpec:
    shard_id: str
    shard_root: Path  # repo-relative
    batch_start: int
    batch_end: int

    @property
    def n_batches(self) -> int:
        return self.batch_end - self.batch_start + 1

    @property
    def batch_range_str(self) -> str:
        return f"BATCH-{self.batch_start:03d}..BATCH-{self.batch_end:03d}"


# ---- Helpers ------------------------------------------------------------


def fail(msg: str, code: int = 1) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(code)


def info(msg: str) -> None:
    print(f"[render_shards] {msg}")


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


def write_text(p: Path, content: str) -> None:
    p.write_text(content, encoding="utf-8")


def git_tracked_files_under(rel_dir: Path) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "ls-files", str(rel_dir)],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return []
    return [ln for ln in out.splitlines() if ln]


def count_verdict_lines(p: Path) -> int:
    if not p.exists():
        return 0
    with p.open(encoding="utf-8") as f:
        return sum(1 for ln in f if ln.strip())


# ---- Splits parsing -----------------------------------------------------


def parse_splits_arg(splits_arg: str) -> list[tuple[int, int]]:
    """Parse '1-13,14-26,27-39,40-53' into [(1,13),(14,26),(27,39),(40,53)]."""
    out: list[tuple[int, int]] = []
    for tok in splits_arg.split(","):
        tok = tok.strip()
        if "-" not in tok:
            fail(f"--splits range must look like 'A-B', got '{tok}'")
        a, b = tok.split("-", 1)
        try:
            ai, bi = int(a), int(b)
        except ValueError:
            fail(f"--splits range must be integers, got '{tok}'")
        if ai > bi:
            fail(f"--splits range start > end: '{tok}'")
        out.append((ai, bi))
    return out


def load_splits_file(p: Path) -> list[tuple[int, int]]:
    data = read_json(p)
    raw = data.get("splits") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        fail(f"--splits-file: expected list of [start,end] pairs, got {type(raw).__name__}")
    out: list[tuple[int, int]] = []
    for item in raw:
        if not (isinstance(item, list) and len(item) == 2):
            fail(f"--splits-file: each entry must be [start,end] pair, got {item!r}")
        out.append((int(item[0]), int(item[1])))
    return out


def auto_split(num_shards: int, total_batches: int) -> list[tuple[int, int]]:
    """Even split rounded so the last shard absorbs the remainder."""
    base = total_batches // num_shards
    if base == 0:
        fail(f"num_shards={num_shards} > total_batches={total_batches}")
    out: list[tuple[int, int]] = []
    cursor = 1
    for i in range(num_shards):
        if i == num_shards - 1:
            end = total_batches
        else:
            end = cursor + base - 1
        out.append((cursor, end))
        cursor = end + 1
    return out


def validate_splits(splits: list[tuple[int, int]], total_batches: int) -> None:
    if not splits:
        fail("splits is empty")
    seen: set[int] = set()
    cursor = 0
    for i, (a, b) in enumerate(splits):
        if a < 1 or b > total_batches:
            fail(f"split {i} ({a}..{b}) out of range [1..{total_batches}]")
        if a > b:
            fail(f"split {i} ({a}..{b}) has start > end")
        if a <= cursor:
            fail(
                f"split {i} ({a}..{b}) overlaps or is out of order with previous "
                f"(cursor={cursor})"
            )
        for x in range(a, b + 1):
            if x in seen:
                fail(f"split {i} ({a}..{b}) overlaps existing splits at {x}")
            seen.add(x)
        cursor = b
    if seen != set(range(1, total_batches + 1)):
        missing = sorted(set(range(1, total_batches + 1)) - seen)
        fail(f"splits do not cover all batches; missing {missing[:5]}...")


# ---- Core rendering -----------------------------------------------------


def build_shard_specs(num_shards: int, splits: list[tuple[int, int]]) -> list[ShardSpec]:
    if num_shards != len(splits):
        fail(f"num_shards={num_shards} != len(splits)={len(splits)}")
    if num_shards > len(SHARD_LETTERS):
        fail(f"num_shards={num_shards} exceeds available shard letters ({len(SHARD_LETTERS)})")
    specs: list[ShardSpec] = []
    for i, (start, end) in enumerate(splits):
        sid = SHARD_LETTERS[i]
        specs.append(
            ShardSpec(
                shard_id=sid,
                shard_root=Path(f".ralph-shard-{sid}"),
                batch_start=start,
                batch_end=end,
            )
        )
    return specs


def assert_baseline_ready() -> tuple[dict, dict, int, int]:
    if not BASELINE_ROOT.exists():
        fail(f"baseline {BASELINE_ROOT} does not exist — run /ralph-init first")
    manifest_p = BASELINE_ROOT / "stories" / "manifest.json"
    prd_p = BASELINE_ROOT / "prd.json"
    if not manifest_p.exists():
        fail(f"baseline manifest {manifest_p} not found")
    if not prd_p.exists():
        fail(f"baseline prd {prd_p} not found")
    if not TMPL_FILE.exists():
        fail(f"PROMPT template {TMPL_FILE} not found")
    manifest = read_json(manifest_p)
    prd = read_json(prd_p)
    n_batches = manifest.get("n_batches")
    total_tasks = manifest.get("total_tasks")
    if not isinstance(n_batches, int) or n_batches <= 0:
        fail(f"baseline manifest.n_batches missing or invalid: {n_batches}")
    if not isinstance(total_tasks, int) or total_tasks <= 0:
        fail(f"baseline manifest.total_tasks missing or invalid: {total_tasks}")
    if len(manifest.get("batches", [])) != n_batches:
        fail(
            f"baseline manifest.batches has {len(manifest['batches'])} entries "
            f"but n_batches={n_batches}"
        )
    return manifest, prd, n_batches, total_tasks


def check_safety(spec: ShardSpec, mode: str) -> None:
    if mode == "force":
        return
    state_dir = REPO_ROOT / spec.shard_root / "state"
    verdicts_p = state_dir / "verdicts.jsonl"

    if mode == "default":
        n_verdicts = count_verdict_lines(verdicts_p)
        if n_verdicts > 0:
            fail(
                f"{state_dir} already has {n_verdicts} verdict lines, refuse to wipe.\n"
                f"  To intentionally reset, pass --force (will lose those verdicts).\n"
                f"  To refresh PROMPT.md / prd.json / manifest only, use --refresh-template-only."
            )
        tracked = git_tracked_files_under(spec.shard_root / "state")
        if tracked:
            fail(
                f"{spec.shard_root}/state has {len(tracked)} git-tracked file(s) "
                f"(e.g. {tracked[0]}), refuse to overwrite.\n"
                f"  --force skips this check explicitly."
            )

    if mode == "refresh-template-only":
        if not state_dir.exists():
            fail(
                f"{state_dir} does not exist — refresh-template-only requires the shard "
                f"to have been rendered before. For first-time render, do NOT pass "
                f"--refresh-template-only."
            )


def shard_subset_of_batches(manifest: dict, spec: ShardSpec) -> list[dict]:
    out: list[dict] = []
    for b in manifest["batches"]:
        story_id = b["story_id"]
        idx = int(story_id.split("-")[1])
        if spec.batch_start <= idx <= spec.batch_end:
            out.append(b)
    if len(out) != spec.n_batches:
        fail(
            f"shard {spec.shard_id}: expected {spec.n_batches} batches, got {len(out)}"
        )
    return out


def shard_subset_of_stories(prd: dict, spec: ShardSpec) -> list[dict]:
    out: list[dict] = []
    for s in prd["userStories"]:
        story_id = s["id"]
        idx = int(story_id.split("-")[1])
        if spec.batch_start <= idx <= spec.batch_end:
            out.append(s)
    return out


def render_shard_manifest(
    baseline_manifest: dict, spec: ShardSpec, batches_subset: list[dict]
) -> dict:
    rewritten: list[dict] = []
    for b in batches_subset:
        nb = dict(b)
        story_id = b["story_id"]
        idx = int(story_id.split("-")[1])
        nb["input_file"] = f"{spec.shard_root}/stories/batch-{idx:03d}.jsonl"
        rewritten.append(nb)

    n_tasks_total = sum(b["n_tasks"] for b in rewritten)
    out = {
        "shard_id": spec.shard_id,
        "shard_root": str(spec.shard_root),
        "batch_range": spec.batch_range_str,
        "total_tasks": n_tasks_total,
        "n_batches": len(rewritten),
        "batches": rewritten,
    }
    # Carry over any baseline-level metadata
    for k in ("source", "schema_version", "batch_size"):
        if k in baseline_manifest:
            out[k] = baseline_manifest[k]
    return out


def render_shard_prd(
    baseline_prd: dict, spec: ShardSpec, stories_subset: list[dict]
) -> dict:
    new_stories: list[dict] = []
    for new_priority, s in enumerate(
        sorted(stories_subset, key=lambda x: x["priority"]), start=1
    ):
        ns = dict(s)
        ns["modifies"] = [
            m.replace(".ralph/", f"{spec.shard_root}/", 1)
            if m.startswith(".ralph/")
            else m
            for m in s.get("modifies", [])
        ]
        ns["priority"] = new_priority
        ns["passes"] = False
        ns["attempts"] = 0
        new_stories.append(ns)

    base_project = baseline_prd.get("project", "ralph")
    base_description = baseline_prd.get("description", "")
    out = {
        "project": f"{base_project}-shard-{spec.shard_id}",
        "branchName": baseline_prd["branchName"],
        "description": (
            f"{base_description} · shard {spec.shard_id} ({spec.batch_range_str})"
            if base_description
            else f"shard {spec.shard_id} ({spec.batch_range_str})"
        ),
        "designDocs": baseline_prd.get("designDocs", []),
        "userStories": new_stories,
    }
    if "acceptance" in baseline_prd:
        out["acceptance"] = baseline_prd["acceptance"]
    return out


def render_shard_prompt(spec: ShardSpec, n_tasks: int) -> str:
    tmpl = TMPL_FILE.read_text(encoding="utf-8")
    return (
        tmpl.replace("{{SHARD_ID}}", spec.shard_id)
        .replace("{{SHARD_ROOT}}", str(spec.shard_root))
        .replace("{{BATCH_RANGE}}", spec.batch_range_str)
        .replace("{{N_BATCHES}}", str(spec.n_batches))
        .replace("{{N_TASKS}}", str(n_tasks))
        .replace("{{COMPLETE_THRESHOLD}}", str(spec.n_batches))
    )


PROGRESS_TEMPLATE = """## Codebase Patterns

(Generated by scripts_4x/render_shards.py. The Codebase Patterns block is
equivalent to baseline `.ralph/progress.txt`. `## Completed Stories` and
`## Current Blockers` are appended by run_batch.py finalize and the operator.)

- Helpers live at `.ralph/scripts/{{run_batch,append_verdict}}.py` (single
  source of truth, --shard-root forwards reads/writes).
- Shard root: {shard_root} ({batch_range}, {n_batches} batches, {n_tasks} tasks)
- Write boundary: PreToolUse hook `scripts_4x/hooks/deny_outside_shard.py`
  is activated by `RALPH_SHARD_ROOT={shard_root}`.
- Batch input: stories/batch-NNN.jsonl is a symlink → ../../.ralph/stories/batch-NNN.jsonl
- The acceptance gate is whatever `prd.json` configures (jsonl_schema /
  command / composite). See `.ralph/scripts/acceptance.py`.

## Completed Stories

## Current Blockers
"""


def link_batch_input(spec: ShardSpec, batches_subset: list[dict]) -> None:
    stories_dir = REPO_ROOT / spec.shard_root / "stories"
    stories_dir.mkdir(parents=True, exist_ok=True)
    for b in batches_subset:
        story_id = b["story_id"]
        idx = int(story_id.split("-")[1])
        link = stories_dir / f"batch-{idx:03d}.jsonl"
        target = Path("../..") / ".ralph" / "stories" / f"batch-{idx:03d}.jsonl"
        abs_target = REPO_ROOT / ".ralph" / "stories" / f"batch-{idx:03d}.jsonl"
        if not abs_target.exists():
            fail(f"baseline batch input {abs_target} not found")
        if link.is_symlink() or link.exists():
            if link.is_symlink():
                link.unlink()
            else:
                fail(f"{link} exists as a real file (not symlink); refuse to overwrite")
        link.symlink_to(target)


def render_one_shard(
    spec: ShardSpec,
    baseline_manifest: dict,
    baseline_prd: dict,
    mode: str,
) -> None:
    info(f"render shard {spec.shard_id}: {spec.batch_range_str} ({spec.n_batches} batches)")
    shard_root_abs = REPO_ROOT / spec.shard_root

    batches_subset = shard_subset_of_batches(baseline_manifest, spec)
    stories_subset = shard_subset_of_stories(baseline_prd, spec)
    n_tasks = sum(int(b["n_tasks"]) for b in batches_subset)

    shard_root_abs.mkdir(parents=True, exist_ok=True)
    state_dir = shard_root_abs / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    stories_dir = shard_root_abs / "stories"
    stories_dir.mkdir(parents=True, exist_ok=True)

    write_text(shard_root_abs / "PROMPT.md", render_shard_prompt(spec, n_tasks))
    new_prd = render_shard_prd(baseline_prd, spec, stories_subset)
    if mode == "refresh-template-only":
        existing_prd_p = shard_root_abs / "prd.json"
        if existing_prd_p.exists():
            existing_prd = read_json(existing_prd_p)
            existing_by_id = {s["id"]: s for s in existing_prd.get("userStories", [])}
            for s in new_prd["userStories"]:
                if s["id"] in existing_by_id:
                    s["passes"] = existing_by_id[s["id"]].get("passes", False)
                    s["attempts"] = existing_by_id[s["id"]].get("attempts", 0)
    atomic_write_json(shard_root_abs / "prd.json", new_prd)
    atomic_write_json(
        shard_root_abs / "stories" / "manifest.json",
        render_shard_manifest(baseline_manifest, spec, batches_subset),
    )

    progress_p = shard_root_abs / "progress.txt"
    if mode == "refresh-template-only" and progress_p.exists():
        info(f"  [refresh-template-only] keep existing {progress_p}")
    else:
        write_text(
            progress_p,
            PROGRESS_TEMPLATE.format(
                shard_root=spec.shard_root,
                batch_range=spec.batch_range_str,
                n_batches=spec.n_batches,
                n_tasks=n_tasks,
            ),
        )

    link_batch_input(spec, batches_subset)

    verdicts_p = state_dir / "verdicts.jsonl"
    seen_p = state_dir / "seen_task_ids.json"
    if mode == "refresh-template-only":
        if verdicts_p.exists():
            info(
                f"  [refresh-template-only] keep state "
                f"({count_verdict_lines(verdicts_p)} verdicts)"
            )
        else:
            verdicts_p.touch()
        if not seen_p.exists():
            atomic_write_json(seen_p, [])
    else:
        verdicts_p.write_text("", encoding="utf-8")
        atomic_write_json(seen_p, [])

    info(f"  ✓ {spec.shard_root}/ rendered ({n_tasks} tasks, {spec.n_batches} batches)")


# ---- Main ---------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--num-shards", type=int, required=True)
    parser.add_argument(
        "--splits",
        type=str,
        default=None,
        help="Inclusive batch ranges per shard, e.g. '1-13,14-26,27-39,40-53'.",
    )
    parser.add_argument(
        "--splits-file",
        type=Path,
        default=None,
        help="JSON file with {\"splits\": [[1,13],[14,26],...]}",
    )
    excl = parser.add_mutually_exclusive_group()
    excl.add_argument(
        "--force",
        action="store_true",
        help="full wipe + reinit state (dangerous; loses written verdicts)",
    )
    excl.add_argument(
        "--refresh-template-only",
        action="store_true",
        help="rewrite PROMPT.md / prd.json / manifest.json only; keep state + progress",
    )
    args = parser.parse_args()

    if args.num_shards < 2:
        fail(f"--num-shards must be ≥ 2, got {args.num_shards}")

    mode = (
        "force"
        if args.force
        else "refresh-template-only"
        if args.refresh_template_only
        else "default"
    )
    info(f"mode={mode}, num_shards={args.num_shards}")

    baseline_manifest, baseline_prd, n_batches, total_tasks = assert_baseline_ready()

    if args.splits and args.splits_file:
        fail("pass either --splits or --splits-file, not both")
    if args.splits:
        splits = parse_splits_arg(args.splits)
    elif args.splits_file:
        splits = load_splits_file(args.splits_file)
    else:
        splits = auto_split(args.num_shards, n_batches)
        info(f"auto-split: {splits}")

    validate_splits(splits, n_batches)
    specs = build_shard_specs(args.num_shards, splits)

    for spec in specs:
        check_safety(spec, mode)

    for spec in specs:
        render_one_shard(spec, baseline_manifest, baseline_prd, mode)

    total_tasks_check = 0
    for spec in specs:
        m = read_json(REPO_ROOT / spec.shard_root / "stories" / "manifest.json")
        total_tasks_check += m["total_tasks"]
    if total_tasks_check != total_tasks:
        fail(
            f"after render, cross-shard task total {total_tasks_check} "
            f"!= baseline {total_tasks}; splits configuration is broken"
        )
    info(
        f"✅ {len(specs)} shard(s) rendered, total {total_tasks_check} tasks match baseline.\n"
        f"   Next: python3 scripts_4x/audit_shards.py --num-shards {args.num_shards}"
    )


if __name__ == "__main__":
    main()
