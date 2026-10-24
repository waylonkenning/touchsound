#!/usr/bin/env python3
"""Run TouchSound under a small detached process watchdog on macOS/Linux."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from runtime import _telnet_reboot


PROJECT = Path(__file__).resolve().parent
PYTHON = PROJECT / ".venv" / "bin" / "python"
LOG_DIR = Path.home() / "Library" / "Logs" / "TouchSound"
STATE_DIR = Path.home() / "Library" / "Application Support" / "TouchSound"
HISTORY_FILE = STATE_DIR / "speaker-history.jsonl"
PID_FILE = STATE_DIR / "watchdog.pid"
LOG_FILE = LOG_DIR / "service.log"


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pid_is_watchdog(pid: int) -> bool:
    if not _pid_is_running(pid):
        return False
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    command = result.stdout.strip()
    return str(Path(__file__).resolve()) in command and " run " in f" {command} "


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _bridge_args(
    speaker: str, relay_url: str | None, no_auto_reboot: bool, event_log: str | None = None
) -> list[str]:
    command = [str(PYTHON), str(PROJECT / "bridge.py"), "--speaker", speaker]
    if relay_url:
        command.extend(["--relay-url", relay_url])
    command.extend(["--state-file", str(STATE_DIR / "runtime.json")])
    command.extend(["--event-log", event_log or str(HISTORY_FILE)])
    if no_auto_reboot:
        command.append("--no-auto-reboot")
    return command


def _run_watchdog(args: argparse.Namespace) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    command = _bridge_args(args.speaker, args.relay_url, args.no_auto_reboot, args.event_log)
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PATH"] = ":".join(dict.fromkeys([
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        *environment.get("PATH", "").split(":"),
    ]))

    try:
        with LOG_FILE.open("ab", buffering=0) as log:
            while not stop_event.is_set():
                log.write(f"Starting bridge: {' '.join(command)}\n".encode())
                try:
                    child = subprocess.Popen(
                        command,
                        cwd=PROJECT,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                except OSError as exc:
                    log.write(f"Could not start bridge: {exc}\n".encode())
                    stop_event.wait(5)
                    continue

                while child.poll() is None and not stop_event.wait(1):
                    pass
                if stop_event.is_set() and child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                        child.wait(timeout=10)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    break
                if not stop_event.is_set():
                    log.write(f"Bridge exited with status {child.returncode}; restarting in 5 seconds.\n".encode())
                    stop_event.wait(5)
    finally:
        try:
            if _read_pid() == os.getpid():
                PID_FILE.unlink()
        except OSError:
            pass


def start(args: argparse.Namespace) -> None:
    if not PYTHON.is_file():
        raise SystemExit(f"Python environment not found at {PYTHON}; create it and install requirements first.")
    existing = _read_pid()
    if existing is not None and _pid_is_watchdog(existing):
        print(f"TouchSound watchdog is already running (PID {existing}).")
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    command = [str(PYTHON), str(Path(__file__).resolve()), "run", "--speaker", args.speaker]
    if args.relay_url:
        command.extend(["--relay-url", args.relay_url])
    if args.no_auto_reboot:
        command.append("--no-auto-reboot")
    if args.event_log:
        command.extend(["--event-log", args.event_log])
    with LOG_FILE.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            cwd=PROJECT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    PID_FILE.write_text(f"{process.pid}\n", encoding="utf-8")
    print(f"Started TouchSound watchdog (PID {process.pid}).")
    print(f"Log: {LOG_FILE}")


def stop(_args: argparse.Namespace) -> None:
    pid = _read_pid()
    if pid is None or not _pid_is_watchdog(pid):
        print("TouchSound watchdog is not running.")
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline and _pid_is_running(pid):
        time.sleep(0.2)
    if _pid_is_running(pid):
        os.kill(pid, signal.SIGKILL)
        print(f"Stopped TouchSound watchdog (PID {pid}) forcefully.")
    else:
        print(f"Stopped TouchSound watchdog (PID {pid}).")


def status(_args: argparse.Namespace) -> None:
    pid = _read_pid()
    if pid is None or not _pid_is_watchdog(pid):
        print("TouchSound watchdog is not running.")
        return
    print(f"TouchSound watchdog is running (PID {pid}).")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:8765/status", timeout=2) as response:
            print(response.read().decode("utf-8", "replace"))
    except OSError as exc:
        print(f"Bridge status endpoint is not responding: {exc}")
    print(f"Log: {LOG_FILE}")


def cycle_speaker(args: argparse.Namespace) -> None:
    """Restart a SoundTouch speaker through its local diagnostic shell."""
    try:
        reply = _telnet_reboot(args.speaker)
    except Exception as exc:
        raise SystemExit(f"Could not restart speaker {args.speaker}: {exc}") from exc
    print(f"Speaker {args.speaker} accepted restart command: {reply}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start", help="Start the detached watchdog and bridge")
    start_parser.add_argument("--speaker", required=True, help="SoundTouch IP or hostname")
    start_parser.add_argument("--relay-url", help="LAN URL the speaker can reach")
    start_parser.add_argument("--event-log", help="Path for the 48-hour speaker state history")
    start_parser.add_argument("--no-auto-reboot", action="store_true", help="Disable automatic speaker reboot")
    start_parser.set_defaults(handler=start)
    stop_parser = commands.add_parser("stop", help="Stop the watchdog and bridge")
    stop_parser.set_defaults(handler=stop)
    uninstall_parser = commands.add_parser("uninstall", help="Alias for stop; logs and configuration are kept")
    uninstall_parser.set_defaults(handler=stop)
    status_parser = commands.add_parser("status", help="Show watchdog and bridge health")
    status_parser.set_defaults(handler=status)
    cycle_parser = commands.add_parser("cycle-speaker", help="Restart the speaker using its local diagnostic shell")
    cycle_parser.add_argument("--speaker", required=True, help="SoundTouch IP or hostname")
    cycle_parser.set_defaults(handler=cycle_speaker)
    run_parser = commands.add_parser("run", help="Internal watchdog loop")
    run_parser.add_argument("--speaker", required=True)
    run_parser.add_argument("--relay-url")
    run_parser.add_argument("--no-auto-reboot", action="store_true")
    run_parser.add_argument("--event-log")
    run_parser.set_defaults(handler=_run_watchdog)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
