import AppKit
import Foundation
import Network

private let configURL = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/TouchSound/config.json")
final class TouchSoundApp: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private let bridgeStatusItem = NSMenuItem(title: "Bridge: checking…", action: nil, keyEquivalent: "")
    private let websocketItem = NSMenuItem(title: "Button listener: checking…", action: nil, keyEquivalent: "")
    private let speakerItem = NSMenuItem(title: "Speaker: checking…", action: nil, keyEquivalent: "")
    private let playbackItem = NSMenuItem(title: "Playback: checking…", action: nil, keyEquivalent: "")
    private let streamItem = NSMenuItem(title: "Stream: checking…", action: nil, keyEquivalent: "")
    private let actionItem = NSMenuItem(title: "Recovery: checking…", action: nil, keyEquivalent: "")
    private let startStopItem = NSMenuItem(title: "Stop Bridge Service", action: #selector(toggleService), keyEquivalent: "")
    private let restartItem = NSMenuItem(title: "Restart Bridge", action: #selector(restartBridge), keyEquivalent: "")
    private let cycleSpeakerItem = NSMenuItem(title: "Restart SoundTouch Speaker…", action: #selector(cycleSpeaker), keyEquivalent: "")
    private let logItem = NSMenuItem(title: "Open Service Log", action: #selector(openLog), keyEquivalent: "")

    private var latestStatus: [String: Any] = [:]
    private var config: [String: Any] = [:]
    private var speakerProbe: NWConnection?
    private var localSpeakerReachable: Bool?
    private var lastProbeAt = Date.distantPast
    private var refreshTimer: Timer?
    private var isRefreshing = false
    private var serviceProcess: Process?
    private var servicePID: Int?
    private var serviceShouldRun = true
    private var startingService = false
    private var serviceRestartDelay: TimeInterval = 2

    private var statusURL: URL {
        let port = config["port"] as? Int ?? 8765
        return URL(string: "http://127.0.0.1:\(port)/status")!
    }

    private var bridgeServiceRunning: Bool {
        startingService || serviceProcess?.isRunning == true
    }

    private var rebootURL: URL {
        let port = config["port"] as? Int ?? 8765
        return URL(string: "http://127.0.0.1:\(port)/control/reboot")!
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        loadConfig()
        configureMenu()
        startBridgeService()
        refreshStatus()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.refreshStatus()
        }
        NSApp.activate(ignoringOtherApps: false)
    }

    func applicationWillTerminate(_ notification: Notification) {
        refreshTimer?.invalidate()
        speakerProbe?.cancel()
        serviceShouldRun = false
        serviceProcess?.terminate()
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        updateMenuLabels()
    }

    private func configureMenu() {
        guard let button = statusItem.button else { return }
        button.image = NSImage(systemSymbolName: "speaker.wave.2.fill", accessibilityDescription: "TouchSound status")
        button.image?.isTemplate = true
        button.toolTip = "TouchSound SoundTouch bridge"

        menu.delegate = self
        menu.autoenablesItems = false
        for item in [bridgeStatusItem, websocketItem, speakerItem, playbackItem, streamItem, actionItem] {
            item.isEnabled = false
            menu.addItem(item)
        }
        menu.addItem(.separator())
        for (item, selector) in [(startStopItem, #selector(toggleService)), (restartItem, #selector(restartBridge)), (cycleSpeakerItem, #selector(cycleSpeaker))] {
            item.target = self
            item.action = selector
            menu.addItem(item)
        }
        menu.addItem(.separator())
        logItem.target = self
        logItem.action = #selector(openLog)
        menu.addItem(logItem)
        let refresh = NSMenuItem(title: "Refresh Status", action: #selector(refreshNow), keyEquivalent: "")
        refresh.target = self
        menu.addItem(refresh)
        statusItem.menu = menu
        updateMenuLabels()
    }

    private func loadConfig() {
        guard let data = try? Data(contentsOf: configURL),
              let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            config = [:]
            return
        }
        config = parsed
    }

    private var speakerAddress: String {
        config["speaker"] as? String ?? "192.168.1.100"
    }

    private func refreshStatus() {
        guard !isRefreshing else { return }
        isRefreshing = true
        servicePID = serviceProcess.flatMap { $0.isRunning ? Int($0.processIdentifier) : nil }

        var request = URLRequest(url: statusURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 3)
        request.httpMethod = "GET"
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.isRefreshing = false
                if error == nil, let response = response as? HTTPURLResponse, response.statusCode == 200,
                   let data, let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.latestStatus = parsed
                } else {
                    self.latestStatus = [:]
                }
                self.updateMenuLabels()
                self.probeSpeakerIfDue()
                if self.latestStatus["healthy"] as? Bool == true {
                    self.serviceRestartDelay = 2
                }
            }
        }.resume()
    }

    private func probeSpeakerIfDue() {
        guard Date().timeIntervalSince(lastProbeAt) >= 30, speakerProbe == nil else { return }
        lastProbeAt = Date()
        let connection = NWConnection(host: NWEndpoint.Host(speakerAddress), port: 8090, using: .tcp)
        speakerProbe = connection
        connection.stateUpdateHandler = { [weak self, weak connection] state in
            guard let self else { return }
            switch state {
            case .ready:
                DispatchQueue.main.async {
                    self.localSpeakerReachable = true
                    self.speakerProbe = nil
                    connection?.cancel()
                    self.updateMenuLabels()
                }
            case .failed:
                DispatchQueue.main.async {
                    self.localSpeakerReachable = false
                    self.speakerProbe = nil
                    connection?.cancel()
                    self.updateMenuLabels()
                }
            default:
                break
            }
        }
        connection.start(queue: .global(qos: .utility))
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 5) { [weak self, weak connection] in
            guard let self, let connection, self.speakerProbe === connection else { return }
            DispatchQueue.main.async {
                guard self.speakerProbe === connection else { return }
                self.localSpeakerReachable = false
                self.speakerProbe = nil
                connection.cancel()
                self.updateMenuLabels()
            }
        }
    }

    private func updateMenuLabels() {
        let bridge = latestStatus["bridge"] as? [String: Any] ?? [:]
        let speaker = latestStatus["speaker"] as? [String: Any] ?? [:]
        let playback = latestStatus["playback"] as? [String: Any] ?? [:]
        let supervisor = latestStatus["supervisor"] as? [String: Any] ?? [:]
        let connected = bridge["websocketConnected"] as? Bool == true
        let reachable = speaker["reachable"] as? Bool == true
        let playing = (speaker["transportState"] as? String) == "PLAYING"
        let healthy = latestStatus["healthy"] as? Bool == true
        let expected = playback["expectedPreset"] as? String
        let station = playback["station"] as? String
        let streamConnected = playback["streamClientConnected"] as? Bool == true
        let streamBytes = playback["streamBytes"] as? Int ?? 0
        let lastStreamBytesAt = playback["lastStreamBytesAt"] as? String
        let streamDataRecent = lastStreamBytesAt.flatMap(parseUTCDate).map { Date().timeIntervalSince($0) < 60 } ?? false
        let hasStatus = !latestStatus.isEmpty

        let serviceDescription = bridgeServiceRunning
            ? " · service \(servicePID.map { "PID \($0)" } ?? "loaded")"
            : " · service stopped"
        bridgeStatusItem.title = hasStatus
            ? "Bridge: \(healthy ? "Healthy" : "Degraded")\(serviceDescription)"
            : (bridgeServiceRunning ? "Bridge: Starting / status unavailable" : "Bridge: Service stopped")
        websocketItem.title = "Button listener: \(connected ? "Connected" : "Disconnected")"
        let speakerResponding = reachable || localSpeakerReachable == true
        let speakerFailed = !reachable && localSpeakerReachable == false
        speakerItem.title = "Speaker: \(speakerResponding ? "Responding" : (speakerFailed ? "No response (check Local Network access)" : (bridgeServiceRunning ? "Checking" : "Not checked")))"
        if let expected, let station {
            playbackItem.title = "Playback: Preset \(expected) · \(station) · \(playing ? "PLAYING" : (speaker["transportState"] as? String ?? "not playing"))"
        } else {
            playbackItem.title = "Playback: \(playing ? "Playing" : "No managed preset")"
        }
        streamItem.title = streamConnected && streamBytes > 0 && streamDataRecent
            ? "Stream: Receiving audio data (\(ByteCountFormatter.string(fromByteCount: Int64(streamBytes), countStyle: .file)))"
            : "Stream: \(expected == nil ? "Idle" : (streamConnected ? "No recent audio data" : "Waiting for speaker stream"))"
        let lastAction = supervisor["lastAction"] as? String ?? "Waiting for health check"
        actionItem.title = "Recovery: \(lastAction)"

        if let button = statusItem.button {
            button.contentTintColor = healthy ? .systemGreen : (bridgeServiceRunning ? .systemOrange : .systemRed)
            button.toolTip = hasStatus ? "TouchSound: \(healthy ? "healthy" : "degraded")" : "TouchSound: status unavailable"
        }
        startStopItem.title = bridgeServiceRunning ? "Stop Bridge Service" : "Start Bridge Service"
        startStopItem.isEnabled = true
        restartItem.isEnabled = bridgeServiceRunning
        cycleSpeakerItem.isEnabled = true
        if !hasStatus, bridgeServiceRunning, let servicePID {
            bridgeStatusItem.title = "Bridge: Starting (service PID \(servicePID))"
        }
    }

    private func parseUTCDate(_ value: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: value) { return date }
        let standard = ISO8601DateFormatter()
        standard.formatOptions = [.withInternetDateTime]
        return standard.date(from: value)
    }

    @objc private func refreshNow() {
        lastProbeAt = .distantPast
        refreshStatus()
    }

    @objc private func toggleService() {
        if bridgeServiceRunning {
            serviceShouldRun = false
            startingService = false
            serviceProcess?.terminationHandler = nil
            serviceProcess?.terminate()
            serviceProcess = nil
            servicePID = nil
        } else {
            serviceShouldRun = true
            startBridgeService()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.refreshStatus() }
    }

    @objc private func restartBridge() {
        serviceShouldRun = true
        if let process = serviceProcess, process.isRunning {
            process.terminationHandler = nil
            process.terminate()
            serviceProcess = nil
            servicePID = nil
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.startBridgeService() }
        } else {
            startBridgeService()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.refreshStatus() }
    }

    private func startBridgeService() {
        guard serviceShouldRun, !startingService, serviceProcess?.isRunning != true else { return }
        startingService = true
        updateMenuLabels()

        let projectPath = config["projectPath"] as? String ?? ""
        let pythonPath = config["pythonPath"] as? String ?? "\(projectPath)/.venv/bin/python"
        let servicePath = config["servicePath"] as? String ?? "\(projectPath)/service.py"
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let cleanup = Process()
            cleanup.executableURL = URL(fileURLWithPath: pythonPath)
            cleanup.arguments = [servicePath, "stop"]
            cleanup.standardOutput = FileHandle.nullDevice
            cleanup.standardError = FileHandle.nullDevice
            try? cleanup.run()
            cleanup.waitUntilExit()

            DispatchQueue.main.async {
                guard let self else { return }
                self.startingService = false
                guard self.serviceShouldRun else {
                    self.updateMenuLabels()
                    return
                }

                let process = Process()
                process.executableURL = URL(fileURLWithPath: pythonPath)
                var arguments = [servicePath, "run", "--speaker", self.speakerAddress]
                if let relayURL = self.config["relayURL"] as? String, !relayURL.isEmpty {
                    arguments += ["--relay-url", relayURL]
                }
                process.arguments = arguments
                process.environment = [
                    "PYTHONUNBUFFERED": "1",
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                ]
                process.standardOutput = FileHandle.standardOutput
                process.standardError = FileHandle.standardError
                process.terminationHandler = { [weak self] terminated in
                    DispatchQueue.main.async {
                        guard let self else { return }
                        if self.serviceProcess?.processIdentifier == terminated.processIdentifier {
                            self.serviceProcess = nil
                            self.servicePID = nil
                        }
                        self.updateMenuLabels()
                        if self.serviceShouldRun {
                            let delay = self.serviceRestartDelay
                            self.serviceRestartDelay = min(delay * 2, 60)
                            DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                                self.startBridgeService()
                            }
                        }
                    }
                }
                do {
                    try process.run()
                    self.serviceProcess = process
                    self.servicePID = Int(process.processIdentifier)
                    self.serviceRestartDelay = 2
                    self.updateMenuLabels()
                } catch {
                    self.serviceProcess = nil
                    self.servicePID = nil
                    self.updateMenuLabels()
                    if self.serviceShouldRun {
                        let delay = self.serviceRestartDelay
                        self.serviceRestartDelay = min(delay * 2, 60)
                        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                            self.startBridgeService()
                        }
                    }
                }
            }
        }
    }

    @objc private func cycleSpeaker() {
        let alert = NSAlert()
        alert.messageText = "Restart the SoundTouch speaker?"
        alert.informativeText = "This will interrupt playback while the speaker restarts. The bridge will check playback again after the speaker returns."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Restart Speaker")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        var request = URLRequest(url: rebootURL, timeoutInterval: 5)
        request.httpMethod = "POST"
        URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                let accepted = (response as? HTTPURLResponse)?.statusCode == 202
                let alert = NSAlert()
                alert.messageText = accepted ? "Speaker restart queued" : "Could not request a speaker restart"
                alert.informativeText = accepted
                    ? "The bridge will restart the speaker and check it again as it returns."
                    : (error?.localizedDescription ?? "The bridge service is not responding. Start it and try again.")
                alert.alertStyle = accepted ? .informational : .critical
                alert.addButton(withTitle: "OK")
                alert.runModal()
                self.refreshStatus()
            }
        }.resume()
    }

    @objc private func openLog() {
        let log = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/TouchSound/service.log")
        NSWorkspace.shared.open(log)
    }

}

let application = NSApplication.shared
let delegate = TouchSoundApp()
application.delegate = delegate
application.run()
