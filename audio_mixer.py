"""Discover transit recordings and mix them into a verified WAV journey.

16-bit PCM WAV is the canonical input format. The standard-library ``wave``
module reads it in every environment, so neither FFmpeg nor pydub is needed at
runtime. Other formats in ``audio/`` (such as the original .m4a files) are
treated as archived originals and are never mixed.
"""
import json
import math
import os
import re
import sys
import tempfile
import wave
from array import array
from pathlib import Path
from uuid import uuid4

AUDIO_DIR = Path(__file__).with_name("audio")
# The app folder may be read-only when hosted, so journeys go to a temp folder.
OUTPUT_DIR = Path(
    os.environ.get("SONIC_JOURNEY_OUTPUT_DIR")
    or Path(tempfile.gettempdir()) / "sonic-journey"
)
EXCERPTS_NAME = "excerpts.json"
LOCATIONS_NAME = "locations.json"
CATALOG_NAME = "catalog.json"
CATALOG_TEXT_FIELDS = ("title", "city", "category", "description", "source", "license")
ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CANONICAL_SUFFIX = ".wav"
ARCHIVE_SUFFIXES = {".m4a", ".mp3", ".aac", ".flac", ".aif", ".aiff", ".ogg", ".opus"}

SAMPLE_RATE = 24_000
CHANNELS = 2
SAMPLE_WIDTH = 2
MIN_SECONDS = 10
MAX_SECONDS = 180
DEFAULT_SECONDS = 60
MAX_RECORDINGS = 8
CROSSFADE_SECONDS = 1.5
EDGE_FADE_SECONDS = 1.0
TARGET_RMS = 0.1  # About -20 dBFS.
MAX_GAIN = 10.0  # +20 dB, so near-silent clips are not boosted into noise.
PEAK_CEILING = 0.95
KEEP_OUTPUTS = 5
OUTPUT_PATTERN = re.compile(r"journey-[0-9a-f]{32}\.wav")

CITY_ALIASES = {"ny": "New York", "nyc": "New York"}
STAGE_HINTS = (
    ("approach", "train approaching"),
    ("arriv", "train arriving"),
    ("leav", "train departing"),
    ("depart", "train departing"),
    ("board", "boarding"),
    ("platform", "platform"),
)


def recording_key(stem: str) -> str:
    """Identify one recording across formats, spacing, separators and case."""
    return re.sub(r"[\s_-]+", " ", stem).strip().casefold()


def describe(stem: str) -> tuple[str, str, str]:
    """Infer location, detail and journey stage from a filename stem."""
    city, _, rest = (re.split(r"([_-])", stem, maxsplit=1) + ["", ""])[:3]
    city = city.strip()
    location = CITY_ALIASES.get(city.casefold(), city.title() if city.islower() else city)
    detail = re.sub(r"[\s_-]+", " ", rest).strip()
    lowered = stem.casefold()
    stage = next((hint for word, hint in STAGE_HINTS if word in lowered), "unlabeled")
    return location, detail, stage


def inspect_wav(path: Path) -> dict:
    """Return verified WAV properties, or raise ValueError if unusable."""
    try:
        with wave.open(str(path), "rb") as wav:
            compression = wav.getcomptype()
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError(f"not a readable PCM WAV ({exc})") from exc

    if compression != "NONE":
        raise ValueError("compressed WAV; expected PCM")
    if width != SAMPLE_WIDTH:
        raise ValueError(f"{width * 8}-bit samples; expected 16-bit PCM")
    if channels not in (1, 2):
        raise ValueError(f"{channels} channels; expected mono or stereo")
    if rate < 8_000:
        raise ValueError(f"sample rate {rate} Hz is too low")
    if frames == 0:
        raise ValueError("contains no audio")

    return {
        "duration_seconds": round(frames / rate, 2),
        "sample_rate": rate,
        "channels": channels,
        "frames": frames,
    }


def _read_excerpts(audio_dir: Path) -> dict:
    """Read the packager's notes about shortened hosted copies, if present."""
    try:
        value = json.loads((audio_dir / EXCERPTS_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load_locations(audio_dir: Path, filenames: list[str]) -> tuple[dict, list[str]]:
    """Read verified recording coordinates from audio/locations.json.

    Coordinates are never inferred. Each entry must name an available
    recording and say where its coordinates come from; anything else is
    reported as an issue and left off the map.
    """
    path = Path(audio_dir) / LOCATIONS_NAME
    if not path.is_file():
        return {}, []
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return {}, [f"{LOCATIONS_NAME} is not valid JSON ({exc})."]
    if not isinstance(entries, dict):
        return {}, [f"{LOCATIONS_NAME} must map recording filenames to locations."]

    locations, issues = {}, []
    for name, entry in entries.items():
        if name not in filenames:
            issues.append(f"{name}: not an available WAV recording.")
            continue
        if not isinstance(entry, dict):
            issues.append(f"{name}: expected an object.")
            continue
        lat, lng = entry.get("lat"), entry.get("lng")
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (lat, lng)
        ) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            issues.append(f"{name}: lat and lng must be valid numbers.")
            continue
        source = entry.get("source")
        if not isinstance(source, str) or not source.strip():
            issues.append(f"{name}: add a source explaining how the location was verified.")
            continue
        place = entry.get("place")
        locations[name] = {
            "lat": lat,
            "lng": lng,
            "place": place.strip() if isinstance(place, str) and place.strip() else None,
            "source": source.strip(),
        }
    return locations, issues


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-") or "recording"


def load_catalog(audio_dir: Path, filenames: list[str]) -> tuple[dict, list[str]]:
    """Read optional per-recording metadata from audio/catalog.json.

    Entries are keyed by WAV filename. Unknown files and malformed fields are
    reported as issues instead of being shown.
    """
    path = Path(audio_dir) / CATALOG_NAME
    if not path.is_file():
        return {}, []
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return {}, [f"{CATALOG_NAME} is not valid JSON ({exc})."]
    entries = value.get("recordings") if isinstance(value, dict) else None
    if not isinstance(entries, dict):
        return {}, [f"{CATALOG_NAME} needs a \"recordings\" object keyed by filename."]

    catalog, issues, ids = {}, [], set()
    for name, entry in entries.items():
        if name not in filenames:
            issues.append(f"{name}: not an available WAV recording.")
            continue
        if not isinstance(entry, dict):
            issues.append(f"{name}: expected an object.")
            continue
        clean = {}
        for field in CATALOG_TEXT_FIELDS:
            text = entry.get(field)
            if text is not None and not (isinstance(text, str) and text.strip()):
                issues.append(f"{name}: {field} must be text or null.")
                break
            clean[field] = text.strip() if text else None
        else:
            tags = entry.get("tags", [])
            recording_id = entry.get("id")
            if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
                issues.append(f"{name}: tags must be a list of text.")
            elif recording_id is not None and not (
                isinstance(recording_id, str) and ID_PATTERN.fullmatch(recording_id)
            ):
                issues.append(f"{name}: id must use lowercase letters, digits and hyphens.")
            elif recording_id in ids:
                issues.append(f"{name}: id {recording_id} is already used.")
            else:
                ids.add(recording_id)
                catalog[name] = {**clean, "id": recording_id, "tags": [t.strip() for t in tags]}
    return catalog, issues


def discover_recordings(audio_dir: Path | None = None) -> dict:
    """Build an inventory of the WAV recordings that can be mixed right now."""
    audio_dir = Path(audio_dir or AUDIO_DIR)
    if not audio_dir.is_dir():
        raise ValueError(f"The audio folder is missing: {audio_dir.name}/")

    groups: dict[str, list[Path]] = {}
    for path in sorted(audio_dir.iterdir()):
        suffix = path.suffix.lower()
        if (
            path.is_file()
            and not path.name.startswith(".")
            and (suffix == CANONICAL_SUFFIX or suffix in ARCHIVE_SUFFIXES)
        ):
            groups.setdefault(recording_key(path.stem), []).append(path)

    excerpts = _read_excerpts(audio_dir)
    recordings, without_wav, unusable = [], [], []
    archived = 0

    for _, paths in sorted(groups.items()):
        wavs = [path for path in paths if path.suffix.lower() == CANONICAL_SUFFIX]
        archived += len(paths) - len(wavs)

        if not wavs:
            without_wav.extend(path.name for path in paths)
            continue

        for duplicate in wavs[1:]:
            unusable.append({
                "filename": duplicate.name,
                "reason": f"duplicate of {wavs[0].name}",
            })

        path = wavs[0]
        try:
            info = inspect_wav(path)
        except ValueError as exc:
            unusable.append({"filename": path.name, "reason": str(exc)})
            continue

        location, detail, stage = describe(path.stem)
        recording = {
            "filename": path.name,
            "location": location,
            "detail": detail,
            "stage_hint": stage,
            **info,
        }
        if isinstance(excerpts.get(path.name), dict):
            recording["packaged_excerpt"] = excerpts[path.name]
        recordings.append(recording)

    catalog, catalog_issues = load_catalog(
        audio_dir, [recording["filename"] for recording in recordings]
    )
    used_ids = {entry["id"] for entry in catalog.values() if entry["id"]}
    for recording in recordings:
        entry = catalog.get(recording["filename"], {})
        recording_id = entry.get("id")
        if not recording_id:
            base = recording_id = slugify(Path(recording["filename"]).stem)
            suffix = 2
            while recording_id in used_ids:
                recording_id, suffix = f"{base}-{suffix}", suffix + 1
            used_ids.add(recording_id)
        recording.update({
            "id": recording_id,
            "title": entry.get("title") or re.sub(r"[\s_-]+", " ", Path(recording["filename"]).stem).strip(),
            # The catalog's city wins over the filename prefix, so the agent,
            # the mixer and the gallery all name the same place.
            "location": entry.get("city") or recording["location"],
            "category": entry.get("category"),
            "tags": entry.get("tags") or ([] if recording["stage_hint"] == "unlabeled" else [recording["stage_hint"]]),
            "description": entry.get("description"),
            "source": entry.get("source"),
            "license": entry.get("license"),
            "metadata": "catalog" if entry else "filename",
        })

    return {
        "audio_format": "16-bit PCM WAV",
        "recordings": recordings,
        "archived_originals_ignored": archived,
        "archived_without_wav": without_wav,
        "unusable": unusable,
        "catalog_issues": catalog_issues,
    }


def read_samples(path: Path) -> tuple[array, int, int]:
    """Return interleaved 16-bit samples, channel count and sample rate."""
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())

    samples = array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % (SAMPLE_WIDTH * channels)])
    # WAV PCM data is little-endian.
    if sys.byteorder != "little":
        samples.byteswap()
    return samples, channels, rate


def loudest_window(samples: array, channels: int, rate: int, window: int) -> int:
    """Return the first frame of the most energetic window, in 0.25 s steps."""
    total = len(samples) // channels
    if window >= total:
        return 0

    block = max(1, rate // 4)
    # Every 8th sample of the first channel is enough to rank loudness.
    stride = 8 * channels
    energies = [
        sum(value * value for value in samples[start * channels:(start + block) * channels:stride])
        for start in range(0, total, block)
    ]
    span = max(1, window // block)
    last_start = (total - window) // block

    running = sum(energies[:span])
    best, best_block = running, 0
    for index in range(1, last_start + 1):
        running += energies[index + span - 1] - energies[index - 1]
        if running > best:
            best, best_block = running, index
    return min(best_block * block, total - window)


def resample(values, source_rate: int, target_rate: int = SAMPLE_RATE) -> list[float]:
    """Convert 16-bit integers at source_rate to floats at target_rate."""
    scale = 1 / 32768
    if source_rate == target_rate:
        return [value * scale for value in values]

    if source_rate % target_rate == 0:
        # Averaging each group also filters content the lower rate cannot hold.
        factor = source_rate // target_rate
        scale /= factor
        return [
            sum(group) * scale
            for group in zip(*(values[offset::factor] for offset in range(factor)))
        ]

    ratio = source_rate / target_rate
    last = len(values) - 1
    result = []
    for index in range(max(1, round(len(values) / ratio))):
        position = index * ratio
        base = int(position)
        if base >= last:
            result.append(values[last] * scale)
        else:
            fraction = position - base
            result.append((values[base] + (values[base + 1] - values[base]) * fraction) * scale)
    return result


def to_stereo(samples: array, channels: int, rate: int) -> list[list[float]]:
    if channels == 1:
        mono = resample(samples, rate)
        return [mono, mono[:]]
    return [resample(samples[0::2], rate), resample(samples[1::2], rate)]


def level(track: list[list[float]]) -> tuple[float, float]:
    """Return RMS and peak of a stereo track."""
    count = sum(len(channel) for channel in track)
    if not count:
        return 0.0, 0.0
    rms = math.sqrt(sum(value * value for channel in track for value in channel) / count)
    peak = max(max(map(abs, channel), default=0.0) for channel in track)
    return rms, peak


def scale_track(track: list[list[float]], gain: float) -> list[list[float]]:
    return [[value * gain for value in channel] for channel in track]


def join(first: list[list[float]], second: list[list[float]], fade: int) -> list[list[float]]:
    """Join two clips with an equal-power crossfade of `fade` frames."""
    fade = min(fade, len(first[0]), len(second[0]))
    if fade <= 0:
        return [a + b for a, b in zip(first, second)]

    fade_out = [math.cos(i / fade * math.pi / 2) for i in range(fade)]
    fade_in = [math.sin(i / fade * math.pi / 2) for i in range(fade)]
    return [
        a[:-fade]
        + [x * o + y * i for x, y, o, i in zip(a[-fade:], b[:fade], fade_out, fade_in)]
        + b[fade:]
        for a, b in zip(first, second)
    ]


def allocate(available: list[int], needed: int) -> list[int]:
    """Share `needed` frames across clips, giving short clips all they have."""
    allocation = [0] * len(available)
    remaining = needed
    order = sorted(range(len(available)), key=available.__getitem__)
    for position, index in enumerate(order):
        left = len(order) - position
        share = remaining if left == 1 else remaining // left
        allocation[index] = min(available[index], share)
        remaining -= allocation[index]
    return allocation


def write_wav(path: Path, track: list[list[float]]) -> None:
    pcm = array(
        "h",
        (
            max(-32768, min(32767, round(value * 32767)))
            for frame in zip(*track)
            for value in frame
        ),
    )
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())


def verify_output(path: Path, expected_frames: int) -> float:
    """Reopen the exported journey and return its measured duration."""
    info = inspect_wav(path)
    if (info["channels"], info["sample_rate"], info["frames"]) != (
        CHANNELS,
        SAMPLE_RATE,
        expected_frames,
    ):
        raise ValueError("The exported journey does not have the expected format.")

    samples, _, _ = read_samples(path)
    if len(samples) != expected_frames * CHANNELS:
        raise ValueError("The exported journey is truncated.")
    if not any(samples):
        raise ValueError("The exported journey is silent.")
    return round(info["frames"] / info["sample_rate"], 3)


def prune_outputs(output_dir: Path, keep: Path) -> None:
    journeys = sorted(
        (path for path in output_dir.glob("journey-*.wav") if path != keep),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old in journeys[KEEP_OUTPUTS - 1:]:
        old.unlink(missing_ok=True)


def create_journey(
    audio_files: list[str],
    duration_seconds: float = DEFAULT_SECONDS,
    audio_dir: Path | None = None,
    output_dir: Path | None = None,
    start_offsets: dict | None = None,
    crossfade_seconds: float | None = None,
    fade_seconds: float | None = None,
) -> dict:
    """Mix the selected WAV recordings, in order, into a verified journey.

    start_offsets optionally fixes where each named recording starts (seconds);
    otherwise its most energetic section is used. crossfade_seconds and
    fade_seconds override the default transition and edge-fade lengths.
    """
    audio_dir = Path(audio_dir or AUDIO_DIR)
    output_dir = Path(output_dir or OUTPUT_DIR)

    if not isinstance(audio_files, list) or not 1 <= len(audio_files) <= MAX_RECORDINGS:
        raise ValueError(f"Select between 1 and {MAX_RECORDINGS} recordings.")
    if any(not isinstance(name, str) or not name for name in audio_files):
        raise ValueError("Recording filenames must be non-empty strings.")
    if len(set(audio_files)) != len(audio_files):
        raise ValueError("Do not select the same recording more than once.")
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not MIN_SECONDS <= duration_seconds <= MAX_SECONDS
    ):
        raise ValueError(
            f"duration_seconds must be a number from {MIN_SECONDS} to {MAX_SECONDS}."
        )

    inventory = discover_recordings(audio_dir)
    by_name = {item["filename"]: item for item in inventory["recordings"]}
    by_key = {recording_key(Path(name).stem): name for name in by_name}
    if not by_name:
        raise ValueError(
            "No usable WAV recordings are available. Convert the recordings in "
            "audio/ to 16-bit PCM WAV, then package the app again."
        )

    selected = []
    for name in audio_files:
        if name not in by_name:
            twin = by_key.get(recording_key(Path(name).stem))
            hint = f" Use '{twin}' instead." if twin else ""
            raise ValueError(f"'{name}' is not an available WAV recording.{hint}")
        selected.append(by_name[name])

    target = round(duration_seconds * SAMPLE_RATE)
    start_offsets = start_offsets or {}
    if not isinstance(start_offsets, dict) or any(name not in audio_files for name in start_offsets):
        raise ValueError("start_offsets may only name selected recordings.")
    starts = {}
    for item in selected:
        if item["filename"] not in start_offsets:
            continue
        offset = start_offsets[item["filename"]]
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not 0 <= offset < item["duration_seconds"] - 0.5:
            raise ValueError(
                f"The start offset for '{item['filename']}' must be between 0 and "
                f"{item['duration_seconds'] - 0.5:.1f} seconds."
            )
        starts[item["filename"]] = round(offset * item["sample_rate"])
    for name, value in (("crossfade_seconds", crossfade_seconds), ("fade_seconds", fade_seconds)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 5):
            raise ValueError(f"{name} must be between 0 and 5 seconds.")

    # A fixed start leaves only the audio after it available for that clip.
    available = [
        (item["frames"] - starts.get(item["filename"], 0)) * SAMPLE_RATE // item["sample_rate"]
        for item in selected
    ]
    crossfade = CROSSFADE_SECONDS if crossfade_seconds is None else crossfade_seconds
    fade = min(round(crossfade * SAMPLE_RATE), min(available) // 4)
    needed = target + fade * (len(selected) - 1)
    looped = sum(available) < needed
    allocation = available if looped else allocate(available, needed)

    clips, sources, position = [], [], 0
    for item, frames in zip(selected, allocation):
        samples, channels, rate = read_samples(audio_dir / item["filename"])
        source_frames = min(len(samples) // channels, math.ceil(frames * rate / SAMPLE_RATE) + 2)
        if item["filename"] in starts:
            start = starts[item["filename"]]
            source_frames = min(source_frames, len(samples) // channels - start)
        else:
            start = loudest_window(samples, channels, rate, source_frames)
        segment = samples[start * channels:(start + source_frames) * channels]
        clip = [channel[:frames] for channel in to_stereo(segment, channels, rate)]

        rms, peak = level(clip)
        if rms == 0:
            raise ValueError(f"'{item['filename']}' is silent in the selected section.")
        gain = min(TARGET_RMS / rms, PEAK_CEILING / peak, MAX_GAIN)
        clips.append(scale_track(clip, gain))

        source = {
            "filename": item["filename"],
            "location": item["location"],
            "stage_hint": item["stage_hint"],
            "starts_at_seconds": round(position / SAMPLE_RATE, 2),
            "source_offset_seconds": round(start / rate, 2),
            "offset_chosen_by": "request" if item["filename"] in starts else "loudest section",
            "seconds_used": round(len(clip[0]) / SAMPLE_RATE, 2),
            "gain_db": round(20 * math.log10(gain), 1),
        }
        if "packaged_excerpt" in item:
            source["packaged_excerpt"] = item["packaged_excerpt"]
        sources.append(source)
        position += len(clip[0]) - fade

    sequence = clips[0]
    for clip in clips[1:]:
        sequence = join(sequence, clip, fade)
    track, repeats = sequence, 1
    while len(track[0]) < target:
        track = join(track, sequence, fade)
        repeats += 1

    # Trim to the exact length; pad only the few frames lost to rounding.
    track = [channel[:target] + [0.0] * (target - len(channel[:target])) for channel in track]
    edge_seconds = EDGE_FADE_SECONDS if fade_seconds is None else fade_seconds
    edge = min(round(edge_seconds * SAMPLE_RATE), target // 4)
    for channel in track:
        for i in range(edge):
            ramp = i / edge
            channel[i] *= ramp
            channel[-1 - i] *= ramp

    _, peak = level(track)
    if peak == 0:
        raise ValueError("The mixed journey is silent.")
    if peak > PEAK_CEILING:
        track = scale_track(track, PEAK_CEILING / peak)

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"journey-{uuid4().hex}.wav"
    partial = output.with_suffix(".partial")
    try:
        write_wav(partial, track)
        measured = verify_output(partial, target)
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    prune_outputs(output_dir, output)

    notes = []
    if looped:
        total = sum(available) / SAMPLE_RATE
        notes.append(
            f"The selected recordings provide about {total:.1f} s, so the "
            f"sequence plays {repeats} times to fill {measured:.1f} s."
        )
    locations = sorted({item["location"] for item in selected})
    if len(locations) > 1:
        notes.append("The recordings come from different places: " + ", ".join(locations) + ".")
    packaged = [item for item in selected if isinstance(item.get("packaged_excerpt"), dict)]
    if packaged:
        notes.append("The hosted copies of these recordings are mono 24 kHz WAVs.")
    shortened = [
        f"{item['filename']} ({excerpt['seconds']} s of {excerpt['original_seconds']} s)"
        for item in packaged
        for excerpt in [item["packaged_excerpt"]]
        if excerpt.get("seconds", 0) < excerpt.get("original_seconds", 0) - 0.1
    ]
    if shortened:
        notes.append("Shortened hosted excerpts: " + ", ".join(shortened) + ".")

    transitions = [
        {
            "at_seconds": source["starts_at_seconds"],
            "from": previous["filename"],
            "to": source["filename"],
            "type": "equal-power crossfade",
            "seconds": round(fade / SAMPLE_RATE, 2),
        }
        for previous, source in zip(sources, sources[1:])
    ]

    return {
        "path": output,
        "filename": output.name,
        "duration_seconds": measured,
        "requested_seconds": duration_seconds,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "audio_files": [item["filename"] for item in selected],
        "locations": locations,
        "sources": sources,
        "looped": looped,
        "transitions": transitions,
        "fade_in_seconds": round(edge / SAMPLE_RATE, 2),
        "fade_out_seconds": round(edge / SAMPLE_RATE, 2),
        "notes": notes,
    }
