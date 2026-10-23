#!/usr/bin/env python3
"""Restore physical SoundTouch preset buttons with a local WebSocket-to-audio bridge."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer

import websocket

from server import StationHandler, load_stations


def local_ip_for(speaker: str) -> str:
    """Return the local interface address the OS routes toward the speaker."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((speaker, 8090))
        return str(sock.getsockname()[0])


def tag_name(tag: str) -> str:
    """Strip an optional XML namespace from an element tag."""
    return tag.rsplit("}", 1)[-1]


def parse_preset_press(message: str) -> str | None:
    """Return a preset slot from a SoundTouch selection event, if present."""
    try:
        root = ET.fromstring(message)
    except ET.ParseError:
        return None
    for update in root.iter():
        if tag_name(update.tag) != "nowSelectionUpdated":
            continue
        for child in update.iter():
            if tag_name(child.tag) == "preset":
                slot = child.attrib.get("id", "")
                if slot in {str(value) for value in range(1, 7)}:
                    return slot
    return None


def make_didl(name: str, url: str) -> str:
    """Build UPnP DIDL-Lite metadata describing one live audio stream."""
    didl_ns = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
    dc_ns = "http://purl.org/dc/elements/1.1/"
    upnp_ns = "urn:schemas-upnp-org:metadata-1-0/upnp/"
    ET.register_namespace("", didl_ns)
    ET.register_namespace("dc", dc_ns)
    ET.register_namespace("upnp", upnp_ns)
    root = ET.Element(f"{{{didl_ns}}}DIDL-Lite")
    item = ET.SubElement(root, f"{{{didl_ns}}}item", {"id": "1", "parentID": "0", "restricted": "1"})
    ET.SubElement(item, f"{{{dc_ns}}}title").text = name
    ET.SubElement(item, f"{{{upnp_ns}}}class").text = "object.item.audioItem.audioBroadcast"
    ET.SubElement(item, f"{{{didl_ns}}}res", {"protocolInfo": "http-get:*:audio/mpeg:*"}).text = url
    return ET.tostring(root, encoding="unicode")


def soap_request(speaker: str, action_name: str, action_args: dict[str, str]) -> None:
    """Send one UPnP AVTransport SOAP action to the SoundTouch speaker."""
    soap_ns = "http://schemas.xmlsoap.org/soap/envelope/"
    av_ns = "urn:schemas-upnp-org:service:AVTransport:1"
    ET.register_namespace("s", soap_ns)
    ET.register_namespace("u", av_ns)
    envelope = ET.Element(f"{{{soap_ns}}}Envelope", {f"{{{soap_ns}}}encodingStyle": "http://schemas.xmlsoap.org/soap/encoding/"})
    body = ET.SubElement(envelope, f"{{{soap_ns}}}Body")
    action = ET.SubElement(body, f"{{{av_ns}}}{action_name}")
    for key, value in action_args.items():
        ET.SubElement(action, key).text = value
    payload = ET.tostring(envelope, encoding="utf-8", xml_declaration=True)
    request = urllib.request.Request(
        f"http://{speaker}:8091/AVTransport/Control",
        data=payload,
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPACTION": f'"{av_ns}#{action_name}"',
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Speaker rejected UPnP {action_name} ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not send UPnP {action_name} to {speaker}: {exc}") from exc


class PresetBridge:
    """Map SoundTouch preset events to configured stations and start playback."""

    def __init__(self, speaker: str, relay_url: str, stations: dict[str, dict[str, str]]) -> None:
        self.speaker = speaker
        self.relay_url = relay_url.rstrip("/")
        self.stations = stations
        self.reconnect_delay = 1

    def play_preset(self, slot: str) -> None:
        """Ask the speaker to play the local relay URL for ``slot``."""
        station = self.stations.get(slot)
        if station is None:
            print(f"Preset {slot} has no station configured", file=sys.stderr)
            return
        url = f"{self.relay_url}/stream/{slot}"
        print(f"Preset {slot}: playing {station['name']}", flush=True)
        metadata = make_didl(station["name"], url)
        soap_request(self.speaker, "SetAVTransportURI", {
            "InstanceID": "0",
            "CurrentURI": url,
            "CurrentURIMetaData": metadata,
        })
        soap_request(self.speaker, "Play", {"InstanceID": "0", "Speed": "1"})

    def on_message(self, _ws: websocket.WebSocketApp, message: str) -> None:
        slot = parse_preset_press(message)
        if slot is None:
            return
        try:
            self.play_preset(slot)
        except Exception as exc:
            print(f"Preset {slot} playback error: {exc}", file=sys.stderr, flush=True)

    @staticmethod
    def on_error(_ws: websocket.WebSocketApp, error: object) -> None:
        print(f"WebSocket error: {error}", file=sys.stderr, flush=True)

    def on_open(self, _ws: websocket.WebSocketApp) -> None:
        self.reconnect_delay = 1
        print("Connected to SoundTouch button events", flush=True)

    @staticmethod
    def on_close(_ws: websocket.WebSocketApp, code: int | None, message: str | None) -> None:
        print(f"WebSocket closed ({code}): {message or ''}", flush=True)

    def run(self) -> None:
        """Maintain the speaker WebSocket and reconnect after interruptions."""
        url = f"ws://{self.speaker}:8080"
        delay = 1
        while True:
            client = websocket.WebSocketApp(
                url,
                subprotocols=["gabbo"],
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
            )
            client.run_forever(ping_interval=20, ping_timeout=10, http_no_proxy=[self.speaker])
            delay = self.reconnect_delay
            print(f"Reconnecting to speaker in {delay}s", flush=True)
            time.sleep(delay)
            self.reconnect_delay = min(delay * 2, 60)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speaker", required=True, help="SoundTouch IP or hostname")
    parser.add_argument("--relay-url", help="LAN URL the speaker can reach; derived from the route to the speaker if omitted")
    parser.add_argument("--bind", default="0.0.0.0", help="Address for the local audio relay (default: all LAN interfaces)")
    parser.add_argument("--port", type=int, default=8765, help="Local relay port (default: 8765)")
    args = parser.parse_args()

    try:
        stations = load_stations()
        relay_url = args.relay_url or f"http://{local_ip_for(args.speaker)}:{args.port}"
        parsed = urllib.parse.urlsplit(relay_url)
        if parsed.scheme != "http" or not parsed.netloc or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("--relay-url must be an http:// host:port URL without a path")
        relay_url = relay_url.rstrip("/")
        server = ThreadingHTTPServer((args.bind, args.port), StationHandler)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    threading.Thread(target=server.serve_forever, name="touchsound-relay", daemon=True).start()
    print(f"Serving transcoded radio streams at {relay_url}/stream/<slot>", flush=True)
    bridge = PresetBridge(args.speaker, relay_url, stations)
    try:
        bridge.run()
    except KeyboardInterrupt:
        print("Stopping TouchSound", flush=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
