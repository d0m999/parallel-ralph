#!/usr/bin/env python3
"""PreToolUse hook: deny shard subagents from writing outside their shard.

Activation: env var `RALPH_SHARD_ROOT` must be set (ralph.sh injects this in
shard mode). When unset, the hook is a no-op so single-process workflows
are unaffected.

Allowed writes when active:
    1. {RALPH_SHARD_ROOT}/state/        (verdicts.jsonl, seen_task_ids.json,
                                         subagent_prompt.md)
    2. {RALPH_SHARD_ROOT}/loop.log
    3. {RALPH_SHARD_ROOT}/.retries/
    4. {RALPH_SHARD_ROOT}/current_story.json
    5. {RALPH_SHARD_ROOT}/progress.txt
    6. {RALPH_SHARD_ROOT}/prd.json
    7. /tmp/ and /var/folders/ (macOS tempdirs)
    8. python __pycache__ / *.pyc

Hard-denied (even in shard mode):
    - other `.ralph-shard-*/` directories
    - `.ralph/` (the baseline; main session owns it exclusively)
    - any `*.py` modification (subagents must not edit code)

Project-specific extra deny prefixes (input data, generated artifacts,
documentation SoT) can be added via env var:

    RALPH_HARD_DENY_PREFIXES="eval_results/:data_processing/gold:docs/"

(colon-separated list of repo-relative path prefixes). The default is empty
so the harness ships generic; forks should configure this in their startup
script.

Hook protocol (Claude Code):
    stdin: tool call JSON
    exit 0  → allow
    exit 2  → deny + stderr message returned to the agent
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Always-deny prefixes (the harness's own structural invariants)
ALWAYS_DENY_PREFIXES = (
    ".ralph/",  # baseline owned by the main session
)

SHARD_DIR_PATTERN_PREFIX = ".ralph-shard-"
ALLOW_TEMP_PREFIXES = ("/tmp/", "/var/folders/")


def _read_input() -> dict:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _resolve_path(p: str, repo_root: Path) -> Path:
    path = Path(p)
    if path.is_absolute():
        try:
            return path.relative_to(repo_root)
        except ValueError:
            return path
    return path


def _project_hard_deny_prefixes() -> tuple[str, ...]:
    raw = os.environ.get("RALPH_HARD_DENY_PREFIXES", "").strip()
    if not raw:
        return ()
    return tuple(tok.strip() for tok in raw.split(":") if tok.strip())


def _is_allowed(target: Path, shard_root: Path, repo_root: Path) -> tuple[bool, str]:
    target_str = str(target if target.is_absolute() else (repo_root / target))
    if any(target_str.startswith(p) for p in ALLOW_TEMP_PREFIXES):
        return (True, "tmp path")

    rel = _resolve_path(str(target), repo_root)
    rel_str = str(rel) if not rel.is_absolute() else str(target)

    # 1. Always-deny structural prefixes
    for prefix in ALWAYS_DENY_PREFIXES:
        if rel_str.startswith(prefix):
            return (False, f"baseline deny: {prefix} (subagent must not write here)")

    # 2. Project-configured deny prefixes
    for prefix in _project_hard_deny_prefixes():
        if rel_str.startswith(prefix):
            return (
                False,
                f"project red-line deny: {prefix} (RALPH_HARD_DENY_PREFIXES)",
            )

    # 3. Other shard directories
    if rel_str.startswith(SHARD_DIR_PATTERN_PREFIX):
        try:
            shard_rel = shard_root.name
        except Exception:
            shard_rel = ""
        if not rel_str.startswith(shard_rel + "/") and rel_str != shard_rel:
            return (
                False,
                f"cross-shard deny: {rel_str} not in current shard ({shard_rel})",
            )

    # 4. *.py modification
    if rel_str.endswith(".py") and "__pycache__" not in rel_str:
        return (False, f"code deny: subagent must not modify *.py ({rel_str})")

    # 5. Inside current shard
    try:
        shard_rel = (
            str(shard_root.relative_to(repo_root))
            if shard_root.is_absolute()
            else str(shard_root)
        )
    except ValueError:
        shard_rel = str(shard_root)
    if rel_str.startswith(shard_rel + "/") or rel_str == shard_rel:
        return (True, "inside current shard")

    # 6. Default: deny (conservative)
    return (False, f"default deny: {rel_str} (not in allowlist)")


def main() -> int:
    shard_root_env = os.environ.get("RALPH_SHARD_ROOT", "")
    if not shard_root_env:
        return 0

    payload = _read_input()
    tool_name = payload.get("tool_name", payload.get("name", ""))

    if tool_name not in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        return 0

    tool_input = payload.get("tool_input", payload.get("input", {}))
    target_path_raw = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("notebook_path")
        or ""
    )
    if not target_path_raw:
        return 0

    repo_root = Path(__file__).resolve().parents[2]
    shard_root = Path(shard_root_env)
    if not shard_root.is_absolute():
        shard_root = repo_root / shard_root

    target = Path(target_path_raw)
    allowed, reason = _is_allowed(target, shard_root, repo_root)

    if allowed:
        return 0

    print(
        f"DENIED by shard write-boundary hook: {target_path_raw}\n"
        f"  Reason: {reason}\n"
        f"  Shard: RALPH_SHARD_ROOT={shard_root_env}\n"
        f"  Allowed: {shard_root}/state/, {shard_root}/loop.log, "
        f"{shard_root}/progress.txt, /tmp/*\n"
        f"  Forbidden: .ralph/, other .ralph-shard-*/, any *.py, plus any "
        f"prefix in RALPH_HARD_DENY_PREFIXES",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
