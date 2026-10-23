# Architecture

TouchSound is a local bridge between a Bose SoundTouch speaker's preset-button notifications and its UPnP audio renderer. It has two long-running parts in one process: the WebSocket listener in `bridge.py` and the HTTP station relay in `server.py`.

## Playback sequence

```text
Physical preset button
        |
        v
Speaker WebSocket :8080 (subprotocol: gabbo)
        |
        | nowSelectionUpdated with preset id 1-6
        v
bridge.py reads stations.json
        |
        | UPnP SetAVTransportURI, then Play
        v
Speaker UPnP AVTransport :8091
        |
        | HTTP GET /stream/<slot>
        v
server.py starts FFmpeg for that station
        |
        | progressive audio/mpeg response
        v
SoundTouch speaker plays the station
```

The `nowSelectionUpdated` event is parsed as XML. A recognized preset id selects the matching entry in `stations.json`. `bridge.py` creates UPnP DIDL-Lite metadata for the station, sends `SetAVTransportURI` and `Play` SOAP actions to `/AVTransport/Control`, and reconnects to the WebSocket with increasing delays if the connection closes.

The stream URL sent to the speaker is the relay URL, not the provider URL. The bridge therefore does not depend on the speaker understanding the upstream format. For each `/stream/<slot>` request, `server.py` runs FFmpeg with the station's source URL and writes MP3 audio to the HTTP response. When the speaker disconnects, the FFmpeg process is terminated.

## HTTP endpoints

The HTTP server listens on TCP port `8765` by default and binds to all interfaces.

| Path | Use |
| --- | --- |
| `/stream/1` through `/stream/6` | Live MP3 relay for a configured preset |
| `/radio/1.json` through `/radio/6.json` | JSON descriptor used by the optional `LOCAL_INTERNET_RADIO` preset helper |
| `/health` | Check that the server can read `stations.json` |

The bridge needs network access to speaker ports `8080` (WebSocket notifications) and `8091` (UPnP control). The optional `touchsound.py status` command uses port `8090` (SoundTouch HTTP API).

## Files

| File | Responsibility |
| --- | --- |
| `bridge.py` | Connect to button events, map slots, and issue UPnP playback commands |
| `server.py` | Serve station descriptors and FFmpeg-backed MP3 streams |
| `stations.json` | Preset-to-station names and source URLs |
| `touchsound.py` | Read speaker status and provide an experimental preset-writing command |
| `Dockerfile`, `compose.yaml` | Container image and Compose deployment |

## Network and deployment notes

- The bridge host must maintain a connection to the speaker. The speaker must be able to connect back to the host on the relay port.
- By default, `bridge.py` chooses the source address the host uses to reach the speaker. Use `--relay-url` if routing, VPNs, or multiple network interfaces make that address unreachable from the speaker.
- A Docker bridge network changes the container's address. Set `RELAY_URL` to the Docker host's LAN address and published port, not the container address.
- The HTTP relay has no authentication. Keep port `8765` on a trusted local network and do not expose it to the internet.

## Limits

- The project targets one speaker at a time. Multi-room zones are not managed.
- It plays configured internet-radio streams. It does not sign in to music services or restore cloud browsing.
- FFmpeg must be able to read the configured source. Protected streams and URLs requiring account credentials are not supported by the included configuration loader.
- Stream URLs are maintained by their providers and can change without notice.
