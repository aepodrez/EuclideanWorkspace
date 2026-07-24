#!/usr/bin/env python3
"""Monitor the local EDGAR backfill and restart it when it is not running."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
from datetime import datetime, timezone
from urllib.request import urlopen


WORKSPACE = Path(__file__).resolve().parent
BACKFILL_SCRIPT = WORKSPACE / "backfill_ollama.py"
PROGRESS_FILE = Path("/tmp/backfill_progress.jsonl")
BACKFILL_LOG = Path("/tmp/backfill_ollama.log")
WATCHDOG_LOG = Path("/tmp/backfill_watchdog.log")
BACKFILL_PID_FILE = Path("/tmp/backfill_ollama.pid")
WATCHDOG_PID_FILE = Path("/tmp/backfill_watchdog.pid")
LOCK_FILE = Path("/tmp/backfill_watchdog.lock")
RUN_STATE_FILE = Path(
    os.environ.get(
        "BACKFILL_RUN_STATE_FILE",
        str(WORKSPACE / "local-runs/backfill_reprocess_state.json"),
    )
)

MODEL = "mlx-community/Qwen3-8B-4bit"
MLX_BASE_URL = "http://host.docker.internal:8080"
INTERVAL_SECONDS = 6 * 60 * 60


def _log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"{timestamp} {message}"
    print(line, flush=True)
    with WATCHDOG_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _backfill_pids() -> list[int]:
    """Find live Python processes running this workspace's backfill script."""
    matches: list[int] = []
    for proc_dir in Path("/proc").glob("[0-9]*"):
        try:
            argv = (proc_dir / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        args = [part.decode(errors="replace") for part in argv if part]
        if not args or "python" not in Path(args[0]).name.lower():
            continue
        if any(Path(arg).name == BACKFILL_SCRIPT.name for arg in args[1:]):
            matches.append(int(proc_dir.name))
    return sorted(matches)


def _progress_summary() -> str:
    counts: dict[str, int] = {}
    last_timestamp = "none"
    if not PROGRESS_FILE.exists():
        return "progress_rows=0"
    try:
        with PROGRESS_FILE.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                status = str(row.get("status", "unknown"))
                counts[status] = counts.get(status, 0) + 1
                last_timestamp = str(row.get("ts", last_timestamp))
    except OSError as exc:
        return f"progress_unreadable={exc}"
    total = sum(counts.values())
    statuses = ",".join(f"{key}={counts[key]}" for key in sorted(counts))
    return f"progress_rows={total} {statuses} last_progress={last_timestamp}"


def _mlx_status() -> str:
    try:
        with urlopen(f"{MLX_BASE_URL}/v1/models", timeout=5) as response:
            payload = json.load(response)
        models = {item.get("id") for item in payload.get("data", [])}
        return "mlx=ready" if MODEL in models else "mlx=reachable_model_missing"
    except Exception as exc:  # health reporting must not prevent a restart
        return f"mlx=unreachable({type(exc).__name__})"


def _last_run_completed() -> bool:
    """Return true only when the checkpointed reprocess run is complete."""
    if RUN_STATE_FILE.exists():
        try:
            state = json.loads(RUN_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return state.get("status") == "complete"

    # Backward-compatible fallback for non-checkpointed runs.
    if not BACKFILL_LOG.exists():
        return False
    try:
        lines = BACKFILL_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return any(" INFO Done." in line for line in lines[-5:])


def _restart_backfill() -> int:
    env = os.environ.copy()
    env.update(
        {
            "LLM_MODEL": MODEL,
            "LLM_BASE_URL": f"{MLX_BASE_URL}/v1/chat/completions",
        }
    )
    log_handle = BACKFILL_LOG.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [
                "python3",
                "-u",
                str(BACKFILL_SCRIPT),
                "--workers",
                "2",
                "--rescan",
                "--reprocess",
            ],
            cwd=WORKSPACE,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_handle.close()
    BACKFILL_PID_FILE.write_text(f"{process.pid}\n", encoding="utf-8")
    return process.pid


def _check() -> None:
    pids = _backfill_pids()
    progress = _progress_summary()
    mlx = _mlx_status()
    if pids:
        BACKFILL_PID_FILE.write_text(f"{pids[0]}\n", encoding="utf-8")
        _log(f"healthy pids={','.join(map(str, pids))} {mlx} {progress}")
        return
    if _last_run_completed():
        _log(f"completed no_restart {mlx} {progress}")
        return
    pid = _restart_backfill()
    _log(f"restarted pid={pid} {mlx} {progress}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds")

    lock_handle = LOCK_FILE.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another backfill watchdog is already running")

    WATCHDOG_PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    _log(f"watchdog_started interval_seconds={args.interval}")
    while not stop.is_set():
        _check()
        if args.once or stop.wait(args.interval):
            break
    _log("watchdog_stopped")


if __name__ == "__main__":
    main()
