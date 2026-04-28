"""Tests for the acceptance gate plugin module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acceptance import (
    CommandGate,
    CompositeGate,
    GateResult,
    JsonlSchemaGate,
    VerdictSchema,
    build_gate,
    load_default_verdict_schema,
    load_gate_for_story,
)


# ---- VerdictSchema ----------------------------------------------------


@pytest.mark.unit
def test_verdict_schema_defaults():
    s = VerdictSchema.from_dict({})
    assert s.id_field == "task_id"
    assert s.qa_field == "qa"
    assert s.reason_field == "reason"
    assert s.min_reason_chars == 150
    assert s.distinct_qa_threshold_for(50) == 2
    assert s.distinct_qa_threshold_for(33) == 1
    assert s.distinct_qa_threshold_for(20) == 1


@pytest.mark.unit
def test_verdict_schema_custom_threshold():
    s = VerdictSchema.from_dict(
        {"distinct_qa_min": 3, "distinct_qa_min_small": 2, "small_batch_threshold": 10}
    )
    assert s.distinct_qa_threshold_for(11) == 3
    assert s.distinct_qa_threshold_for(10) == 2


# ---- JsonlSchemaGate ---------------------------------------------------


def _setup_jsonl_state(tmp_path: Path, rows: list[dict]) -> Path:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    p = state / "verdicts.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


@pytest.mark.unit
def test_jsonl_schema_gate_passes_full_batch(tmp_path: Path):
    expected_ids = ["t1", "t2", "t3"]
    rows = [
        {
            "task_id": tid,
            "qa": ("yes" if i == 0 else "no"),
            "reason": "x" * 200,
            "schema_version": "v1",
        }
        for i, tid in enumerate(expected_ids)
    ]
    verdicts_path = _setup_jsonl_state(tmp_path, rows)

    gate = JsonlSchemaGate(
        schema_version="v1",
        verdict_schema=VerdictSchema.from_dict(
            {"required_fields": ["task_id", "qa", "reason"]}
        ),
    )
    story = {
        "id": "S1",
        "_context": {
            "manifest_batch": {"task_ids": expected_ids, "n_tasks": 3},
            "verdicts_path": str(verdicts_path),
        },
    }
    result = gate.validate(story, tmp_path)
    assert result.passed, result.failures


@pytest.mark.unit
def test_jsonl_schema_gate_fails_on_short_reason(tmp_path: Path):
    expected_ids = ["t1", "t2"]
    rows = [
        {"task_id": "t1", "qa": "yes", "reason": "x" * 200, "schema_version": "v1"},
        {"task_id": "t2", "qa": "no", "reason": "short", "schema_version": "v1"},
    ]
    verdicts_path = _setup_jsonl_state(tmp_path, rows)

    gate = JsonlSchemaGate("v1", VerdictSchema.from_dict({}))
    story = {
        "_context": {
            "manifest_batch": {"task_ids": expected_ids, "n_tasks": 2},
            "verdicts_path": str(verdicts_path),
        }
    }
    result = gate.validate(story, tmp_path)
    assert not result.passed
    assert any("(c)" in f for f in result.failures)


@pytest.mark.unit
def test_jsonl_schema_gate_fails_on_missing_id(tmp_path: Path):
    expected_ids = ["t1", "t2"]
    rows = [
        {"task_id": "t1", "qa": "yes", "reason": "x" * 200, "schema_version": "v1"},
    ]
    verdicts_path = _setup_jsonl_state(tmp_path, rows)

    gate = JsonlSchemaGate("v1", VerdictSchema.from_dict({}))
    story = {
        "_context": {
            "manifest_batch": {"task_ids": expected_ids, "n_tasks": 2},
            "verdicts_path": str(verdicts_path),
        }
    }
    result = gate.validate(story, tmp_path)
    assert not result.passed
    assert any("(a)" in f or "(b)" in f for f in result.failures)


@pytest.mark.unit
def test_jsonl_schema_gate_fails_on_wrong_schema_version(tmp_path: Path):
    rows = [
        {"task_id": "t1", "qa": "yes", "reason": "x" * 200, "schema_version": "wrong"},
        {"task_id": "t2", "qa": "no", "reason": "x" * 200, "schema_version": "wrong"},
    ]
    verdicts_path = _setup_jsonl_state(tmp_path, rows)

    gate = JsonlSchemaGate("v1", VerdictSchema.from_dict({}))
    story = {
        "_context": {
            "manifest_batch": {"task_ids": ["t1", "t2"], "n_tasks": 2},
            "verdicts_path": str(verdicts_path),
        }
    }
    result = gate.validate(story, tmp_path)
    assert not result.passed
    assert any("(schema)" in f for f in result.failures)


@pytest.mark.unit
def test_jsonl_schema_gate_distinct_qa_small_batch(tmp_path: Path):
    # 3-task batch, all "yes" — should pass because small_batch_threshold defaults
    # to 33 → distinct_qa_min_small = 1.
    rows = [
        {"task_id": f"t{i}", "qa": "yes", "reason": "x" * 200, "schema_version": "v1"}
        for i in range(3)
    ]
    verdicts_path = _setup_jsonl_state(tmp_path, rows)

    gate = JsonlSchemaGate("v1", VerdictSchema.from_dict({}))
    story = {
        "_context": {
            "manifest_batch": {"task_ids": [f"t{i}" for i in range(3)], "n_tasks": 3},
            "verdicts_path": str(verdicts_path),
        }
    }
    result = gate.validate(story, tmp_path)
    assert result.passed, result.failures


# ---- CommandGate -------------------------------------------------------


@pytest.mark.unit
def test_command_gate_passes_on_zero_exit(tmp_path: Path):
    gate = CommandGate(command="true")
    result = gate.validate({}, tmp_path)
    assert result.passed
    assert result.diagnostics["exit_code"] == 0


@pytest.mark.unit
def test_command_gate_fails_on_nonzero_exit(tmp_path: Path):
    gate = CommandGate(command="false")
    result = gate.validate({}, tmp_path)
    assert not result.passed
    assert result.diagnostics["exit_code"] != 0


@pytest.mark.unit
def test_command_gate_uses_cwd(tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "marker.txt").write_text("hi", encoding="utf-8")
    gate = CommandGate(command="test -f marker.txt", cwd=str(sub))
    result = gate.validate({}, tmp_path)
    assert result.passed


# ---- CompositeGate -----------------------------------------------------


@pytest.mark.unit
def test_composite_gate_passes_when_all_pass(tmp_path: Path):
    gate = CompositeGate(
        sub_gates=[CommandGate(command="true"), CommandGate(command="true")]
    )
    result = gate.validate({}, tmp_path)
    assert result.passed


@pytest.mark.unit
def test_composite_gate_fails_when_any_fails(tmp_path: Path):
    gate = CompositeGate(
        sub_gates=[CommandGate(command="true"), CommandGate(command="false")]
    )
    result = gate.validate({}, tmp_path)
    assert not result.passed
    assert any("[command]" in f for f in result.failures)


# ---- build_gate / load_gate_for_story ---------------------------------


@pytest.mark.unit
def test_build_gate_unknown_type():
    with pytest.raises(SystemExit):
        build_gate({"type": "no-such-gate"})


@pytest.mark.unit
def test_build_gate_missing_type():
    with pytest.raises(SystemExit):
        build_gate({})


@pytest.mark.unit
def test_load_gate_per_story_overrides_default():
    prd = {
        "acceptance": {"default_gate": {"type": "command", "command": "true"}},
        "userStories": [],
    }
    story = {"id": "S1", "acceptanceGate": {"type": "command", "command": "false"}}
    gate = load_gate_for_story(prd, story)
    assert isinstance(gate, CommandGate)
    assert gate.command == "false"


@pytest.mark.unit
def test_load_gate_falls_back_to_default():
    prd = {
        "acceptance": {"default_gate": {"type": "command", "command": "true"}},
    }
    gate = load_gate_for_story(prd, {"id": "S1"})
    assert isinstance(gate, CommandGate)
    assert gate.command == "true"


@pytest.mark.unit
def test_load_gate_fatal_when_no_config():
    with pytest.raises(SystemExit):
        load_gate_for_story({}, {"id": "S1"})


@pytest.mark.unit
def test_load_default_verdict_schema_returns_pair():
    prd = {
        "acceptance": {
            "default_gate": {
                "type": "jsonl_schema",
                "schema_version": "v9",
                "verdict_schema": {"min_reason_chars": 5},
            }
        }
    }
    sv, vs = load_default_verdict_schema(prd)
    assert sv == "v9"
    assert vs.min_reason_chars == 5


@pytest.mark.unit
def test_load_default_verdict_schema_fatal_when_command_only():
    prd = {"acceptance": {"default_gate": {"type": "command", "command": "true"}}}
    with pytest.raises(SystemExit):
        load_default_verdict_schema(prd)


@pytest.mark.unit
def test_gate_result_dataclass_immutable():
    r = GateResult(True, [], {})
    with pytest.raises(Exception):
        r.passed = False  # frozen dataclass
