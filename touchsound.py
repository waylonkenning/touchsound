#!/usr/bin/env python3
"""Inspect a SoundTouch speaker and write local internet-radio presets."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "stations.json"


class SoundTouchError(RuntimeError):
    pass


def request(url: str, *, data: bytes | None = None, content_type: str | None = None) -> bytes:
    """Make a bounded HTTP request to a speaker or the local descriptor server."""
    headers = {"User-Agent": "TouchSound/1.0"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SoundTouchError(f"Request to {url} failed: {exc}") from exc


def api_url(host: str, path: str) -> str:
    return f"http://{host}:8090{path}"


def get_xml(host: str, path: str) -> ET.Element:
    try:
        payload = request(api_url(host, path))
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise SoundTouchError(f"Speaker returned invalid XML from {path}: {exc}") from exc


def load_stations() -> dict[str, dict[str, str]]:
    """Load and validate the station entries used by the optional set command."""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SoundTouchError(f"Cannot read {CONFIG_PATH}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SoundTouchError("stations.json must contain an object keyed by preset number")

    stations: dict[str, dict[str, str]] = {}
    for slot in map(str, range(1, 7)):
        item = raw.get(slot, {})
        if not isinstance(item, dict):
            raise SoundTouchError(f"Preset {slot} must be an object")
        name, stream_url = item.get("name", ""), item.get("streamUrl", "")
        if not isinstance(name, str) or not isinstance(stream_url, str):
            raise SoundTouchError(f"Preset {slot} name and streamUrl must be strings")
        name, stream_url = name.strip(), stream_url.strip()
        if not name and not stream_url:
            continue
        if not name or not stream_url:
            raise SoundTouchError(f"Preset {slot} needs both a name and a streamUrl")
        parsed = urllib.parse.urlsplit(stream_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SoundTouchError(f"Preset {slot} streamUrl must be an absolute HTTP or HTTPS URL")
        if parsed.username or parsed.password:
            raise SoundTouchError(f"Preset {slot} streamUrl must not contain credentials")
        stations[slot] = {"name": name, "streamUrl": stream_url}
    if not stations:
        raise SoundTouchError("No presets are configured. Edit stations.json first.")
    return stations


def inspect(host: str) -> tuple[ET.Element, ET.Element, ET.Element]:
    """Read speaker information, source availability, and saved presets."""
    info = get_xml(host, "/info")
    sources = get_xml(host, "/sources")
    presets = get_xml(host, "/presets")
    return info, sources, presets


def show_status(host: str) -> None:
    """Print read-only speaker and preset information for ``host``."""
    info, sources, presets = inspect(host)
    name = info.findtext("name", "Unknown speaker")
    model = info.findtext("type", "Unknown model")
    device_id = info.attrib.get("deviceID", "unknown")
    print(f"{name} ({model}), device {device_id}, at {host}")

    radio_source = next(
        (source for source in sources.findall("sourceItem")
         if source.attrib.get("source") == "LOCAL_INTERNET_RADIO"),
        None,
    )
    if radio_source is None:
        print("LOCAL_INTERNET_RADIO: not listed by this speaker")
    else:
        print(f"LOCAL_INTERNET_RADIO: {radio_source.attrib.get('status', 'unknown')}")

    by_slot = {item.attrib.get("id", ""): item for item in presets.findall("preset")}
    print("Current preset slots:")
    for slot in range(1, 7):
        preset = by_slot.get(str(slot))
        item_name = preset.findtext("./ContentItem/itemName") if preset is not None else None
        source = preset.find("ContentItem").attrib.get("source", "") if preset is not None and preset.find("ContentItem") is not None else ""
        print(f"  {slot}: {item_name or '(empty)'}{f' [{source}]' if source else ''}")


def preset_payload(slot: str, name: str, location: str) -> bytes:
    preset = ET.Element("preset", {"id": slot})
    content = ET.SubElement(
        preset,
        "ContentItem",
        {
            "source": "LOCAL_INTERNET_RADIO",
            "sourceAccount": "",
            "type": "stationurl",
            "location": location,
            "isPresetable": "true",
        },
    )
    ET.SubElement(content, "itemName").text = name
    return ET.tostring(preset, encoding="utf-8", xml_declaration=False)


def apply_presets(host: str, base_url: str) -> None:
    """Write station URLs as presets using the firmware-dependent endpoint."""
    stations = load_stations()
    base = urllib.parse.urlsplit(base_url)
    if (
        base.scheme != "http" or not base.netloc or base.username or base.password
        or base.path not in {"", "/"} or base.query or base.fragment
    ):
        raise SoundTouchError("--base-url must be a LAN-reachable http:// host:port URL without a path or credentials")
    base_url = base_url.rstrip("/")

    # Read all relevant device state before making any changes.
    info, sources, presets = inspect(host)
    radio_source = next(
        (source for source in sources.findall("sourceItem")
         if source.attrib.get("source") == "LOCAL_INTERNET_RADIO"),
        None,
    )
    status = radio_source.attrib.get("status", "unknown") if radio_source is not None else "missing"
    if status != "READY":
        raise SoundTouchError(
            "This speaker does not report LOCAL_INTERNET_RADIO as READY. "
            "No presets were changed. Check firmware/model compatibility before continuing."
        )

    # Confirm the descriptor host is online before changing speaker state.
    for slot, station in stations.items():
        descriptor_url = f"{base_url}/radio/{slot}.json"
        try:
            descriptor = json.loads(request(descriptor_url))
        except (SoundTouchError, json.JSONDecodeError) as exc:
            raise SoundTouchError(f"Cannot read a valid station descriptor from {descriptor_url}: {exc}") from exc
        actual = descriptor.get("audio", {}).get("streamUrl") if isinstance(descriptor, dict) else None
        if actual != station["streamUrl"]:
            raise SoundTouchError(f"Descriptor {descriptor_url} does not match stations.json. No presets were changed.")

    print(f"Writing {len(stations)} preset(s) to {info.findtext('name', host)} at {host}:")
    for slot, station in sorted(stations.items(), key=lambda pair: int(pair[0])):
        location = f"{base_url}/radio/{slot}.json"
        body = preset_payload(slot, station["name"], location)
        try:
            result = request(api_url(host, "/storePreset"), data=body, content_type="application/xml")
        except SoundTouchError as exc:
            raise SoundTouchError(
                f"Could not write preset {slot}. The Bose PDF does not document /storePreset; "
                f"this local endpoint is firmware-dependent. {exc}"
            ) from exc
        try:
            response_xml = ET.fromstring(result)
        except ET.ParseError:
            response_xml = None
        if response_xml is not None and (response_xml.tag == "errors" or response_xml.find("error") is not None):
            raise SoundTouchError(f"Speaker rejected preset {slot}: {ET.tostring(response_xml, encoding='unicode')}")
        print(f"  {slot}: {station['name']} -> {location} ({result.decode('utf-8', 'replace').strip() or 'OK'})")
    print("Done. Read back the slots with: python3 touchsound.py status --speaker <IP>")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Read speaker info, available sources, and presets")
    status_parser.add_argument("--speaker", required=True, help="Speaker IP or hostname on your LAN")

    set_parser = subparsers.add_parser("set", help="Write configured radio stations to preset slots")
    set_parser.add_argument("--speaker", required=True, help="Speaker IP or hostname on your LAN")
    set_parser.add_argument(
        "--base-url", required=True,
        help="LAN URL the speaker can reach, e.g. http://192.168.1.20:8765",
    )

    args = parser.parse_args()
    try:
        if args.command == "status":
            show_status(args.speaker)
        else:
            apply_presets(args.speaker, args.base_url)
    except SoundTouchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
