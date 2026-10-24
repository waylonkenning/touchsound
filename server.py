#!/usr/bin/env python3
"""Serve local SoundTouch station descriptors and transcoded radio streams."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "stations.json"
SLOT_PATTERN = re.compile(r"^/radio/([1-6])\.json$")
STREAM_PATTERN = re.compile(r"^/stream/([1-6])$")
WEB_ROOT = ROOT / "web"
WEB_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}


def load_stations() -> dict[str, dict[str, str]]:
    """Load and validate the preset-to-station map from stations.json."""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read {CONFIG_PATH}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("stations.json must contain an object keyed by preset number")

    stations: dict[str, dict[str, str]] = {}
    for slot, item in raw.items():
        if slot not in {str(i) for i in range(1, 7)}:
            raise ValueError(f"Invalid preset slot {slot!r}; use 1 through 6")
        if not isinstance(item, dict):
            raise ValueError(f"Preset {slot} must be an object")
        name = item.get("name", "")
        stream_url = item.get("streamUrl", "")
        if not isinstance(name, str) or not isinstance(stream_url, str):
            raise ValueError(f"Preset {slot} name and streamUrl must be strings")
        name, stream_url = name.strip(), stream_url.strip()
        if not name and not stream_url:
            continue
        if not name or not stream_url:
            raise ValueError(f"Preset {slot} needs both a name and a streamUrl")
        parsed = urlsplit(stream_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Preset {slot} streamUrl must be an absolute HTTP or HTTPS URL")
        if parsed.username or parsed.password:
            raise ValueError(f"Preset {slot} streamUrl must not contain credentials")
        stations[slot] = {"name": name, "streamUrl": stream_url}
    return stations


class MobileAccess:
    """Pair phone browsers with short-lived control sessions using a local code."""

    def __init__(self, token_file: Path) -> None:
        self.token_file = token_file.expanduser()
        self.code = self._load_or_create_code()
        self._sessions: set[str] = set()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def _load_or_create_code(self) -> str:
        self.token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            code = self.token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            code = f"{secrets.randbelow(1_000_000_000_000):012d}"
            try:
                descriptor = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                code = self.token_file.read_text(encoding="utf-8").strip()
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(code + "\n")
        if not re.fullmatch(r"\d{12}", code):
            raise ValueError(f"Mobile access code in {self.token_file} must contain exactly 12 digits")
        try:
            self.token_file.chmod(0o600)
        except OSError:
            pass
        return code

    def pair(self, client_ip: str, candidate: str) -> tuple[str | None, int | None]:
        """Return a session token, or a retry delay when the code is rejected."""
        try:
            address = ipaddress.ip_address(client_ip)
        except ValueError:
            return None, 900
        if not (address.is_private or address.is_loopback or address.is_link_local):
            return None, 900
        now = time.monotonic()
        with self._lock:
            locked_until = self._locked_until.get(client_ip, 0)
            if now < locked_until:
                return None, max(1, int(locked_until - now))
            if not hmac.compare_digest(self.code, candidate):
                failures = [stamp for stamp in self._failures.get(client_ip, []) if now - stamp < 900]
                failures.append(now)
                self._failures[client_ip] = failures
                if len(failures) >= 5:
                    self._locked_until[client_ip] = now + 900
                    return None, 900
                return None, None
            self._failures.pop(client_ip, None)
            self._locked_until.pop(client_ip, None)
            if len(self._sessions) >= 16:
                self._sessions.clear()
            session = secrets.token_urlsafe(32)
            self._sessions.add(session)
            return session, None

    def authorized(self, authorization: str) -> bool:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        with self._lock:
            return any(hmac.compare_digest(token, active) for active in self._sessions)


class StationHandler(BaseHTTPRequestHandler):
    """Serve station descriptors and live MP3 streams for configured slots."""

    server_version = "TouchSound/1.0"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in WEB_FILES:
            filename, content_type = WEB_FILES[path]
            try:
                body = (WEB_ROOT / filename).read_bytes()
            except OSError:
                self._send(503, b"TouchSound web app is unavailable\n", "text/plain; charset=utf-8")
                return
            self._send(200, body, content_type)
            return

        if path == "/api/config":
            try:
                stations = load_stations()
            except ValueError as exc:
                self._send_json(500, {"error": str(exc)})
                return
            runtime_status = getattr(self.server, "runtime_status", None)
            speaker = getattr(runtime_status, "speaker", "")
            speaker_name = getattr(self.server, "speaker_name", "") or speaker
            self._send_json(200, {
                "speaker": {"name": speaker_name, "address": speaker},
                "stations": [
                    {"slot": slot, "name": stations[slot]["name"] if slot in stations else None}
                    for slot in (str(number) for number in range(1, 7))
                ],
            })
            return

        if path == "/api/session":
            access = getattr(self.server, "mobile_access", None)
            if access is not None and access.authorized(self.headers.get("Authorization", "")):
                self._send_json(200, {"paired": True})
            else:
                self._send_json(401, {"error": "Phone is not paired with TouchSound"})
            return

        if path == "/health":
            try:
                load_stations()
            except ValueError as exc:
                self._send(500, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
                return
            runtime_status = getattr(self.server, "runtime_status", None)
            if runtime_status is None:
                self._send(200, b"ok\n", "text/plain; charset=utf-8")
                return
            snapshot = runtime_status.snapshot()
            healthy = bool(snapshot.get("healthy"))
            self._send(200 if healthy else 503, b"ok\n" if healthy else b"degraded\n", "text/plain; charset=utf-8")
            return

        if path == "/status":
            runtime_status = getattr(self.server, "runtime_status", None)
            if runtime_status is None:
                self._send(200, json.dumps({"healthy": True, "relay": "running"}).encode(), "application/json; charset=utf-8")
            else:
                self._send(200, json.dumps(runtime_status.snapshot(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            return

        if path == "/events":
            runtime_status = getattr(self.server, "runtime_status", None)
            history = (
                {"retentionHours": 48, "count": 0, "events": []}
                if runtime_status is None else runtime_status.event_history.recent()
            )
            self._send(200, json.dumps(history, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return

        stream_match = STREAM_PATTERN.fullmatch(path)
        if stream_match:
            self._stream(stream_match.group(1))
            return

        match = SLOT_PATTERN.fullmatch(path)
        if not match:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return

        slot = match.group(1)
        try:
            station = load_stations().get(slot)
        except ValueError as exc:
            self._send(500, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return
        if station is None:
            self._send(404, f"preset {slot} is not configured\n".encode(), "text/plain; charset=utf-8")
            return

        # This descriptor format is used by SoundTouch's LOCAL_INTERNET_RADIO source.
        descriptor = {
            "audio": {
                "hasPlaylist": True,
                "isRealtime": True,
                "streamUrl": station["streamUrl"],
            },
            "name": station["name"],
            "streamType": "liveRadio",
        }
        body = json.dumps(descriptor, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/pair":
            self._pair_phone()
            return

        reboot_request = path == "/control/reboot"
        play_match = re.fullmatch(r"/control/play/([1-6])", path)
        transport_action = path.removeprefix("/control/") if path in {
            "/control/pause", "/control/resume", "/control/stop"
        } else None
        volume_request = path == "/control/volume"
        if not reboot_request and play_match is None and transport_action is None and not volume_request:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        if not self._is_loopback_client() and not self._is_private_network_client():
            self._send_json(403, {"error": "Speaker controls are available only from the local network"})
            return
        if not self._is_loopback_client() and not self._mobile_authorized():
            self._send_json(401, {"error": "Pair this phone with TouchSound to control the speaker"})
            return
        if play_match is not None:
            request_play = getattr(self.server, "request_preset_play", None)
            if request_play is None:
                self._send(503, b"speaker control unavailable\n", "text/plain; charset=utf-8")
                return
            try:
                request_play(play_match.group(1))
            except Exception as exc:
                self._send(502, f"preset command failed: {exc}\n".encode("utf-8"), "text/plain; charset=utf-8")
                return
            self._send_json(202, {"message": "Preset command sent"})
            return
        if transport_action is not None:
            request_transport = getattr(self.server, "request_transport_control", None)
            if request_transport is None:
                self._send_json(503, {"error": "Speaker control unavailable"})
                return
            try:
                request_transport(transport_action)
            except Exception as exc:
                self._send_json(502, {"error": f"Speaker command failed: {exc}"})
                return
            self._send_json(202, {"message": f"{transport_action.title()} command sent"})
            return
        if volume_request:
            request_volume = getattr(self.server, "request_volume", None)
            if request_volume is None:
                self._send_json(503, {"error": "Speaker control unavailable"})
                return
            try:
                body = self._read_json_body()
                volume = body.get("volume")
                if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 100:
                    raise ValueError("Volume must be a whole number from 0 to 100")
                request_volume(volume)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._send_json(502, {"error": f"Speaker volume command failed: {exc}"})
                return
            self._send_json(202, {"message": "Volume command sent"})
            return
        request_reboot = getattr(self.server, "request_speaker_reboot", None)
        if request_reboot is None:
            self._send(503, b"speaker control unavailable\n", "text/plain; charset=utf-8")
            return
        request_reboot()
        self._send_json(202, {"message": "Speaker restart queued"})

    def _pair_phone(self) -> None:
        access = getattr(self.server, "mobile_access", None)
        if access is None:
            self._send_json(503, {"error": "Phone pairing is unavailable"})
            return
        if not self._is_loopback_client() and not self._is_private_network_client():
            self._send_json(403, {"error": "Phone pairing is available only from the local network"})
            return
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        code = body.get("code")
        if not isinstance(code, str):
            self._send_json(400, {"error": "Enter the 12-digit pairing code"})
            return
        session, retry_after = access.pair(self.client_address[0], code.strip())
        if session is None:
            if retry_after is not None:
                self._send_json(429, {"error": "Too many attempts. Try again in 15 minutes."})
            else:
                self._send_json(403, {"error": "That pairing code did not match"})
            return
        self._send_json(200, {"token": session})

    def _read_json_body(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request length") from exc
        if length <= 0 or length > 4096:
            raise ValueError("Request body must be between 1 and 4096 bytes")
        try:
            parsed = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must contain valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Request body must be a JSON object")
        return parsed

    def _is_loopback_client(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _is_private_network_client(self) -> bool:
        try:
            address = ipaddress.ip_address(self.client_address[0])
            return address.is_private or address.is_link_local
        except ValueError:
            return False

    def _mobile_authorized(self) -> bool:
        access = getattr(self.server, "mobile_access", None)
        return access is not None and access.authorized(self.headers.get("Authorization", ""))

    def _stream(self, slot: str) -> None:
        """Relay one upstream source as MP3, stopping FFmpeg on disconnect."""
        try:
            station = load_stations().get(slot)
        except ValueError as exc:
            self._send(500, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
            return
        if station is None:
            self._send(404, f"preset {slot} is not configured\n".encode(), "text/plain; charset=utf-8")
            return
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            self._send(503, b"ffmpeg is required for stream relay\n", "text/plain; charset=utf-8")
            return

        command = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", station["streamUrl"], "-map", "0:a:0", "-vn",
            "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-f", "mp3", "pipe:1",
        ]
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._send(503, f"Cannot start ffmpeg: {exc}\n".encode(), "text/plain; charset=utf-8")
            return

        stream_id = f"{threading.get_ident()}-{time.monotonic_ns()}"
        runtime_status = getattr(self.server, "runtime_status", None)
        if runtime_status is not None:
            runtime_status.stream_started(stream_id, slot, self.client_address[0])

        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            assert process.stdout is not None
            while True:
                block = process.stdout.read(32768)
                if not block:
                    break
                self.wfile.write(block)
                self.wfile.flush()
                if runtime_status is not None:
                    runtime_status.stream_bytes(stream_id, len(block))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if runtime_status is not None:
                runtime_status.stream_finished(stream_id)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args: object) -> None:
        if not self.path.startswith("/stream/"):
            print(f"{self.client_address[0]} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: all LAN interfaces)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    args = parser.parse_args()

    try:
        load_stations()
    except ValueError as exc:
        parser.error(str(exc))

    server = ThreadingHTTPServer((args.host, args.port), StationHandler)
    print(f"Serving local audio endpoints on http://{args.host}:{args.port}")
    print("Keep this service running while using the presets. Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
