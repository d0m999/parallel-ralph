#!/usr/bin/env python3
"""Cause-classifying monitor + auto-restart for sharded ralph runs.

Behavior:
    - Read each shard's run.pid, decide alive/dead.
    - For dead shards still with pending work, classify the cause from
      run.log + loop.log: dirty_tree, rate_limit, or other.
    - For dirty_tree: do nothing automatically (alert and stop) unless
      --auto-stage-paths is set (allowlist of paths the monitor may stage
      and commit before restart).
    - For rate_limit: sleep --rate-limit-wait-sec and restart.
    - For other: alert and stop (operator must inspect).
    - When all shards report all stories pass=true, runs merge_shards.py
      and (on success) stop_shards.sh, then exits 0.

Time zone defaults to UTC; pass --tz to override (e.g. "America/Los_Angeles"
or "Asia/Tokyo"). Affects --start-at parsing and log timestamps only.

Usage:
    python3 scripts_4x/monitor_shards.py \\
        --start-at "2026-04-30 14:00:00" --shards a b c
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class ShardProgress:
    shard: str
    done: int
    total: int
    pending_ids: list[str]


_TZ = timezone.utc
_LOG_PATH = Path("/tmp/ralph_shard_monitor.log")
_ALERT_PATH = Path("/tmp/ralph_shard_monitor.alert")
_STATUS_PATH = Path("/tmp/ralph_shard_monitor.status.json")
_AUTO_STAGE_PATHS: set[str] = set()


def now_ts() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg: str) -> None:
    line = f"[{now_ts()}] {msg}"
    print(line, flush=True)
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def alert(msg: str, *, details: str = "") -> None:
    payload = {"ts": now_ts(), "message": msg, "details": details}
    _ALERT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log(f"ALERT: {msg}")
    if details:
        for line in details.splitlines():
            log(f"ALERT_DETAIL: {line}")


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd, text=True, check=check, capture_output=capture
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start-at",
        required=False,
        help='start time in --tz, e.g. "2026-04-30 14:00:00"; default: now',
    )
    parser.add_argument(
        "--tz",
        default="UTC",
        help="time zone for --start-at + log timestamps (default: UTC)",
    )
    parser.add_argument("--interval-sec", type=int, default=1800, help="poll interval")
    parser.add_argument(
        "--rate-limit-wait-sec",
        type=int,
        default=420,
        help="sleep this many seconds after detecting a rate-limit error before restart",
    )
    parser.add_argument("--max-iter", type=int, default=200)
    parser.add_argument("--max-retries", type=int, default=15)
    parser.add_argument("--launch-delay", type=int, default=45)
    parser.add_argument(
        "--shards",
        nargs="+",
        required=True,
        help="shard ids to monitor (e.g. 'a b c')",
    )
    parser.add_argument(
        "--auto-stage-paths",
        nargs="*",
        default=[],
        help=(
            "allowlist of repo-relative paths the monitor may auto-stage and "
            "commit before restarting on dirty_tree. Default: empty (= alert + stop)."
        ),
    )
    parser.add_argument(
        "--num-shards-for-merge",
        type=int,
        default=None,
        help=(
            "if all shards complete, merge_shards.py is invoked with this "
            "--num-shards (default: len(shards))"
        ),
    )
    parser.add_argument(
        "--log-path", type=Path, default=_LOG_PATH, help="log file path"
    )
    parser.add_argument(
        "--alert-path", type=Path, default=_ALERT_PATH, help="alert file path"
    )
    parser.add_argument(
        "--status-path", type=Path, default=_STATUS_PATH, help="status JSON path"
    )
    return parser.parse_args()


def wait_until(start_at: str | None) -> None:
    if not start_at:
        return
    target = datetime.strptime(start_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TZ)
    while True:
        now = datetime.now(_TZ)
        remaining = int((target - now).total_seconds())
        if remaining <= 0:
            return
        sleep_sec = min(remaining, 60)
        log(
            f"waiting until {target.strftime('%Y-%m-%d %H:%M:%S %Z')} "
            f"({remaining}s remaining)"
        )
        time.sleep(sleep_sec)


def shard_root(shard: str) -> Path:
    return REPO_ROOT / f".ralph-shard-{shard}"


def read_run_pid(shard: str) -> int | None:
    pid_file = shard_root(shard) / "run.pid"
    if not pid_file.exists():
        return None
    raw = pid_file.read_text(encoding="utf-8").strip()
    return int(raw) if raw.isdigit() else None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_progress(shard: str) -> ShardProgress:
    prd_path = shard_root(shard) / "prd.json"
    data = json.loads(prd_path.read_text(encoding="utf-8"))
    stories = data["userStories"]
    done = sum(1 for story in stories if story.get("passes") is True)
    pending_ids = [story["id"] for story in stories if not story.get("passes", False)]
    return ShardProgress(shard=shard, done=done, total=len(stories), pending_ids=pending_ids)


def tail_lines(path: Path, count: int = 50) -> str:
    if not path.exists():
        return f"[missing] {path}"
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(lines[-count:])


def diagnose_dead_shard(shard: str) -> tuple[str, str]:
    root = shard_root(shard)
    run_tail = tail_lines(root / "run.log", 50)
    loop_tail = tail_lines(root / "loop.log", 50)
    details = f"--- run.log tail ---\n{run_tail}\n--- loop.log tail ---\n{loop_tail}"
    haystack = f"{run_tail}\n{loop_tail}"
    if "working tree is dirty outside ralph scope" in haystack:
        return "dirty_tree", details
    if re.search(r"(You've hit your limit|rate[ _-]?limit|429)", haystack, re.IGNORECASE):
        return "rate_limit", details
    return "other", details


def git_status_paths() -> list[str]:
    proc = run(["git", "status", "--short"], capture=True)
    paths: list[str] = []
    for raw in proc.stdout.splitlines():
        if not raw.strip():
            continue
        path = raw[3:].strip()
        if path.startswith(".ralph-shard-"):
            continue
        paths.append(path)
    return paths


def handle_dirty_tree() -> bool:
    paths = git_status_paths()
    if not paths:
        log("dirty-tree diagnostic found no relevant paths; continuing")
        return True
    if not _AUTO_STAGE_PATHS:
        alert(
            "git tree is dirty and no --auto-stage-paths configured; monitor stopping",
            details="\n".join(paths),
        )
        return False
    unexpected = [path for path in paths if path not in _AUTO_STAGE_PATHS]
    if unexpected:
        alert(
            "git tree has paths outside --auto-stage-paths; monitor stopping",
            details="\n".join(unexpected),
        )
        return False
    log(f"dirty-tree fix: staging + committing {paths}")
    run(["git", "add", "--", *sorted(paths)])
    run(["git", "commit", "-m", "chore: monitor auto-commit allowed paths"])
    return True


def clear_stale_lock(shard: str) -> None:
    root = shard_root(shard)
    for name in (".instance", "run.pid", "current_story.json"):
        p = root / name
        if p.exists():
            p.unlink()
    log(f"shard-{shard}: stale locks cleared")


def restart_shard(
    shard: str, *, max_iter: int, max_retries: int, launch_delay: int
) -> bool:
    env = os.environ.copy()
    env.update(
        {
            "LAUNCH_DELAY": str(launch_delay),
            "MAX_ITER": str(max_iter),
            "MAX_RETRIES": str(max_retries),
        }
    )
    log(f"shard-{shard}: restarting via run_shards.sh {shard}")
    proc = subprocess.run(
        [str(REPO_ROOT / "scripts_4x" / "run_shards.sh"), shard],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
    )
    if proc.stdout:
        for line in proc.stdout.splitlines():
            log(f"run_shards[{shard}]: {line}")
    if proc.stderr:
        for line in proc.stderr.splitlines():
            log(f"run_shards[{shard}][stderr]: {line}")
    if proc.returncode != 0:
        alert(
            f"restart failed for shard-{shard}",
            details=f"exit={proc.returncode}\n{proc.stdout}\n{proc.stderr}",
        )
        return False
    time.sleep(2)
    alive = pid_alive(read_run_pid(shard))
    if not alive:
        alert(f"restart launched but shard-{shard} pid is not alive", details=proc.stdout)
    return alive


def maybe_merge_and_stop(shards: Iterable[str], num_shards_for_merge: int) -> bool:
    progresses = [read_progress(shard) for shard in shards]
    if not all(progress.done == progress.total for progress in progresses):
        return False
    log("all shard prd.json stories pass=true; running merge_shards.py validation")
    proc = subprocess.run(
        [
            "python3",
            "scripts_4x/merge_shards.py",
            "--num-shards",
            str(num_shards_for_merge),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    if proc.stdout:
        for line in proc.stdout.splitlines():
            log(f"merge: {line}")
    if proc.stderr:
        for line in proc.stderr.splitlines():
            log(f"merge[stderr]: {line}")
    if proc.returncode != 0:
        alert(
            "merge_shards.py failed after all stories passed",
            details=f"exit={proc.returncode}\n{proc.stdout}\n{proc.stderr}",
        )
        return True
    log("merge_shards.py passed; stopping shard loops")
    stop_proc = subprocess.run(
        [str(REPO_ROOT / "scripts_4x" / "stop_shards.sh"), *list(shards)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    if stop_proc.stdout:
        for line in stop_proc.stdout.splitlines():
            log(f"stop_shards: {line}")
    if stop_proc.stderr:
        for line in stop_proc.stderr.splitlines():
            log(f"stop_shards[stderr]: {line}")
    log("monitor complete; exiting")
    return True


def write_status(progresses: list[ShardProgress], alive: dict[str, bool], iteration: int) -> None:
    payload = {
        "ts": now_ts(),
        "iteration": iteration,
        "shards": {
            progress.shard: {
                "alive": alive[progress.shard],
                "done": progress.done,
                "total": progress.total,
                "pending_ids": progress.pending_ids,
            }
            for progress in progresses
        },
    }
    _STATUS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    global _TZ, _LOG_PATH, _ALERT_PATH, _STATUS_PATH, _AUTO_STAGE_PATHS

    args = parse_args()
    _TZ = ZoneInfo(args.tz)
    _LOG_PATH = args.log_path
    _ALERT_PATH = args.alert_path
    _STATUS_PATH = args.status_path
    _AUTO_STAGE_PATHS = set(args.auto_stage_paths)

    num_shards_for_merge = args.num_shards_for_merge or len(args.shards)

    wait_until(args.start_at)
    log(
        "monitor started "
        f"(shards={args.shards}, interval={args.interval_sec}s, "
        f"rate_limit_wait={args.rate_limit_wait_sec}s, tz={args.tz})"
    )

    iteration = 0
    while True:
        iteration += 1
        log(f"sweep iteration={iteration} begin")

        progresses = [read_progress(shard) for shard in args.shards]
        alive = {shard: pid_alive(read_run_pid(shard)) for shard in args.shards}
        write_status(progresses, alive, iteration)

        for progress in progresses:
            state = "ALIVE" if alive[progress.shard] else "DEAD"
            log(
                f"shard-{progress.shard}: {state} progress={progress.done}/{progress.total} "
                f"pending={','.join(progress.pending_ids) if progress.pending_ids else '(none)'}"
            )

        if maybe_merge_and_stop(args.shards, num_shards_for_merge):
            return 0

        if not any(alive.values()):
            alert("all shards are dead before completion; monitor stopping", details=json.dumps(alive))
            return 2

        for progress in progresses:
            shard = progress.shard
            if alive[shard]:
                continue
            if progress.done == progress.total:
                log(f"shard-{shard}: process dead but already complete; skip restart")
                continue
            cause, details = diagnose_dead_shard(shard)
            log(f"shard-{shard}: cause={cause}")

            if cause == "dirty_tree":
                if not handle_dirty_tree():
                    return 3
                clear_stale_lock(shard)
                if not restart_shard(
                    shard,
                    max_iter=args.max_iter,
                    max_retries=args.max_retries,
                    launch_delay=args.launch_delay,
                ):
                    return 4
                continue

            if cause == "rate_limit":
                log(
                    f"shard-{shard}: rate limited; sleeping "
                    f"{args.rate_limit_wait_sec}s before restart"
                )
                time.sleep(args.rate_limit_wait_sec)
                clear_stale_lock(shard)
                if not restart_shard(
                    shard,
                    max_iter=args.max_iter,
                    max_retries=args.max_retries,
                    launch_delay=args.launch_delay,
                ):
                    return 5
                continue

            alert(f"shard-{shard}: unclassified failure; monitor stopping", details=details)
            return 6

        log(f"sweep iteration={iteration} end; sleeping {args.interval_sec}s")
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    sys.exit(main())
