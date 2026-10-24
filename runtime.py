#!/usr/bin/env python3
"""Runtime health state and recovery supervisor for TouchSound."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


def _utc(timestamp: float | None) -> str | None:
    if timestamp is None or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _local_open(url_or_request: str | urllib.request.Request, timeout: int):
    """Talk directly to a speaker on the LAN, ignoring configured HTTP proxies."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(url_or_request, timeout=timeout)


class SpeakerEventHistory:
    """Keep a small, time-bounded record of speaker state changes and bridge commands."""

    RETENTION_SECONDS = 48 * 60 * 60
    STATE_EVENT_TYPES = {"speaker_state_initial", "speaker_state_changed"}

    def __init__(self, path: Path, relay_url: str) -> None:
        self.path = path.expanduser()
        self.relay_url = relay_url.rstrip("/")
        self._lock = threading.RLock()
        self._events: list[dict[str, object]] = []
        self._last_state: dict[str, object] | None = None
        self._last_state_at: float | None = None
        self._last_command: dict[str, object] | None = None
        self._load()

    @staticmethod
    def _event_time(event: dict[str, object]) -> float | None:
        at = event.get("at")
        if not isinstance(at, str):
            return None
        try:
            return datetime.fromisoformat(at).timestamp()
        except ValueError:
            return None

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and self._event_time(event) is not None:
                self._events.append(event)
        with self._lock:
            self._prune_locked(time.time())
            self._restore_markers_locked()
            self._write_locked()

    def _restore_markers_locked(self) -> None:
        self._last_state = None
        self._last_state_at = None
        self._last_command = None
        for event in self._events:
            event_time = self._event_time(event)
            if event_time is None:
                continue
            if event.get("type") in self.STATE_EVENT_TYPES and isinstance(event.get("state"), dict):
                self._last_state = event["state"]
                self._last_state_at = event_time
            elif event.get("type") == "touchsound_command":
                self._last_command = {**event, "_epoch": event_time}

    def _prune_locked(self, now: float) -> bool:
        cutoff = now - self.RETENTION_SECONDS
        previous_count = len(self._events)
        self._events = [
            event for event in self._events
            if (event_time := self._event_time(event)) is not None and event_time >= cutoff
        ]
        self._restore_markers_locked()
        return len(self._events) != previous_count

    def _write_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            content = "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in self._events)
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            # A logging failure must not interrupt speaker playback or health checks.
            pass

    def record(self, event_type: str, **details: object) -> None:
        now = time.time()
        event = {"at": _utc(now), "type": event_type, **details}
        with self._lock:
            self._prune_locked(now)
            self._events.append(event)
            self._prune_locked(now)
            self._write_locked()

    def command(self, action: str, **details: object) -> None:
        self.record("touchsound_command", action=action, **details)

    def _safe_track(self, uri: object) -> str | None:
        value = str(uri or "")
        if not value:
            return None
        match = re.fullmatch(re.escape(self.relay_url) + r"/stream/([1-6])", value)
        if match:
            return f"TouchSound preset {match.group(1)}"
        if value == "http://invalidHost.com/invalidUri.mp3":
            return "speaker standby placeholder"
        parsed = urlsplit(value)
        if parsed.scheme and parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}"
        return "other URI"

    def observe(self, speaker: dict[str, object], expected_slot: str | None, station: str | None) -> None:
        now = time.time()
        state: dict[str, object] = {
            "reachable": speaker.get("reachable"),
            "transportState": speaker.get("transportState"),
            "source": speaker.get("source"),
            "track": self._safe_track(speaker.get("trackUri")),
            "volume": speaker.get("volume"),
            "muted": speaker.get("muted"),
        }
        with self._lock:
            pruned = self._prune_locked(now)
            previous = self._last_state
            if previous is None:
                event_type = "speaker_state_initial"
                changes: dict[str, object] = {}
                attribution = "initial observation"
            else:
                changes = {
                    key: {"from": previous.get(key), "to": value}
                    for key, value in state.items()
                    if previous.get(key) != value
                }
                if not changes:
                    if pruned:
                        self._write_locked()
                    return
                event_type = "speaker_state_changed"
                command_epoch = float(self._last_command.get("_epoch", 0)) if self._last_command else 0
                after_last_observation = self._last_state_at is not None and command_epoch > self._last_state_at
                recent_command = after_last_observation and now - command_epoch <= 120
                attribution = (
                    "after TouchSound command" if recent_command
                    else "no recent TouchSound command; external action or automatic standby"
                )
            event: dict[str, object] = {
                "at": _utc(now),
                "type": event_type,
                "attribution": attribution,
                "changes": changes,
                "state": state,
                "expectedPreset": expected_slot,
                "station": station,
            }
            if attribution == "after TouchSound command" and self._last_command is not None:
                event["relatedCommand"] = {
                    "action": self._last_command.get("action"),
                    "slot": self._last_command.get("slot"),
                    "station": self._last_command.get("station"),
                }
            self._last_state = state
            self._last_state_at = now
            self._events.append(event)
            self._prune_locked(now)
            self._write_locked()

    def recent(self) -> dict[str, object]:
        with self._lock:
            self._prune_locked(time.time())
            self._write_locked()
            return {
                "retentionHours": 48,
                "count": len(self._events),
                "events": [dict(event) for event in self._events],
            }

    def count(self) -> int:
        with self._lock:
            self._prune_locked(time.time())
            return len(self._events)


class RuntimeStatus:
    """Thread-safe status shared by the bridge, relay, and supervisor."""

    def __init__(self, speaker: str, relay_url: str, event_log: Path | None = None) -> None:
        self.speaker = speaker
        self.relay_url = relay_url.rstrip("/")
        self.event_history = SpeakerEventHistory(
            event_log or (Path.home() / ".touchsound" / "speaker-history.jsonl"), self.relay_url
        )
        self._speaker_ips = {speaker}
        try:
            self._speaker_ips.update(item[4][0] for item in socket.getaddrinfo(speaker, None, type=socket.SOCK_STREAM))
        except socket.gaierror:
            pass
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._ws_connected = False
        self._relay_running = False
        self._relay_error: str | None = None
        self._last_ws_message: float | None = None
        self._expected_slot: str | None = None
        self._station_name: str | None = None
        self._play_requested_at: float | None = None
        self._selection_generation = 0
        self._last_play_error: str | None = None
        self._speaker: dict[str, object] = {
            "reachable": None,
            "checkedAt": None,
            "transportState": None,
            "playStatus": None,
            "trackUri": None,
            "source": None,
            "volume": None,
            "muted": None,
            "error": None,
        }
        self._streams: dict[str, dict[str, object]] = {}
        self._last_stream_request: float | None = None
        self._last_stream_bytes: float | None = None
        self._stream_bytes_total = 0
        self._supervisor: dict[str, object] = {
            "lastCheckAt": None,
            "lastAction": "starting",
            "lastActionAt": None,
            "lastError": None,
            "consecutiveFailures": 0,
            "lastRebootAttemptAt": None,
        }

    def websocket_opened(self) -> None:
        with self._lock:
            self._ws_connected = True

    def websocket_closed(self) -> None:
        with self._lock:
            self._ws_connected = False

    def websocket_message(self) -> None:
        with self._lock:
            self._last_ws_message = time.time()

    def relay_state(self, running: bool, error: str | None = None) -> None:
        with self._lock:
            self._relay_running = running
            self._relay_error = error

    def select_preset(self, slot: str, name: str) -> None:
        with self._lock:
            self._selection_generation += 1
            self._expected_slot = slot
            self._station_name = name
            self._play_requested_at = time.time()
            self._last_play_error = None
            self._supervisor["consecutiveFailures"] = 0

    def clear_expected_preset(self, reason: str) -> None:
        with self._lock:
            self._selection_generation += 1
            self._expected_slot = None
            self._station_name = None
            self._play_requested_at = None
            self._supervisor["lastAction"] = reason
            self._supervisor["lastActionAt"] = time.time()
            self._supervisor["consecutiveFailures"] = 0
        self.event_history.record("monitor_decision", action="clear_expected_preset", reason=reason)

    def record_command(self, action: str, **details: object) -> None:
        self.event_history.command(action, **details)

    def record_event(self, event_type: str, **details: object) -> None:
        self.event_history.record(event_type, **details)

    def play_command_started(self) -> None:
        with self._lock:
            self._play_requested_at = time.time()
            self._last_play_error = None

    def playback_error(self, error: str) -> None:
        with self._lock:
            self._last_play_error = error

    def speaker_check(self, result: dict[str, object]) -> None:
        with self._lock:
            self._speaker = {**result, "checkedAt": time.time()}
            slot = self._expected_slot
            station = self._station_name
        self.event_history.observe(result, slot, station)

    def stream_started(self, stream_id: str, slot: str, client_ip: str) -> None:
        with self._lock:
            self._streams[stream_id] = {
                "slot": slot,
                "clientIp": client_ip,
                "startedAt": time.time(),
                "bytes": 0,
                "lastByteAt": None,
            }
            if client_ip in self._speaker_ips:
                self._last_stream_request = time.time()

    def stream_bytes(self, stream_id: str, count: int) -> None:
        with self._lock:
            record = self._streams.get(stream_id)
            if record is None:
                return
            record["bytes"] = int(record["bytes"]) + count
            if record["clientIp"] in self._speaker_ips:
                self._stream_bytes_total += count
                self._last_stream_bytes = time.time()
                record["lastByteAt"] = self._last_stream_bytes

    def stream_finished(self, stream_id: str) -> None:
        with self._lock:
            self._streams.pop(stream_id, None)

    def supervisor_update(self, **values: object) -> None:
        with self._lock:
            if "lastAction" in values:
                values.setdefault("lastActionAt", time.time())
            self._supervisor.update(values)

    def expected_preset(self) -> tuple[str | None, str | None, float | None]:
        with self._lock:
            return self._expected_slot, self._station_name, self._play_requested_at

    def note_failure(self) -> int:
        with self._lock:
            count = int(self._supervisor["consecutiveFailures"]) + 1
            self._supervisor["consecutiveFailures"] = count
            return count

    def reset_failures(self) -> None:
        with self._lock:
            self._supervisor["consecutiveFailures"] = 0

    def mark_action(self, action: str, error: str | None = None, reboot_attempt: bool = False) -> None:
        now = time.time()
        with self._lock:
            self._supervisor["lastAction"] = action
            self._supervisor["lastActionAt"] = now
            self._supervisor["lastError"] = error
            if reboot_attempt:
                self._supervisor["lastRebootAttemptAt"] = now

    def snapshot(self) -> dict[str, object]:
        now = time.time()
        with self._lock:
            slot = self._expected_slot
            expected_uri = f"{self.relay_url}/stream/{slot}" if slot else None
            active_speaker_streams = [
                stream for stream in self._streams.values()
                if stream["clientIp"] in self._speaker_ips and stream["slot"] == slot
            ] if slot else []
            websocket_ok = self._ws_connected
            relay_ok = self._relay_running
            speaker_reachable = self._speaker.get("reachable") is True
            playback_active = slot is not None
            stream_recent = any(
                stream["lastByteAt"] is not None and now - float(stream["lastByteAt"]) < 60
                for stream in active_speaker_streams
            )
            playback_ok = (
                not playback_active
                or (
                    self._speaker.get("transportState") == "PLAYING"
                    and self._speaker.get("trackUri") == expected_uri
                    and bool(active_speaker_streams)
                    and stream_recent
                )
            )
            healthy = relay_ok and websocket_ok and speaker_reachable and playback_ok
            stream_bytes = sum(int(stream["bytes"]) for stream in active_speaker_streams)
            return {
                "healthy": healthy,
                "startedAt": _utc(self._started_at),
                "bridge": {
                    "relayRunning": relay_ok,
                    "relayError": self._relay_error,
                    "websocketConnected": websocket_ok,
                    "lastWebsocketMessageAt": _utc(self._last_ws_message),
                },
                "speaker": {
                    **self._speaker,
                    "checkedAt": _utc(self._speaker.get("checkedAt")),
                },
                "playback": {
                    "expectedPreset": slot,
                    "station": self._station_name,
                    "selectionGeneration": self._selection_generation,
                    "requestedAt": _utc(self._play_requested_at),
                    "expectedUri": expected_uri,
                    "streamClientConnected": bool(active_speaker_streams),
                    "streamBytes": stream_bytes,
                    "streamBytesTotal": self._stream_bytes_total,
                    "lastStreamRequestAt": _utc(self._last_stream_request),
                    "lastStreamBytesAt": _utc(self._last_stream_bytes),
                    "lastError": self._last_play_error,
                    "healthy": playback_ok,
                },
                "supervisor": {
                    **self._supervisor,
                    "lastCheckAt": _utc(self._supervisor.get("lastCheckAt")),
                    "lastActionAt": _utc(self._supervisor.get("lastActionAt")),
                    "lastRebootAttemptAt": _utc(self._supervisor.get("lastRebootAttemptAt")),
                },
                "eventHistory": {
                    "retentionHours": 48,
                    "count": self.event_history.count(),
                },
            }


def _speaker_snapshot(speaker: str) -> dict[str, object]:
    """Read the SoundTouch API and UPnP renderer state without changing playback."""
    now_playing: dict[str, str] = {}
    volume: int | None = None
    muted: bool | None = None
    try:
        with _local_open(f"http://{speaker}:8090/now_playing", timeout=4) as response:
            root = ET.fromstring(response.read())
        now_playing = { _tag_name(node.tag): (node.text or "").strip() for node in root.iter() }
        source = root.attrib.get("source")
        content_item = next((node for node in root.iter() if _tag_name(node.tag) == "ContentItem"), None)
        if content_item is not None and content_item.attrib.get("location"):
            now_playing["location"] = content_item.attrib["location"]
        now_playing["source"] = source or ""
        try:
            with _local_open(f"http://{speaker}:8090/volume", timeout=4) as response:
                volume_root = ET.fromstring(response.read())
            volume = int(next(node.text for node in volume_root.iter() if _tag_name(node.tag) == "actualvolume"))
            mute_text = next(node.text for node in volume_root.iter() if _tag_name(node.tag) == "muteenabled")
            muted = str(mute_text).lower() == "true"
        except (urllib.error.URLError, ET.ParseError, StopIteration, ValueError, TimeoutError):
            pass
    except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError) as exc:
        return {"reachable": False, "transportState": None, "playStatus": None, "trackUri": None,
                "source": None, "volume": volume, "muted": muted, "error": str(exc)}

    transport_state: str | None = None
    track_uri: str | None = None
    try:
        transport_state, track_uri = _read_upnp_transport(speaker)
    except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError, RuntimeError) as exc:
        return {"reachable": True, "transportState": None,
                "playStatus": now_playing.get("playStatus"), "trackUri": None,
                "source": now_playing.get("source"), "volume": volume, "muted": muted,
                "error": f"SoundTouch API responds, but UPnP status failed: {exc}"}
    return {
        "reachable": True,
        "transportState": transport_state,
        "playStatus": now_playing.get("playStatus"),
        "trackUri": track_uri,
        "source": now_playing.get("source"),
        "volume": volume,
        "muted": muted,
        "error": None,
    }


def _read_upnp_transport(speaker: str) -> tuple[str, str]:
    soap_ns = "http://schemas.xmlsoap.org/soap/envelope/"
    av_ns = "urn:schemas-upnp-org:service:AVTransport:1"

    def request(action_name: str) -> ET.Element:
        envelope = ET.Element(
            f"{{{soap_ns}}}Envelope",
            {f"{{{soap_ns}}}encodingStyle": "http://schemas.xmlsoap.org/soap/encoding/"},
        )
        body = ET.SubElement(envelope, f"{{{soap_ns}}}Body")
        action = ET.SubElement(body, f"{{{av_ns}}}{action_name}")
        ET.SubElement(action, "InstanceID").text = "0"
        payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
        req = urllib.request.Request(
            f"http://{speaker}:8091/AVTransport/Control",
            data=payload,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPACTION": f'"{av_ns}#{action_name}"',
            },
            method="POST",
        )
        with _local_open(req, timeout=4) as response:
            return ET.fromstring(response.read())

    def value(root: ET.Element, name: str) -> str:
        for node in root.iter():
            if _tag_name(node.tag) == name:
                return (node.text or "").strip()
        raise RuntimeError(f"UPnP response did not contain {name}")

    state_root = request("GetTransportInfo")
    position_root = request("GetPositionInfo")
    return value(state_root, "CurrentTransportState"), value(position_root, "TrackURI")


def _telnet_reboot(speaker: str) -> str:
    """Issue the SoundTouch diagnostic-shell reboot command on TCP 17000."""
    chunks: list[bytes] = []
    with socket.create_connection((speaker, 17000), timeout=4) as connection:
        connection.settimeout(1)
        try:
            chunks.append(connection.recv(256))
        except (socket.timeout, OSError):
            pass
        connection.sendall(b"sys reboot\r\n")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                data = connection.recv(512)
            except (socket.timeout, OSError):
                break
            if not data:
                break
            chunks.append(data)
            if b"Rebooting" in data or b"OK" in data:
                break
    reply = b"".join(chunks).decode("utf-8", "replace").strip()
    if "Rebooting" not in reply and "OK" not in reply:
        raise RuntimeError(f"No reboot acknowledgement from speaker: {reply or 'empty response'}")
    return reply


class SpeakerSupervisor:
    """Watch the bridge and speaker, recover playback, and reboot sparingly."""

    def __init__(
        self,
        speaker: str,
        relay_url: str,
        status: RuntimeStatus,
        play_preset: Callable[[str], None],
        stations: dict[str, dict[str, str]],
        interval: int = 15,
        reboot_cooldown: int = 1800,
        auto_reboot: bool = True,
        state_file: Path | None = None,
    ) -> None:
        self.speaker = speaker
        self.relay_url = relay_url.rstrip("/")
        self.status = status
        self.play_preset = play_preset
        self.stations = stations
        self.interval = interval
        self.reboot_cooldown = reboot_cooldown
        self.auto_reboot = auto_reboot
        self.state_file = state_file or (Path.home() / ".touchsound" / "runtime.json")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="touchsound-supervisor", daemon=True)
        self._failures = 0
        self._replayed_for_selection = False
        self._selection_generation: int | None = None
        self._last_reboot = self._load_last_reboot()
        self._reboot_recovery_at: float | None = None
        self._reboot_recovery_deadline: float | None = None
        self._wake = threading.Event()
        self._manual_reboot_requested = threading.Event()

    def _load_last_reboot(self) -> float:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            value = float(data.get("lastRebootAttemptAt", 0))
            self.status.supervisor_update(lastRebootAttemptAt=value)
            return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0.0

    def _save_last_reboot(self, timestamp: float) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            temporary.write_text(json.dumps({"lastRebootAttemptAt": timestamp}) + "\n", encoding="utf-8")
            temporary.replace(self.state_file)
        except OSError as exc:
            self.status.supervisor_update(lastError=f"Could not save reboot cooldown: {exc}")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=3)

    def request_manual_reboot(self) -> None:
        """Queue a user-requested reboot for the supervisor thread."""
        self._manual_reboot_requested.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._manual_reboot_requested.is_set():
                self._manual_reboot_requested.clear()
                self._manual_reboot()
            else:
                try:
                    self.check_once()
                except Exception as exc:  # Keep the health loop alive after an unexpected probe error.
                    self.status.supervisor_update(lastError=f"Health monitor error: {exc}")
            self._wake.wait(self.interval)
            self._wake.clear()

    def _manual_reboot(self) -> None:
        now = time.time()
        try:
            reply = _telnet_reboot(self.speaker)
        except Exception as exc:
            self.status.record_event("speaker_reboot_attempt", requestedBy="user", acknowledged=False)
            self.status.mark_action("Manual speaker reboot failed", str(exc))
            return
        self.status.record_command("speaker_reboot", requestedBy="user", acknowledged=True)
        self._last_reboot = now
        self._save_last_reboot(now)
        self._reboot_recovery_at = now
        self._reboot_recovery_deadline = now + 60
        self._failures = 0
        self.status.mark_action("Speaker restarted manually", reply, reboot_attempt=True)

    def check_once(self) -> None:
        now = time.time()
        result = _speaker_snapshot(self.speaker)
        self.status.speaker_check(result)
        self.status.supervisor_update(lastCheckAt=now)
        slot, _name, requested_at = self.status.expected_preset()
        if slot is None and result.get("reachable") is True:
            track_uri = str(result.get("trackUri") or "")
            match = re.fullmatch(re.escape(self.relay_url) + r"/stream/([1-6])", track_uri)
            if match and match.group(1) in self.stations:
                slot = match.group(1)
                station_name = self.stations[slot]["name"]
                self.status.select_preset(slot, station_name)
                slot, _name, requested_at = self.status.expected_preset()
        selection_generation = self.status.snapshot()["playback"]["selectionGeneration"]
        if selection_generation != self._selection_generation:
            self._selection_generation = int(selection_generation)
            self._failures = 0
            self._replayed_for_selection = False

        if self._reboot_recovery_at is not None:
            if now > (self._reboot_recovery_deadline or now) + 60:
                self.status.mark_action("Speaker did not return after reboot", "Check speaker power and Wi-Fi")
                self._reboot_recovery_at = None
                self._reboot_recovery_deadline = None
                return
            if now >= (self._reboot_recovery_deadline or now) and result.get("reachable") is True:
                slot, _name, _requested_at = self.status.expected_preset()
                if slot:
                    try:
                        self.play_preset(slot)
                        self.status.mark_action(f"Replayed preset {slot} after speaker reboot")
                    except Exception as exc:
                        self.status.mark_action("Playback replay after reboot failed", str(exc))
                else:
                    self.status.mark_action("Speaker responded after reboot; no preset is active")
                self._reboot_recovery_at = None
                self._reboot_recovery_deadline = None
                self._failures = 0
            return

        if result.get("reachable") is not True:
            self._failures += 1
            self.status.supervisor_update(consecutiveFailures=self._failures)
            if self._failures >= 3:
                self._reboot(None, "SoundTouch API has not responded for three checks")
            return

        if slot is None:
            self._failures = 0
            self.status.reset_failures()
            current = self.status.snapshot()
            bridge = current["bridge"]
            if isinstance(bridge, dict) and bridge.get("relayRunning") and bridge.get("websocketConnected"):
                self.status.supervisor_update(lastAction="Bridge and speaker healthy", lastError=None)
            return

        state = str(result.get("transportState") or "")
        source = str(result.get("source") or "")
        track_uri = str(result.get("trackUri") or "")
        expected_uri = f"{self.relay_url}/stream/{slot}"
        snapshot = self.status.snapshot()
        stream = snapshot["playback"]
        assert isinstance(stream, dict)

        # A manual pause or switch to another source is user intent, not a fault.
        if state == "PAUSED_PLAYBACK" or (source and source not in {"UPNP", "INVALID_SOURCE"}):
            self.status.clear_expected_preset("Playback monitoring paused after a manual pause or source change")
            self._failures = 0
            self._replayed_for_selection = False
            return

        still_starting = requested_at is not None and now - requested_at < 45
        stream_ok = bool(stream["streamClientConnected"]) and bool(stream["lastStreamBytesAt"])
        playback_ok = state == "PLAYING" and track_uri == expected_uri and stream_ok and bool(stream["healthy"])
        if playback_ok:
            self._failures = 0
            self._replayed_for_selection = False
            self.status.reset_failures()
            self.status.supervisor_update(lastAction="Preset playback healthy", lastError=None)
            return
        if still_starting:
            self.status.supervisor_update(lastAction="Waiting for speaker stream", lastError=None)
            return

        self._failures += 1
        self.status.supervisor_update(consecutiveFailures=self._failures)
        if not self._replayed_for_selection and self._failures >= 2:
            try:
                self.play_preset(slot)
                self._replayed_for_selection = True
                self._failures = 0
                self.status.mark_action(f"Reissued preset {slot} after playback check failed")
            except Exception as exc:
                self.status.mark_action(f"Could not reissue preset {slot}", str(exc))
            return
        if self._replayed_for_selection and self._failures >= 5:
            self._reboot(slot, "Playback remained unhealthy after a replay attempt")
            return
        if not self._replayed_for_selection:
            self.status.supervisor_update(lastAction="Checking playback recovery", lastError=None)

    def _reboot(self, slot: str | None, reason: str) -> None:
        now = time.time()
        if not self.auto_reboot:
            self.status.mark_action("Automatic reboot disabled", reason)
            return
        if now - self._last_reboot < self.reboot_cooldown:
            self.status.mark_action(
                "Automatic reboot suppressed by cooldown",
                f"{reason}; last reboot was less than {self.reboot_cooldown} seconds ago",
            )
            return
        try:
            reply = _telnet_reboot(self.speaker)
        except (OSError, socket.timeout) as exc:
            # A connection failure means the command was not delivered. Retry
            # after the recovery wait; do not consume the reboot cooldown.
            self.status.record_event("speaker_reboot_attempt", requestedBy="supervisor", reason=reason, acknowledged=False)
            self.status.mark_action("Speaker reboot command could not connect", f"{reason}: {exc}")
            self._reboot_recovery_at = now
            self._reboot_recovery_deadline = now + 60
            return
        except RuntimeError as exc:
            # The command was sent, but the speaker did not confirm it. Keep a
            # cooldown because it may have rebooted before the reply was read.
            self._last_reboot = now
            self._save_last_reboot(now)
            self.status.record_event("speaker_reboot_attempt", requestedBy="supervisor", reason=reason, acknowledged=False)
            self.status.mark_action("Speaker reboot acknowledgement missing", f"{reason}: {exc}", reboot_attempt=True)
            self._reboot_recovery_at = now
            self._reboot_recovery_deadline = now + 60
            return
        self.status.record_command("speaker_reboot", requestedBy="supervisor", reason=reason, acknowledged=True)
        self._last_reboot = now
        self._save_last_reboot(now)
        self._reboot_recovery_at = now
        self._reboot_recovery_deadline = now + 60
        self._failures = 0
        self.status.mark_action(f"Rebooted speaker after: {reason}", reply, reboot_attempt=True)
