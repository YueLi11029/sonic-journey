"""Measurements of real audio: waveform peaks, recording levels and speech-like activity.

Everything here is computed from the samples of a 16-bit PCM WAV with the
standard library. Results are cached by the file's content hash, so editing or
replacing a recording invalidates its cached analysis.

The speech detector is a heuristic, not a trained model. It flags stretches
where the 300 Hz+ band rises above the recording's own background, its level
fluctuates at a syllable-like 2-8 Hz rate, and frames alternate between
pitched (voiced) and unpitched sound. That is evidence of possible speech, not
proof, and it says nothing about whether the speech is an announcement. No
speech recognition model is bundled, so transcripts are reported as
unavailable rather than guessed.
"""
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import wave
from array import array
from operator import mul
from pathlib import Path

import audio_mixer

WAVEFORM_BINS = 240
ANALYSIS_RATE = 8_000
FRAME = 160  # 20 ms at 8 kHz
METHOD = "speech-heuristic-v1"
CACHE_DIR = Path(
    os.environ.get("SONIC_JOURNEY_CACHE_DIR")
    or Path(tempfile.gettempdir()) / "sonic-journey-cache"
)
REVIEWS_PATH = Path(
    os.environ.get("SONIC_JOURNEY_REVIEWS")
    or Path(__file__).with_name("analysis") / "reviews.json"
)
REVIEW_LABELS = ("confirmed_announcement", "not_announcement", "uncertain")
# Phrases typical of transit public-address messages. Used only on real transcripts.
ANNOUNCEMENT_PATTERNS = re.compile(
    r"\b(next (station|stop|train)|this (is|train)|stand clear|doors? (are )?(closing|opening)|"
    r"mind the gap|platform|change (here )?for|terminat|bound|service|line|passengers?|"
    r"please (stand|move|hold)|arriving|departing|transfer)\b",
    re.IGNORECASE,
)

_lock = threading.Lock()
_versions: dict[tuple, str] = {}
_memory: dict[tuple, dict] = {}


# ---------- File identity and caching ----------

def file_version(path: Path) -> str:
    """A short content hash, memoised by path, size and modification time."""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _versions:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1 << 20), b""):
                digest.update(block)
        _versions[key] = digest.hexdigest()[:16]
    return _versions[key]


def _cached(kind: str, path: Path, compute) -> dict:
    version = file_version(path)
    key = (kind, version)
    with _lock:
        if key in _memory:
            return _memory[key]
    cache_file = CACHE_DIR / f"{kind}-{METHOD}-{version}.json"
    try:
        result = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        result = {**compute(path), "version": version}
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            partial = cache_file.with_suffix(".partial")
            partial.write_text(json.dumps(result))
            os.replace(partial, cache_file)
        except OSError:
            pass  # A read-only disk only costs a recomputation next time.
    with _lock:
        _memory[key] = result
    return result


# ---------- Decoding ----------

def mono_8k(samples: array, channels: int, rate: int) -> list[float]:
    """Downmix to mono and resample to 8 kHz floats in [-1, 1]."""
    if channels == 2:
        left = audio_mixer.resample(samples[0::2], rate, ANALYSIS_RATE)
        right = audio_mixer.resample(samples[1::2], rate, ANALYSIS_RATE)
        return [(a + b) / 2 for a, b in zip(left, right)]
    return audio_mixer.resample(samples, rate, ANALYSIS_RATE)


# ---------- Waveform and levels ----------

def _waveform(path: Path) -> dict:
    samples, channels, rate = audio_mixer.read_samples(path)
    frames = len(samples) // channels
    if not frames:
        raise ValueError("The recording contains no audio.")
    bins = min(WAVEFORM_BINS, frames)
    peaks = []
    for index in range(bins):
        start = index * frames // bins * channels
        end = max(start + channels, (index + 1) * frames // bins * channels)
        chunk = samples[start:end]
        peaks.append(max(abs(min(chunk)), abs(max(chunk))) / 32768)
    top = max(peaks) or 1.0
    return {
        "duration_seconds": round(frames / rate, 3),
        "bins": bins,
        "max_amplitude": round(top, 4),
        # Normalised to the file's own peak so quiet recordings stay legible.
        "peaks": [round(peak / top, 3) for peak in peaks],
    }


def waveform(path: Path) -> dict:
    return _cached("waveform", Path(path), _waveform)


def _levels(signal: list[float]) -> dict:
    """Recording levels per 1 s window. These describe the file, not the place."""
    window = ANALYSIS_RATE
    levels = []
    for start in range(0, len(signal) - window + 1, window):
        chunk = signal[start:start + window]
        energy = sum(map(mul, chunk, chunk)) / window
        levels.append(10 * math.log10(energy + 1e-12))
    if not levels:
        chunk = signal or [0.0]
        levels = [10 * math.log10(sum(map(mul, chunk, chunk)) / len(chunk) + 1e-12)]
    ordered = sorted(levels)
    average = 10 * math.log10(sum(10 ** (level / 10) for level in levels) / len(levels))
    return {
        "rms_dbfs": round(average, 1),
        "quiet_dbfs": round(ordered[len(ordered) // 10], 1),
        "loud_dbfs": round(ordered[(len(ordered) * 9) // 10], 1),
        # Spread between the loudest and quietest seconds: steady vs eventful.
        "level_range_db": round(ordered[(len(ordered) * 9) // 10] - ordered[len(ordered) // 10], 1),
    }


# ---------- Speech-like activity ----------

def _voicing(frame: list[float]) -> float:
    """Peak normalised autocorrelation over 80-400 Hz pitch lags (0 = unpitched)."""
    energy = sum(map(mul, frame, frame))
    if energy < 1e-9:
        return 0.0
    best = 0.0
    for lag in range(20, 101, 2):
        head, tail = frame[:-lag], frame[lag:]
        denominator = math.sqrt(sum(map(mul, head, head)) * sum(map(mul, tail, tail)))
        if denominator:
            best = max(best, sum(map(mul, head, tail)) / denominator)
    return best


def _smooth(values: list[float], width: int) -> list[float]:
    """Centred moving average."""
    half = width // 2
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
    count = len(values)
    return [
        (prefix[min(count, i + half + 1)] - prefix[max(0, i - half)])
        / (min(count, i + half + 1) - max(0, i - half))
        for i in range(count)
    ]


def detect_speech(signal: list[float]) -> list[dict]:
    """Return candidate speech intervals (seconds) in an 8 kHz mono signal."""
    # Two first-order high-pass stages (~300 Hz) keep the speech band and drop rumble.
    alpha = 0.809
    band, previous_x, stage_1, stage_2 = [], 0.0, 0.0, 0.0
    previous_1 = 0.0
    for x in signal:
        stage_1 = alpha * (stage_1 + x - previous_x)
        stage_2 = alpha * (stage_2 + stage_1 - previous_1)
        previous_x, previous_1 = x, stage_1
        band.append(stage_2)

    frames = len(band) // FRAME
    if frames < 50:
        return []
    level = []
    for index in range(frames):
        chunk = band[index * FRAME:(index + 1) * FRAME]
        level.append(10 * math.log10(sum(map(mul, chunk, chunk)) / FRAME + 1e-12))

    floor = sorted(level)[frames // 10]
    # Syllable-rate fluctuation: 60 ms smoothing minus 500 ms smoothing of the level.
    fluctuation = [a - b for a, b in zip(_smooth(level, 3), _smooth(level, 25))]
    modulation = [math.sqrt(v) for v in _smooth([f * f for f in fluctuation], 51)]
    snr = [v - floor for v in _smooth(level, 51)]

    # Pitch analysis only where the cheaper evidence already points to speech.
    gate = [m >= 3.0 and s >= 4.0 for m, s in zip(modulation, snr)]
    voiced = [False] * frames
    for index in range(frames):
        if gate[index] and level[index] >= floor + 3:
            voiced[index] = _voicing(band[index * FRAME:(index + 1) * FRAME]) >= 0.55
    voiced_share = _smooth([1.0 if v else 0.0 for v in voiced], 51)

    speech = [
        gate[i] and 0.15 <= voiced_share[i] <= 0.85 and level[i] >= floor + 3
        for i in range(frames)
    ]

    intervals, start, gap = [], None, 0
    for index, flag in enumerate(speech + [False] * 30):
        if flag:
            if start is None:
                start = index
            gap = 0
        elif start is not None:
            gap += 1
            if gap > 20:  # Bridge pauses up to 0.4 s between words.
                end = index - gap + 1
                if end - start >= 30:  # At least 0.6 s.
                    window = range(start, min(end, frames))
                    intervals.append({
                        "start_seconds": round(max(0, start - 5) * FRAME / ANALYSIS_RATE, 2),
                        "end_seconds": round(min(frames, end + 5) * FRAME / ANALYSIS_RATE, 2),
                        "evidence": {
                            "modulation_db": round(sum(modulation[i] for i in window) / len(window), 1),
                            "above_background_db": round(sum(snr[i] for i in window) / len(window), 1),
                            "voiced_share": round(sum(voiced_share[i] for i in window) / len(window), 2),
                        },
                    })
                start, gap = None, 0
    return intervals


def classify(intervals: list[dict], transcripts: list[str | None]) -> str:
    """Combine machine evidence into a cautious overall status."""
    if not intervals:
        return "no_speech_detected"
    if any(text and ANNOUNCEMENT_PATTERNS.search(text) for text in transcripts):
        return "likely_announcement"
    if any(text and len(text.split()) >= 3 for text in transcripts):
        return "speech_type_uncertain"  # Words without transit phrasing: maybe conversation.
    return "speech_type_uncertain"


def transcribe(path: Path, intervals: list[dict]) -> tuple[list[str | None], str]:
    """No speech recognition model is available in this project or its runtime."""
    return [None] * len(intervals), "unavailable: no local speech-recognition model is installed"


def _analysis(path: Path) -> dict:
    samples, channels, rate = audio_mixer.read_samples(path)
    signal = mono_8k(samples, channels, rate)
    intervals = detect_speech(signal)
    transcripts, transcription = transcribe(path, intervals)
    for interval, text in zip(intervals, transcripts):
        interval["transcript"] = text
    return {
        "method": METHOD,
        "status": classify(intervals, transcripts),
        "transcription": transcription,
        "confidence": None,  # The heuristic produces evidence values, not calibrated probabilities.
        "levels": _levels(signal),
        "intervals": intervals,
    }


def analysis(path: Path) -> dict:
    """Machine analysis of one recording, cached by content; never includes reviews."""
    try:
        return _cached("analysis", Path(path), _analysis)
    except (OSError, ValueError, EOFError, wave.Error) as exc:
        return {"method": METHOD, "status": "unavailable", "error": f"Could not decode the audio ({exc}).", "intervals": []}


# ---------- Human review, stored apart from machine results ----------

def _read_reviews() -> dict:
    try:
        value = json.loads(REVIEWS_PATH.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def reviews_for(version: str) -> dict:
    return _read_reviews().get(version, {})


def save_review(path: Path, start_seconds: float, label: str | None) -> dict:
    """Record a person's label for one detected interval of this exact file version."""
    if label is not None and label not in REVIEW_LABELS:
        raise ValueError(f"label must be one of {', '.join(REVIEW_LABELS)} or null.")
    result = analysis(path)
    starts = [interval["start_seconds"] for interval in result["intervals"]]
    if start_seconds not in starts:
        raise ValueError("No detected interval starts at that time in this recording.")
    version = file_version(Path(path))
    with _lock:
        reviews = _read_reviews()
        entry = reviews.setdefault(version, {})
        key = f"{start_seconds:.2f}"
        if label is None:
            entry.pop(key, None)
        else:
            entry[key] = label
        REVIEWS_PATH.parent.mkdir(parents=True, exist_ok=True)
        partial = REVIEWS_PATH.with_suffix(".partial")
        partial.write_text(json.dumps(reviews, indent=2, sort_keys=True))
        os.replace(partial, REVIEWS_PATH)
    return entry


def with_reviews(path: Path, result: dict) -> dict:
    """Machine analysis plus any human labels, with the effective overall status."""
    if result.get("status") == "unavailable":
        return {**result, "review_status": "not_reviewed"}
    labels = reviews_for(file_version(Path(path)))
    intervals = [
        {**interval, "review": labels.get(f"{interval['start_seconds']:.2f}")}
        for interval in result["intervals"]
    ]
    reviewed = [i["review"] for i in intervals if i["review"]]
    status = result["status"]
    if "confirmed_announcement" in reviewed:
        status = "confirmed_announcement"
    elif intervals and len(reviewed) == len(intervals) and set(reviewed) == {"not_announcement"}:
        status = "reviewed_no_announcement"
    return {
        **result,
        "intervals": intervals,
        "machine_status": result["status"],
        "status": status,
        "review_status": "reviewed" if reviewed else "not_reviewed",
    }


def summary(path: Path) -> dict:
    """Compact facts for the agent's inventory, from cache only (never slow)."""
    version = file_version(Path(path))
    cached = _memory.get(("analysis", version))
    if cached is None:
        try:
            cached = json.loads((CACHE_DIR / f"analysis-{METHOD}-{version}.json").read_text())
            _memory[("analysis", version)] = cached
        except (OSError, ValueError):
            return {"speech": "not_analyzed"}
    full = with_reviews(path, cached)
    return {
        "levels": full.get("levels"),
        "speech": full["status"],
        "speech_intervals": [
            {"start_seconds": i["start_seconds"], "end_seconds": i["end_seconds"], "review": i["review"]}
            for i in full["intervals"]
        ],
        "transcription": full.get("transcription"),
    }
