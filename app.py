import json
import os
import re
import shutil
import sys
import traceback
import wave
from collections import Counter
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import urlsplit
from uuid import uuid4

import audio_analysis
import audio_mixer
import saved_journeys

history = [
    {
        "role": "system",
        "content": (
            "You are Sonic Journey, an AI sound-journey composer. You work only "
            "with the real recordings this server can mix.\n"
            "Workflow for every request about recordings or journeys: "
            "1) Call list_audio_files. It returns each WAV's exact filename, city "
            "(location), title, description, tags, stage hint and duration, plus "
            "measured facts: recording levels (rms_dbfs, quiet_dbfs, loud_dbfs and "
            "level_range_db describe the file's recorded level, not real-world "
            "loudness), a speech-analysis status with candidate intervals, and "
            "whether it was used in the last journey. Library facts summarise "
            "cities, tags and stage hints. "
            "2) Pick the recordings that best match the requested city, sound "
            "type, mood and sequence. 3) Explain the plan briefly: order, why "
            "each recording fits, and which requested sounds or stages are "
            "missing. Say the plan rests on metadata and measurements, not on "
            "listening. 4) Call create_journey with audio_files in playback order "
            "and duration_seconds set to the requested length (60 if unspecified). "
            "5) Only after status success, report the duration, sources, "
            "transitions and notes exactly as returned; if status error, say it "
            "failed and why.\n"
            "Selection rules: Follow explicit constraints exactly. A named city "
            "means only that city's recordings unless the user allows others. "
            "Named filenames must be used exactly. If a request cannot be met, "
            "say why and offer a feasible alternative; never substitute "
            "silently. Relevance comes first. When several recordings or "
            "combinations fit equally well, prefer ones not used in the last "
            "journey, so different requests lead to different results, but never "
            "add an unrelated recording just for variety. Never assume sound "
            "types the metadata does not show, such as announcements, footsteps, "
            "boarding, passing trains or crowds; say they are missing. Map moods "
            "only to evidence: calm, quiet or reflective suggests lower levels and "
            "a small level_range_db; energetic, busy or eventful suggests higher "
            "levels or a larger range. Present this as an inference from "
            "recording levels. Never state a mood as a fact about a recording.\n"
            "Speech and announcements: speech status speech_type_uncertain means "
            "a heuristic detector found speech-like sound of unknown type; it is "
            "not an announcement. confirmed_announcement means a person reviewed "
            "and confirmed it. likely_announcement needs transcript evidence. No "
            "transcripts are available, because no speech recognizer is installed. "
            "Never call ordinary noise an announcement. To centre a clip on a "
            "candidate interval, pass its start_seconds in start_offsets.\n"
            "Creation modes: a user message may begin with a [Creation mode ...] "
            "line set by the interface. Follow its constraints.\n"
            "Users can browse a recording gallery and may refer to a recording by "
            "its title, id or filename. Map it to the exact filename from "
            "list_audio_files, and say so if it is not available.\n"
            "You cannot listen to audio, but you do deliver it: whenever "
            "create_journey or get_current_journey returns status success, this "
            "web app shows an audio player for the returned url, and the user can "
            "press play there. Tell the user so; never say that playback or "
            "delivery is unavailable. When the user asks to play, hear, replay or "
            "find the journey, call get_current_journey instead of creating a new "
            "one. Only call create_journey again when the user asks for a "
            "different journey. Never invent filenames, URLs, tags, durations, "
            "audio characteristics or results. Be concise, creative and transparent."
        ),
    }
]

chat, error = [], ""
# Outcome of the latest agent run: idle, running, succeeded or failed.
run_status = "idle"
busy = Lock()

tool_declaration = {
    "type": "function",
    "function": {
        "name": "list_audio_files",
        "description": (
            "List the WAV recordings available for a Sonic Journey, with "
            "location, stage hint, duration, sample rate and channels. "
            "Call this before selecting recordings."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

create_journey_tool = {
    "type": "function",
    "function": {
        "name": "create_journey",
        "description": (
            "Mix the selected recordings, in order, into a WAV journey of the "
            "requested duration, with level matching and crossfades. Returns "
            "the verified duration, the sources actually used and a url; the "
            "web app shows an audio player for that url to the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "audio_files": {
                    "type": "array",
                    "description": (
                        "Exact filenames returned by list_audio_files, in the "
                        "intended playback order."
                    ),
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": audio_mixer.MAX_RECORDINGS,
                },
                "duration_seconds": {
                    "type": "number",
                    "description": "Length of the finished journey in seconds.",
                    "minimum": audio_mixer.MIN_SECONDS,
                    "maximum": audio_mixer.MAX_SECONDS,
                },
                "start_offsets": {
                    "type": "object",
                    "description": (
                        "Optional: exact filename -> start time in seconds within "
                        "that recording, e.g. a speech candidate's start_seconds. "
                        "Recordings not listed start at their most energetic section."
                    ),
                    "additionalProperties": {"type": "number", "minimum": 0},
                },
                "crossfade_seconds": {
                    "type": "number",
                    "description": "Optional crossfade between clips (default 1.5).",
                    "minimum": 0,
                    "maximum": 5,
                },
                "fade_seconds": {
                    "type": "number",
                    "description": "Optional fade-in and fade-out length (default 1).",
                    "minimum": 0,
                    "maximum": 5,
                },
            },
            "required": ["audio_files"],
            "additionalProperties": False,
        },
    },
}


get_current_journey_tool = {
    "type": "function",
    "function": {
        "name": "get_current_journey",
        "description": (
            "Return the journey already created in this visit, without mixing "
            "a new one. The web app shows its audio player to the user again. "
            "Use this when the user asks to play, hear or find the journey."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

# The latest verified journey, kept apart from model history so a failed
# model call cannot lose a journey the user can already play.
latest_journey = None


def list_audio_files():
    """Inventory of mixable WAVs with measured facts the agent can rely on."""
    inventory = audio_mixer.discover_recordings()
    last_used = set(latest_journey["audio_files"]) if latest_journey else set()
    cities, tags, stages = Counter(), set(), set()
    for item in inventory["recordings"]:
        item.pop("frames", None)
        item["measured"] = audio_analysis.summary(audio_mixer.AUDIO_DIR / item["filename"])
        item["used_in_last_journey"] = item["filename"] in last_used
        cities[item["location"]] += 1
        tags.update(item["tags"])
        if item["stage_hint"] != "unlabeled":
            stages.add(item["stage_hint"])
    inventory["library_facts"] = {
        "recordings_per_city": dict(sorted(cities.items())),
        "tags_present": sorted(tags),
        "stage_hints_present": sorted(stages),
        "speech_analysis": (
            f"{audio_analysis.METHOD}: heuristic speech-like activity detection; "
            "no speech recognition, so no transcripts."
        ),
    }
    return inventory


def create_journey(arguments):
    """Mix the selected recordings and return only verified facts."""
    if not isinstance(arguments, dict) or "audio_files" not in arguments:
        raise ValueError("create_journey needs an audio_files list.")
    allowed = {"audio_files", "duration_seconds", "start_offsets", "crossfade_seconds", "fade_seconds"}
    if set(arguments) - allowed:
        raise ValueError("create_journey accepts only " + ", ".join(sorted(allowed)) + ".")

    global latest_journey

    journey = audio_mixer.create_journey(
        arguments["audio_files"],
        arguments.get("duration_seconds", audio_mixer.DEFAULT_SECONDS),
        start_offsets=arguments.get("start_offsets"),
        crossfade_seconds=arguments.get("crossfade_seconds"),
        fade_seconds=arguments.get("fade_seconds"),
    )
    latest_journey = {
        "status": "success",
        "message": (
            f"Created and verified a {journey['duration_seconds']:.1f}-second "
            "WAV journey."
        ),
        "filename": journey["filename"],
        "url": f"/journeys/{journey['filename']}",
        **{
            key: journey[key]
            for key in (
                "duration_seconds",
                "requested_seconds",
                "sample_rate",
                "channels",
                "audio_files",
                "locations",
                "sources",
                "looped",
                "transitions",
                "fade_in_seconds",
                "fade_out_seconds",
                "notes",
            )
        },
        "playback": "The web app shows an audio player for url under this result.",
    }
    return latest_journey


def get_current_journey(arguments):
    """Return the existing journey after checking its file is still served."""
    if latest_journey is None:
        raise ValueError("No journey has been created in this visit yet.")
    if not (audio_mixer.OUTPUT_DIR / latest_journey["filename"]).is_file():
        raise ValueError(
            "The latest journey file is no longer available; create a new journey."
        )
    return {
        **latest_journey,
        "message": (
            f"This is the existing {latest_journey['duration_seconds']:.1f}-second "
            "journey; nothing was regenerated."
        ),
    }


tools = {
    "list_audio_files": lambda arguments: {
        "status": "success",
        **list_audio_files(),
    },
    "create_journey": create_journey,
    "get_current_journey": get_current_journey,
}


CITIES_DIR = Path(__file__).with_name("cities")
CITY_IMAGE_PATTERN = re.compile(r"[a-z0-9-]+\.(?:jpg|jpeg|png|webp)")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def is_text(value):
    return isinstance(value, str) and bool(value.strip())


def load_cities():
    """Read city-level map centres and representative photos (not recording spots)."""
    try:
        value = json.loads((CITIES_DIR / "cities.json").read_text())
        entries = value["cities"]
        assert isinstance(entries, dict)
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        return {}, ["cities/cities.json is missing or invalid."]

    cities, issues = {}, []
    for name, entry in entries.items():
        entry = entry if isinstance(entry, dict) else {}
        lat, lng = entry.get("lat"), entry.get("lng")
        center = None
        if (
            all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lng))
            and -90 <= lat <= 90 and -180 <= lng <= 180
            and is_text(entry.get("coordinates_source"))
        ):
            center = {"lat": lat, "lng": lng, "source": entry["coordinates_source"]}
        else:
            issues.append(f"{name}: city coordinates need lat, lng and coordinates_source.")

        image = entry.get("image")
        if isinstance(image, dict) and all(
            is_text(image.get(field)) for field in ("file", "author", "license", "source_url")
        ) and CITY_IMAGE_PATTERN.fullmatch(image["file"]) and (CITIES_DIR / image["file"]).is_file():
            image = {
                "url": f"/cities/{image['file']}",
                **{key: image.get(key) for key in ("title", "author", "license", "license_url", "source_url", "modified")},
            }
        else:
            if image is not None:
                issues.append(f"{name}: image needs an existing file, author, license and source_url.")
            image = None
        cities[name] = {"center": center, "image": image}
    return cities, issues


def recordings_overview():
    """Inventory for the page, with only explicitly sourced coordinates."""
    inventory = audio_mixer.discover_recordings()
    filenames = [item["filename"] for item in inventory["recordings"]]
    locations, issues = audio_mixer.load_locations(audio_mixer.AUDIO_DIR, filenames)
    city_info, city_issues = load_cities()

    for item in inventory["recordings"]:
        item.pop("frames", None)
        item["audio_url"] = f"/recordings/{item['id']}.wav"
        item["version"] = audio_analysis.file_version(audio_mixer.AUDIO_DIR / item["filename"])
        item["measured"] = audio_analysis.summary(audio_mixer.AUDIO_DIR / item["filename"])
        if item["filename"] in locations:
            item["coordinates"] = locations[item["filename"]]
            item["location_precision"] = "exact"
        elif city_info.get(item["location"], {}).get("center"):
            item["location_precision"] = "city"
        else:
            item["location_precision"] = "unknown"

    counts = Counter(item["location"] for item in inventory["recordings"])
    cities = [
        {
            "name": name,
            "recording_count": count,
            "center": city_info.get(name, {}).get("center"),
            "image": city_info.get(name, {}).get("image"),
        }
        for name, count in sorted(counts.items())
    ]
    return {
        **inventory,
        "cities": cities,
        "located": len(locations),
        "location_issues": issues,
        "city_issues": city_issues,
    }


MODE_TYPES = {"city": "Explore a City", "mood": "Mood & Atmosphere", "sequence": "Sound Journey", "custom": "Custom Mix"}


def short_text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(c in value for c in "[]\n"):
        raise ValueError("Creation mode text must be short plain text.")
    return value.strip()


def mode_preface(mode):
    """Validate the interface's creation-mode choice and describe it for the model."""
    if mode is None:
        return None, None
    if not isinstance(mode, dict) or mode.get("type") not in MODE_TYPES:
        raise ValueError("Unknown creation mode.")
    inventory = audio_mixer.discover_recordings()["recordings"]
    kind, label = mode["type"], MODE_TYPES[mode["type"]]
    if kind == "city":
        city = mode.get("city")
        if city not in {item["location"] for item in inventory}:
            raise ValueError("Choose a city that has recordings.")
        clean = {"type": kind, "city": city}
        text = f"City: {city}. Use only {city} recordings unless the user explicitly allows others."
    elif kind == "mood":
        clean = {"type": kind, "mood": short_text(mode.get("mood"), 60)}
        text = f"Mood: {clean['mood']}. Match it only to metadata and measured levels."
    elif kind == "sequence":
        clean = {"type": kind, "sequence": short_text(mode.get("sequence"), 120)}
        text = (
            f"Requested sequence: {clean['sequence']}. Build each stage only from "
            "recordings whose metadata supports it, and name any missing stage."
        )
    else:
        names = {item["filename"] for item in inventory}
        files = mode.get("files", [])
        if not isinstance(files, list) or len(files) > audio_mixer.MAX_RECORDINGS or any(f not in names for f in files):
            raise ValueError("Custom mix files must be available recordings.")
        clean = {"type": kind, "files": files, "allow_others": mode.get("allow_others") is True}
        parts = []
        if files:
            parts.append(
                "Files, in this order: " + ", ".join(files) + "."
                + (" Other recordings may be added." if clean["allow_others"] else " Use only these files.")
            )
        for key, low, high, unit in (
            ("duration_seconds", audio_mixer.MIN_SECONDS, audio_mixer.MAX_SECONDS, "Duration"),
            ("crossfade_seconds", 0, 5, "Crossfade"),
            ("fade_seconds", 0, 5, "Fade in and out"),
        ):
            value = mode.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f"{unit} must be between {low} and {high} seconds.")
            clean[key] = value
            parts.append(f"{unit}: {value} s.")
        text = " ".join(parts) or "No fixed settings; follow the message."
    return clean, f"[Creation mode: {label}. {text}]"


TOKEN_NAMES = ("WORKSHOP_RUN_TOKEN", "WORKSHOP_DEVELOPMENT_TOKEN")


def redact(text):
    """Remove workshop tokens from text that may be shown or logged."""
    for name in TOKEN_NAMES:
        token = os.environ.get(name)
        if token:
            text = text.replace(token, "[redacted]")
    return text


class GatewayError(ValueError):
    """A non-200 gateway reply, with the details needed to act on it."""

    def __init__(self, path, status, code, message, correlation_id, development):
        operation = "bootstrap" if path == "/v1/bootstrap" else "model request"
        details = ", ".join(
            part
            for part in (f"HTTP {status}", code, correlation_id and f"correlation ID {correlation_id}")
            if part
        )
        text = f"{message} ({operation} failed: {details})."
        if development and status == 401:
            text += (
                " Create a new token in the class app store under Account → "
                "Local development token, paste it into WORKSHOP_DEVELOPMENT_TOKEN "
                "in .env, and restart the server."
            )
        super().__init__(text)
        self.status, self.code = status, code


LOCAL_START = "uv run --locked --env-file .env python app.py"


def gateway_settings():
    """Return (development, url, token) or explain exactly which setting is missing.

    Locally, .env supplies WORKSHOP_DEVELOPMENT_URL and WORKSHOP_DEVELOPMENT_TOKEN
    when the server is started with --env-file. When hosted, the platform
    supplies WORKSHOP_BROKER_URL, WORKSHOP_RUN_TOKEN and WORKSHOP_RUN_ID.
    """
    development = bool(os.environ.get("WORKSHOP_DEVELOPMENT_URL"))
    if development:
        if not os.environ.get("WORKSHOP_DEVELOPMENT_TOKEN"):
            raise ValueError(
                "WORKSHOP_DEVELOPMENT_TOKEN is empty. Paste your local development "
                f"token into .env and restart the server with: {LOCAL_START}"
            )
        return True, os.environ["WORKSHOP_DEVELOPMENT_URL"], os.environ["WORKSHOP_DEVELOPMENT_TOKEN"]

    missing = [
        name
        for name in ("WORKSHOP_BROKER_URL", "WORKSHOP_RUN_TOKEN", "WORKSHOP_RUN_ID")
        if not os.environ.get(name)
    ]
    if missing:
        raise ValueError(
            "No workshop gateway is configured: WORKSHOP_DEVELOPMENT_URL is not set "
            f"and the hosted settings ({', '.join(missing)}) are missing. To run "
            f"locally, start the server with: {LOCAL_START}"
        )
    return False, os.environ["WORKSHOP_BROKER_URL"], os.environ["WORKSHOP_RUN_TOKEN"]


def broker(path, body=None, profile=None):
    development, gateway_url, token = gateway_settings()
    url = urlsplit(gateway_url)

    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError("Use a valid HTTP or HTTPS workshop gateway.")

    connection_type = HTTPSConnection if url.scheme == "https" else HTTPConnection
    connection = connection_type(url.hostname, url.port, timeout=90)

    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "X-Workshop-Contract-Version": "workshop.app.v1",
    }

    if body is not None:
        headers.update(
            {
                "X-Workshop-Operation-ID": str(uuid4()),
                "X-Workshop-Expected-Profile": profile,
                "X-Workshop-Attempt-Limit": "1",
            }
        )

    try:
        connection.request(
            "GET" if body is None else "POST",
            url.path.rstrip("/") + path,
            None if body is None else json.dumps(body),
            headers,
        )
        response = connection.getresponse()
        response_body = response.read()
        try:
            value = json.loads(response_body)
        except ValueError:
            value = None

        if response.status != 200:
            details = value.get("error", {}) if isinstance(value, dict) else {}
            raise GatewayError(
                path,
                response.status,
                details.get("code"),
                details.get("message") or "The workshop gateway request failed",
                details.get("correlationId"),
                development,
            )
        if value is None:
            raise ValueError(f"The workshop gateway returned invalid JSON for {path}.")

        return value
    finally:
        connection.close()


def log_failure(message):
    """Write the failure and its traceback to the server log, without tokens."""
    print(f"[sonic-journey] Agent run failed: {message}", file=sys.stderr)
    print(redact(traceback.format_exc()), file=sys.stderr, end="")


def run(prompt):
    global history, error, run_status

    try:
        boot = broker("/v1/bootstrap")
        development = bool(os.environ.get("WORKSHOP_DEVELOPMENT_URL"))

        if (
            boot["mode"] != ("development" if development else "hosted")
            or boot["simulated"]
            or boot["contractVersion"] != "workshop.app.v1"
            or (
                not development
                and boot["runId"] != os.environ["WORKSHOP_RUN_ID"]
            )
        ):
            raise ValueError(
                "The workshop bootstrap does not match this runtime."
            )

        selection = boot["inference"]["selection"]
        messages = history + [{"role": "user", "content": prompt}]

        while True:
            model_request = {
                "model": selection["modelId"],
                "messages": messages,
                "tools": [tool_declaration, create_journey_tool, get_current_journey_tool],
                "max_tokens": 4096,
            }

            model_response = broker(
                "/v1/llm/chat/completions",
                model_request,
                selection["profileId"],
            )

            choice = model_response["choices"][0]
            assistant_message = {
                "role": "assistant",
                **choice["message"],
            }
            tool_calls = assistant_message.get("tool_calls", [])

            if (
                choice["finish_reason"] not in ("stop", "tool_calls")
                or (choice["finish_reason"] == "tool_calls") != bool(tool_calls)
                or assistant_message.pop("refusal", None)
            ):
                raise ValueError("The model did not complete its response.")

            parsed_calls = []
            for tool_call in tool_calls:
                function_name = tool_call["function"]["name"]

                try:
                    arguments = json.loads(
                        tool_call["function"]["arguments"]
                    )
                except (json.JSONDecodeError, TypeError):
                    raise ValueError("The model returned invalid tool arguments.")

                if function_name not in tools:
                    raise ValueError("Invalid tool call.")
                if function_name != "create_journey" and arguments != {}:
                    raise ValueError(f"{function_name} does not accept arguments.")

                parsed_calls.append((tool_call, function_name, arguments))

            messages.append(assistant_message)

            if assistant_message.get("content"):
                chat.append(["agent", assistant_message["content"]])

            if not tool_calls:
                if not assistant_message.get("content"):
                    raise ValueError("The model returned no answer.")

                history = messages
                run_status = "succeeded"
                return

            for tool_call, function_name, arguments in parsed_calls:
                # Display-only record of the real call; it is not sent to the model.
                chat.append([
                    "call",
                    json.dumps(
                        {"tool": function_name, "arguments": arguments},
                        ensure_ascii=False,
                    ),
                ])

                # Tool failures go back to the model as facts, so it cannot
                # report a journey that the backend did not produce.
                try:
                    result = tools[function_name](arguments)
                except (OSError, ValueError, TypeError) as exception:
                    result = {"status": "error", "error": str(exception)}
                result = {"tool": function_name, **result}

                observation = json.dumps(result, ensure_ascii=False)
                chat.append(["tool", observation])

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": observation,
                    }
                )

    except (
        HTTPException,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
    ) as exception:
        error = redact(str(exception))
        run_status = "failed"
        log_failure(error)

    except Exception as exception:
        # Anything else would otherwise end the run with no visible error.
        error = f"Unexpected server error ({type(exception).__name__}). See the server log."
        run_status = "failed"
        log_failure(redact(f"{type(exception).__name__}: {exception}"))

    finally:
        busy.release()


STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/chat.js": ("chat.js", "text/javascript; charset=utf-8"),
    "/globe.js": ("globe.js", "text/javascript; charset=utf-8"),
    "/waveform.js": ("waveform.js", "text/javascript; charset=utf-8"),
    "/montserrat.woff2": ("montserrat.woff2", "font/woff2"),
    "/vendor/globe.gl.min.js": ("vendor/globe.gl.min.js", "text/javascript; charset=utf-8"),
    "/vendor/ne_110m_land.geojson": ("vendor/ne_110m_land.geojson", "application/geo+json"),
    "/vendor/THIRD-PARTY-NOTICES.txt": ("vendor/THIRD-PARTY-NOTICES.txt", "text/plain; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    def reply(
        self, body, content_type="application/json", status=200, headers=(), cache="no-store"
    ):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, file_path, content_type, cache="no-store"):
        """Stream a file from disk, honoring the single byte ranges media players use."""
        size = file_path.stat().st_size
        requested = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        start, end, status = 0, size - 1, 200

        if requested and requested.groups() != ("", ""):
            first, last = requested.groups()
            if first:
                start, end = int(first), min(int(last or size - 1), size - 1)
            else:
                start, end = max(0, size - int(last)), size - 1
            if start > end or start >= size:
                return self.reply(b"", content_type, 416, [("Content-Range", f"bytes */{size}")])
            status = 206

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", cache)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with file_path.open("rb") as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The player cancelled the request; nothing to report.

    def do_GET(self):
        path = urlsplit(self.path).path

        if path == "/healthz":
            return self.reply(b"ok", "text/plain")

        if path == "/state":
            return self.reply(
                json.dumps(
                    {
                        "chat": chat,
                        "busy": busy.locked(),
                        "error": error,
                        "run": "running" if busy.locked() else run_status,
                    },
                    ensure_ascii=False,
                ).encode()
            )

        if path == "/recordings":
            try:
                overview = recordings_overview()
            except (OSError, ValueError) as exception:
                return self.reply(
                    json.dumps({"error": str(exception)}).encode(), status=503
                )
            return self.reply(json.dumps(overview, ensure_ascii=False).encode())

        journey = re.fullmatch(r"/journeys/([^/]+)", path)
        if journey and audio_mixer.OUTPUT_PATTERN.fullmatch(journey[1]):
            file_path = audio_mixer.OUTPUT_DIR / journey[1]
            if not file_path.is_file():
                return self.send_error(404)
            return self.send_file(file_path, "audio/wav")

        measured = re.fullmatch(r"/recordings/([a-z0-9-]+)/(waveform|analysis)", path)
        if measured:
            file_path = self.recording_path(measured[1])
            if not file_path:
                return self.send_error(404)
            if measured[2] == "waveform":
                try:
                    data = audio_analysis.waveform(file_path)
                except (OSError, ValueError, EOFError, wave.Error) as exception:
                    return self.reply(json.dumps({"error": str(exception)}).encode(), status=422)
            else:
                data = audio_analysis.with_reviews(file_path, audio_analysis.analysis(file_path))
            return self.reply(json.dumps(data).encode())

        if path == "/saved":
            return self.reply(json.dumps({"journeys": saved_journeys.list_saved()}, ensure_ascii=False).encode())

        saved = re.fullmatch(r"/saved/([^/]+?)(/waveform)?", path)
        if saved:
            file_path = saved_journeys.path_for(saved[1])
            if not file_path:
                return self.send_error(404)
            if not saved[2]:
                return self.send_file(file_path, "audio/wav")
            try:
                return self.reply(json.dumps(audio_analysis.waveform(file_path)).encode())
            except (OSError, ValueError, EOFError, wave.Error) as exception:
                return self.reply(json.dumps({"error": str(exception)}).encode(), status=422)

        journey_wave = re.fullmatch(r"/journeys/([^/]+)/waveform", path)
        if journey_wave and audio_mixer.OUTPUT_PATTERN.fullmatch(journey_wave[1]):
            file_path = audio_mixer.OUTPUT_DIR / journey_wave[1]
            if not file_path.is_file():
                return self.send_error(404)
            try:
                return self.reply(json.dumps(audio_analysis.waveform(file_path)).encode())
            except (OSError, ValueError, EOFError, wave.Error) as exception:
                return self.reply(json.dumps({"error": str(exception)}).encode(), status=422)

        recording = re.fullmatch(r"/recordings/([a-z0-9-]+)\.wav", path)
        if recording:
            try:
                inventory = audio_mixer.discover_recordings()["recordings"]
            except (OSError, ValueError):
                return self.send_error(503)
            match = next((item for item in inventory if item["id"] == recording[1]), None)
            if not match:
                return self.send_error(404)
            return self.send_file(audio_mixer.AUDIO_DIR / match["filename"], "audio/wav")

        city_image = re.fullmatch(r"/cities/([^/]+)", path)
        if city_image and CITY_IMAGE_PATTERN.fullmatch(city_image[1]):
            file_path = CITIES_DIR / city_image[1]
            if not file_path.is_file():
                return self.send_error(404)
            return self.send_file(
                file_path, IMAGE_TYPES[file_path.suffix], cache="public, max-age=86400"
            )

        if path not in STATIC_FILES:
            return self.send_error(404)

        name, content_type = STATIC_FILES[path]
        file_path = Path(__file__).parent / name

        if not file_path.is_file():
            return self.send_error(404)

        # Pinned vendor files never change in place, so browsers may keep them.
        cache = "public, max-age=86400" if name.startswith("vendor/") else "no-store"
        self.reply(file_path.read_bytes(), content_type, cache=cache)

    def recording_path(self, recording_id):
        try:
            inventory = audio_mixer.discover_recordings()["recordings"]
        except (OSError, ValueError):
            return None
        match = next((item for item in inventory if item["id"] == recording_id), None)
        return audio_mixer.AUDIO_DIR / match["filename"] if match else None

    def save_review(self, recording_id):
        path = self.recording_path(recording_id)
        if not path:
            return self.send_error(404)
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 2000:
                raise ValueError("Invalid review.")
            payload = json.loads(self.rfile.read(size))
            start = payload["start_seconds"]
            if isinstance(start, bool) or not isinstance(start, (int, float)):
                raise ValueError("start_seconds must be a number.")
            audio_analysis.save_review(path, round(float(start), 2), payload.get("label"))
        except (ValueError, KeyError, TypeError) as exception:
            return self.reply(json.dumps({"error": str(exception)}).encode(), status=400)
        except OSError:
            return self.reply(b'{"error":"Reviews cannot be saved on this server."}', status=503)
        result = audio_analysis.with_reviews(path, audio_analysis.analysis(path))
        self.reply(json.dumps(result).encode())

    def save_journey(self):
        """Save a journey the server itself recorded as created; metadata comes from that record."""
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 2000:
                raise ValueError("Invalid request.")
            payload = json.loads(self.rfile.read(size))
            name = payload["journey"]
            if not isinstance(name, str) or not audio_mixer.OUTPUT_PATTERN.fullmatch(name):
                raise ValueError("Unknown journey.")
            record = next(
                (
                    result
                    for result in (json.loads(text) for role, text, *_ in chat if role == "tool")
                    if result.get("status") == "success" and result.get("filename") == name
                ),
                None,
            )
            if not record:
                raise ValueError("Only journeys created in this session can be saved.")
            entry = saved_journeys.save(
                audio_mixer.OUTPUT_DIR / name,
                payload.get("title") or "",
                record.get("locations", []),
                record.get("audio_files", []),
                "Generated by create_journey and saved from the result panel.",
            )
        except (ValueError, KeyError, TypeError) as exception:
            return self.reply(json.dumps({"error": str(exception)}).encode(), status=400)
        except OSError:
            return self.reply(b'{"error":"Saved journeys cannot be stored on this server."}', status=503)
        self.reply(json.dumps(entry, ensure_ascii=False).encode())

    def do_DELETE(self):
        deleted = re.fullmatch(r"/saved/([^/]+)", urlsplit(self.path).path)
        if not deleted:
            return self.send_error(404)
        try:
            entry = saved_journeys.delete(deleted[1])
        except LookupError as exception:
            return self.reply(json.dumps({"error": str(exception)}).encode(), status=404)
        except ValueError as exception:
            return self.reply(json.dumps({"error": str(exception)}).encode(), status=400)
        except OSError:
            return self.reply(b'{"error":"The saved journey could not be deleted on this server."}', status=503)
        self.reply(json.dumps({"deleted": entry.get("id"), "title": entry.get("title")}, ensure_ascii=False).encode())

    def do_POST(self):
        global error, run_status

        if self.path == "/saved":
            return self.save_journey()
        review = re.fullmatch(r"/recordings/([a-z0-9-]+)/review", self.path)
        if review:
            return self.save_review(review[1])
        if self.path != "/chat":
            return self.send_error(404)

        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 24000:
                raise ValueError("Message too large.")

            payload = json.loads(self.rfile.read(size))
            text = payload["text"]

            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 4000
            ):
                raise ValueError("Enter 1–4,000 characters.")

        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return self.reply(
                b'{"error":"Enter 1-4,000 characters."}',
                status=400,
            )

        try:
            mode, preface = mode_preface(payload.get("mode"))
        except (ValueError, OSError) as exception:
            return self.reply(json.dumps({"error": str(exception)}).encode(), status=400)

        if not busy.acquire(blocking=False):
            return self.reply(
                b'{"error":"Wait for the reply."}',
                status=409,
            )

        error = ""
        run_status = "running"
        chat.append(["you", text, {"mode": mode}] if mode else ["you", text])

        prompt = f"{preface}\n{text}" if preface else text
        Thread(target=run, args=(prompt,), daemon=True).start()

        self.reply(b'{"accepted":true}', status=202)


def warm_analysis_cache():
    """Analyse each recording once in the background; readiness never waits for it."""
    try:
        for item in audio_mixer.discover_recordings()["recordings"]:
            path = audio_mixer.AUDIO_DIR / item["filename"]
            audio_analysis.waveform(path)
            audio_analysis.analysis(path)
    except Exception as exception:  # Analysis is optional; log and keep serving.
        print(f"[sonic-journey] Background analysis stopped: {exception}", file=sys.stderr)


if __name__ == "__main__":
    # Report the gateway mode at startup so a missing --env-file shows up before the first chat.
    try:
        print(f"[sonic-journey] Workshop gateway: {'development' if gateway_settings()[0] else 'hosted'}.", file=sys.stderr)
    except ValueError as exception:
        print(f"[sonic-journey] Warning: {exception}", file=sys.stderr)
    Thread(target=warm_analysis_cache, daemon=True).start()
    ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", "8080"))),
        Handler,
    ).serve_forever()