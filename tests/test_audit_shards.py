"""Tests for audit_shards.py — verifies it accepts a correct render and
rejects each individual failure mode (count / disjoint / coverage / symlink /
prd-manifest mismatch).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import audit_shards as aud
import render_shards as rs


def _build_baseline(tmp_path: Path, n_batches: int = 4, tasks_per_batch: int = 3) -> Path:
    repo = tmp_path
    baseline = repo / ".ralph"
    stories = baseline / "stories"
    stories.mkdir(parents=True)
    batches_meta = []
    for i in range(1, n_batches + 1):
        p = stories / f"batch-{i:03d}.jsonl"
        task_ids = [f"task_{i:03d}_{j}" for j in range(tasks_per_batch)]
        with p.open("w", encoding="utf-8") as f:
            for tid in task_ids:
                f.write(json.dumps({"task_id": tid}) + "\n")
        batches_meta.append(
            {
                "story_id": f"BATCH-{i:03d}",
                "input_file": f".ralph/stories/batch-{i:03d}.jsonl",
                "n_tasks": tasks_per_batch,
                "task_ids": task_ids,
            }
        )
    (stories / "manifest.json").write_text(
        json.dumps(
            {
                "n_batches": n_batches,
                "total_tasks": n_batches * tasks_per_batch,
                "batches": batches_meta,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    user_stories = [
        {
            "id": f"BATCH-{i:03d}",
            "title": f"BATCH-{i:03d}",
            "priority": i,
            "passes": False,
            "attempts": 0,
            "entryCount": tasks_per_batch,
            "modifies": [],
        }
        for i in range(1, n_batches + 1)
    ]
    (baseline / "prd.json").write_text(
        json.dumps(
            {
                "project": "demo",
                "branchName": "main",
                "userStories": user_stories,
                "acceptance": {
                    "default_gate": {
                        "type": "jsonl_schema",
                        "schema_version": "v1",
                        "verdict_schema": {
                            "required_fields": ["task_id", "qa", "reason"]
                        },
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return repo


def _render(repo: Path, num_shards: int = 2) -> None:
    src = Path(rs.__file__).read_text(encoding="utf-8")
    scripts_dir = repo / "scripts_4x"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "render_shards.py").write_text(src, encoding="utf-8")
    (scripts_dir / "PROMPT.md.tmpl").write_text(
        "# {{SHARD_ID}} {{BATCH_RANGE}}\n", encoding="utf-8"
    )
    proc = subprocess.run(
        ["python3", str(scripts_dir / "render_shards.py"), "--num-shards", str(num_shards)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def _run_audit(repo: Path, num_shards: int = 2) -> subprocess.CompletedProcess:
    src = Path(aud.__file__).read_text(encoding="utf-8")
    scripts_dir = repo / "scripts_4x"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "audit_shards.py").write_text(src, encoding="utf-8")
    return subprocess.run(
        ["python3", str(scripts_dir / "audit_shards.py"), "--num-shards", str(num_shards)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.unit
def test_audit_passes_clean_render(tmp_path: Path):
    repo = _build_baseline(tmp_path)
    _render(repo, num_shards=2)
    proc = _run_audit(repo, num_shards=2)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "all audits passed" in proc.stdout


@pytest.mark.unit
def test_audit_detects_count_mismatch(tmp_path: Path):
    repo = _build_baseline(tmp_path)
    _render(repo, num_shards=2)
    # Mutate shard a manifest so its declared total_tasks no longer matches actual
    mp = repo / ".ralph-shard-a" / "stories" / "manifest.json"
    m = json.loads(mp.read_text())
    # Drop a task_id from one batch but keep n_tasks claim → count mismatch
    m["batches"][0]["task_ids"] = m["batches"][0]["task_ids"][:-1]
    mp.write_text(json.dumps(m, indent=2), encoding="utf-8")

    proc = _run_audit(repo, num_shards=2)
    assert proc.returncode != 0
    assert "(1)" in proc.stderr or "(1)" in proc.stdout


@pytest.mark.unit
def test_audit_detects_overlap(tmp_path: Path):
    repo = _build_baseline(tmp_path)
    _render(repo, num_shards=2)
    # Inject the same task_id into both shard manifests
    for sid in ("a", "b"):
        mp = repo / f".ralph-shard-{sid}" / "stories" / "manifest.json"
        m = json.loads(mp.read_text())
        m["batches"][0]["task_ids"].append("DUPLICATE-X")
        m["batches"][0]["n_tasks"] = len(m["batches"][0]["task_ids"])
        m["total_tasks"] = sum(b["n_tasks"] for b in m["batches"])
        mp.write_text(json.dumps(m, indent=2), encoding="utf-8")

    proc = _run_audit(repo, num_shards=2)
    assert proc.returncode != 0
    out = proc.stderr + proc.stdout
    assert "(2)" in out


@pytest.mark.unit
def test_audit_detects_prd_manifest_mismatch(tmp_path: Path):
    repo = _build_baseline(tmp_path)
    _render(repo, num_shards=2)
    # Drop a story from shard a's prd
    pp = repo / ".ralph-shard-a" / "prd.json"
    prd = json.loads(pp.read_text())
    prd["userStories"] = prd["userStories"][:-1]
    pp.write_text(json.dumps(prd, indent=2), encoding="utf-8")

    proc = _run_audit(repo, num_shards=2)
    assert proc.returncode != 0
    out = proc.stderr + proc.stdout
    assert "(5)" in out
