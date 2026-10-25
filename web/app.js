(() => {
  "use strict";

  const TOKEN_KEY = "touchsound.phoneSession";
  // Keep this aligned with SpeakerSupervisor's startup grace period in runtime.py.
  const PRESET_STARTUP_GRACE_MS = 45_000;
  const PRESET_METADATA_SETTLE_MS = 15_000;
  const elements = {
    connectionPill: document.querySelector("#connectionPill"),
    connectionLabel: document.querySelector("#connectionLabel"),
    speakerSelect: document.querySelector("#speakerSelect"),
    speakerMessage: document.querySelector("#speakerMessage"),
    stationSearchForm: document.querySelector("#stationSearchForm"),
    stationQuery: document.querySelector("#stationQuery"),
    stationSearchMode: document.querySelector("#stationSearchMode"),
    stationSearchButton: document.querySelector("#stationSearchButton"),
    stationTargetSlot: document.querySelector("#stationTargetSlot"),
    stationSearchMessage: document.querySelector("#stationSearchMessage"),
    stationResults: document.querySelector("#stationResults"),
    presetEditorForm: document.querySelector("#presetEditorForm"),
    presetEditorSlot: document.querySelector("#presetEditorSlot"),
    presetEditorName: document.querySelector("#presetEditorName"),
    presetEditorStreamUrl: document.querySelector("#presetEditorStreamUrl"),
    presetEditorSaveButton: document.querySelector("#presetEditorSaveButton"),
    presetEditorMessage: document.querySelector("#presetEditorMessage"),
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
  let pendingPresetTransition = null;
  let stalePresetTrack = null;
  let toastTimer = null;
  let volumeTimer = null;
  let speakerChoices = [];
  let currentSpeakerAddress = "";
  let pendingSpeakerAddress = "";
  let stationSearchResults = [];
  let assigningStationUuid = "";
  let presetEditorDirty = false;
  let presetEditorSaving = false;

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
      renderStationTargetSlots(config?.stations || []);
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

  function setSpeakerMessage(message = "", isError = false) {
    elements.speakerMessage.textContent = message;
    elements.speakerMessage.dataset.error = String(isError);
  }

  function setStationSearchMessage(message = "", isError = false) {
    elements.stationSearchMessage.textContent = message;
    elements.stationSearchMessage.dataset.error = String(isError);
  }

  function setPresetEditorMessage(message = "", isError = false) {
    elements.presetEditorMessage.textContent = message;
    elements.presetEditorMessage.dataset.error = String(isError);
  }

  function setConnection(state, label) {
    elements.connectionPill.dataset.state = state;
    elements.connectionLabel.textContent = label;
  }

  function renderSpeakerPicker(inventory) {
    const current = inventory?.current || config?.speaker || {};
    currentSpeakerAddress = String(current.address || "");
    const currentName = String(current.name || current.address || "SoundTouch speaker");
    const discovered = Array.isArray(inventory?.speakers) ? inventory.speakers : [];
    const choices = new Map();
    if (currentSpeakerAddress) {
      choices.set(currentSpeakerAddress, { name: currentName, address: currentSpeakerAddress });
    }
    for (const speaker of discovered) {
      if (speaker?.address && speaker?.name) {
        choices.set(String(speaker.address), { name: String(speaker.name), address: String(speaker.address) });
      }
    }
    speakerChoices = [...choices.values()];

    const fragment = document.createDocumentFragment();
    if (!speakerChoices.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No speaker selected";
      fragment.append(option);
    } else {
      for (const speaker of speakerChoices) {
        const option = document.createElement("option");
        option.value = speaker.address;
        option.textContent = speaker.name.toLocaleLowerCase() === speaker.address.toLocaleLowerCase()
          ? speaker.address
          : `${speaker.name} · ${speaker.address}`;
        fragment.append(option);
      }
    }
    elements.speakerSelect.replaceChildren(fragment);

    const pendingChoice = speakerChoices.find((speaker) => speaker.address === pendingSpeakerAddress);
    const selectedAddress = pendingChoice ? pendingSpeakerAddress : currentSpeakerAddress;
    if (selectedAddress) elements.speakerSelect.value = selectedAddress;
    if (pendingSpeakerAddress && currentSpeakerAddress === pendingSpeakerAddress) {
      pendingSpeakerAddress = "";
      setSpeakerMessage(`Connected to ${currentName}.`);
      showToast(`Connected to ${currentName}`);
    } else if (pendingSpeakerAddress) {
      setSpeakerMessage(`Switching to ${pendingChoice?.name || "the selected speaker"}…`);
    } else if (!inventory?.canSwitch) {
      setSpeakerMessage("Speaker switching is available when TouchSound runs from the Mac menu bar app.");
    } else if (speakerChoices.length < 2) {
      setSpeakerMessage(discovered.length ? "No other SoundTouch speakers found." : "Looking for SoundTouch speakers…");
    } else {
      setSpeakerMessage("");
    }
    elements.speakerSelect.disabled = !token || !inventory?.canSwitch || speakerChoices.length < 2 || Boolean(pendingSpeakerAddress);
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
    const previousStatus = latestStatus;
    latestStatus = status;
    const speaker = status.speaker || {};
    const playback = status.playback || {};
    const reachable = speaker.reachable === true;
    const healthy = status.healthy === true;

    setConnection(healthy ? "healthy" : "degraded", healthy ? "All good" : "Check bridge");
    const transport = String(speaker.transportState || "").toUpperCase();
    const playing = transport === "PLAYING";
    const paused = transport.includes("PAUSED");
    const expectedPreset = playback.expectedPreset ? String(playback.expectedPreset) : "";
    const stationName = playback.station || speaker.stationName || "";
    const trackName = speaker.track || speaker.contentItemName || "";
    const expectedUri = String(playback.expectedUri || "");
    const speakerUri = String(speaker.trackUri || "");
    const requestedAt = playback.requestedAt ? Date.parse(playback.requestedAt) : Number.NaN;
    const requestAge = Number.isFinite(requestedAt) ? Math.max(0, Date.now() - requestedAt) : PRESET_STARTUP_GRACE_MS;
    const previousPlayback = previousStatus?.playback || {};
    const previousSpeaker = previousStatus?.speaker || {};
    const selectionChanged = previousStatus
      && Number(playback.selectionGeneration || 0) !== Number(previousPlayback.selectionGeneration || 0);
    const alreadyOnRequestedStream = String(previousPlayback.expectedPreset || "") === expectedPreset
      && String(previousSpeaker.trackUri || "") === expectedUri;
    if (!expectedPreset) {
      pendingPresetTransition = null;
      stalePresetTrack = null;
    } else if (selectionChanged && expectedUri && !alreadyOnRequestedStream) {
      pendingPresetTransition = {
        slot: expectedPreset,
        previousTrack: String(previousSpeaker.track || previousSpeaker.contentItemName || ""),
        requestedAt,
      };
      stalePresetTrack = null;
    } else if (pendingPresetTransition && pendingPresetTransition.slot !== expectedPreset) {
      pendingPresetTransition = null;
      stalePresetTrack = null;
    }

    const transitionAge = pendingPresetTransition && Number.isFinite(pendingPresetTransition.requestedAt)
      ? Math.max(0, Date.now() - pendingPresetTransition.requestedAt)
      : requestAge;
    const trackChanged = pendingPresetTransition
      && (!pendingPresetTransition.previousTrack || (trackName && trackName !== pendingPresetTransition.previousTrack));
    if (trackChanged) {
      pendingPresetTransition = null;
      stalePresetTrack = null;
    } else if (pendingPresetTransition && speakerUri === expectedUri && transitionAge >= PRESET_METADATA_SETTLE_MS) {
      stalePresetTrack = {
        slot: expectedPreset,
        track: pendingPresetTransition.previousTrack,
      };
      pendingPresetTransition = null;
    }
    if (stalePresetTrack && (stalePresetTrack.slot !== expectedPreset || trackName !== stalePresetTrack.track)) {
      stalePresetTrack = null;
    }

    const awaitingPreset = reachable && Boolean(expectedPreset && stationName && expectedUri)
      && speakerUri !== expectedUri;
    const waitingForMetadata = Boolean(pendingPresetTransition)
      && pendingPresetTransition.slot === expectedPreset
      && speakerUri === expectedUri
      && transitionAge < PRESET_METADATA_SETTLE_MS;
    const switching = (awaitingPreset && !playback.lastError && requestAge < PRESET_STARTUP_GRACE_MS)
      || (reachable && waitingForMetadata && !playback.lastError);
    const presetUnconfirmed = (awaitingPreset && !switching)
      || (reachable && Boolean(expectedPreset && playback.lastError) && !switching);
    const trackIsStale = stalePresetTrack?.slot === expectedPreset && trackName === stalePresetTrack.track;
    const title = (trackIsStale ? stationName : trackName)
      || stationName
      || (speaker.source && speaker.source !== "STANDBY" ? String(speaker.source).replaceAll("_", " ") : "No preset playing");
    elements.trackTitle.textContent = !reachable
      ? "Speaker not responding"
      : (switching ? `Switching to ${stationName}…` : (presetUnconfirmed ? `${stationName} not confirmed` : title));
    elements.stationMark.textContent = stationName ? stationName.trim().slice(0, 2).toUpperCase() : "TS";
    if (!reachable) {
      elements.trackSubtitle.textContent = speaker.error || "Check that the speaker is powered on and connected to Wi-Fi.";
    } else if (switching) {
      elements.trackSubtitle.textContent = `Waiting for the speaker to start Preset ${expectedPreset}`;
    } else if (presetUnconfirmed) {
      elements.trackSubtitle.textContent = playback.lastError
        || (trackName ? `Speaker still reports ${trackName}` : `Speaker has not confirmed Preset ${expectedPreset}`);
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
    let stateLabel = "READY";
    if (!reachable) stateLabel = "OFFLINE";
    else if (switching) stateLabel = "SWITCHING";
    else if (presetUnconfirmed) stateLabel = "NOT CONFIRMED";
    else if (playing) stateLabel = "PLAYING";
    else if (paused) stateLabel = "PAUSED";
    else if (transport === "STOPPED") stateLabel = "STOPPED";
    elements.playState.textContent = stateLabel;
    if (!reachable || presetUnconfirmed) elements.playState.dataset.state = "error";
    else if (switching) elements.playState.dataset.state = "switching";
    else if (playing) elements.playState.dataset.state = "playing";
    else if (paused) elements.playState.dataset.state = "paused";
    else elements.playState.dataset.state = "";

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
      hint.textContent = configured ? "Tap to play" : "Use Edit presets";
      button.append(top, name, hint);
      if (configured) button.addEventListener("click", () => sendCommand(`/control/play/${station.slot}`, {}));
      fragment.append(button);
    }
    elements.presetGrid.replaceChildren(fragment);
  }

  function renderStationTargetSlots(stations) {
    const previousValue = elements.stationTargetSlot.value;
    const existingOptions = [...elements.stationTargetSlot.options];
    const expectedOptions = [
      { value: "", text: "Choose a preset" },
      ...stations.map((station) => ({
        value: String(station.slot),
        text: station.name ? `Preset ${station.slot} · ${station.name}` : `Preset ${station.slot} · empty`,
      })),
    ];
    const optionsMatch = existingOptions.length === expectedOptions.length
      && expectedOptions.every((option, index) => existingOptions[index].value === option.value && existingOptions[index].textContent === option.text);
    if (!optionsMatch) {
      const fragment = document.createDocumentFragment();
      for (const option of expectedOptions) {
        const element = document.createElement("option");
        element.value = option.value;
        element.textContent = option.text;
        fragment.append(element);
      }
      elements.stationTargetSlot.replaceChildren(fragment);
    }
    if (stations.some((station) => String(station.slot) === previousValue)) {
      elements.stationTargetSlot.value = previousValue;
    }
    elements.stationTargetSlot.disabled = !token || stations.length === 0;
    renderStationSearchResults(stationSearchResults);
    renderPresetEditor(stations);
  }

  function renderPresetEditor(stations) {
    const previousValue = elements.presetEditorSlot.value;
    const existingOptions = [...elements.presetEditorSlot.options];
    const expectedOptions = stations.map((station) => ({
      value: String(station.slot),
      text: station.name ? `Preset ${station.slot} · ${station.name}` : `Preset ${station.slot} · empty`,
    }));
    const optionsMatch = existingOptions.length === expectedOptions.length
      && expectedOptions.every((option, index) => existingOptions[index].value === option.value && existingOptions[index].textContent === option.text);
    if (!optionsMatch) {
      const fragment = document.createDocumentFragment();
      for (const option of expectedOptions) {
        const element = document.createElement("option");
        element.value = option.value;
        element.textContent = option.text;
        fragment.append(element);
      }
      elements.presetEditorSlot.replaceChildren(fragment);
    }
    if (stations.some((station) => String(station.slot) === previousValue)) {
      elements.presetEditorSlot.value = previousValue;
    } else if (stations.length) {
      elements.presetEditorSlot.value = String(stations[0].slot);
    }

    const selected = stations.find((station) => String(station.slot) === elements.presetEditorSlot.value);
    if (!presetEditorDirty) {
      elements.presetEditorName.value = selected?.name || "";
      elements.presetEditorStreamUrl.value = selected?.streamUrl || "";
    }
    const editable = Boolean(token && selected);
    elements.presetEditorSlot.disabled = !token || stations.length === 0;
    elements.presetEditorName.disabled = !editable || presetEditorSaving;
    elements.presetEditorStreamUrl.disabled = !editable || presetEditorSaving;
    elements.presetEditorSaveButton.disabled = !editable || presetEditorSaving;
    elements.presetEditorSaveButton.textContent = presetEditorSaving ? "Saving…" : "Save preset";
  }

  function renderStationSearchResults(stations) {
    const fragment = document.createDocumentFragment();
    if (!stations.length) {
      const placeholder = document.createElement("li");
      placeholder.className = "station-placeholder";
      placeholder.textContent = "Search for a station or genre to see results.";
      fragment.append(placeholder);
      elements.stationResults.replaceChildren(fragment);
      return;
    }

    const targetSlot = elements.stationTargetSlot.value;
    for (const station of stations) {
      const item = document.createElement("li");
      item.className = "station-result";
      const copy = document.createElement("div");
      copy.className = "station-result-copy";
      const name = document.createElement("p");
      name.className = "station-result-name";
      name.textContent = station.name || "Unnamed station";
      const details = document.createElement("p");
      details.className = "station-result-meta";
      details.textContent = [
        station.country,
        Number(station.bitrate) > 0 ? `${station.bitrate} kb/s` : "",
        station.codec,
      ].filter(Boolean).join(" · ") || "Internet radio";
      copy.append(name, details);
      const tags = String(station.tags || "").split(",").map((tag) => tag.trim()).filter(Boolean).slice(0, 3);
      if (tags.length) {
        const tagLine = document.createElement("p");
        tagLine.className = "station-result-tags";
        tagLine.textContent = tags.join(" · ");
        copy.append(tagLine);
      }

      const button = document.createElement("button");
      button.type = "button";
      button.className = "button button-secondary station-use-button";
      button.textContent = targetSlot
        ? `${config?.stations?.find((preset) => String(preset.slot) === targetSlot)?.name ? "Replace" : "Save"} ${targetSlot}`
        : "Choose preset";
      button.disabled = !token || !targetSlot || Boolean(assigningStationUuid);
      button.setAttribute("aria-label", targetSlot
        ? `Save ${station.name} to preset ${targetSlot}`
        : `Choose a preset before saving ${station.name}`);
      button.addEventListener("click", () => saveStationToPreset(station));
      item.append(copy, button);
      fragment.append(item);
    }
    elements.stationResults.replaceChildren(fragment);
  }

  async function searchStations(event) {
    event.preventDefault();
    const query = elements.stationQuery.value.trim();
    if (query.length < 2 || query.length > 80) {
      setStationSearchMessage("Enter 2 to 80 characters to search.", true);
      return;
    }
    elements.stationSearchButton.disabled = true;
    elements.stationSearchButton.textContent = "Searching…";
    setStationSearchMessage("Searching working stations in Radio Browser…");
    const skeletons = document.createDocumentFragment();
    for (let index = 0; index < 3; index += 1) {
      const skeleton = document.createElement("li");
      skeleton.className = "station-skeleton";
      skeleton.setAttribute("aria-hidden", "true");
      skeletons.append(skeleton);
    }
    elements.stationResults.replaceChildren(skeletons);
    try {
      const parameters = new URLSearchParams({ q: query, mode: elements.stationSearchMode.value });
      const result = await request(`/api/stations/search?${parameters.toString()}`);
      stationSearchResults = Array.isArray(result.stations) ? result.stations : [];
      renderStationSearchResults(stationSearchResults);
      setStationSearchMessage(stationSearchResults.length
        ? `${stationSearchResults.length} working stations found.`
        : "No working stations matched. Try another name or genre.");
    } catch (error) {
      stationSearchResults = [];
      renderStationSearchResults(stationSearchResults);
      setStationSearchMessage(error.message, true);
    } finally {
      elements.stationSearchButton.disabled = false;
      elements.stationSearchButton.textContent = "Search";
    }
  }

  async function saveStationToPreset(station) {
    const slot = elements.stationTargetSlot.value;
    if (!token) {
      setPairingVisible(true);
      return;
    }
    if (!slot) {
      setStationSearchMessage("Choose which preset to replace first.", true);
      return;
    }
    if (assigningStationUuid) return;

    assigningStationUuid = station.stationUuid;
    renderStationSearchResults(stationSearchResults);
    setStationSearchMessage(`Saving ${station.name} to preset ${slot}…`);
    try {
      const result = await request("/control/stations/assign", {
        method: "POST",
        body: JSON.stringify({ slot, stationUuid: station.stationUuid }),
      });
      setStationSearchMessage(result.message || `Preset ${slot} saved.`);
      showToast(result.message || `Preset ${slot} saved`);
      await refresh();
    } catch (error) {
      setStationSearchMessage(error.message, true);
      showToast(error.message, true);
    } finally {
      assigningStationUuid = "";
      renderStationSearchResults(stationSearchResults);
    }
  }

  async function savePresetEdit(event) {
    event.preventDefault();
    if (!token) {
      setPairingVisible(true);
      return;
    }
    const slot = elements.presetEditorSlot.value;
    const name = elements.presetEditorName.value.trim();
    const streamUrl = elements.presetEditorStreamUrl.value.trim();
    if (!slot || !name || !streamUrl) {
      setPresetEditorMessage("Enter a station name and direct stream URL.", true);
      return;
    }
    if (presetEditorSaving) return;

    presetEditorSaving = true;
    renderPresetEditor(config?.stations || []);
    setPresetEditorMessage(`Saving preset ${slot}…`);
    try {
      const result = await request("/control/stations/edit", {
        method: "POST",
        body: JSON.stringify({ slot, name, streamUrl }),
      });
      presetEditorDirty = false;
      setPresetEditorMessage(result.message || `Preset ${slot} saved.`);
      showToast(result.message || `Preset ${slot} saved`);
      await refresh();
    } catch (error) {
      setPresetEditorMessage(error.message, true);
      showToast(error.message, true);
    } finally {
      presetEditorSaving = false;
      renderPresetEditor(config?.stations || []);
    }
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
    if (event.type === "preset_station_edited") return `Preset ${event.slot} updated`;
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
      const [status, events, nextConfig, speakerInventory] = await Promise.all([
        request("/status"), request("/events"), request("/api/config"), request("/api/speakers"),
      ]);
      config = nextConfig;
      renderStationTargetSlots(config.stations || []);
      updateStatus(status);
      renderEvents(Array.isArray(events.events) ? events.events : []);
      renderSpeakerPicker(speakerInventory);
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

  async function switchSpeaker(address) {
    const target = speakerChoices.find((speaker) => speaker.address === address);
    if (!target || address === currentSpeakerAddress || pendingSpeakerAddress) return;
    elements.speakerSelect.disabled = true;
    setSpeakerMessage(`Switching to ${target.name}…`);
    try {
      const result = await request("/control/speaker", {
        method: "POST",
        body: JSON.stringify({ address }),
      });
      pendingSpeakerAddress = address;
      elements.speakerSelect.value = address;
      setSpeakerMessage(result.message || `Switching to ${target.name}…`);
      showToast(`Switching to ${target.name}`);
    } catch (error) {
      elements.speakerSelect.value = currentSpeakerAddress;
      setSpeakerMessage(error.message, true);
      showToast(error.message, true);
      elements.speakerSelect.disabled = !token || speakerChoices.length < 2;
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
  elements.speakerSelect.addEventListener("change", () => switchSpeaker(elements.speakerSelect.value));
  elements.stationSearchForm.addEventListener("submit", searchStations);
  elements.stationTargetSlot.addEventListener("change", () => renderStationSearchResults(stationSearchResults));
  elements.presetEditorSlot.addEventListener("change", () => {
    presetEditorDirty = false;
    renderPresetEditor(config?.stations || []);
  });
  elements.presetEditorForm.addEventListener("input", () => { presetEditorDirty = true; });
  elements.presetEditorForm.addEventListener("submit", savePresetEdit);
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
    await refresh();
    window.setInterval(refresh, 5000);
  }

  initialize();
})();
