# Architecture

TouchSound is a local bridge between a Bose SoundTouch speaker's preset-button notifications and its UPnP audio renderer. The bridge process runs a WebSocket listener, an HTTP station relay, and a health supervisor. On macOS, a menu bar app supervises the bridge and provides a visible health and control panel.

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

`runtime.py` checks the SoundTouch HTTP API and UPnP transport every 15 seconds. During a managed preset, it compares the current transport URI and state with the expected relay URL and tracks bytes sent to the speaker. If the stream stalls, it reissues the preset once, then tries a speaker reboot after further failed checks. If the speaker API is unreachable for three checks, it attempts a reboot directly. Automatic reboots have a 30-minute cooldown; after reboot, the supervisor waits for the speaker to return and replays the last preset. A manual pause or input change clears the active playback expectation.

The macOS menu bar app polls `/status` every five seconds and also checks whether the speaker accepts TCP connections. It browses Bonjour service `_soundtouch._tcp` in the local domain and resolves each result to its reachable host address. Choose a discovered device from the **SoundTouch Speakers** submenu to save it as the active speaker and restart the bridge service. The app starts `service.py run` as a child process and restarts that process if it exits. The watchdog restarts `bridge.py` if the bridge process exits. `launchd` starts the menu app at login and relaunches it if it exits. The menu exposes bridge and speaker status, bridge start/stop/restart controls, a speaker restart action, and the service log. A requested speaker restart is sent to the loopback-only `/control/reboot` endpoint and queued on the supervisor thread, which records the reboot time and observes the normal recovery cooldown. This lets the app helper perform speaker network access under the app's macOS Local Network permission.

The stream URL sent to the speaker is the relay URL, not the provider URL. The bridge therefore does not depend on the speaker understanding the upstream format. For each `/stream/<slot>` request, `server.py` runs FFmpeg with the station's source URL and writes MP3 audio to the HTTP response. When the speaker disconnects, the FFmpeg process is terminated.

## HTTP endpoints

The HTTP server listens on TCP port `8765` by default and binds to all interfaces.

| Path | Use |
| --- | --- |
| `/stream/1` through `/stream/6` | Live MP3 relay for a configured preset |
| `/radio/1.json` through `/radio/6.json` | JSON descriptor used by the optional `LOCAL_INTERNET_RADIO` preset helper |
| `/health` | Return `ok` only when the relay, event connection, and speaker are healthy |
| `/status` | JSON health snapshot for the bridge, speaker, playback, relay, and supervisor |
| `/control/reboot` | Loopback-only POST that queues a manual speaker restart on the supervisor thread |

The bridge needs network access to speaker ports `8080` (WebSocket notifications), `8090` (SoundTouch status), and `8091` (UPnP control). Automatic reboot also uses the speaker's local diagnostic shell on port `17000`.

## Files

| File | Responsibility |
| --- | --- |
| `bridge.py` | Connect to button events, map slots, and issue UPnP playback commands |
| `server.py` | Serve station descriptors and FFmpeg-backed MP3 streams |
| `runtime.py` | Track health and recover stalled playback or an unresponsive speaker |
| `service.py` | Run a watchdog that restarts a crashed bridge process; provide CLI status and lifecycle commands |
| `macos/TouchSoundMenu.swift` | Native menu bar UI that supervises the bridge child and displays health and controls |
| `macos/install.py` | Build/install the menu app, configure the user LaunchAgent, and remove legacy service registration |
| `stations.json` | Preset-to-station names and source URLs |
| `touchsound.py` | Read speaker status and provide an experimental preset-writing command |
| `Dockerfile`, `compose.yaml` | Container image and Compose deployment |

## Network and deployment notes

- The bridge host must maintain a connection to the speaker. The speaker must be able to connect back to the host on the relay port.
- The macOS installer creates a per-user LaunchAgent. Allow TouchSound under **System Settings → Privacy & Security → Local Network** so the app and its bridge helper can contact the speaker.
- Bonjour discovery requires the speaker and Mac to share a local network where mDNS multicast is available. The app browses `_soundtouch._tcp`.
- By default, `bridge.py` chooses the source address the host uses to reach the speaker. Use `--relay-url` if routing, VPNs, or multiple network interfaces make that address unreachable from the speaker.
- A Docker bridge network changes the container's address. Set `RELAY_URL` to the Docker host's LAN address and published port, not the container address.
- The HTTP relay has no authentication. Keep port `8765` on a trusted local network and do not expose it to the internet.

## Limits

- The project targets one speaker at a time. Multi-room zones are not managed.
- Health checks confirm that data reaches the speaker's network renderer. They cannot confirm audible output from the physical speaker hardware.
- It plays configured internet-radio streams. It does not sign in to music services or restore cloud browsing.
- FFmpeg must be able to read the configured source. Protected streams and URLs requiring account credentials are not supported by the included configuration loader.
- Stream URLs are maintained by their providers and can change without notice.
