# Troubleshooting

## Button presses do not appear in the bridge log

1. Confirm the speaker is on and has a stable IP address.
2. Confirm the bridge host can reach the speaker on TCP port `8080`.
3. Confirm that the bridge reports `Connected to SoundTouch button events` after startup.
4. Press a physical preset button and look for a `Preset N: playing ...` log line.
5. If the WebSocket closes, check Wi-Fi stability and the host firewall. The bridge reconnects automatically.

The button bridge does not use the speaker's `/key` endpoint to poll for button presses. It needs the WebSocket notification connection to stay open.

## Preset event appears, but playback does not start

- Read the error printed after `Preset N playback error`.
- Confirm the speaker is reachable on TCP port `8091` from the bridge host.
- Confirm the speaker can reach the relay host on TCP port `8765`. The address must be the LAN address visible to the speaker, not `localhost`, `127.0.0.1`, or a container-only address.
- If the bridge auto-detected an unreachable address, restart it with `--relay-url http://<host-lan-ip>:8765`.

The speaker normally requests the stream only after the UPnP URI and play actions succeed. You can check the current playback metadata with:

```sh
curl http://<speaker-ip>:8090/now_playing
```

## Playback is silent, stops, or never buffers

- Check that the selected slot has both a `name` and a `streamUrl` in `stations.json`.
- Verify that the station still publishes the stream URL. A station's website URL is not necessarily an audio stream.
- Check that FFmpeg is installed and can open the URL. Some providers restrict streams by region or change URLs.
- Confirm outbound internet access from the host running FFmpeg.
- HLS, AAC, and HTTPS sources are relayed as MP3 when the installed FFmpeg build and the upstream stream support them.

## Test the local server

While the bridge is running, check its health and station descriptor from a machine on the same LAN:

```sh
curl http://<bridge-host-ip>:8765/health
curl http://<bridge-host-ip>:8765/status
curl http://<bridge-host-ip>:8765/radio/1.json
```

`/health` returns `ok` only when the bridge's WebSocket and speaker checks pass and, during managed playback, the speaker is receiving the expected stream. `/status` gives the detailed state. The station descriptor should include the configured station name and source URL. A `/stream/<slot>` request starts a live audio response and is not intended to return a short text response.

The supervisor checks every 15 seconds. It retries the active preset before attempting a reboot and will not reboot more than once per 30 minutes. It does not intervene after a manual pause or input change. If `/status` shows the speaker API responding but the relay has no recent bytes, check the upstream station URL and FFmpeg before suspecting the speaker. The health check cannot measure sound from the physical speaker.

## Check speaker status

The read-only status command uses the SoundTouch HTTP API on port `8090`. The health supervisor also uses this port to inspect the active source and volume:

```sh
python3 touchsound.py status --speaker <speaker-ip>
```

This reports the saved preset labels and whether `LOCAL_INTERNET_RADIO` is available. The bridge itself does not need that source and does not rewrite saved presets.

## macOS firewall and sleep

Allow incoming local connections to the bridge host on TCP port `8765`. Keep the Mac awake while the bridge is running. If the Mac sleeps, the speaker cannot fetch the live relay stream.

## Docker Compose

Set `RELAY_URL` to the Docker host's LAN address and port `8765`. The speaker cannot use the container's private address. Check startup and playback logs with:

```sh
docker compose logs -f
```
