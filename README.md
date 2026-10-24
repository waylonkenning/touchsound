# TouchSound

TouchSound restores the six physical preset buttons on Bose SoundTouch speakers for internet radio. It runs on your local network, listens for preset-button events, and starts the matching station through the speaker's UPnP audio player.

The bridge does not use Bose cloud services or change the presets stored on the speaker. It supports internet radio streams; it does not recreate Spotify, TuneIn browsing, or other authenticated music services.

## How it works

1. TouchSound listens to the speaker's local WebSocket on port `8080` for a `nowSelectionUpdated` event.
2. It reads the preset number from that event and looks up the station in `stations.json`.
3. It sends UPnP AVTransport commands to the speaker on port `8091`.
4. The speaker requests a local stream from TouchSound. FFmpeg fetches the configured station and relays it as MP3.
5. A supervisor checks the WebSocket, speaker transport, and live relay stream every 15 seconds. It retries playback before it reboots a stalled speaker.

The relay normalizes HTTP, HTTPS, AAC, and HLS radio sources to MP3 when FFmpeg can read the source. See [Architecture](docs/ARCHITECTURE.md) for the request flow and endpoint details.

For step-by-step installation on macOS, Docker, or Linux, see the [Installation Guide](docs/INSTALL.md).

## Requirements

- A Bose SoundTouch speaker on the same local network as the bridge host.
- Python 3.10 or newer.
- FFmpeg available on the bridge host.
- The speaker must be able to reach the bridge host on TCP port `8765`.
- The bridge host must be able to reach the speaker on TCP ports `8080`, `8090`, and `8091`.
- Automatic recovery uses the speaker's local diagnostic shell on TCP port `17000`. Use `--no-auto-reboot` to disable this.

## Quick start on macOS

Install FFmpeg and create a Python environment:

```sh
brew install ffmpeg
git clone https://github.com/waylonkenning/touchsound.git
cd touchsound
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit `stations.json` if you want different stations, then install the macOS service and menu bar app:

```sh
python3 macos/install.py install --speaker 192.168.1.100 --relay-url http://192.168.1.50:8765
```

Replace both example addresses with your speaker and Mac's LAN addresses. The relay URL can be omitted; the installer derives the Mac address used to reach the speaker. A DHCP reservation for the Mac helps keep that URL stable. The installer builds a menu bar app and registers it as a per-user macOS login agent. The app starts the supervised bridge, restarts it if it exits, and is restarted by launchd if the app itself exits. It shows bridge and speaker health under the speaker icon in the menu bar. macOS may ask you to allow TouchSound local network access.

The menu bar app shows whether the listener is connected, whether the speaker responds, the transport playback state, and whether stream data is arriving. It discovers SoundTouch speakers on the local network with Bonjour; choose one from the **SoundTouch Speakers** menu to switch the bridge to that device. It can also start, stop, or restart the bridge, request a speaker restart, and open the service log. The speaker restart action asks for confirmation first.

TouchSound also serves a mobile web controller at the bridge URL. Choose **Open Phone Web App** from the menu bar to open it on the Mac and copy the 12-digit pairing code. On your phone, open the same `http://<Mac-LAN-address>:8765` address, enter the code, then use Safari's **Share → Add to Home Screen** if you want an app-style shortcut. The phone can see bridge and speaker status, play any configured preset, pause/resume/stop playback, adjust volume, and review recent speaker activity. On iPhone, the compact first view keeps playback controls and all six presets together; tap **Speaker activity** to expand the 48-hour log. Pairing sessions end when the bridge restarts; use the Mac menu or `./.venv/bin/python service.py mobile-code` to pair again.

To remove the login services and menu bar app while keeping configuration and logs, run:

```sh
python3 macos/install.py uninstall
```

## Reliability and status

The bridge checks itself and the speaker every 15 seconds. It verifies the SoundTouch API and UPnP transport state. While a preset is active, it also checks that the speaker is playing the expected relay URL and is receiving stream bytes. It retries the preset once before attempting a speaker reboot. Reboots are limited to one attempt every 30 minutes. A manual pause or switch to another input stops playback recovery.

TouchSound keeps a rolling 48-hour speaker history. It records speaker state changes, preset selections, and bridge commands, with timestamps and an indication of whether a change followed a recent TouchSound command. A change with no recent bridge command could come from a speaker control, another controller, or automatic standby; the log cannot identify which one. On macOS, choose **Open Speaker History (48 h)** in the menu bar app. The history is also available at `/events` and is stored at `~/Library/Application Support/TouchSound/speaker-history.jsonl`.

Inspect the current checks from a browser or terminal:

```sh
curl http://<bridge-host-ip>:8765/status
curl -i http://<bridge-host-ip>:8765/health
```

`/status` returns bridge, speaker, playback, stream, and recovery details. `/health` returns `ok` only when the WebSocket and speaker respond and, during managed playback, the expected stream is active. The monitor can verify the digital stream path; it cannot detect whether the speaker's physical amplifier is producing audible sound.

On macOS, the menu bar app runs as a LaunchAgent under the logged-in user and owns the supervised bridge process. launchd relaunches the app at login or after an app crash; the app restarts the watchdog if it exits; the watchdog restarts the bridge if that process exits. The menu bar app polls `/status` every five seconds and checks speaker TCP reachability. The menu bar app can restart the speaker through loopback; phone access uses a paired session.

```sh
./.venv/bin/python service.py status
```

Use `./.venv/bin/python service.py status` to inspect the bridge, or use the menu bar app to stop, start, or restart the bridge service. The login agent remains loaded so the menu bar app can continue to run. Service logs are written to `~/Library/Logs/TouchSound/service.log`; the rolling speaker history, configuration, and persisted reboot cooldown are under `~/Library/Application Support/TouchSound`. Automatic speaker recovery and the manual restart control use the speaker's local diagnostic shell on TCP port `17000`, which is not part of Bose's documented Web API. Keep the speaker and bridge on a trusted home network.

## Run with Docker Compose

On a Linux host or NAS with Docker Compose, set the speaker address and the host's LAN address:

```sh
SOUNDTOUCH_IP=192.168.1.100 \
RELAY_URL=http://192.168.1.50:8765 \
docker compose up -d --build
```

Replace the example addresses. The speaker must be able to reach the Docker host at `RELAY_URL`. View logs with `docker compose logs -f`; stop the service with `docker compose down`.

The Docker image includes the same health supervisor and 48-hour speaker history as the macOS setup. The Compose volume keeps history across container recreation. It also needs network access to the speaker's local diagnostic port `17000` for automatic speaker recovery. Append `--no-auto-reboot` to the `command` list in `compose.yaml` if you want playback retries without automatic speaker restarts.

## Station configuration

`stations.json` maps preset numbers `1` through `6` to a display name and a direct audio stream URL:

```json
{
  "1": {
    "name": "My Radio Station",
    "streamUrl": "https://radio.example.org/live.m3u8"
  }
}
```

Configure a direct HTTP or HTTPS audio stream URL, not a station web page. Leave a slot out of the file to disable it. The included configuration is an editable example based on radio presets that were verified on a SoundTouch 20:

| Button | Example station |
| --- | --- |
| 1 | Radio New Zealand National |
| 2 | ABC Radio Australia English |
| 3 | Channel X |
| 4 | Hawaiian Rainbow |
| 5 | CBC Music Toronto |
| 6 | Jazz Cafe |

Station providers can change stream URLs at any time. Some streams may also be restricted by region or provider terms.

## Speaker status helper

Read the speaker model, available sources, and currently stored presets without changing them:

```sh
python3 touchsound.py status --speaker 192.168.1.100
```

`touchsound.py set` is a separate, experimental helper. It writes through the firmware-dependent `/storePreset` endpoint, which is not part of the official Bose Web API specification. It stops before writing if the speaker does not report `LOCAL_INTERNET_RADIO` as ready. The button bridge does not require this command.

## Troubleshooting

See [Troubleshooting](docs/TROUBLESHOOTING.md) for connection checks and common playback problems.

- No button events: check that the speaker and bridge can communicate on the same LAN and that TCP port `8080` is reachable from the host.
- Button event appears but playback does not start: check the bridge log and allow the speaker to reach the host on TCP port `8765`.
- Playback starts and then stops: check the configured URL with FFmpeg and confirm that the upstream station is still available.
- Wrong relay address: set `--relay-url` to a LAN URL the speaker can reach.
- macOS reports no speaker response from the login agent: open **System Settings → Privacy & Security → Local Network** and enable **TouchSound**. Then use **Restart Bridge** in its menu bar menu.

The stream relay and read-only status/history endpoints are available to devices on the LAN. Phone playback and volume controls require the pairing code. Keep port `8765` on a trusted home LAN and do not forward it to the public internet.

## Related work and references

There are other projects that restore SoundTouch preset playback, including [soundtouch-preset-bridge](https://github.com/thbaja/soundtouch-preset-bridge), [soundtouch-preset-revival](https://github.com/Beseco/soundtouch-preset-revival), [SoundTouch Bridge for Homey](https://github.com/codingmammoth/SoundTouchBridge), and [soundtouch-bridge-win](https://github.com/CsipkeP/soundtouch-bridge-win). TouchSound's particular focus is a small Python bridge with a built-in FFmpeg relay for stream formats such as HTTPS and HLS.

The local interfaces used here are documented in Bose's [SoundTouch Web API specification](https://assets.bosecreative.com/m/496577402d128874/original/SoundTouch-Web-API.pdf). The WebSocket-to-UPnP approach was also informed by public community work, including `thbaja/soundtouch-preset-bridge`; see [Acknowledgments](ACKNOWLEDGMENTS.md).

TouchSound is an independent community project and is not affiliated with Bose. See [LICENSE](LICENSE) for the license.
