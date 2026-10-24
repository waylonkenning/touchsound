#!/usr/bin/env python3
"""Build and install TouchSound's macOS menu bar app and login services."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
APP_NAME = "TouchSound.app"
APP_IDENTIFIER = "com.waylonkenning.touchsound.MenuBar"
BRIDGE_LABEL = "com.waylonkenning.touchsound.bridge"
MENU_LABEL = "com.waylonkenning.touchsound.menu"
SUPPORT = Path.home() / "Library/Application Support/TouchSound"
LOGS = Path.home() / "Library/Logs/TouchSound"
APPLICATIONS = Path.home() / "Applications"
AGENTS = Path.home() / "Library/LaunchAgents"


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def route_address(speaker: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.connect((speaker, 8090))
        return str(connection.getsockname()[0])


def build_app(destination: Path) -> None:
    executable_dir = destination / "Contents/MacOS"
    resources_dir = destination / "Contents/Resources"
    executable_dir.mkdir(parents=True)
    resources_dir.mkdir(parents=True)
    executable = executable_dir / "TouchSoundMenu"
    architecture = run(["/usr/bin/uname", "-m"]).stdout.strip()
    run([
        "/usr/bin/swiftc", "-O", "-target", f"{architecture}-apple-macosx13.0",
        "-framework", "AppKit", "-framework", "Foundation",
        "-framework", "Network", "-o", str(executable), str(PROJECT / "macos/TouchSoundMenu.swift"),
    ])
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": "TouchSoundMenu",
        "CFBundleIdentifier": APP_IDENTIFIER,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "TouchSound",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.1",
        "CFBundleVersion": "2",
        "LSUIElement": True,
        "NSLocalNetworkUsageDescription": (
            "TouchSound checks and controls your Bose SoundTouch speaker on your home network."
        ),
        "NSBonjourServices": ["_soundtouch._tcp"],
    }
    with (destination / "Contents/Info.plist").open("wb") as output:
        plistlib.dump(info, output)
    run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(destination)])


def write_plist(path: Path, contents: dict[str, object]) -> None:
    with path.open("wb") as output:
        plistlib.dump(contents, output)


def launchctl(args: list[str]) -> None:
    run(["/bin/launchctl", *args], check=False)


def install(args: argparse.Namespace) -> None:
    python = PROJECT / ".venv/bin/python"
    if not python.is_file():
        raise SystemExit(f"Python environment not found at {python}; follow the README setup steps first.")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("FFmpeg is required. Install it with `brew install ffmpeg`, then rerun the installer.")

    relay_url = args.relay_url or f"http://{route_address(args.speaker)}:8765"
    if not relay_url.startswith("http://"):
        raise SystemExit("--relay-url must be an http:// URL reachable by the speaker.")
    try:
        check = run([str(python), "-c", "import websocket"], check=False)
    except OSError as exc:
        raise SystemExit(f"Could not check Python dependencies: {exc}") from exc
    if check.returncode != 0:
        run([str(python), "-m", "pip", "install", "-r", str(PROJECT / "requirements.txt")])

    uid = os.getuid()
    domain = f"gui/{uid}"
    state_file = SUPPORT / "runtime.json"
    LOGS.mkdir(parents=True, exist_ok=True)
    SUPPORT.mkdir(parents=True, exist_ok=True)
    APPLICATIONS.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    config = {
        "speaker": args.speaker,
        "relayURL": relay_url.rstrip("/"),
        "port": 8765,
        "projectPath": str(PROJECT),
        "pythonPath": str(python),
        "servicePath": str(PROJECT / "service.py"),
        "bridgeLabel": BRIDGE_LABEL,
    }
    try:
        previous_config = json.loads((SUPPORT / "config.json").read_text(encoding="utf-8"))
        if previous_config.get("speaker") == args.speaker and previous_config.get("speakerName"):
            config["speakerName"] = previous_config["speakerName"]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    (SUPPORT / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="touchsound-app-") as temp:
        staged_app = Path(temp) / APP_NAME
        build_app(staged_app)

        menu_plist_path = AGENTS / f"{MENU_LABEL}.plist"
        menu_job = {
            "Label": MENU_LABEL,
            "ProgramArguments": [str(APPLICATIONS / APP_NAME / "Contents/MacOS/TouchSoundMenu")],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "LimitLoadToSessionType": "Aqua",
            "AssociatedBundleIdentifiers": [APP_IDENTIFIER],
            "StandardOutPath": str(LOGS / "menubar.stdout.log"),
            "StandardErrorPath": str(LOGS / "menubar.stderr.log"),
        }
        # Remove the previous separate bridge agent before replacing the app.
        launchctl(["bootout", f"{domain}/{BRIDGE_LABEL}"])
        launchctl(["disable", f"{domain}/{BRIDGE_LABEL}"])
        launchctl(["bootout", f"{domain}/{MENU_LABEL}"])
        run([str(python), str(PROJECT / "service.py"), "stop"], check=False)

        app_path = APPLICATIONS / APP_NAME
        if app_path.exists():
            shutil.rmtree(app_path)
        shutil.copytree(staged_app, app_path)
        try:
            (AGENTS / f"{BRIDGE_LABEL}.plist").unlink()
        except FileNotFoundError:
            pass
        write_plist(menu_plist_path, menu_job)

    launchctl(["enable", f"{domain}/{MENU_LABEL}"])
    menu_result = run(["/bin/launchctl", "bootstrap", domain, str(AGENTS / f"{MENU_LABEL}.plist")], check=False)
    if menu_result.returncode != 0:
        raise SystemExit(f"Could not start the menu bar app: {menu_result.stderr.strip()}")

    print("Installed TouchSound as a macOS login service.")
    print(f"Speaker: {args.speaker}")
    print(f"Relay URL: {relay_url.rstrip('/')}")
    print(f"Menu bar app: {APPLICATIONS / APP_NAME}")
    print(f"Logs: {LOGS}")
    print("TouchSound will start when you log in. macOS may ask to allow local network access.")
    print("Use the speaker icon in the menu bar to view health and control the service.")


def uninstall(_args: argparse.Namespace) -> None:
    domain = f"gui/{os.getuid()}"
    for label in (BRIDGE_LABEL, MENU_LABEL):
        launchctl(["bootout", f"{domain}/{label}"])
        launchctl(["disable", f"{domain}/{label}"])
    for label in (BRIDGE_LABEL, MENU_LABEL):
        try:
            (AGENTS / f"{label}.plist").unlink()
        except FileNotFoundError:
            pass
    app_path = APPLICATIONS / APP_NAME
    if app_path.exists():
        shutil.rmtree(app_path)
    print("Removed the TouchSound login jobs and menu bar app.")
    print(f"Configuration and logs remain in {SUPPORT} and {LOGS}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    install_parser = subcommands.add_parser("install", help="Install and start the menu bar app and login services")
    install_parser.add_argument("--speaker", required=True, help="SoundTouch IP or hostname")
    install_parser.add_argument("--relay-url", help="LAN URL reachable by the speaker; derived from the route when omitted")
    install_parser.set_defaults(handler=install)
    uninstall_parser = subcommands.add_parser("uninstall", help="Remove login jobs and the app; keep configuration and logs")
    uninstall_parser.set_defaults(handler=uninstall)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
