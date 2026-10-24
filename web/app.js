(() => {
  "use strict";

  const TOKEN_KEY = "touchsound.phoneSession";
  const elements = {
    connectionPill: document.querySelector("#connectionPill"),
    connectionLabel: document.querySelector("#connectionLabel"),
    speakerLine: document.querySelector("#speakerLine"),
    playState: document.querySelector("#playState"),
    stationMark: document.querySelector("#stationMark"),
    trackTitle: document.querySelector("#trackTitle"),
    trackSubtitle: document.querySelector("#trackSubtitle"),
    pauseButton: document.querySelector("#pauseButton"),
    resumeButton: document.querySelector("#resumeButton"),
    stopButton: document.querySelector("#stopButton"),
    volumeSlider: document.querySelector("#volumeSlider"),
    volumeValue: document.querySelector("#volumeValue"),
    actionMessage: document.querySelector("#actionMessage"),
    presetGrid: document.querySelector("#presetGrid"),
    eventList: document.querySelector("#eventList"),
    historyCount: document.querySelector("#historyCount"),
    pairingLayer: document.querySelector("#pairingLayer"),
    pairingForm: document.querySelector("#pairingForm"),
    pairingCode: document.querySelector("#pairingCode"),
    pairingError: document.querySelector("#pairingError"),
    pairButton: document.querySelector("#pairButton"),
    refreshButton: document.querySelector("#refreshButton"),
    toast: document.querySelector("#toast"),
  };

  let token = localStorage.getItem(TOKEN_KEY) || "";
  let config = null;
  let latestStatus = null;
  let toastTimer = null;
  let volumeTimer = null;

  function setPairingVisible(visible) {
    elements.pairingLayer.hidden = !visible;
    if (visible) window.setTimeout(() => elements.pairingCode.focus(), 80);
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (token && path !== "/api/pair") headers.set("Authorization", `Bearer ${token}`);
    if (options.body) headers.set("Content-Type", "application/json");
    const response = await fetch(path, { ...options, headers, cache: "no-store" });
    const data = response.headers.get("content-type")?.includes("application/json")
      ? await response.json()
      : null;
    if (response.status === 401 && path !== "/api/pair") {
      token = "";
      localStorage.removeItem(TOKEN_KEY);
      setPairingVisible(true);
    }
    if (!response.ok) throw new Error(data?.error || `Request failed (${response.status})`);
    return data;
  }

  function showToast(message, isError = false) {
    elements.toast.textContent = message;
    elements.toast.dataset.error = String(isError);
    elements.toast.classList.add("is-visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => elements.toast.classList.remove("is-visible"), 2600);
  }

  function setMessage(message = "", isError = false) {
    elements.actionMessage.textContent = message;
    elements.actionMessage.dataset.error = String(isError);
  }

  function speakerDescription() {
    const name = String(config?.speaker?.name || "").trim();
    const address = String(config?.speaker?.address || "").trim();
    const description = name && address && name.toLocaleLowerCase() !== address.toLocaleLowerCase()
      ? `${name} · ${address}`
      : (name || address);
    return description || "SoundTouch speaker";
  }

  function setConnection(state, label) {
    elements.connectionPill.dataset.state = state;
    elements.connectionLabel.textContent = label;
  }

  function formatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date);
  }

  function formatAge(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} hr ago`;
    return `${Math.floor(hours / 24)} d ago`;
  }

  function updateStatus(status) {
    latestStatus = status;
    const speaker = status.speaker || {};
    const playback = status.playback || {};
    const reachable = speaker.reachable === true;
    const healthy = status.healthy === true;

    setConnection(healthy ? "healthy" : "degraded", healthy ? "All good" : "Check bridge");
    elements.speakerLine.textContent = speakerDescription();

    const transport = String(speaker.transportState || "").toUpperCase();
    const playing = transport === "PLAYING";
    const paused = transport.includes("PAUSED");
    const expectedPreset = playback.expectedPreset ? String(playback.expectedPreset) : "";
    const stationName = playback.station || speaker.stationName || "";
    const trackName = speaker.track || speaker.contentItemName || "";
    const title = trackName || stationName || (speaker.source && speaker.source !== "STANDBY" ? String(speaker.source).replaceAll("_", " ") : "No preset playing");
    elements.trackTitle.textContent = !reachable ? "Speaker not responding" : title;
    elements.stationMark.textContent = stationName ? stationName.trim().slice(0, 2).toUpperCase() : "TS";
    if (!reachable) {
      elements.trackSubtitle.textContent = speaker.error || "Check that the speaker is powered on and connected to Wi-Fi.";
    } else if (playing && stationName) {
      const detail = [speaker.artist, stationName, `Preset ${expectedPreset || "—"}`].filter(Boolean);
      elements.trackSubtitle.textContent = detail.join(" · ");
    } else if (playing) {
      const detail = [speaker.artist, speaker.album, String(speaker.source || "SoundTouch").replaceAll("_", " ")].filter(Boolean);
      elements.trackSubtitle.textContent = detail.join(" · ");
    } else if (paused) {
      elements.trackSubtitle.textContent = "Playback is paused";
    } else if (transport === "STOPPED") {
      elements.trackSubtitle.textContent = "Playback is stopped";
    } else {
      elements.trackSubtitle.textContent = "Choose a preset to start a station";
    }
    const stateLabel = !reachable ? "OFFLINE" : (playing ? "PLAYING" : (paused ? "PAUSED" : (transport === "STOPPED" ? "STOPPED" : "READY")));
    elements.playState.textContent = stateLabel;
    elements.playState.dataset.state = !reachable ? "error" : (playing ? "playing" : (paused ? "paused" : ""));

    const paired = Boolean(token);
    elements.pauseButton.disabled = !paired || !reachable || !playing;
    elements.resumeButton.disabled = !paired || !reachable || (!paused && playing);
    elements.stopButton.disabled = !paired || !reachable || (!playing && !paused);
    const volume = Number.isInteger(speaker.volume) ? speaker.volume : null;
    if (volume !== null && document.activeElement !== elements.volumeSlider) {
      elements.volumeSlider.value = String(volume);
    }
    elements.volumeSlider.disabled = !paired || !reachable || volume === null;
    elements.volumeValue.textContent = volume === null ? "—" : String(volume);
    renderPresets(config?.stations || [], expectedPreset, paired);
  }

  function renderPresets(stations, currentSlot, paired) {
    const fragment = document.createDocumentFragment();
    for (const station of stations) {
      const button = document.createElement("button");
      const configured = Boolean(station.name);
      button.type = "button";
      button.className = `preset-button${configured && station.slot === currentSlot ? " is-current" : ""}${configured ? "" : " is-empty"}`;
      button.disabled = !configured || !paired || latestStatus?.speaker?.reachable !== true;
      button.setAttribute("aria-label", configured ? `Play preset ${station.slot}, ${station.name}` : `Preset ${station.slot} is not configured`);

      const top = document.createElement("span");
      top.className = "preset-topline";
      const key = document.createElement("span");
      key.className = "preset-slot";
      key.textContent = `PRESET ${station.slot}`;
      const action = document.createElement("span");
      action.textContent = configured && station.slot === currentSlot ? "ON" : "";
      top.append(key, action);

      const name = document.createElement("span");
      name.className = "preset-name";
      name.textContent = station.name || "Not configured";
      const hint = document.createElement("span");
      hint.className = "preset-action";
      hint.textContent = configured ? "Tap to play" : "Edit stations.json";
      button.append(top, name, hint);
      if (configured) button.addEventListener("click", () => sendCommand(`/control/play/${station.slot}`, {}));
      fragment.append(button);
    }
    elements.presetGrid.replaceChildren(fragment);
  }

  function describeEvent(event) {
    const details = event.changes && typeof event.changes === "object" ? event.changes : {};
    if (event.type === "speaker_state_initial") return "Speaker state recorded";
    if (event.type === "speaker_state_changed") {
      const fields = Object.keys(details).map((key) => key.replace(/[A-Z]/g, (letter) => ` ${letter.toLowerCase()}`));
      return fields.length ? `State changed · ${fields.join(", ")}` : "Speaker state changed";
    }
    if (event.type === "touchsound_command") {
      if (event.action === "play_preset") return `Preset ${event.slot} started`;
      return String(event.action || "TouchSound command").replaceAll("_", " ");
    }
    if (event.type === "speaker_preset_selected") return `Preset ${event.slot} selected on speaker`;
    if (event.type === "speaker_reboot_attempt") return "Speaker restart attempted";
    if (event.type === "monitor_decision") return String(event.reason || event.action || "Playback monitor update");
    return String(event.type || "Speaker event").replaceAll("_", " ");
  }

  function renderEvents(events) {
    elements.historyCount.textContent = String(events.length);
    if (!events.length) {
      const empty = document.createElement("li");
      empty.className = "event-placeholder";
      empty.textContent = "No speaker changes recorded in the last 48 hours.";
      elements.eventList.replaceChildren(empty);
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const event of events.slice(-8).reverse()) {
      const item = document.createElement("li");
      item.className = "event-item";
      const marker = document.createElement("span");
      marker.className = "event-marker";
      marker.setAttribute("aria-hidden", "true");
      const copy = document.createElement("div");
      copy.className = "event-copy";
      const title = document.createElement("p");
      title.className = "event-title";
      title.textContent = describeEvent(event);
      const detail = document.createElement("p");
      detail.className = "event-detail";
      const station = event.station ? String(event.station) : "";
      const attribution = event.attribution ? String(event.attribution) : "";
      detail.textContent = [station, attribution].filter(Boolean).join(" · ");
      copy.append(title);
      if (detail.textContent) copy.append(detail);
      const time = document.createElement("time");
      time.className = "event-time";
      time.dateTime = String(event.at || "");
      time.textContent = formatAge(event.at);
      item.append(marker, copy, time);
      fragment.append(item);
    }
    elements.eventList.replaceChildren(fragment);
  }

  async function refresh() {
    try {
      const [status, events] = await Promise.all([request("/status"), request("/events")]);
      updateStatus(status);
      renderEvents(Array.isArray(events.events) ? events.events : []);
      setConnection(status.healthy === true ? "healthy" : "degraded", status.healthy === true ? "All good" : "Check bridge");
    } catch (error) {
      setConnection("degraded", "No connection");
      elements.trackTitle.textContent = "Bridge is unavailable";
      elements.trackSubtitle.textContent = error.message;
      if (!elements.eventList.children.length) {
        const item = document.createElement("li");
        item.className = "event-placeholder";
        item.textContent = "Could not load speaker activity. Pull down to refresh or check the Mac bridge.";
        elements.eventList.replaceChildren(item);
      }
    }
  }

  async function sendCommand(path, body) {
    if (!token) {
      setPairingVisible(true);
      return;
    }
    setMessage("Sending command…");
    try {
      await request(path, { method: "POST", body: JSON.stringify(body) });
      setMessage("Command sent");
      showToast("TouchSound command sent");
      window.setTimeout(refresh, 700);
    } catch (error) {
      setMessage(error.message, true);
      showToast(error.message, true);
    }
  }

  async function loadConfiguration() {
    try {
      config = await request("/api/config");
      elements.speakerLine.textContent = speakerDescription();
      renderPresets(config.stations || [], "", Boolean(token));
    } catch (error) {
      elements.speakerLine.textContent = error.message;
    }
  }

  elements.pairingForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    elements.pairingError.textContent = "";
    elements.pairButton.disabled = true;
    elements.pairButton.textContent = "Pairing…";
    try {
      const result = await request("/api/pair", {
        method: "POST",
        body: JSON.stringify({ code: elements.pairingCode.value.trim() }),
      });
      token = result.token;
      localStorage.setItem(TOKEN_KEY, token);
      elements.pairingCode.value = "";
      setPairingVisible(false);
      setMessage("");
      showToast("Phone paired with TouchSound");
      await loadConfiguration();
      await refresh();
    } catch (error) {
      elements.pairingError.textContent = error.message;
    } finally {
      elements.pairButton.disabled = false;
      elements.pairButton.textContent = "Pair phone";
    }
  });

  elements.pauseButton.addEventListener("click", () => sendCommand("/control/pause", {}));
  elements.resumeButton.addEventListener("click", () => sendCommand("/control/resume", {}));
  elements.stopButton.addEventListener("click", () => sendCommand("/control/stop", {}));
  elements.refreshButton.addEventListener("click", refresh);
  elements.volumeSlider.addEventListener("change", () => {
    const value = Number.parseInt(elements.volumeSlider.value, 10);
    elements.volumeValue.textContent = String(value);
    sendCommand("/control/volume", { volume: value });
  });

  async function initialize() {
    try {
      if (token) await request("/api/session");
    } catch (_) {
      token = "";
      localStorage.removeItem(TOKEN_KEY);
    }
    setPairingVisible(!token);
    await loadConfiguration();
    await refresh();
    window.setInterval(refresh, 5000);
  }

  initialize();
})();
