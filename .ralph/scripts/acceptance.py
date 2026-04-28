#!/usr/bin/env python3
"""Pluggable acceptance gates for ralph.

Each story in prd.json may carry an `acceptanceGate` spec that picks one of
the built-in gate types (or a composite). The whole project may also set a
top-level `acceptance.default_gate` used when a story omits the field.

Built-in gate types:
    jsonl_schema   verify a JSONL output file matches a configured schema
                   plus 5 size/coverage/length/diversity gates
    command        run a shell command; PASS iff exit code 0
    composite      AND of multiple sub-gates

The labeling 5-gate (count / set / schema_version / reason-length-ratio /
distinct-values) is JsonlSchemaGate with default thresholds. All previously
hardcoded constants (schema version, qa/reason field names, valid qa
values, length thresholds) now come from prd.json's gate config.

Schema example (prd.json):

    {
      "branchName": "...",
      "acceptance": {
        "default_gate": {
          "type": "jsonl_schema",
          "schema_version": "judge-v1",
          "verdict_schema": {
            "required_fields": ["task_id", "qid", "qa", "reason"],
            "id_field": "task_id",
            "qa_field": "qa",
            "reason_field": "reason",
            "valid_qa": ["yes", "no", "uncertain"],
            "min_reason_chars": 150,
            "reason_long_ratio_min": 0.90,
            "distinct_qa_min": 2,
            "distinct_qa_min_small": 1,
            "small_batch_threshold": 33
          }
        }
      },
      "userStories": [
        {"id": "S-001", "acceptanceGate": {"type": "command",
         "command": "pytest tests/test_users.py"}}
      ]
    }
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class GateResult:
    passed: bool
    failures: list[str]
    diagnostics: dict[str, Any] = field(default_factory=dict)


class Gate(Protocol):
    name: str

    def validate(self, story: dict, root: Path) -> GateResult: ...


# ---- Verdict schema (used by JsonlSchemaGate) ---------------------------


@dataclass(frozen=True)
class VerdictSchema:
    required_fields: tuple[str, ...]
    id_field: str = "task_id"
    qa_field: str = "qa"
    reason_field: str = "reason"
    valid_qa: tuple[str, ...] = ("yes", "no", "uncertain")
    min_reason_chars: int = 150
    reason_long_ratio_min: float = 0.90
    distinct_qa_min: int = 2
    distinct_qa_min_small: int = 1
    small_batch_threshold: int = 33

    @classmethod
    def from_dict(cls, d: dict) -> "VerdictSchema":
        return cls(
            required_fields=tuple(d.get("required_fields", ("task_id", "qa", "reason"))),
            id_field=d.get("id_field", "task_id"),
            qa_field=d.get("qa_field", "qa"),
            reason_field=d.get("reason_field", "reason"),
            valid_qa=tuple(d.get("valid_qa", ("yes", "no", "uncertain"))),
            min_reason_chars=int(d.get("min_reason_chars", 150)),
            reason_long_ratio_min=float(d.get("reason_long_ratio_min", 0.90)),
            distinct_qa_min=int(d.get("distinct_qa_min", 2)),
            distinct_qa_min_small=int(d.get("distinct_qa_min_small", 1)),
            small_batch_threshold=int(d.get("small_batch_threshold", 33)),
        )

    def distinct_qa_threshold_for(self, n_tasks: int) -> int:
        return (
            self.distinct_qa_min_small
            if n_tasks <= self.small_batch_threshold
            else self.distinct_qa_min
        )


# ---- JsonlSchemaGate ---------------------------------------------------


class JsonlSchemaGate:
    """The original 5-gate JSONL schema check, parameterized.

    Required story shape:
        story["entryCount"] (int) — expected verdict row count
    Required external context (passed via context dict):
        manifest_batch — the batch entry from stories/manifest.json
        verdicts_path — Path to <ROOT>/state/verdicts.jsonl
    """

    name = "jsonl_schema"

    def __init__(self, schema_version: str, verdict_schema: VerdictSchema):
        self.schema_version = schema_version
        self.verdict_schema = verdict_schema

    @classmethod
    def from_spec(cls, spec: dict) -> "JsonlSchemaGate":
        return cls(
            schema_version=spec.get("schema_version", "default-v1"),
            verdict_schema=VerdictSchema.from_dict(spec.get("verdict_schema", {})),
        )

    def validate(self, story: dict, root: Path) -> GateResult:
        ctx = story.get("_context", {})
        batch = ctx.get("manifest_batch")
        verdicts_path = ctx.get("verdicts_path")
        if not batch or not verdicts_path:
            return GateResult(False, ["jsonl_schema gate needs manifest_batch + verdicts_path in story._context"])

        expected_ids: set[str] = set(batch["task_ids"])
        n_tasks: int = int(batch["n_tasks"])
        verdicts = self._load_for_batch(Path(verdicts_path), expected_ids)
        return self._run_gates(verdicts, expected_ids, n_tasks)

    def _load_for_batch(self, p: Path, expected_ids: set[str]) -> list[dict]:
        if not p.exists():
            return []
        out: list[dict] = []
        id_field = self.verdict_schema.id_field
        with p.open(encoding="utf-8") as f:
            for ln_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    raise SystemExit(f"FATAL: {p} line {ln_no} malformed JSON: {e}")
                if rec.get("__meta__"):
                    continue
                if rec.get(id_field) in expected_ids:
                    out.append(rec)
        return out

    def _run_gates(
        self,
        verdicts: list[dict],
        expected_ids: set[str],
        n_tasks: int,
    ) -> GateResult:
        s = self.verdict_schema
        ids = [v.get(s.id_field) for v in verdicts]
        seen_set = set(ids)
        reasons = [v.get(s.reason_field, "") for v in verdicts]
        qa_values = [v.get(s.qa_field) for v in verdicts]
        schema_versions = [v.get("schema_version") for v in verdicts]

        gate_a_ok = len(verdicts) == n_tasks
        gate_b_ok = seen_set == expected_ids and len(ids) == len(seen_set)
        gate_schema_ok = (
            all(sv == self.schema_version for sv in schema_versions)
            and bool(schema_versions)
        )
        long_ratio = (
            sum(1 for r in reasons if isinstance(r, str) and len(r) >= s.min_reason_chars)
            / len(reasons)
            if reasons
            else 0.0
        )
        gate_c_ok = long_ratio >= s.reason_long_ratio_min
        valid_qa_set = set(s.valid_qa)
        distinct_qa = len({q for q in qa_values if q in valid_qa_set})
        gate_d_threshold = s.distinct_qa_threshold_for(n_tasks)
        gate_d_ok = distinct_qa >= gate_d_threshold

        avg_reason = (
            sum(len(r) for r in reasons if isinstance(r, str)) / len(reasons)
            if reasons
            else 0
        )
        qa_dist = {q: qa_values.count(q) for q in s.valid_qa}

        failures: list[str] = []
        if not gate_a_ok:
            failures.append(f"(a) verdicts count {len(verdicts)} != n_tasks {n_tasks}")
        if not gate_b_ok:
            missing = expected_ids - seen_set
            extra = seen_set - expected_ids
            dups = len(ids) - len(seen_set)
            diag = []
            if missing:
                diag.append(f"missing {len(missing)} (e.g. {next(iter(missing))})")
            if extra:
                diag.append(f"extra {len(extra)} (e.g. {next(iter(extra))})")
            if dups:
                diag.append(f"duplicated {dups}")
            failures.append(f"(b) {s.id_field} set mismatch: {', '.join(diag)}")
        if not gate_schema_ok:
            bad = [sv for sv in schema_versions if sv != self.schema_version]
            failures.append(
                f"(schema) {len(bad)}/{len(schema_versions)} verdict schema_version != "
                f"'{self.schema_version}'"
            )
        if not gate_c_ok:
            failures.append(
                f"(c) long-reason ratio {long_ratio:.2f} < "
                f"{s.reason_long_ratio_min}; avg={avg_reason:.0f} chars"
            )
        if not gate_d_ok:
            failures.append(
                f"(d) distinct {s.qa_field} {distinct_qa} < {gate_d_threshold} "
                f"(threshold for n_tasks={n_tasks})"
            )

        passed = all([gate_a_ok, gate_b_ok, gate_schema_ok, gate_c_ok, gate_d_ok])
        diagnostics = {
            "gates": {
                "a": gate_a_ok,
                "b": gate_b_ok,
                "schema": gate_schema_ok,
                "c": gate_c_ok,
                "d": gate_d_ok,
            },
            "gate_d_threshold": gate_d_threshold,
            "qa_dist": qa_dist,
            "avg_reason_chars": int(avg_reason),
            "long_reason_ratio": round(long_ratio, 4),
            "distinct_qa": distinct_qa,
            "n_verdicts": len(verdicts),
        }
        return GateResult(passed, failures, diagnostics)


# ---- CommandGate -------------------------------------------------------


class CommandGate:
    """Run a shell command; PASS iff exit code is 0."""

    name = "command"

    def __init__(self, command: str, cwd: str | None = None, timeout: int = 600):
        self.command = command
        self.cwd = cwd
        self.timeout = timeout

    @classmethod
    def from_spec(cls, spec: dict) -> "CommandGate":
        if "command" not in spec:
            raise SystemExit("FATAL: CommandGate spec missing 'command'")
        return cls(
            command=spec["command"],
            cwd=spec.get("cwd"),
            timeout=int(spec.get("timeout", 600)),
        )

    def validate(self, story: dict, root: Path) -> GateResult:
        cwd = Path(self.cwd) if self.cwd else root
        if not cwd.is_absolute():
            cwd = root / cwd
        try:
            proc = subprocess.run(
                self.command,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            return GateResult(
                False,
                [f"command timed out after {self.timeout}s: {self.command}"],
                {"stderr": str(e)},
            )
        diag = {
            "exit_code": proc.returncode,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        }
        if proc.returncode == 0:
            return GateResult(True, [], diag)
        return GateResult(
            False,
            [f"command exit={proc.returncode}: {shlex.quote(self.command)}"],
            diag,
        )


# ---- CompositeGate -----------------------------------------------------


class CompositeGate:
    """AND of multiple sub-gates. PASS iff every sub-gate passes."""

    name = "composite"

    def __init__(self, sub_gates: list[Gate]):
        self.sub_gates = sub_gates

    @classmethod
    def from_spec(cls, spec: dict) -> "CompositeGate":
        sub_specs = spec.get("gates", [])
        return cls(sub_gates=[build_gate(s) for s in sub_specs])

    def validate(self, story: dict, root: Path) -> GateResult:
        all_passed = True
        all_failures: list[str] = []
        diag: dict[str, Any] = {"sub_gates": []}
        for g in self.sub_gates:
            r = g.validate(story, root)
            diag["sub_gates"].append({"name": g.name, "passed": r.passed, "diag": r.diagnostics})
            if not r.passed:
                all_passed = False
                all_failures.extend(f"[{g.name}] {f}" for f in r.failures)
        return GateResult(all_passed, all_failures, diag)


# ---- Builder & loader --------------------------------------------------


_BUILTIN_GATES: dict[str, type] = {
    "jsonl_schema": JsonlSchemaGate,
    "command": CommandGate,
    "composite": CompositeGate,
}


def build_gate(spec: dict) -> Gate:
    if not isinstance(spec, dict):
        raise SystemExit(f"FATAL: gate spec must be a dict, got {type(spec).__name__}")
    gate_type = spec.get("type")
    if not gate_type:
        raise SystemExit("FATAL: gate spec missing 'type'")
    cls = _BUILTIN_GATES.get(gate_type)
    if cls is None:
        raise SystemExit(
            f"FATAL: unknown gate type '{gate_type}'. "
            f"Built-in: {sorted(_BUILTIN_GATES)}"
        )
    return cls.from_spec(spec)


def load_gate_for_story(prd: dict, story: dict) -> Gate:
    """Resolve gate for a story: per-story override > project-level default.

    If neither is set, a SystemExit is raised — callers should ensure prd.json
    has at least a default gate.
    """
    spec = story.get("acceptanceGate")
    if spec is None:
        accept = prd.get("acceptance", {})
        spec = accept.get("default_gate")
    if spec is None:
        raise SystemExit(
            f"FATAL: no acceptanceGate for story {story.get('id', '?')} and "
            f"no acceptance.default_gate at prd root"
        )
    return build_gate(spec)


def load_default_verdict_schema(prd: dict) -> tuple[str, VerdictSchema]:
    """For helpers that stream-append verdicts, get (schema_version, schema) from prd."""
    accept = prd.get("acceptance", {})
    default = accept.get("default_gate", {})
    if default.get("type") != "jsonl_schema":
        # Try to find the first jsonl_schema gate in default or composite
        if default.get("type") == "composite":
            for sub in default.get("gates", []):
                if sub.get("type") == "jsonl_schema":
                    return (
                        sub.get("schema_version", "default-v1"),
                        VerdictSchema.from_dict(sub.get("verdict_schema", {})),
                    )
        raise SystemExit(
            "FATAL: cannot stream-append verdicts: prd.acceptance.default_gate "
            "is not jsonl_schema (or composite containing one)"
        )
    return (
        default.get("schema_version", "default-v1"),
        VerdictSchema.from_dict(default.get("verdict_schema", {})),
    )


def maybe_env_override(value: str | None, env_var: str) -> str | None:
    """Allow shell scripts to override gate config via env var."""
    return os.environ.get(env_var, value)
