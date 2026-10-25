#!/usr/bin/env python3
"""Serve local SoundTouch station descriptors and transcoded radio streams."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import random
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "stations.json"
_station_overrides = os.environ.get("TOUCHSOUND_STATIONS_OVERRIDES_FILE", "").strip()
STATION_OVERRIDES_PATH = Path(_station_overrides).expanduser() if _station_overrides else None
SLOT_PATTERN = re.compile(r"^/radio/([1-6])\.json$")
STREAM_PATTERN = re.compile(r"^/stream/([1-6])$")
STATION_UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RADIO_BROWSER_DNS_NAME = "all.api.radio-browser.info"
RADIO_BROWSER_TIMEOUT_SECONDS = 6
RADIO_BROWSER_MAX_RESPONSE_BYTES = 2_000_000
RADIO_BROWSER_CACHE_SECONDS = 300
_radio_browser_hosts_lock = threading.Lock()
_radio_browser_hosts_cache: tuple[float, list[str]] = (0, [])
_station_search_cache_lock = threading.Lock()
_station_search_cache: dict[tuple[str, str], tuple[float, list[dict[str, object]]]] = {}
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

    if STATION_OVERRIDES_PATH is not None:
        try:
            overrides = json.loads(STATION_OVERRIDES_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            overrides = {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read {STATION_OVERRIDES_PATH}: {exc}") from exc
        if not isinstance(overrides, dict):
            raise ValueError("Station overrides must contain an object keyed by preset number")
        raw.update(overrides)

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


def radio_browser_hosts() -> list[str]:
    """Discover healthy Radio Browser mirrors through DNS and reverse DNS."""
    global _radio_browser_hosts_cache
    now = time.time()
    with _radio_browser_hosts_lock:
        expires_at, cached = _radio_browser_hosts_cache
        if now < expires_at and cached:
            return list(cached)

    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(RADIO_BROWSER_DNS_NAME, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise RuntimeError("Could not discover Radio Browser servers") from exc

    hosts: set[str] = set()
    for address in addresses:
        try:
            hostname = socket.gethostbyaddr(address)[0].rstrip(".").lower()
        except OSError:
            continue
        if hostname.endswith(".api.radio-browser.info") and hostname != RADIO_BROWSER_DNS_NAME:
            hosts.add(hostname)
    if not hosts:
        raise RuntimeError("No Radio Browser servers were discovered")

    discovered = sorted(hosts)
    random.SystemRandom().shuffle(discovered)
    with _radio_browser_hosts_lock:
        _radio_browser_hosts_cache = (now + 3600, discovered)
    return discovered


def radio_browser_request(path: str, parameters: dict[str, str]) -> object:
    """Read a bounded JSON response from an available Radio Browser mirror."""
    failures: list[str] = []
    for host in radio_browser_hosts():
        url = f"https://{host}{path}?{urlencode(parameters)}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "TouchSound/1.1 (+https://github.com/waylonkenning/touchsound)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=RADIO_BROWSER_TIMEOUT_SECONDS) as response:
                if urlsplit(response.geturl()).scheme != "https":
                    raise RuntimeError("Radio Browser redirected to an insecure connection")
                payload = response.read(RADIO_BROWSER_MAX_RESPONSE_BYTES + 1)
            if len(payload) > RADIO_BROWSER_MAX_RESPONSE_BYTES:
                raise RuntimeError("Radio Browser returned too much data")
            return json.loads(payload)
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError, RuntimeError) as exc:
            failures.append(f"{host}: {exc}")
    raise RuntimeError("Radio Browser is temporarily unavailable") from (OSError("; ".join(failures)) if failures else None)


def search_radio_stations(query: str, mode: str) -> list[dict[str, object]]:
    """Search working Radio Browser stations by name or genre tag."""
    cache_key = (mode, query.casefold())
    now = time.time()
    with _station_search_cache_lock:
        cached = _station_search_cache.get(cache_key)
        if cached and now < cached[0]:
            return [dict(item) for item in cached[1]]

    parameters = {
        "hidebroken": "true",
        "order": "clickcount",
        "reverse": "true",
        "limit": "20",
        mode: query,
    }
    result = radio_browser_request("/json/stations/search", parameters)
    if not isinstance(result, list):
        raise RuntimeError("Radio Browser returned an unexpected search response")

    stations: list[dict[str, object]] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        station_uuid = item.get("stationuuid")
        name = item.get("name")
        if not isinstance(station_uuid, str) or not STATION_UUID_PATTERN.fullmatch(station_uuid):
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        name = normalize_station_name(name)
        try:
            bitrate = int(item.get("bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        stations.append({
            "stationUuid": station_uuid.lower(),
            "name": name.strip()[:200],
            "country": str(item.get("country") or ""),
            "countryCode": str(item.get("countrycode") or ""),
            "codec": str(item.get("codec") or "").upper(),
            "bitrate": max(0, bitrate),
            "tags": str(item.get("tags") or "")[:160],
        })

    with _station_search_cache_lock:
        _station_search_cache[cache_key] = (now + RADIO_BROWSER_CACHE_SECONDS, stations)
        if len(_station_search_cache) > 128:
            expired = [key for key, value in _station_search_cache.items() if value[0] <= now]
            for key in expired:
                _station_search_cache.pop(key, None)
            if len(_station_search_cache) > 128:
                oldest = min(_station_search_cache, key=lambda key: _station_search_cache[key][0])
                _station_search_cache.pop(oldest, None)
    return [dict(item) for item in stations]


def fetch_radio_station(station_uuid: str) -> dict[str, str]:
    """Fetch a station's current direct stream URL by its stable UUID."""
    result = radio_browser_request("/json/stations/byuuid", {"uuids": station_uuid})
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        raise ValueError("That station is no longer listed in Radio Browser")
    item = result[0]
    name, stream_url = item.get("name"), item.get("url_resolved") or item.get("url")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Radio Browser did not provide a station name")
    if not isinstance(stream_url, str) or not stream_url.strip():
        raise ValueError("Radio Browser did not provide a direct station stream URL")
    name = normalize_station_name(name)
    try:
        return validate_station_definition(name, stream_url)
    except ValueError as exc:
        raise ValueError(f"Radio Browser returned an invalid station stream URL: {exc}") from exc


def normalize_station_name(name: str) -> str:
    """Normalize directory titles and fit the 200-character UI field limit."""
    normalized = " ".join(name.split())
    if len(normalized) > 200:
        return f"{normalized[:197].rstrip()}..."
    return normalized


def validate_station_definition(name_value: object, stream_url_value: object) -> dict[str, str]:
    """Validate a manually entered or directory-provided station definition."""
    if not isinstance(name_value, str) or not isinstance(stream_url_value, str):
        raise ValueError("Enter both a station name and stream URL")
    name = name_value.strip()
    stream_url = stream_url_value.strip()
    if not name or not stream_url:
        raise ValueError("Enter both a station name and stream URL")
    if len(name) > 200 or any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ValueError("Station names must be at most 200 characters and contain no control characters")
    if len(stream_url) > 2048 or any(character.isspace() or ord(character) < 32 for character in stream_url):
        raise ValueError("Stream URLs must be at most 2048 characters and contain no whitespace")
    try:
        parsed = urlsplit(stream_url)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid stream URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Stream URL must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("Stream URLs must not contain login credentials")
    return {"name": name, "streamUrl": stream_url}


def save_station_preset(slot: str, station: dict[str, str]) -> None:
    """Persist one preset in the writable override file or base configuration."""
    target = STATION_OVERRIDES_PATH or CONFIG_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read station configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Station configuration must be an object keyed by preset number")
    raw[slot] = station

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        permissions = target.stat().st_mode & 0o777
    except OSError:
        permissions = 0o600
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent,
            prefix=f".{target.name}-", suffix=".tmp", delete=False,
        ) as output:
            temporary_path = Path(output.name)
            os.fchmod(output.fileno(), permissions)
            json.dump(raw, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def load_discovered_speakers(path: Path | str | None) -> list[dict[str, str]]:
    """Read the Bonjour inventory maintained by the macOS menu app."""
    if not path:
        return []
    try:
        raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    speakers: dict[str, dict[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name, address = item.get("name"), item.get("address")
        if not isinstance(name, str) or not isinstance(address, str):
            continue
        name, address = name.strip(), address.strip()
        if name and address:
            # Network.framework can append an interface zone to an IPv4 host
            # (for example, 192.168.4.28%en0). SoundTouch control uses the
            # plain IPv4 address, so remove that zone before exposing it.
            unscoped = address.partition("%")[0]
            try:
                parsed = ipaddress.ip_address(unscoped)
            except ValueError:
                pass
            else:
                if isinstance(parsed, ipaddress.IPv4Address):
                    address = str(parsed)
            speakers[address] = {"name": name, "address": address}
    return sorted(speakers.values(), key=lambda speaker: speaker["name"].casefold())


class MobileAccess:
    """Pair local phone browsers with sessions that survive bridge restarts."""

    SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
    MAX_SESSIONS = 16

    def __init__(self, token_file: Path) -> None:
        self.token_file = token_file.expanduser()
        self.session_file = self.token_file.with_name("mobile-sessions.json")
        self.code = self._load_or_create_code()
        self._lock = threading.Lock()
        self._sessions = self._load_sessions()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

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

    def _load_sessions(self) -> dict[str, float]:
        try:
            raw = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        now = time.time()
        sessions = {
            token: float(expires)
            for token, expires in raw.items()
            if isinstance(token, str)
            and len(token) == 43
            and isinstance(expires, (int, float))
            and not isinstance(expires, bool)
            and now < expires <= now + self.SESSION_TTL_SECONDS
        }
        if len(sessions) > self.MAX_SESSIONS:
            sessions = dict(sorted(sessions.items(), key=lambda item: item[1], reverse=True)[:self.MAX_SESSIONS])
        try:
            self.session_file.chmod(0o600)
        except OSError:
            pass
        return sessions

    def _save_sessions(self) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.session_file.parent,
                prefix=".mobile-sessions-", suffix=".tmp", delete=False,
            ) as output:
                temporary_path = Path(output.name)
                os.fchmod(output.fileno(), 0o600)
                json.dump(self._sessions, output, separators=(",", ":"))
                output.write("\n")
            os.replace(temporary_path, self.session_file)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
        try:
            self.session_file.chmod(0o600)
        except OSError:
            pass

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
            current_time = time.time()
            self._sessions = {
                token: expires for token, expires in self._sessions.items() if expires > current_time
            }
            if len(self._sessions) >= self.MAX_SESSIONS:
                oldest = min(self._sessions, key=self._sessions.get)
                del self._sessions[oldest]
            session = secrets.token_urlsafe(32)
            self._sessions[session] = current_time + self.SESSION_TTL_SECONDS
            try:
                self._save_sessions()
            except OSError:
                self._sessions.pop(session, None)
                raise
            return session, None

    def authorized(self, authorization: str) -> bool:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        with self._lock:
            matched = next((active for active in self._sessions if hmac.compare_digest(token, active)), None)
            if matched is None:
                return False
            if self._sessions[matched] > time.time():
                return True
            del self._sessions[matched]
            try:
                self._save_sessions()
            except OSError:
                # The expired session is already rejected in memory; a stale
                # expired entry on disk is harmless and will be pruned on load.
                pass
            return False


class StationHandler(BaseHTTPRequestHandler):
    """Serve station descriptors and live MP3 streams for configured slots."""

    server_version = "TouchSound/1.0"

    def do_GET(self) -> None:
        parsed_path = urlsplit(self.path)
        path = parsed_path.path
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
                    {
                        "slot": slot,
                        "name": stations[slot]["name"] if slot in stations else None,
                        "streamUrl": stations[slot]["streamUrl"] if slot in stations else None,
                    }
                    for slot in (str(number) for number in range(1, 7))
                ],
            })
            return

        if path == "/api/speakers":
            runtime_status = getattr(self.server, "runtime_status", None)
            address = str(getattr(runtime_status, "speaker", "") or "")
            name = str(getattr(self.server, "speaker_name", "") or address)
            discovery_file = getattr(self.server, "speaker_discovery_file", None)
            config_file = getattr(self.server, "speaker_config_file", None)
            self._send_json(200, {
                "canSwitch": bool(discovery_file and config_file),
                "current": {"name": name, "address": address},
                "speakers": load_discovered_speakers(discovery_file),
            })
            return

        if path == "/api/stations/search":
            query_values = parse_qs(parsed_path.query, keep_blank_values=True)
            query = query_values.get("q", [""])[0].strip()
            mode = query_values.get("mode", ["name"])[0].strip().lower()
            if len(query) < 2 or len(query) > 80:
                self._send_json(400, {"error": "Enter 2 to 80 characters to search"})
                return
            if mode not in {"name", "tag"}:
                self._send_json(400, {"error": "Search by station name or genre"})
                return
            try:
                stations = search_radio_stations(query, mode)
            except RuntimeError:
                self._send_json(502, {"error": "Radio station search is temporarily unavailable. Try again shortly."})
                return
            self._send_json(200, {"provider": "Radio Browser", "count": len(stations), "stations": stations})
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
        speaker_request = path == "/control/speaker"
        station_assignment_request = path == "/control/stations/assign"
        station_edit_request = path == "/control/stations/edit"
        transport_action = path.removeprefix("/control/") if path in {
            "/control/pause", "/control/resume", "/control/stop"
        } else None
        volume_request = path == "/control/volume"
        if not reboot_request and play_match is None and not speaker_request and not station_assignment_request and not station_edit_request and transport_action is None and not volume_request:
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
        if station_edit_request:
            request_edit = getattr(self.server, "request_station_edit", None)
            if request_edit is None:
                self._send_json(503, {"error": "Preset editing is unavailable in this TouchSound setup"})
                return
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            slot = body.get("slot")
            if not isinstance(slot, str) or slot not in {str(number) for number in range(1, 7)}:
                self._send_json(400, {"error": "Choose preset 1 through 6"})
                return
            try:
                station = validate_station_definition(body.get("name"), body.get("streamUrl"))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            try:
                save_station_preset(slot, station)
                request_edit(slot, station)
            except (OSError, ValueError) as exc:
                self._send_json(500, {"error": f"Could not save preset {slot}: {exc}"})
                return
            except Exception as exc:
                self._send_json(502, {"error": f"Preset was saved, but TouchSound could not reload it: {exc}"})
                return
            self._send_json(200, {
                "message": f"Preset {slot} now uses {station['name']}",
                "slot": slot,
                "name": station["name"],
            })
            return
        if station_assignment_request:
            request_assignment = getattr(self.server, "request_station_assignment", None)
            if request_assignment is None:
                self._send_json(503, {"error": "Preset editing is unavailable in this TouchSound setup"})
                return
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            slot = body.get("slot")
            station_uuid = body.get("stationUuid")
            if not isinstance(slot, str) or slot not in {str(number) for number in range(1, 7)}:
                self._send_json(400, {"error": "Choose preset 1 through 6"})
                return
            if not isinstance(station_uuid, str) or not STATION_UUID_PATTERN.fullmatch(station_uuid):
                self._send_json(400, {"error": "Choose a station from the search results"})
                return
            try:
                station = fetch_radio_station(station_uuid.lower())
            except ValueError as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except RuntimeError:
                self._send_json(502, {"error": "Radio station details are temporarily unavailable. Try again shortly."})
                return
            try:
                save_station_preset(slot, station)
                request_assignment(slot, station, station_uuid.lower())
            except (OSError, ValueError) as exc:
                self._send_json(500, {"error": f"Could not save preset {slot}: {exc}"})
                return
            except Exception as exc:
                self._send_json(502, {"error": f"Preset was saved, but TouchSound could not reload it: {exc}"})
                return
            self._send_json(200, {
                "message": f"Preset {slot} now uses {station['name']}",
                "slot": slot,
                "name": station["name"],
            })
            return
        if speaker_request:
            config_file = getattr(self.server, "speaker_config_file", None)
            discovery_file = getattr(self.server, "speaker_discovery_file", None)
            if not config_file or not discovery_file:
                self._send_json(503, {"error": "Speaker switching is available from the macOS menu bar app"})
                return
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            address = body.get("address")
            if not isinstance(address, str) or not address.strip():
                self._send_json(400, {"error": "Choose a discovered SoundTouch speaker"})
                return
            selected = next(
                (speaker for speaker in load_discovered_speakers(discovery_file) if speaker["address"] == address.strip()),
                None,
            )
            if selected is None:
                self._send_json(409, {"error": "That speaker is no longer available. Refresh and try again."})
                return
            runtime_status = getattr(self.server, "runtime_status", None)
            if selected["address"] == str(getattr(runtime_status, "speaker", "") or ""):
                self._send_json(200, {"message": "That speaker is already selected"})
                return
            try:
                self._save_speaker_selection(Path(config_file), selected)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send_json(500, {"error": f"Could not save the selected speaker: {exc}"})
                return
            self._send_json(202, {
                "message": f"Switching to {selected['name']}; TouchSound will reconnect shortly",
                "speaker": selected,
            })
            return
        request_reboot = getattr(self.server, "request_speaker_reboot", None)
        if request_reboot is None:
            self._send(503, b"speaker control unavailable\n", "text/plain; charset=utf-8")
            return
        request_reboot()
        self._send_json(202, {"message": "Speaker restart queued"})

    def _save_speaker_selection(self, config_file: Path, speaker: dict[str, str]) -> None:
        try:
            config = json.loads(config_file.expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read TouchSound configuration: {exc}") from exc
        if not isinstance(config, dict):
            raise ValueError("TouchSound configuration must be a JSON object")
        config["speaker"] = speaker["address"]
        config["speakerName"] = speaker["name"]
        target = config_file.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=target.parent,
                prefix=f".{target.name}-", suffix=".tmp", delete=False,
            ) as output:
                temporary_path = Path(output.name)
                os.fchmod(output.fileno(), 0o600)
                json.dump(config, output, ensure_ascii=False, indent=2, sort_keys=True)
                output.write("\n")
            os.replace(temporary_path, target)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

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
        try:
            session, retry_after = access.pair(self.client_address[0], code.strip())
        except OSError as exc:
            self._send_json(500, {"error": f"Could not save phone pairing: {exc}"})
            return
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
