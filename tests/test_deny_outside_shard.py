"""Tests for the PreToolUse write-boundary hook.

The hook is invoked as a subprocess receiving a Claude Code tool-call JSON
on stdin. We exercise it the same way to be honest about exit codes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "scripts_4x" / "hooks" / "deny_outside_shard.py"


def _invoke(
    payload: dict,
    *,
    shard_root: str | None = ".ralph-shard-a",
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin"}
    if shard_root is not None:
        env["RALPH_SHARD_ROOT"] = shard_root
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["python3", str(HOOK_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _write_payload(path: str) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": path}}


@pytest.mark.unit
def test_no_op_when_env_unset():
    proc = _invoke(_write_payload("anywhere.py"), shard_root=None)
    assert proc.returncode == 0


@pytest.mark.unit
def test_allows_inside_shard_state():
    proc = _invoke(_write_payload(".ralph-shard-a/state/verdicts.jsonl"))
    assert proc.returncode == 0, proc.stderr


@pytest.mark.unit
def test_allows_tmp():
    proc = _invoke(_write_payload("/tmp/scratch.txt"))
    assert proc.returncode == 0, proc.stderr


@pytest.mark.unit
def test_denies_baseline_ralph():
    proc = _invoke(_write_payload(".ralph/state/verdicts.jsonl"))
    assert proc.returncode == 2
    assert "baseline deny" in proc.stderr


@pytest.mark.unit
def test_denies_other_shard():
    proc = _invoke(_write_payload(".ralph-shard-b/state/verdicts.jsonl"))
    assert proc.returncode == 2
    assert "cross-shard deny" in proc.stderr


@pytest.mark.unit
def test_denies_python_modification():
    proc = _invoke(_write_payload(".ralph-shard-a/scripts/something.py"))
    assert proc.returncode == 2
    assert "code deny" in proc.stderr


@pytest.mark.unit
def test_denies_arbitrary_outside_path():
    proc = _invoke(_write_payload("foo/bar.txt"))
    assert proc.returncode == 2
    assert "default deny" in proc.stderr


@pytest.mark.unit
def test_project_red_line_prefixes():
    proc = _invoke(
        _write_payload("eval_results/out.jsonl"),
        extra_env={"RALPH_HARD_DENY_PREFIXES": "eval_results/:docs/"},
    )
    assert proc.returncode == 2
    assert "project red-line deny" in proc.stderr


@pytest.mark.unit
def test_read_tools_pass_through():
    proc = _invoke(
        {"tool_name": "Read", "tool_input": {"file_path": ".ralph/whatever"}}
    )
    assert proc.returncode == 0


@pytest.mark.unit
def test_no_path_passes_through():
    proc = _invoke({"tool_name": "Write", "tool_input": {}})
    assert proc.returncode == 0
