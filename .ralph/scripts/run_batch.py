#!/usr/bin/env python3
"""Ralph single-batch helper for jsonl_schema-style stories.

This helper is opt-in: it implements the labeling/judging pattern where each
story produces N verdict rows in `<ROOT>/state/verdicts.jsonl`. For
code-implementation stories, use a CommandGate from prd.json's
`acceptanceGate` (see `.ralph/scripts/acceptance.py`) and a different
operator workflow that runs tests directly — this script is not required.

Operator agent (spawned by ralph.sh) calls this script three times per
iteration:

    prepare    read locked story, compute pending tasks, write subagent prompt
               (auto-recover: if attempts>0 and the whole batch is already in
               seen_task_ids, drop verdicts+seen for this batch and redo)
    validate   read verdicts.jsonl, run the acceptance gate, print JSON result
    finalize   on PASS flip prd.json + append progress.txt; on FAIL log
               blocker; emit promise token (YIELD / COMPLETE / VIOLATION)

The LLM call is NOT done by this script — operator agent dispatches a
subagent (Agent tool) per the prompt written by `prepare`. This script
handles deterministic plumbing only.

Default mode (no flag): ROOT=.ralph (single-process baseline).
Shard mode: --shard-root .ralph-shard-X — paths switch to the given shard
root; the helper itself stays at .ralph/scripts/ as the single source of
truth.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance import (  # noqa: E402
    GateResult,
    VerdictSchema,
    load_default_verdict_schema,
    load_gate_for_story,
)

# Module-level path constants — defaults to .ralph; main() resets per --shard-root
ROOT = Path(".ralph")
PRD_FILE = ROOT / "prd.json"
PROGRESS_FILE = ROOT / "progress.txt"
CURRENT_STORY = ROOT / "current_story.json"
MANIFEST = ROOT / "stories" / "manifest.json"
STATE_DIR = ROOT / "state"
VERDICTS = STATE_DIR / "verdicts.jsonl"
SEEN_IDS = STATE_DIR / "seen_task_ids.json"
SUBAGENT_PROMPT = STATE_DIR / "subagent_prompt.md"

MAX_ATTEMPTS_DEFAULT = 3


def _set_root(root: Path) -> None:
    global ROOT, PRD_FILE, PROGRESS_FILE, CURRENT_STORY, MANIFEST
    global STATE_DIR, VERDICTS, SEEN_IDS, SUBAGENT_PROMPT
    ROOT = root
    PRD_FILE = ROOT / "prd.json"
    PROGRESS_FILE = ROOT / "progress.txt"
    CURRENT_STORY = ROOT / "current_story.json"
    MANIFEST = ROOT / "stories" / "manifest.json"
    STATE_DIR = ROOT / "state"
    VERDICTS = STATE_DIR / "verdicts.jsonl"
    SEEN_IDS = STATE_DIR / "seen_task_ids.json"
    SUBAGENT_PROMPT = STATE_DIR / "subagent_prompt.md"


def read_json(p: Path) -> Any:
    with p.open() as f:
        return json.load(f)


def atomic_write_json(p: Path, data: Any) -> None:
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    with tmp.open("w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(p)


def append_progress(line: str) -> None:
    with PROGRESS_FILE.open("a") as f:
        f.write(line.rstrip() + "\n")
        f.flush()
        os.fsync(f.fileno())


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_locked_story(prd: dict, locked_id: str) -> dict:
    for s in prd["userStories"]:
        if s["id"] == locked_id:
            return s
    sys.exit(f"FATAL: locked id '{locked_id}' not found in prd.json")


def get_batch(manifest: dict, locked_id: str) -> dict:
    for b in manifest["batches"]:
        if b["story_id"] == locked_id:
            return b
    sys.exit(f"FATAL: '{locked_id}' not found in manifest.batches")


def _max_attempts(prd: dict) -> int:
    return int(prd.get("acceptance", {}).get("max_attempts", MAX_ATTEMPTS_DEFAULT))


def _attach_context(story: dict, batch: dict) -> None:
    """Stuff manifest_batch + verdicts_path into the story dict for gate use."""
    story["_context"] = {
        "manifest_batch": batch,
        "verdicts_path": str(VERDICTS),
    }


def _validate_with_gate(prd: dict, story: dict, batch: dict) -> GateResult:
    _attach_context(story, batch)
    gate = load_gate_for_story(prd, story)
    return gate.validate(story, ROOT)


def _clear_batch_state(
    expected_task_ids: set[str], id_field: str
) -> tuple[int, int]:
    """Drop verdict rows for this batch + remove their ids from seen file.

    Atomic write keeps consistency. Returns (verdicts_dropped, seen_dropped).
    Used by auto-recover when the previous attempt wrote everything but the
    gate still failed.
    """
    n_verdicts_dropped = 0
    n_seen_dropped = 0

    if VERDICTS.exists():
        kept_lines: list[str] = []
        with VERDICTS.open(encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    kept_lines.append(line.rstrip("\n"))
                    continue
                if rec.get("__meta__"):
                    kept_lines.append(line.rstrip("\n"))
                    continue
                if rec.get(id_field) in expected_task_ids:
                    n_verdicts_dropped += 1
                    continue
                kept_lines.append(line.rstrip("\n"))
        tmp = VERDICTS.with_suffix(VERDICTS.suffix + f".tmp.{os.getpid()}")
        with tmp.open("w", encoding="utf-8") as f:
            for ln in kept_lines:
                f.write(ln + "\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(VERDICTS)

    if SEEN_IDS.exists():
        try:
            seen = json.load(SEEN_IDS.open())
        except json.JSONDecodeError:
            seen = []
        before = len(seen)
        seen_after = sorted(set(seen) - expected_task_ids)
        n_seen_dropped = before - len(seen_after)
        atomic_write_json(SEEN_IDS, seen_after)

    return n_verdicts_dropped, n_seen_dropped


def cmd_prepare(args: argparse.Namespace) -> None:
    locked_id = read_json(CURRENT_STORY)["id"]
    prd = read_json(PRD_FILE)
    story = get_locked_story(prd, locked_id)
    manifest = read_json(MANIFEST)
    batch = get_batch(manifest, locked_id)

    schema_version, vschema = load_default_verdict_schema(prd)
    expected_task_ids = set(batch["task_ids"])
    n_tasks = batch["n_tasks"]
    if len(expected_task_ids) != n_tasks:
        sys.exit(
            f"FATAL: manifest task_ids count {len(expected_task_ids)} "
            f"!= n_tasks {n_tasks} for {locked_id}"
        )

    seen = set(read_json(SEEN_IDS)) if SEEN_IDS.exists() else set()

    attempts = int(story.get("attempts", 0))
    recovered = False
    n_dropped_v = 0
    n_dropped_s = 0
    if attempts > 0 and expected_task_ids.issubset(seen):
        n_dropped_v, n_dropped_s = _clear_batch_state(
            expected_task_ids, vschema.id_field
        )
        seen = set(read_json(SEEN_IDS)) if SEEN_IDS.exists() else set()
        recovered = True

    input_path = Path(batch["input_file"])
    if not input_path.exists():
        sys.exit(f"FATAL: batch input file {input_path} missing")

    pending: list[dict] = []
    with input_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            task = json.loads(line)
            if task[vschema.id_field] not in seen:
                pending.append(task)

    prompt_template = args.prompt_template
    prompt = _build_subagent_prompt(
        locked_id=locked_id,
        pending=pending,
        schema_version=schema_version,
        vschema=vschema,
        template_path=prompt_template,
    )
    SUBAGENT_PROMPT.write_text(prompt, encoding="utf-8")

    out = {
        "locked_id": locked_id,
        "n_tasks": n_tasks,
        "n_pending": len(pending),
        "n_seen_in_batch": n_tasks - len(pending),
        "attempts": attempts,
        "input_file": str(input_path),
        "subagent_prompt_path": str(SUBAGENT_PROMPT),
        "shard_root": str(ROOT),
        "schema_version": schema_version,
    }
    if recovered:
        out["auto_recovered"] = {
            "verdicts_dropped": n_dropped_v,
            "seen_dropped": n_dropped_s,
            "reason": (
                f"attempts={attempts} and the whole batch was already in seen — "
                f"the prior attempt wrote everything but the gate failed; "
                f"clearing batch state and redoing"
            ),
        }
    print(json.dumps(out, indent=2, ensure_ascii=False))


def _default_subagent_prompt_template() -> str:
    """Returned when no template path is provided. Generic verdict-streaming flow."""
    return """# Subagent task: gate-driven verdict pass for {locked_id} ({n_pending} tasks)

You are a one-shot subagent. For each pending task, decide a verdict and
APPEND a single JSON line to `{state_dir}/verdicts.jsonl` via the streaming
helper. Update `{state_dir}/seen_task_ids.json` atomically (the helper does
this for you).

## Verdict schema (one JSON line per task)

Required fields: {required_fields}
- `{id_field}`: copy from input
- `{qa_field}`: one of {valid_qa}
- `{reason_field}`: explanation, ≥{min_reason_chars} characters
- `schema_version`: "{schema_version}" verbatim

## MANDATORY workflow per task — STREAMING ONLY

For each pending task, INDEPENDENTLY:
  1. Read the task line from §Pending tasks below
  2. Reason about the verdict (use full extended thinking)
  3. Pipe the single-line verdict JSON to the helper:
         echo '<verdict-json>' | {helper_invocation}
     The helper validates schema, schema_version, and reason length, then
     appends + fsyncs and updates seen_task_ids atomically. It exits non-zero
     on any validation failure.
  4. Confirm the helper printed `"appended":true` and move to the next task.

### PROHIBITED
- Accumulating verdicts in a list/dict and bulk-writing at the end. Every
  verdict must hit verdicts.jsonl within seconds of being reasoned.
- Wrapping the helper in a Python loop. Use the shell helper one-shot per
  task.
- Bypassing the helper and opening verdicts.jsonl directly.

### Rules
- Reason MUST be ≥{min_reason_chars} characters.
- Across the {n_pending} tasks emit at least {distinct_qa_min} distinct
  `{qa_field}` values (the helper does NOT enforce this — self-police it).
- Process tasks in the order given.
- schema_version is `{schema_version}` verbatim.

## Pending tasks (raw JSONL, {n_pending} lines)
```jsonl
{pending_block}
```

## Bash one-liner per task (only sanctioned write path)

```bash
echo '<single-line-verdict-json>' | {helper_invocation}
```

After all {n_pending} appends report `"appended":true`, stop.
"""


def _build_subagent_prompt(
    locked_id: str,
    pending: list[dict],
    schema_version: str,
    vschema: VerdictSchema,
    template_path: str | None,
) -> str:
    pending_block = "\n".join(json.dumps(t, ensure_ascii=False) for t in pending)
    is_shard_mode = ROOT != Path(".ralph")
    helper_invocation = "python3 .ralph/scripts/append_verdict.py"
    if is_shard_mode:
        helper_invocation += f" --shard-root {ROOT}"

    fmt_kwargs = {
        "locked_id": locked_id,
        "n_pending": len(pending),
        "state_dir": str(STATE_DIR),
        "shard_root": str(ROOT),
        "helper_invocation": helper_invocation,
        "schema_version": schema_version,
        "required_fields": list(vschema.required_fields),
        "id_field": vschema.id_field,
        "qa_field": vschema.qa_field,
        "reason_field": vschema.reason_field,
        "valid_qa": list(vschema.valid_qa),
        "min_reason_chars": vschema.min_reason_chars,
        "distinct_qa_min": vschema.distinct_qa_threshold_for(len(pending)),
        "pending_block": pending_block,
    }

    if template_path:
        try:
            tmpl = Path(template_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            sys.exit(f"FATAL: prompt template {template_path} not found")
    else:
        tmpl = _default_subagent_prompt_template()

    try:
        return tmpl.format(**fmt_kwargs)
    except KeyError as e:
        sys.exit(f"FATAL: prompt template missing placeholder: {e}")


def cmd_validate(args: argparse.Namespace) -> None:
    locked_id = read_json(CURRENT_STORY)["id"]
    prd = read_json(PRD_FILE)
    story = get_locked_story(prd, locked_id)
    manifest = read_json(MANIFEST)
    batch = get_batch(manifest, locked_id)
    result = _validate_with_gate(prd, story, batch)

    payload = {
        "pass": result.passed,
        "locked_id": locked_id,
        "failures": result.failures,
        **result.diagnostics,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(0 if result.passed else 1)


def cmd_finalize(args: argparse.Namespace) -> None:
    locked_id = read_json(CURRENT_STORY)["id"]
    prd = read_json(PRD_FILE)
    story = get_locked_story(prd, locked_id)
    manifest = read_json(MANIFEST)
    batch = get_batch(manifest, locked_id)
    result = _validate_with_gate(prd, story, batch)
    max_attempts = _max_attempts(prd)

    ts = utc_iso()

    if result.passed:
        for s in prd["userStories"]:
            if s["id"] == locked_id:
                s["passes"] = True
                s.pop("_context", None)
                break
        atomic_write_json(PRD_FILE, prd)

        diag = result.diagnostics
        qa_dist = diag.get("qa_dist", {})
        qa_dist_str = "/".join(f"{k}:{v}" for k, v in qa_dist.items())
        avg = diag.get("avg_reason_chars", 0)
        n = batch["n_tasks"]
        line = (
            f"- {locked_id} · n={n} · "
            f"dist={qa_dist_str} · avg={avg}c · {ts}"
        )
        append_progress(line)

        remaining = sum(1 for s in prd["userStories"] if not s["passes"])
        token = "<promise>COMPLETE</promise>" if remaining == 0 else "<promise>YIELD</promise>"
        print(json.dumps({
            "result": "PASS",
            "locked_id": locked_id,
            "remaining_stories": remaining,
            "promise": token,
            **result.diagnostics,
        }, ensure_ascii=False, indent=2))
        print(token)
        return

    story["attempts"] = int(story.get("attempts", 0)) + 1
    story.pop("_context", None)
    atomic_write_json(PRD_FILE, prd)
    diag_str = "; ".join(result.failures)
    blocker = f"- {locked_id} attempt={story['attempts']} failed at {ts}: {diag_str}"
    append_progress(blocker)

    if story["attempts"] >= max_attempts:
        msg = (
            f"VIOLATION: {locked_id} failed {story['attempts']} times in a row, "
            f"manual intervention required: {diag_str}"
        )
        print(json.dumps({
            "result": "VIOLATION",
            "locked_id": locked_id,
            "attempts": story["attempts"],
            **result.diagnostics,
        }, ensure_ascii=False, indent=2))
        print(msg)
        sys.exit(2)

    print(json.dumps({
        "result": "FAIL",
        "locked_id": locked_id,
        "attempts": story["attempts"],
        "promise": "<promise>YIELD</promise>",
        "failures": result.failures,
        **result.diagnostics,
    }, ensure_ascii=False, indent=2))
    print("<promise>YIELD</promise>")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shard-root",
        type=Path,
        default=Path(".ralph"),
        help="ralph root (default .ralph; shard mode: .ralph-shard-X)",
    )
    parser.add_argument(
        "--prompt-template",
        type=str,
        default=None,
        help=(
            "Path to a subagent prompt template (str.format style). If omitted, "
            "a generic streaming-verdict template is used."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare", help="compute pending tasks + write subagent prompt")
    sub.add_parser("validate", help="run the configured acceptance gate")
    sub.add_parser("finalize", help="flip passes/log result, print promise token")
    args = parser.parse_args()

    _set_root(args.shard_root)

    {"prepare": cmd_prepare, "validate": cmd_validate, "finalize": cmd_finalize}[
        args.cmd
    ](args)


if __name__ == "__main__":
    main()
