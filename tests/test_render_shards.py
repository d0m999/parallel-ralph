"""Tests for render_shards.py.

Focus on the pure-logic helpers (splits parsing + invariants). Full I/O
rendering is exercised via a tmp_path baseline fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import render_shards as rs


# ---- splits parsing ----------------------------------------------------


@pytest.mark.unit
def test_parse_splits_arg_basic():
    assert rs.parse_splits_arg("1-13,14-26,27-39,40-53") == [
        (1, 13), (14, 26), (27, 39), (40, 53)
    ]


@pytest.mark.unit
def test_parse_splits_arg_single():
    assert rs.parse_splits_arg("1-10") == [(1, 10)]


@pytest.mark.unit
def test_parse_splits_arg_rejects_non_int():
    with pytest.raises(SystemExit):
        rs.parse_splits_arg("1-x")


@pytest.mark.unit
def test_parse_splits_arg_rejects_inverted_range():
    with pytest.raises(SystemExit):
        rs.parse_splits_arg("10-5")


@pytest.mark.unit
def test_parse_splits_arg_rejects_missing_dash():
    with pytest.raises(SystemExit):
        rs.parse_splits_arg("1,2,3")


# ---- auto_split --------------------------------------------------------


@pytest.mark.unit
def test_auto_split_even_division():
    assert rs.auto_split(4, 12) == [(1, 3), (4, 6), (7, 9), (10, 12)]


@pytest.mark.unit
def test_auto_split_with_remainder_goes_to_last():
    # 53 / 4 = 13 rem 1 → last shard gets the extra
    assert rs.auto_split(4, 53) == [(1, 13), (14, 26), (27, 39), (40, 53)]


@pytest.mark.unit
def test_auto_split_two_shards():
    assert rs.auto_split(2, 53) == [(1, 26), (27, 53)]


@pytest.mark.unit
def test_auto_split_too_few_batches():
    with pytest.raises(SystemExit):
        rs.auto_split(5, 3)


# ---- validate_splits ---------------------------------------------------


@pytest.mark.unit
def test_validate_splits_valid_complete_disjoint():
    rs.validate_splits([(1, 13), (14, 26), (27, 39), (40, 53)], 53)


@pytest.mark.unit
def test_validate_splits_rejects_overlap():
    with pytest.raises(SystemExit):
        rs.validate_splits([(1, 13), (10, 20)], 20)


@pytest.mark.unit
def test_validate_splits_rejects_gap():
    with pytest.raises(SystemExit):
        rs.validate_splits([(1, 5), (10, 15)], 15)


@pytest.mark.unit
def test_validate_splits_rejects_out_of_range():
    with pytest.raises(SystemExit):
        rs.validate_splits([(1, 13), (14, 100)], 53)


@pytest.mark.unit
def test_validate_splits_rejects_zero_start():
    with pytest.raises(SystemExit):
        rs.validate_splits([(0, 13)], 13)


# ---- build_shard_specs --------------------------------------------------


@pytest.mark.unit
def test_build_shard_specs_assigns_letters():
    specs = rs.build_shard_specs(3, [(1, 5), (6, 10), (11, 15)])
    assert [s.shard_id for s in specs] == ["a", "b", "c"]
    assert [s.batch_range_str for s in specs] == [
        "BATCH-001..BATCH-005",
        "BATCH-006..BATCH-010",
        "BATCH-011..BATCH-015",
    ]


@pytest.mark.unit
def test_build_shard_specs_mismatch_raises():
    with pytest.raises(SystemExit):
        rs.build_shard_specs(3, [(1, 5), (6, 10)])


# ---- shard subset filters ----------------------------------------------


@pytest.mark.unit
def test_shard_subset_of_batches_exact_match():
    manifest = {
        "batches": [
            {"story_id": f"BATCH-{i:03d}", "n_tasks": 1, "task_ids": [f"t{i}"], "input_file": "x"}
            for i in range(1, 11)
        ]
    }
    spec = rs.ShardSpec("a", Path(".ralph-shard-a"), 3, 6)
    out = rs.shard_subset_of_batches(manifest, spec)
    assert [b["story_id"] for b in out] == [f"BATCH-{i:03d}" for i in range(3, 7)]


# ---- end-to-end render in tmp_path -------------------------------------


def _build_baseline(tmp_path: Path, n_batches: int = 4, tasks_per_batch: int = 5) -> Path:
    """Create a minimal baseline .ralph layout under tmp_path; return tmp_path."""
    repo = tmp_path
    baseline = repo / ".ralph"
    stories = baseline / "stories"
    stories.mkdir(parents=True)
    # batch input files
    batches_meta = []
    for i in range(1, n_batches + 1):
        p = stories / f"batch-{i:03d}.jsonl"
        task_ids = [f"task_{i:03d}_{j}" for j in range(tasks_per_batch)]
        with p.open("w", encoding="utf-8") as f:
            for tid in task_ids:
                f.write(json.dumps({"task_id": tid, "payload": "x"}) + "\n")
        batches_meta.append(
            {
                "story_id": f"BATCH-{i:03d}",
                "input_file": f".ralph/stories/batch-{i:03d}.jsonl",
                "n_tasks": tasks_per_batch,
                "task_ids": task_ids,
            }
        )
    manifest = {
        "n_batches": n_batches,
        "total_tasks": n_batches * tasks_per_batch,
        "batches": batches_meta,
        "batch_size": tasks_per_batch,
    }
    (stories / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    user_stories = []
    for i in range(1, n_batches + 1):
        user_stories.append(
            {
                "id": f"BATCH-{i:03d}",
                "title": f"BATCH-{i:03d}",
                "priority": i,
                "passes": False,
                "attempts": 0,
                "entryCount": tasks_per_batch,
                "modifies": [".ralph/state/verdicts.jsonl"],
                "creates": [],
            }
        )
    prd = {
        "project": "demo",
        "branchName": "main",
        "description": "demo",
        "userStories": user_stories,
        "acceptance": {
            "default_gate": {
                "type": "jsonl_schema",
                "schema_version": "demo-v1",
                "verdict_schema": {
                    "required_fields": ["task_id", "qa", "reason"],
                    "min_reason_chars": 50,
                    "reason_long_ratio_min": 0.9,
                    "distinct_qa_min": 2,
                    "distinct_qa_min_small": 1,
                    "small_batch_threshold": 10,
                },
            }
        },
    }
    (baseline / "prd.json").write_text(
        json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return repo


def _run_render(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Run render_shards.py against an isolated tmp_path repo via env override.

    render_shards.py uses module-level REPO_ROOT computed from __file__. To
    keep the test hermetic we copy the script into tmp_path and run it there.
    """
    src = Path(rs.__file__).read_text(encoding="utf-8")
    # Mirror layout: tmp_path/scripts_4x/render_shards.py + tmp_path/.ralph/
    scripts_dir = repo / "scripts_4x"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "render_shards.py").write_text(src, encoding="utf-8")
    # Provide a tiny PROMPT.md.tmpl
    (scripts_dir / "PROMPT.md.tmpl").write_text(
        "# {{SHARD_ID}} {{BATCH_RANGE}} {{N_TASKS}} tasks\n", encoding="utf-8"
    )
    cmd = [
        "python3",
        str(scripts_dir / "render_shards.py"),
        *args,
    ]
    return subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, check=False)


@pytest.mark.unit
def test_render_full_disjoint_complete(tmp_path: Path):
    repo = _build_baseline(tmp_path, n_batches=4, tasks_per_batch=5)
    proc = _run_render(repo, ["--num-shards", "2"])
    assert proc.returncode == 0, proc.stderr + proc.stdout
    # Verify shard manifests exist + ranges are disjoint + cover baseline
    shard_a = json.loads((repo / ".ralph-shard-a" / "stories" / "manifest.json").read_text())
    shard_b = json.loads((repo / ".ralph-shard-b" / "stories" / "manifest.json").read_text())
    a_ids = {tid for b in shard_a["batches"] for tid in b["task_ids"]}
    b_ids = {tid for b in shard_b["batches"] for tid in b["task_ids"]}
    assert not (a_ids & b_ids)
    assert len(a_ids) + len(b_ids) == 4 * 5

    # Symlinks point to baseline
    link_a = repo / ".ralph-shard-a" / "stories" / "batch-001.jsonl"
    assert link_a.is_symlink()
    target = (link_a.parent / os.readlink(link_a)).resolve()
    assert target == (repo / ".ralph" / "stories" / "batch-001.jsonl").resolve()


@pytest.mark.unit
def test_render_explicit_splits(tmp_path: Path):
    repo = _build_baseline(tmp_path, n_batches=4, tasks_per_batch=3)
    proc = _run_render(
        repo, ["--num-shards", "2", "--splits", "1-1,2-4"]
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    shard_a = json.loads((repo / ".ralph-shard-a" / "stories" / "manifest.json").read_text())
    shard_b = json.loads((repo / ".ralph-shard-b" / "stories" / "manifest.json").read_text())
    assert len(shard_a["batches"]) == 1
    assert len(shard_b["batches"]) == 3


@pytest.mark.unit
def test_render_refuses_when_state_dirty(tmp_path: Path):
    repo = _build_baseline(tmp_path, n_batches=4, tasks_per_batch=2)
    proc = _run_render(repo, ["--num-shards", "2"])
    assert proc.returncode == 0

    # Add a verdict line; default re-render should refuse
    verdict_p = repo / ".ralph-shard-a" / "state" / "verdicts.jsonl"
    verdict_p.write_text('{"task_id":"x"}\n', encoding="utf-8")

    proc2 = _run_render(repo, ["--num-shards", "2"])
    assert proc2.returncode != 0
    assert "refuse to wipe" in (proc2.stderr + proc2.stdout)


@pytest.mark.unit
def test_render_force_overwrites_state(tmp_path: Path):
    repo = _build_baseline(tmp_path, n_batches=4, tasks_per_batch=2)
    _run_render(repo, ["--num-shards", "2"])
    verdict_p = repo / ".ralph-shard-a" / "state" / "verdicts.jsonl"
    verdict_p.write_text('{"task_id":"x"}\n', encoding="utf-8")

    proc = _run_render(repo, ["--num-shards", "2", "--force"])
    assert proc.returncode == 0
    assert verdict_p.read_text(encoding="utf-8") == ""
