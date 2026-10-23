#!/usr/bin/env python3
"""Serve local SoundTouch station descriptors and transcoded radio streams."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "stations.json"
SLOT_PATTERN = re.compile(r"^/radio/([1-6])\.json$")
STREAM_PATTERN = re.compile(r"^/stream/([1-6])$")


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


class StationHandler(BaseHTTPRequestHandler):
    """Serve station descriptors and live MP3 streams for configured slots."""

    server_version = "TouchSound/1.0"

    def do_GET(self) -> None:
        if self.path == "/health":
            try:
                load_stations()
            except ValueError as exc:
                self._send(500, str(exc).encode("utf-8"), "text/plain; charset=utf-8")
                return
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return

        stream_match = STREAM_PATTERN.fullmatch(urlsplit(self.path).path)
        if stream_match:
            self._stream(stream_match.group(1))
            return

        match = SLOT_PATTERN.fullmatch(urlsplit(self.path).path)
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

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
