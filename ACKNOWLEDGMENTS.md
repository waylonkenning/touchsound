# Acknowledgments

TouchSound was built against Bose's published [SoundTouch Web API specification](https://assets.bosecreative.com/m/496577402d128874/original/SoundTouch-Web-API.pdf).

The initial investigation also reviewed community projects that document ways to restore local preset playback, including [thbaja/soundtouch-preset-bridge](https://github.com/thbaja/soundtouch-preset-bridge). That work helped surface the WebSocket notification and UPnP AVTransport flow. TouchSound packages that flow with a configurable station map and its own FFmpeg HTTP relay.

Related projects include [soundtouch-preset-revival](https://github.com/Beseco/soundtouch-preset-revival), [SoundTouch Bridge for Homey](https://github.com/codingmammoth/SoundTouchBridge), and [soundtouch-bridge-win](https://github.com/CsipkeP/soundtouch-bridge-win). These projects have different platforms and features; see their own documentation for current status and licenses.

TouchSound is an independent community project and is not affiliated with Bose or the station providers listed in `stations.json`.
