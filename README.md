# TouchSound

TouchSound restores the six physical preset buttons on Bose SoundTouch speakers for internet radio. It runs on your local network, listens for preset-button events, and starts the matching station through the speaker's UPnP audio player.

The bridge does not use Bose cloud services or change the presets stored on the speaker. It supports internet radio streams; it does not recreate Spotify, TuneIn browsing, or other authenticated music services.

## How it works

1. TouchSound listens to the speaker's local WebSocket on port `8080` for a `nowSelectionUpdated` event.
2. It reads the preset number from that event and looks up the station in `stations.json`.
3. It sends UPnP AVTransport commands to the speaker on port `8091`.
4. The speaker requests a local stream from TouchSound. FFmpeg fetches the configured station and relays it as MP3.

The relay normalizes HTTP, HTTPS, AAC, and HLS radio sources to MP3 when FFmpeg can read the source. See [Architecture](docs/ARCHITECTURE.md) for the request flow and endpoint details.

## Requirements

- A Bose SoundTouch speaker on the same local network as the bridge host.
- Python 3.10 or newer.
- FFmpeg available on the bridge host.
- The speaker must be able to reach the bridge host on TCP port `8765`.
- The bridge host must be able to reach the speaker on TCP ports `8080` and `8091`.

For the optional speaker status command, port `8090` must also be reachable.

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

Edit `stations.json` if you want different stations, then start the bridge with your speaker's IP address:

```sh
python3 bridge.py --speaker 192.168.1.100
```

The example address above is a placeholder. TouchSound selects the network address used to reach the speaker and advertises the relay at `http://<host-address>:8765`. Keep this process running and prevent the host from sleeping while you use the buttons.

If the address selected by your computer is not reachable by the speaker, specify the host's LAN address:

```sh
python3 bridge.py --speaker 192.168.1.100 --relay-url http://192.168.1.50:8765
```

Replace both example addresses with addresses on your own network. A DHCP reservation for the bridge host helps keep the relay address stable.

## Run with Docker Compose

On a Linux host or NAS with Docker Compose, set the speaker address and the host's LAN address:

```sh
SOUNDTOUCH_IP=192.168.1.100 \
RELAY_URL=http://192.168.1.50:8765 \
docker compose up -d --build
```

Replace the example addresses. The speaker must be able to reach the Docker host at `RELAY_URL`. View logs with `docker compose logs -f`; stop the service with `docker compose down`.

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

The relay has no authentication and is intended for a trusted home LAN. Do not forward port `8765` to the public internet.

## Related work and references

There are other projects that restore SoundTouch preset playback, including [soundtouch-preset-bridge](https://github.com/thbaja/soundtouch-preset-bridge), [soundtouch-preset-revival](https://github.com/Beseco/soundtouch-preset-revival), [SoundTouch Bridge for Homey](https://github.com/codingmammoth/SoundTouchBridge), and [soundtouch-bridge-win](https://github.com/CsipkeP/soundtouch-bridge-win). TouchSound's particular focus is a small Python bridge with a built-in FFmpeg relay for stream formats such as HTTPS and HLS.

The local interfaces used here are documented in Bose's [SoundTouch Web API specification](https://assets.bosecreative.com/m/496577402d128874/original/SoundTouch-Web-API.pdf). The WebSocket-to-UPnP approach was also informed by public community work, including `thbaja/soundtouch-preset-bridge`; see [Acknowledgments](ACKNOWLEDGMENTS.md).

TouchSound is an independent community project and is not affiliated with Bose. See [LICENSE](LICENSE) for the license.
