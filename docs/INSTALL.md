# Installation Guide

TouchSound runs on a Mac, Linux host, or Docker host on the same local network as the SoundTouch speaker. The speaker must be able to reach the host on TCP port `8765`. The host must reach the speaker on ports `8080` (button events), `8090` (status), and `8091` (UPnP playback control). Automatic speaker recovery also uses the speaker's local diagnostic port `17000`.

Reserve stable IP addresses for the speaker and bridge host in your router. The relay URL must use an address the speaker can reach; it cannot use `localhost` or `127.0.0.1`.

## macOS menu bar app and login service

### Requirements

- macOS 13 or newer.
- Python 3.10 or newer.
- Homebrew and FFmpeg (`brew install ffmpeg`).
- Xcode Command Line Tools, which provide `swiftc` and `codesign`. Install them with `xcode-select --install` if they are not already available.

### Install

Clone the repository, create its virtual environment, and install the Python dependency:

```sh
git clone https://github.com/waylonkenning/touchsound.git
cd touchsound
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Edit `stations.json` to choose the radio stations for buttons 1 through 6. Each configured slot needs a display `name` and a direct HTTP or HTTPS audio `streamUrl`. Do not put account credentials or private tokens in a URL.

Install the service and menu bar app:

```sh
python3 macos/install.py install --speaker 192.168.1.100 --relay-url http://192.168.1.50:8765
```

Replace the example addresses with the speaker's IP address and the Mac's LAN address. `--relay-url` is optional; the installer can select the Mac address routed to the speaker. Set it explicitly when the Mac uses multiple network interfaces, a VPN, or a nonstandard route.

The installer builds `TouchSound.app` in `~/Applications`, creates a per-user login agent, and starts the menu bar app. When macOS asks, allow TouchSound to access the local network. You can review that setting under **System Settings → Privacy & Security → Local Network**.

Click the speaker icon in the menu bar to view button-listener, speaker, playback, and stream status. The menu can start, stop, or restart the bridge service and request a speaker restart. The login agent stays loaded when you stop the bridge from the menu.

### Check and manage the service

Use the menu bar status or run:

```sh
./.venv/bin/python service.py status
curl http://127.0.0.1:8765/status
curl -i http://127.0.0.1:8765/health
```

Logs are in `~/Library/Logs/TouchSound/`. The service configuration and reboot cooldown state are in `~/Library/Application Support/TouchSound/`. Keep the Mac awake and permit incoming connections on TCP port `8765` while using the presets.

After editing `stations.json`, select **Restart Bridge** from the menu bar menu. To update the app after pulling new source code, rerun the installer with the same speaker address:

```sh
python3 macos/install.py install --speaker 192.168.1.100 --relay-url http://192.168.1.50:8765
```

To uninstall the login agent and app while preserving configuration and logs:

```sh
python3 macos/install.py uninstall
```

## Docker Compose

Install Docker Engine or Docker Desktop with the Compose plugin. Edit `stations.json`, then start the service with addresses from your LAN:

```sh
SOUNDTOUCH_IP=192.168.1.100 \
RELAY_URL=http://192.168.1.50:8765 \
docker compose up -d --build
```

`SOUNDTOUCH_IP` is the speaker address. `RELAY_URL` must use the Docker host's LAN address and published port `8765`; the speaker cannot use the container's private address. The Compose file publishes the relay port and restarts the container if it exits.

Check playback and logs:

```sh
curl http://192.168.1.50:8765/status
docker compose logs -f
```

Stop the service with `docker compose down`. Automatic speaker recovery uses port `17000`; to disable speaker restarts, append `--no-auto-reboot` to the `command` list in `compose.yaml`.

## Run in the foreground on Linux or macOS

Install Python 3.10 or newer and FFmpeg, then install the dependency as shown in the macOS setup. Run the bridge in a terminal:

```sh
python3 bridge.py --speaker 192.168.1.100 --relay-url http://192.168.1.50:8765
```

Omit `--relay-url` to let TouchSound select the local address routed to the speaker. Use `--no-auto-reboot` to retry preset playback without restarting the speaker. This foreground mode exits when the terminal process stops; use Docker Compose or an operating-system service manager if you need unattended restarts.

## Station URLs and network access

Use direct, publicly reachable radio stream URLs. Station web pages, streams requiring login, and URLs that include credentials are not supported. Providers can change stream URLs or apply regional restrictions.

The relay has no authentication. Keep it on a trusted home network and do not forward port `8765` to the public internet. Health checks confirm that the speaker renderer is receiving data; they cannot confirm audible output from the physical speaker.
