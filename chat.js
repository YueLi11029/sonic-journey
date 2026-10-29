const $ = (selector) => document.querySelector(selector);

const form = $("#chat-form");
const input = $("#message-input");
const sendButton = $("#send-button");
const chat = $("#chat");
const chatError = $("#chat-error");
const connection = $("#connection");
const connectionText = $("#connection-text");
const audio = $("#journey-audio");
const playToggle = $("#play-toggle");
const seek = $("#seek");
const app = $("#app");
const preview = $("#preview-audio");
const overlay = $("#city-overlay");
const activity = $("#activity");
const { Waveform, formatTime } = window.Waveforms;

const JOURNEY_TOOLS = new Set(["create_journey", "get_current_journey"]);
const SOURCE_COLORS = ["#ffc04d", "#62c9ff", "#a78bfa", "#52d6a4", "#ff8f7a", "#7fb3ff", "#f472b6", "#cbd5e1"];
const STEPS = [
  "Understand the request",
  "Inspect available recordings",
  "Select recordings and order",
  "Call the journey tool",
  "Receive and validate the result",
  "Make the audio playable",
];
const MODE_LABELS = { city: "Explore a city", mood: "Mood", sequence: "Sound journey", custom: "Custom mix" };
const MOODS = ["Calm and atmospheric", "Energetic and busy", "Cinematic", "Quiet and reflective", "Immersive urban ambience"];
const SPEECH_STATUS = {
  no_speech_detected: ["No speech detected", "The heuristic detector found no speech-like sound. This is not proof that none exists."],
  speech_type_uncertain: ["Speech-like sound · type uncertain", "Speech-like sound was detected. Without a transcript its purpose is unknown; it may not be an announcement."],
  likely_announcement: ["Likely announcement", "A transcript contains typical announcement phrases."],
  confirmed_announcement: ["Announcement confirmed", "A person reviewed a segment and confirmed it is an announcement."],
  reviewed_no_announcement: ["Reviewed · no announcement", "A person reviewed every detected segment and found no announcement."],
  unavailable: ["Analysis unavailable", "The audio could not be analysed."],
  not_analyzed: ["Not analyzed yet", "Speech analysis has not run for this recording yet."],
};
const REVIEW_LABELS = {
  confirmed_announcement: "Confirmed announcement",
  not_announcement: "Not an announcement",
  uncertain: "Uncertain",
};

let submitting = false;
let busy = false;
let renderedKey = "";
let lastState = { chat: [], busy: false, error: "" };
let recordings = null;
let journeys = [];
let selectedUrl = "";
let latestUrl = "";
let renderedJourneyKey = "";
const audioStatus = { url: "", state: "idle" };
let activePreview = null;
let mode = "landing";
let creationMode = null;
const modeState = { city: "", mood: "", sequence: "", files: [], allowOthers: false, duration: "", crossfade: "", fade: "" };
const analysisCache = new Map();

// ---------- Small helpers ----------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(className, text, onClick) {
  const node = el("button", className, text);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

function plural(count, word, many = `${word}s`) {
  return `${count} ${count === 1 ? word : many}`;
}

function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

// Renders **bold**, `code`, lists and paragraphs from agent text without innerHTML.
function appendInline(parent, text) {
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(text.slice(last, match.index));
    const token = match[0];
    parent.append(token.startsWith("**") ? el("strong", "", token.slice(2, -2)) : el("code", "", token.slice(1, -1)));
    last = match.index + token.length;
  }
  parent.append(text.slice(last));
}

function renderRichText(container, text) {
  const listItem = /^\s*(?:([-*•])|(\d+)[.)])\s+(.*)$/;
  let list = null;
  let paragraph = null;
  for (const raw of text.replace(/\r/g, "").split("\n")) {
    const line = raw.replace(/^#{1,6}\s+/, "");
    const item = line.match(listItem);
    if (!line.trim()) {
      list = paragraph = null;
    } else if (item) {
      const ordered = Boolean(item[2]);
      if (!list || (list.tagName === "OL") !== ordered) {
        list = el(ordered ? "ol" : "ul");
        container.append(list);
      }
      const li = el("li");
      appendInline(li, item[3]);
      list.append(li);
      paragraph = null;
    } else if (raw !== line) {
      // A Markdown heading becomes its own bold line.
      list = paragraph = null;
      const heading = el("p");
      heading.append(el("strong", "", line.trim()));
      container.append(heading);
    } else {
      list = null;
      if (!paragraph) {
        paragraph = el("p");
        container.append(paragraph);
      } else {
        paragraph.append(el("br"));
      }
      appendInline(paragraph, line);
    }
  }
}

// ---------- Interpreting the server's chat log ----------

function parseEntries(entries) {
  return entries.map(([role, text, meta]) => ({
    role,
    text,
    meta: meta || null,
    data: role === "call" || role === "tool" ? parseJson(text) : null,
  }));
}

function pairInvocations(entries) {
  const invocations = [];
  for (const entry of entries) {
    if (!entry.data) continue;
    if (entry.role === "call") {
      invocations.push({ tool: entry.data.tool, arguments: entry.data.arguments, result: null });
    } else {
      const open = invocations.find((item) => item.tool === entry.data.tool && !item.result);
      if (open) open.result = entry.data;
      else invocations.push({ tool: entry.data.tool, arguments: null, result: entry.data });
    }
  }
  return invocations;
}

function journeyResults(entries) {
  return entries
    .filter((entry) => entry.role === "tool" && entry.data && JOURNEY_TOOLS.has(entry.data.tool))
    .map((entry) => entry.data);
}

function recordingByFile(filename) {
  return recordings?.recordings.find((item) => item.filename === filename);
}

// ---------- Examples grounded in the real inventory ----------

function examplePrompts() {
  if (!recordings) return ["Which recordings can you use?"];
  const items = recordings.recordings;
  const cities = [...recordings.cities].sort((a, b) => b.recording_count - a.recording_count || a.name.localeCompare(b.name));
  const tags = new Set(items.flatMap((item) => [...item.tags, item.stage_hint]));
  const prompts = [];
  if (cities[0]?.recording_count >= 2) {
    prompts.push(`Create a 30-second soundscape using only the ${cities[0].name} recordings.`);
  }
  // Only offer an arrival-to-departure arc when both stages exist in the metadata.
  if (tags.has("train approaching") && tags.has("train departing")) {
    prompts.push("Create a journey that begins with an approaching train and ends with a departing train.");
  }
  const pair = cities.filter((city) => city !== cities[0]).slice(0, 2);
  if (pair.length === 2) prompts.push(`Compare the recordings from ${pair[0].name} and ${pair[1].name}.`);
  const longest = [...items].sort((a, b) => b.duration_seconds - a.duration_seconds)[0];
  if (longest) prompts.push(`Use only ${longest.filename} to create a 20-second audio experience.`);
  if (items.some((item) => item.measured?.levels)) {
    prompts.push("Create a calm soundscape using the quietest suitable recording.");
  }
  if (items.some((item) => item.measured?.speech_intervals?.length)) {
    prompts.push("Do any recordings contain station announcements?");
  }
  prompts.push("Which sound categories are missing from the library?");
  return prompts.slice(0, 5);
}

// ---------- Chat ----------

function renderWelcome() {
  const welcome = $("#welcome-template").content.cloneNode(true);
  const examples = welcome.querySelector(".examples");
  for (const text of examplePrompts()) {
    examples.append(button("example", text, () => {
      input.value = text;
      autosize();
      input.focus();
    }));
  }
  return welcome;
}

function renderEvent(entry) {
  const result = entry.data;
  if (!result) return null;
  if (result.status === "error") {
    return el("div", "event err", `${result.tool} failed: ${result.error}`);
  }
  if (result.tool === "list_audio_files") {
    return el("div", "event ok", `Checked the library · ${plural((result.recordings || []).length, "WAV recording")}`);
  }
  if (JOURNEY_TOOLS.has(result.tool) && result.url) {
    const label = result.tool === "create_journey" ? "Journey created" : "Existing journey returned";
    const node = el("div", "event ok", `${label} · ${Number(result.duration_seconds).toFixed(1)} s`);
    node.append(button("text-button", result.url === selectedUrl ? "In player" : "Open in player", () => {
      selectJourney(result.url);
      $(".journey").scrollIntoView({ behavior: "smooth", block: "nearest" });
    }));
    return node;
  }
  return null;
}

function modeSummary(meta) {
  const chosen = meta?.mode;
  if (!chosen) return "";
  const detail = chosen.city || chosen.mood || chosen.sequence || (chosen.files?.length ? plural(chosen.files.length, "file") : "");
  return [MODE_LABELS[chosen.type], detail].filter(Boolean).join(" · ");
}

function renderChat(entries) {
  const nodes = [];
  if (!entries.length) nodes.push(renderWelcome());

  for (const entry of entries) {
    if (entry.role === "you" || entry.role === "agent") {
      const message = el("article", `message ${entry.role}`);
      const label = el("span", "message-label", entry.role === "you" ? "You" : "Sonic Journey");
      const summary = entry.role === "you" ? modeSummary(entry.meta) : "";
      if (summary) label.append(el("span", "mode-chip", summary));
      message.append(label);
      const body = el("div", "message-body");
      if (entry.role === "agent") renderRichText(body, entry.text);
      else body.textContent = entry.text;
      message.append(body);
      nodes.push(message);
    } else if (entry.role === "tool") {
      const event = renderEvent(entry);
      if (event) nodes.push(event);
    }
  }

  if (busy) {
    const pending = pairInvocations(entries).filter((item) => !item.result).pop();
    const typing = el("div", "typing");
    const dots = el("span");
    dots.append(el("i"), el("i"), el("i"));
    typing.append(dots, pending ? `Running ${pending.tool}…` : "Sonic Journey is working…");
    nodes.push(typing);
  }

  chat.replaceChildren(...nodes);
  chat.scrollTop = chat.scrollHeight;
}

// ---------- Agent activity ----------

function stepStates(turn, turnBusy, turnError) {
  const steps = STEPS.map((title) => ({ title, state: "pending", detail: "" }));
  if (!turn) return steps;

  const invocations = pairInvocations(turn.entries);
  const replied = turn.entries.some((entry) => entry.role === "agent");
  const listing = invocations.filter((item) => item.tool === "list_audio_files").pop();
  const journey = invocations.filter((item) => JOURNEY_TOOLS.has(item.tool)).pop();
  const finished = !turnBusy;
  const notUsed = (step, detail) => {
    if (finished) Object.assign(step, { state: "skipped", detail });
  };

  const prompt = turn.prompt.length > 90 ? `${turn.prompt.slice(0, 90)}…` : turn.prompt;
  if (invocations.length || replied) {
    Object.assign(steps[0], { state: "done", detail: `“${prompt}”` });
  } else if (turnBusy) {
    Object.assign(steps[0], { state: "active", detail: "Waiting for the model's first response" });
  } else if (turnError) {
    Object.assign(steps[0], { state: "failed", detail: turnError });
  }

  if (listing) {
    if (!listing.result) Object.assign(steps[1], { state: "active", detail: "list_audio_files running" });
    else if (listing.result.status === "success")
      Object.assign(steps[1], { state: "done", detail: `list_audio_files → ${plural((listing.result.recordings || []).length, "WAV recording")}` });
    else Object.assign(steps[1], { state: "failed", detail: listing.result.error });
  } else notUsed(steps[1], "Not needed for this request");

  if (journey && journey.tool === "get_current_journey") {
    Object.assign(steps[2], { state: "skipped", detail: "Reused the journey already created" });
  } else if (journey) {
    const files = journey.arguments?.audio_files || journey.result?.audio_files || [];
    const seconds = journey.arguments?.duration_seconds ?? journey.result?.requested_seconds;
    Object.assign(steps[2], {
      state: "done",
      detail: `${files.join(" → ")}${seconds ? ` · ${seconds} s requested` : ""}`,
    });
  } else notUsed(steps[2], "No journey was requested");

  if (journey) {
    Object.assign(steps[3], journey.result
      ? { state: "done", detail: `${journey.tool} called` }
      : { state: "active", detail: `${journey.tool} running` });
  } else notUsed(steps[3], "No journey tool was called");

  if (journey?.result?.status === "success") {
    const result = journey.result;
    Object.assign(steps[4], {
      state: "done",
      detail: `Server verified ${Number(result.duration_seconds).toFixed(1)} s · ${result.sample_rate / 1000} kHz ${result.channels === 2 ? "stereo" : "mono"} WAV`,
    });
    if (audioStatus.url !== result.url) {
      Object.assign(steps[5], { detail: "Open it in the player to load the audio" });
    } else if (audioStatus.state === "ready") {
      Object.assign(steps[5], { state: "done", detail: `Player loaded ${formatTime(audio.duration)} of audio` });
    } else if (audioStatus.state === "error") {
      Object.assign(steps[5], { state: "failed", detail: "The browser could not load this audio file" });
    } else {
      Object.assign(steps[5], { state: "active", detail: "Loading the WAV in the player" });
    }
  } else if (journey?.result) {
    Object.assign(steps[4], { state: "failed", detail: journey.result.error });
    notUsed(steps[5], "No audio was produced");
  } else {
    if (!journey) notUsed(steps[4], "");
    notUsed(steps[5], "");
  }

  // A run that stopped with an error leaves its unfinished server steps unused.
  // The last step tracks the browser loading the audio, so it keeps its state.
  if (finished) {
    for (const step of steps.slice(0, 5)) {
      if (step.state === "pending" || step.state === "active") step.state = "skipped";
    }
    if (steps[5].state === "pending") steps[5].state = "skipped";
  }
  return steps;
}

let lastRunStatus = null;

function renderActivity() {
  const entries = parseEntries(lastState.chat || []);
  const lastUser = entries.map((entry) => entry.role).lastIndexOf("you");
  const turn = lastUser < 0 ? null : { prompt: entries[lastUser].text, entries: entries.slice(lastUser + 1) };

  const steps = stepStates(turn, busy, lastState.error);
  $("#steps").replaceChildren(
    ...steps.map((step, index) => {
      const item = el("li", "step");
      item.dataset.state = step.state;
      item.append(el("span", "step-icon"));
      const text = el("div");
      text.append(el("span", "step-title", `${index + 1}. ${step.title}`));
      if (step.detail) text.append(el("span", "step-detail", step.detail));
      item.append(text);
      return item;
    })
  );

  const invocations = turn ? pairInvocations(turn.entries) : [];
  $("#tool-count").textContent = invocations.length ? `(${invocations.length})` : "";
  $("#tool-log").replaceChildren(
    ...invocations.map((item) => {
      const row = el("li");
      const args = item.arguments === null ? "(arguments not recorded)" : JSON.stringify(item.arguments);
      row.append(`${item.tool}(${args})`);
      if (item.result) {
        row.append(el("br"));
        row.append(
          item.result.status === "success"
            ? el("span", "ok", `→ success${item.result.duration_seconds ? ` · ${item.result.duration_seconds} s · ${item.result.filename}` : ""}`)
            : el("span", "err", `→ error · ${item.result.error}`)
        );
      } else {
        row.append(el("br"), el("span", "muted", "→ running…"));
      }
      return row;
    })
  );

  // /state reports the run's real outcome; a 202 from /chat only means it started.
  const run = busy ? "running" : lastState.run;
  $("#activity-state").textContent = !turn
    ? "Idle"
    : run === "running"
      ? "Working…"
      : run === "failed"
        ? "Failed"
        : steps.some((step) => step.state === "failed")
          ? "Finished with an error"
          : "Finished";

  // Open while working or after any failure; fold away once a journey succeeds.
  const failedStep = steps.some((step) => step.state === "failed");
  const outcome = `${run}:${failedStep}`;
  if (outcome !== lastRunStatus) {
    lastRunStatus = outcome;
    if (run === "running" || run === "failed" || failedStep) activity.open = true;
    else if (run === "succeeded" && journeys.length) activity.open = false;
  }
}

// ---------- Finished journey ----------

const journeyWave = new Waveform({
  label: "Finished journey position",
  height: 56,
  onSeek: (seconds) => {
    audio.currentTime = seconds;
    journeyWave.setPosition(seconds);
  },
});
$("#journey-wave").append(journeyWave.el);

function selectJourney(url) {
  selectedUrl = url;
  renderJourney();
  renderChatIfNeeded(true);
}

function renderJourney() {
  const entries = parseEntries(lastState.chat || []);
  const results = journeyResults(entries);
  journeys = results.filter((result) => result.status === "success" && result.url);

  const newest = journeys.at(-1)?.url || "";
  if (newest !== latestUrl) {
    latestUrl = newest;
    selectedUrl = newest;
  }
  const journey = journeys.findLast((item) => item.url === selectedUrl);
  const lastAttempt = results.at(-1);
  const failure = lastAttempt && lastAttempt.status === "error" ? lastAttempt.error : "";

  const key = JSON.stringify([selectedUrl, failure, Boolean(journey), Boolean(recordings)]);
  if (key === renderedJourneyKey) return;
  renderedJourneyKey = key;

  app.dataset.result = journey ? "ready" : "none";
  const alert = $("#journey-alert");
  alert.hidden = !failure;
  alert.textContent = failure
    ? `The latest journey request failed: ${failure}${journey ? " Showing your previous journey." : ""}`
    : "";

  $("#journey-empty").hidden = Boolean(journey);
  $("#journey-body").hidden = !journey;
  window.SonicGlobe?.highlightCities(journey ? journey.locations || [] : []);

  if (!journey) {
    $("#journey-meta").textContent = "";
    audio.pause();
    audio.removeAttribute("src");
    audioStatus.url = "";
    audioStatus.state = "idle";
    return;
  }

  const sources = journey.sources || [];
  $("#journey-meta").textContent =
    `${Number(journey.duration_seconds).toFixed(1)} s · ${plural(sources.length, "source")} · ${(journey.locations || []).join(", ")}`;

  // The timeline shows real segment positions; looped journeys repeat, so it is omitted.
  const timeline = $("#timeline");
  timeline.replaceChildren();
  timeline.hidden = journey.looped || !sources.length;
  sources.forEach((source, index) => {
    const end = sources[index + 1]?.starts_at_seconds ?? journey.duration_seconds;
    const segment = el("span");
    segment.style.flex = `${Math.max(end - source.starts_at_seconds, 0.1)}`;
    segment.style.background = SOURCE_COLORS[index % SOURCE_COLORS.length];
    segment.title = source.filename;
    timeline.append(segment);
  });

  $("#source-list").replaceChildren(
    ...sources.map((source, index) => {
      const item = el("li");
      const swatch = el("span", "swatch");
      swatch.style.background = SOURCE_COLORS[index % SOURCE_COLORS.length];
      const title = recordingByFile(source.filename)?.title;
      const name = el("span", "source-name", title || source.filename);
      name.append(el("small", "", [source.location, source.filename].join(" · ")));
      const time = el("span", "source-time", `${formatTime(source.starts_at_seconds)} → ${source.seconds_used} s`);
      const chosen = source.offset_chosen_by ? ` (${source.offset_chosen_by})` : "";
      time.append(el("small", "", `from ${formatTime(source.source_offset_seconds)} in source${chosen}`));
      item.append(swatch, name, time);
      return item;
    })
  );

  // Only transitions the backend reports are described.
  const transitions = (journey.transitions || []).map((item) => {
    const from = recordingByFile(item.from)?.title || item.from;
    const to = recordingByFile(item.to)?.title || item.to;
    return el("li", "", `${formatTime(item.at_seconds)} · ${item.seconds} s ${item.type}: ${from} → ${to}`);
  });
  if (journey.fade_in_seconds) {
    transitions.push(el("li", "", `Fades in and out over ${journey.fade_in_seconds} s at the start and end`));
  }
  $("#transition-list").replaceChildren(...transitions);
  $("#journey-notes").replaceChildren(...(journey.notes || []).map((note) => el("li", "", note)));

  if (audioStatus.url !== journey.url) {
    audio.pause();
    audioStatus.url = journey.url;
    audioStatus.state = "loading";
    audio.src = journey.url;
    audio.load();
    seek.value = 0;
    seek.max = journey.duration_seconds;
    $("#time-total").textContent = formatTime(journey.duration_seconds);
    journeyWave.setDuration(journey.duration_seconds);
    journeyWave.setPosition(0);
    journeyWave.load(`${journey.url}/waveform`, journey.url).then((ok) => {
      // Without a waveform, the plain slider keeps seeking available.
      seek.hidden = ok;
    });
    updatePlayer();
  }
}

function updatePlayer() {
  const playing = !audio.paused && !audio.ended;
  playToggle.dataset.playing = String(playing);
  playToggle.setAttribute("aria-label", playing ? "Pause journey" : "Play journey");
  playToggle.disabled = audioStatus.state === "error";
  if (!seek.matches(":active")) seek.value = audio.currentTime || 0;
  journeyWave.setPosition(audio.currentTime || 0);
  $("#time-current").textContent = formatTime(audio.currentTime);
}

playToggle.addEventListener("click", () => {
  if (audio.paused || audio.ended) {
    preview.pause();
    audio.play().catch(() => {
      audioStatus.state = "error";
      showAudioError();
    });
  } else {
    audio.pause();
  }
});

seek.addEventListener("input", () => {
  audio.currentTime = Number(seek.value);
  $("#time-current").textContent = formatTime(audio.currentTime);
});

for (const type of ["play", "pause", "ended", "timeupdate", "seeked"]) {
  audio.addEventListener(type, updatePlayer);
}
audio.addEventListener("play", () => preview.pause());

audio.addEventListener("loadedmetadata", () => {
  if (!audio.src.endsWith(audioStatus.url)) return;
  audioStatus.state = "ready";
  seek.max = audio.duration;
  journeyWave.setDuration(audio.duration);
  $("#time-total").textContent = formatTime(audio.duration);
  updatePlayer();
  renderActivity();
});

audio.addEventListener("error", () => {
  if (!audioStatus.url) return;
  audioStatus.state = "error";
  showAudioError();
});

function showAudioError() {
  const alert = $("#journey-alert");
  alert.hidden = false;
  alert.textContent = "This journey's audio could not be loaded. It may have been replaced by newer journeys; ask the agent to create it again.";
  updatePlayer();
  renderActivity();
}

$("#revise-button").addEventListener("click", () => composeInChat("Revise this journey: "));

// ---------- City overlay: explore, then browse ----------

let overlayReturnFocus = null;
let overlayCity = null;

function showView(name) {
  for (const view of ["city", "recordings", "library"]) {
    $(`#view-${view}`).hidden = view !== name;
  }
  overlay.dataset.view = name;
  $("#overlay-card").scrollTop = 0;
}

function openOverlay(view) {
  if (overlay.hidden) {
    overlayReturnFocus = document.activeElement;
    overlay.hidden = false;
    document.body.classList.add("overlay-open");
  }
  showView(view);
  $("#overlay-close").focus();
}

function closeOverlay() {
  preview.pause();
  if (overlay.hidden) return;
  overlay.hidden = true;
  document.body.classList.remove("overlay-open");
  overlayReturnFocus?.focus?.();
}

$("#overlay-close").addEventListener("click", closeOverlay);
overlay.querySelector("[data-close]").addEventListener("click", closeOverlay);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !overlay.hidden) {
    event.preventDefault();
    closeOverlay();
  }
});

function setPhoto(image, cityName) {
  const img = $("#city-image");
  const fallback = $("#city-photo-fallback");
  const credit = $("#photo-credit");
  credit.replaceChildren();
  if (!image) {
    img.hidden = true;
    img.removeAttribute("src");
    fallback.hidden = false;
    return;
  }
  fallback.hidden = true;
  img.hidden = false;
  img.alt = `Representative photo of ${cityName}`;
  img.onerror = () => {
    img.hidden = true;
    fallback.hidden = false;
  };
  img.src = image.url;

  credit.append(`Photo: ${image.author}, `);
  const license = el("a", "", image.license);
  if (image.license_url) license.href = image.license_url;
  license.target = "_blank";
  license.rel = "noopener";
  const source = el("a", "", "Wikimedia Commons");
  source.href = image.source_url;
  source.target = "_blank";
  source.rel = "noopener";
  credit.append(license, ", via ", source, ".");
}

function cityRecordings(name) {
  return (recordings?.recordings || []).filter((item) => item.location === name);
}

function openCity(name) {
  const city = recordings?.cities.find((item) => item.name === name);
  if (!city) return;
  overlayCity = name;
  const items = cityRecordings(name);
  setPhoto(city.image, city.name);
  $("#overlay-title").textContent = city.name;
  $("#city-count").textContent = plural(items.length, "recording");
  const exact = items.filter((item) => item.location_precision === "exact").length;
  $("#city-eyebrow").textContent = city.center ? "City-level marker" : "City";
  // A city photo illustrates the place; it is not where the audio was recorded.
  $("#city-disclaimer").textContent =
    `Representative photo of ${city.name}, not a recording location. ` +
    (exact
      ? `${plural(exact, "recording")} here ${exact === 1 ? "has" : "have"} a verified exact spot.`
      : "The marker shows the city, not the exact recording spot.");
  $("#browse-recordings").disabled = !items.length;
  openOverlay("city");
  window.SonicGlobe?.focus(name);
}

function openRecordings(name) {
  const items = cityRecordings(name);
  $("#back-label").textContent = name;
  $("#gallery-heading").textContent = `${name} · ${plural(items.length, "recording")}`;
  const journeyButton = $("#city-journey");
  journeyButton.textContent = `Create a journey from ${name}`;
  journeyButton.onclick = () => {
    creationMode = "city";
    modeState.city = name;
    renderModes();
    composeInChat(`Create a one-minute journey using the ${name} recordings.`);
  };
  const tags = [...new Set(items.flatMap((item) => item.tags))];
  $("#gallery-note").textContent = tags.length
    ? `Tags here: ${tags.join(", ")}. Waveforms are measured from each file; highlighted bands are speech-like sound found by a heuristic detector.`
    : "Waveforms are measured from each file.";
  $("#gallery").replaceChildren(...items.map(recordingCard));
  openOverlay("recordings");
}

function openLibrary() {
  if (!recordings) return;
  $("#library-heading").textContent = plural(recordings.cities.length, "city", "cities");
  $("#library-note").textContent = `${plural(recordings.recordings.length, "recording")} in total. Choose a city to see its photo and recordings.`;
  $("#library").replaceChildren(
    ...recordings.cities.map((city) => {
      const li = el("li");
      const row = button("city-row", "", () => openCity(city.name));
      let thumb = el("span", "thumb-fallback");
      if (city.image) {
        thumb = el("img");
        thumb.src = city.image.url;
        thumb.alt = "";
        thumb.onerror = () => thumb.replaceWith(el("span", "thumb-fallback"));
      }
      const text = el("span");
      text.append(el("strong", "", city.name), plural(city.recording_count, "recording"));
      row.append(thumb, text);
      li.append(row);
      return li;
    })
  );
  openOverlay("library");
}

$("#browse-recordings").addEventListener("click", () => overlayCity && openRecordings(overlayCity));
$("#back-to-city").addEventListener("click", () => overlayCity && openCity(overlayCity));
$("#recordings-toggle").addEventListener("click", openLibrary);
$("#hero-browse").addEventListener("click", openLibrary);
window.SonicGlobe?.onSelect((marker) => openCity(marker.city));

// ---------- Recording cards: waveform, preview and speech analysis ----------

function setActivePreview(card) {
  if (activePreview && activePreview.card !== card.el) {
    activePreview.card.dataset.playing = "false";
    activePreview.wave.setPosition(0);
    activePreview.time.textContent = `0:00 / ${formatTime(activePreview.item.duration_seconds)}`;
  }
  activePreview = { card: card.el, wave: card.wave, item: card.item, time: card.time };
  if (!preview.src.endsWith(card.item.audio_url)) {
    preview.src = card.item.audio_url;
  }
}

// Gallery previews use their own player, so a source clip never touches the finished journey.
function playPreview(card, at = null) {
  audio.pause();
  setActivePreview(card);
  const start = () => {
    if (at !== null) preview.currentTime = at;
    preview.play().catch(() => {
      card.el.dataset.playing = "false";
      card.status.textContent = "This recording could not be played.";
    });
  };
  if (at !== null && preview.readyState < 1) {
    preview.addEventListener("loadedmetadata", start, { once: true });
    preview.load();
  } else {
    start();
  }
}

function togglePreview(card) {
  if (activePreview?.card === card.el && !preview.paused) preview.pause();
  else playPreview(card);
}

preview.addEventListener("play", () => activePreview && (activePreview.card.dataset.playing = "true"));
preview.addEventListener("pause", () => activePreview && (activePreview.card.dataset.playing = "false"));
preview.addEventListener("ended", () => activePreview && (activePreview.card.dataset.playing = "false"));
preview.addEventListener("timeupdate", () => {
  if (!activePreview) return;
  activePreview.wave.setPosition(preview.currentTime);
  activePreview.time.textContent = `${formatTime(preview.currentTime)} / ${formatTime(activePreview.item.duration_seconds)}`;
});
preview.addEventListener("error", () => {
  if (activePreview) activePreview.card.dataset.playing = "false";
});

function playIcons() {
  const svg = (cls, d) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    node.setAttribute("viewBox", "0 0 24 24");
    node.setAttribute("aria-hidden", "true");
    node.classList.add(cls);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    node.append(path);
    return node;
  };
  return [svg("icon-play", "M8 5.5v13l11-6.5z"), svg("icon-pause", "M7 5h3.5v14H7zM13.5 5H17v14h-3.5z")];
}

async function fetchAnalysis(item, force = false) {
  const key = `${item.id}:${item.version}`;
  if (force || !analysisCache.has(key)) {
    const request = fetch(`/recordings/${item.id}/analysis`, { cache: "no-store" }).then(async (response) => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    });
    request.catch(() => analysisCache.delete(key));
    analysisCache.set(key, request);
  }
  return analysisCache.get(key);
}

function renderAnalysis(card, result) {
  const [label, explanation] = SPEECH_STATUS[result.status] || SPEECH_STATUS.unavailable;
  card.badge.textContent = label;
  card.badge.title = explanation;
  card.badge.dataset.status = result.status;
  card.badge.dataset.source = result.review_status === "reviewed" ? "reviewed" : "machine";

  const intervals = result.intervals || [];
  card.wave.setIntervals(intervals.map((interval) => ({ ...interval, kind: interval.review || "candidate" })));

  const notes = [];
  if (result.status === "unavailable") notes.push(result.error || "The audio could not be analysed.");
  else notes.push(`Machine estimate (${result.method}).${result.review_status === "reviewed" ? " Includes your reviews." : ""}`);
  if (intervals.length && result.transcription?.startsWith("unavailable")) notes.push("No transcript: speech recognition is unavailable.");
  card.analysisNote.textContent = notes.join(" ");

  const shown = intervals.slice(0, 6);
  card.intervals.replaceChildren(
    ...shown.map((interval) => {
      const row = el("li", "interval");
      row.append(button(
        "interval-seek",
        `${formatTime(interval.start_seconds)}–${formatTime(interval.end_seconds)} speech-like`,
        () => playPreview(card, interval.start_seconds)
      ));
      if (interval.transcript) row.append(el("q", "transcript", interval.transcript));
      const select = el("select", "review-select");
      select.setAttribute("aria-label", `Review the segment at ${formatTime(interval.start_seconds)}`);
      select.append(new Option(interval.review ? "Clear review" : "Review…", ""));
      for (const [value, text] of Object.entries(REVIEW_LABELS)) {
        select.append(new Option(text, value, false, interval.review === value));
      }
      if (interval.review) select.value = interval.review;
      select.addEventListener("change", async () => {
        select.disabled = true;
        try {
          const response = await fetch(`/recordings/${card.item.id}/review`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ start_seconds: interval.start_seconds, label: select.value || null }),
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
          analysisCache.set(`${card.item.id}:${card.item.version}`, Promise.resolve(data));
          renderAnalysis(card, data);
        } catch (error) {
          card.analysisNote.textContent = `Review not saved: ${error.message}`;
          select.disabled = false;
        }
      });
      row.append(select);
      return row;
    })
  );
  if (intervals.length > shown.length) {
    card.intervals.append(el("li", "muted tiny", `${intervals.length - shown.length} more segments are marked on the waveform.`));
  }
}

function recordingCard(item) {
  const card = { item };
  card.el = el("li", "card");
  card.el.dataset.id = item.id;

  const head = el("div", "card-head");
  const play = el("button", "card-play");
  play.type = "button";
  play.setAttribute("aria-label", `Preview ${item.title}`);
  play.append(...playIcons());
  play.addEventListener("click", () => togglePreview(card));
  const titles = el("div", "card-titles");
  titles.append(el("p", "card-title", item.title));
  titles.append(el("p", "card-meta", [item.location, formatTime(item.duration_seconds), item.category].filter(Boolean).join(" · ")));
  card.badge = el("span", "speech-badge", "Not analyzed yet");
  card.badge.dataset.status = "not_analyzed";
  head.append(play, titles, card.badge);

  card.wave = new Waveform({
    label: `Preview position in ${item.title}`,
    onSeek: (seconds) => playPreview(card, seconds),
  });
  card.wave.setDuration(item.duration_seconds);
  card.time = el("p", "card-time", `0:00 / ${formatTime(item.duration_seconds)}`);
  card.status = el("p", "card-status");
  card.intervals = el("ul", "intervals");
  card.analysisNote = el("p", "card-note");

  card.el.append(head);
  if (item.description) card.el.append(el("p", "card-desc", item.description));
  card.el.append(card.wave.el, card.time, card.status, card.intervals, card.analysisNote);

  if (item.tags.length) {
    const tags = el("div", "tags");
    tags.append(...item.tags.map((tag) => el("span", "", tag)));
    card.el.append(tags);
  }
  const facts = [];
  const excerpt = item.packaged_excerpt;
  if (excerpt && excerpt.seconds < excerpt.original_seconds - 0.1) {
    facts.push(`Hosted preview: ${excerpt.seconds} s excerpt of ${excerpt.original_seconds} s`);
  }
  facts.push(!item.source && !item.license
    ? "Source and license: not documented yet"
    : `Source: ${item.source || "not documented yet"} · License: ${item.license || "not documented yet"}`);
  card.el.append(el("p", "card-note", facts.join(" · ")));
  card.el.append(el("p", "card-file", item.filename));
  const actions = el("div", "card-actions");
  actions.append(button("text-button", "Use in a journey", () =>
    composeInChat(`Create a one-minute journey that features ${item.filename} ("${item.title}").`)
  ));
  card.el.append(actions);

  card.wave.load(`/recordings/${item.id}/waveform`, `${item.id}:${item.version}`);
  fetchAnalysis(item)
    .then((result) => renderAnalysis(card, result))
    .catch(() => renderAnalysis(card, { status: "unavailable", intervals: [], error: "The analysis could not be loaded." }));
  return card.el;
}

// ---------- Creation modes ----------

function sequencePresets() {
  const stages = new Set((recordings?.recordings || []).map((item) => item.stage_hint));
  const presets = [];
  if (stages.has("train approaching") && stages.has("train departing")) {
    presets.push("Train approaching → train departing");
  }
  presets.push("Quiet start → busier middle → quiet ending", "Build intensity, then fade out");
  return presets;
}

function chipGroup(options, selected, onPick) {
  const group = el("div", "chips");
  for (const option of options) {
    const chip = button("chip", option, () => onPick(option));
    chip.setAttribute("aria-pressed", String(option === selected));
    group.append(chip);
  }
  return group;
}

function numberField(label, key, min, max, step) {
  const wrap = el("label", "field");
  wrap.append(label);
  const field = el("input");
  Object.assign(field, { type: "number", min, max, step, value: modeState[key], placeholder: "auto" });
  field.addEventListener("input", () => (modeState[key] = field.value));
  wrap.append(field);
  return wrap;
}

function renderModes() {
  for (const tab of document.querySelectorAll(".mode-tab")) {
    tab.setAttribute("aria-pressed", String(tab.dataset.mode === creationMode));
  }
  const options = $("#mode-options");
  options.hidden = !creationMode;
  options.replaceChildren();
  if (!creationMode || !recordings) return;
  const items = recordings.recordings;

  if (creationMode === "city") {
    if (!modeState.city) modeState.city = recordings.cities[0]?.name || "";
    const select = el("select", "mode-select");
    select.setAttribute("aria-label", "City");
    for (const city of recordings.cities) {
      select.append(new Option(`${city.name} · ${plural(city.recording_count, "recording")}`, city.name, false, city.name === modeState.city));
    }
    select.addEventListener("change", () => {
      modeState.city = select.value;
      renderModes();
    });
    const here = items.filter((item) => item.location === modeState.city);
    const tags = [...new Set(here.flatMap((item) => item.tags))];
    options.append(select, el("p", "mode-note", `${plural(here.length, "recording")} · tags: ${tags.join(", ") || "none"}. The agent will only use ${modeState.city} recordings unless you allow others.`));
  } else if (creationMode === "mood") {
    options.append(
      chipGroup(MOODS, modeState.mood, (value) => {
        modeState.mood = modeState.mood === value ? "" : value;
        renderModes();
      }),
      el("p", "mode-note", "Matched to tags, descriptions and measured recording levels. A level is not a mood, so the agent will describe the match as an inference.")
    );
  } else if (creationMode === "sequence") {
    const stages = [...new Set(items.map((item) => item.stage_hint).filter((hint) => hint !== "unlabeled"))];
    const known = new Set([...stages, ...items.flatMap((item) => item.tags)]);
    const missing = ["train passing", "boarding", "footsteps", "crowds", "rain"].filter((kind) => !known.has(kind));
    if (!items.some((item) => item.measured?.speech === "confirmed_announcement")) missing.push("confirmed announcements");
    options.append(
      chipGroup(sequencePresets(), modeState.sequence, (value) => {
        modeState.sequence = modeState.sequence === value ? "" : value;
        renderModes();
      }),
      el("p", "mode-note", `Stages in the metadata: ${stages.join(", ") || "none"}. Not in the library: ${missing.join(", ")}.`)
    );
  } else if (creationMode === "custom") {
    const files = el("div", "chips");
    for (const item of items) {
      const order = modeState.files.indexOf(item.filename);
      const chip = button("chip", `${order >= 0 ? `${order + 1}. ` : ""}${item.title}`, () => {
        modeState.files = order >= 0
          ? modeState.files.filter((name) => name !== item.filename)
          : [...modeState.files, item.filename];
        renderModes();
      });
      chip.title = item.filename;
      chip.setAttribute("aria-pressed", String(order >= 0));
      files.append(chip);
    }
    const settings = el("div", "fields");
    settings.append(
      numberField("Duration (s)", "duration", 10, 180, 5),
      numberField("Crossfade (s)", "crossfade", 0, 5, 0.5),
      numberField("Fades (s)", "fade", 0, 5, 0.5)
    );
    const allow = el("label", "field check");
    const box = el("input");
    box.type = "checkbox";
    box.checked = modeState.allowOthers;
    box.addEventListener("change", () => (modeState.allowOthers = box.checked));
    allow.append(box, "Allow other recordings");
    settings.append(allow);
    options.append(files, settings, el("p", "mode-note", "Selected recordings are used in the order you pick them."));
  }
}

for (const tab of document.querySelectorAll(".mode-tab")) {
  tab.addEventListener("click", () => {
    creationMode = creationMode === tab.dataset.mode ? null : tab.dataset.mode;
    renderModes();
  });
}

function modePayload() {
  const number = (value) => (value === "" || value === null ? undefined : Number(value));
  if (creationMode === "city" && modeState.city) return { type: "city", city: modeState.city };
  if (creationMode === "mood" && modeState.mood) return { type: "mood", mood: modeState.mood };
  if (creationMode === "sequence" && modeState.sequence) return { type: "sequence", sequence: modeState.sequence };
  if (creationMode === "custom") {
    return {
      type: "custom",
      files: modeState.files,
      allow_others: modeState.allowOthers,
      duration_seconds: number(modeState.duration),
      crossfade_seconds: number(modeState.crossfade),
      fade_seconds: number(modeState.fade),
    };
  }
  return null;
}

// ---------- Recordings inventory ----------

async function loadRecordings() {
  const summary = $("#map-summary");
  try {
    const response = await fetch("/recordings", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    recordings = data;
    window.SonicGlobe?.setData(data);
    summary.textContent = `${plural(data.cities.length, "city", "cities")} · ${plural(data.recordings.length, "recording")}`;
    const issues = [...data.location_issues, ...data.city_issues, ...data.catalog_issues];
    if (issues.length) summary.title = `${plural(issues.length, "metadata issue")} ignored:\n${issues.join("\n")}`;
    $("#recordings-toggle").disabled = !data.cities.length;
    $("#hero-browse").disabled = !data.cities.length;
    renderedJourneyKey = "";
    renderJourney();
    renderModes();
    renderChatIfNeeded(true);
  } catch (error) {
    summary.textContent = `Recordings unavailable (${error.message})`;
    setTimeout(loadRecordings, 5000);
  }
}

// ---------- Landing and workspace ----------

function enterWorkspace({ instant = false } = {}) {
  if (mode === "workspace") return Promise.resolve();
  mode = "workspace";
  const swap = () => {
    app.dataset.mode = "workspace";
    app.dataset.entered = instant ? "instant" : "animated";
    window.SonicGlobe?.setMode("workspace");
  };
  try {
    sessionStorage.setItem("sonic-journey-started", "1");
  } catch {
    // Storage can be unavailable; the landing screen simply shows again.
  }
  if (!instant && document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return document.startViewTransition(swap).updateCallbackDone;
  }
  swap();
  return Promise.resolve();
}

function returnToGlobe() {
  mode = "landing";
  const swap = () => {
    app.dataset.mode = "landing";
    window.SonicGlobe?.setMode("landing");
  };
  try {
    sessionStorage.removeItem("sonic-journey-started");
  } catch {
    // Nothing to forget.
  }
  if (document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    document.startViewTransition(swap);
  } else {
    swap();
  }
}

async function composeInChat(text) {
  closeOverlay();
  input.value = text;
  autosize();
  await enterWorkspace();
  input.focus();
  input.setSelectionRange(text.length, text.length);
}

$("#start-button").addEventListener("click", async () => {
  await enterWorkspace();
  input.focus();
});
$("#explore-button").addEventListener("click", returnToGlobe);

$("#hero-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("#hero-input").value.trim();
  await enterWorkspace();
  if (text) {
    input.value = text;
    $("#hero-input").value = "";
    sendMessage(text);
  } else {
    input.focus();
  }
});

try {
  if (sessionStorage.getItem("sonic-journey-started")) enterWorkspace({ instant: true });
} catch {
  // Without storage, the page starts on the landing screen.
}

// ---------- Polling and sending ----------

function setConnection(state, text) {
  connection.dataset.state = state;
  connectionText.textContent = text;
}

function renderChatIfNeeded(force = false) {
  const key = JSON.stringify([lastState.chat, busy, selectedUrl]);
  if (!force && key === renderedKey) return;
  renderedKey = key;
  renderChat(parseEntries(lastState.chat || []));
}

let firstState = true;

async function refresh() {
  try {
    const response = await fetch("/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`Unable to load chat state (${response.status})`);
    lastState = await response.json();
    busy = Boolean(lastState.busy);
    // A visit that already has a conversation opens in the workspace, once.
    if (firstState && lastState.chat?.length) enterWorkspace({ instant: true });
    firstState = false;

    renderJourney();
    renderChatIfNeeded();
    renderActivity();

    chatError.hidden = !lastState.error;
    chatError.textContent = lastState.error || "";
    setConnection(busy ? "busy" : "ready", busy ? "Composing…" : "Ready");
  } catch {
    setConnection("offline", "Offline");
  }
  sendButton.disabled = busy || submitting || !input.value.trim();
}

function autosize() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  sendButton.disabled = busy || submitting || !input.value.trim();
}

input.addEventListener("input", autosize);

async function sendMessage(message) {
  if (!message || submitting || busy) return;

  submitting = true;
  sendButton.disabled = true;
  chatError.hidden = true;

  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: message, mode: modePayload() }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
    input.value = "";
    autosize();
  } catch (error) {
    chatError.hidden = false;
    chatError.textContent = error.message || "Failed to send message.";
  } finally {
    submitting = false;
    await refresh();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage(input.value.trim());
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

renderChat([]);
renderActivity();
loadRecordings();
refresh();
setInterval(refresh, 1000);
