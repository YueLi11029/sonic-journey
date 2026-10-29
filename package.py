# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import argparse
import hashlib
import io
import json
import re
import wave
from array import array
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import audio_mixer

# Limits used by the workshop's reference packager (demo-app/scripts/package.py).
EXPANDED_LIMIT = 128 * 1024 * 1024
COMPRESSED_LIMIT = 20 * 1024 * 1024
ENTRY_LIMIT = 5000
FORBIDDEN = re.compile(
    r"(^|/)(\.env(\..*)?|\.venv|\.git|__pycache__|__MACOSX|\.DS_Store|\._.*)(/|$)"
)

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--slug", required=True, help="Exact address claimed in My app.")
parser.add_argument("--output", type=Path, required=True)
parser.add_argument(
    "--max-seconds",
    type=float,
    default=45,
    help="Longest hosted excerpt per recording (default 45).",
)
args = parser.parse_args()
manifest_bytes = (root / "workshop-app.json").read_bytes()
manifest = json.loads(manifest_bytes)
# ZIP structure can be valid while its slug differs from the claimed address.
if manifest["slug"] != args.slug:
    parser.error(
        f'workshop-app.json uses "{manifest["slug"]}"; My app uses "{args.slug}". '
        f'Set the manifest slug to "{args.slug}", then package again.'
    )
if not 5 <= args.max_seconds <= audio_mixer.MAX_SECONDS:
    parser.error(f"--max-seconds must be from 5 to {audio_mixer.MAX_SECONDS}.")
names = (
    "app.py",
    "audio_mixer.py",
    "audio_analysis.py",
    "index.html",
    "styles.css",
    "chat.js",
    "globe.js",
    "waveform.js",
    "vendor/globe.gl.min.js",
    "vendor/ne_110m_land.geojson",
    "vendor/THIRD-PARTY-NOTICES.txt",
    "montserrat.woff2",
    "OFL.txt",
    "pyproject.toml",
    "uv.lock",
    "workshop-app.json",
    "README.md",
    "package.py",
    "test_app.py",
)
missing = [name for name in names if not (root / name).is_file()]
if missing:
    parser.error("Required source files are missing: " + ", ".join(missing))
# Local development settings stay in the checkout. Hosted execution receives
# its gateway and port from the platform, so no .env files belong in the ZIP.
data = {
    name: manifest_bytes if name == "workshop-app.json" else (root / name).read_bytes()
    for name in names
}


def excerpt(path: Path) -> tuple[bytes, dict]:
    """Return a mono 24 kHz WAV of the recording's most energetic section."""
    samples, channels, rate = audio_mixer.read_samples(path)
    total = len(samples) // channels
    window = min(total, round(args.max_seconds * rate))
    start = audio_mixer.loudest_window(samples, channels, rate, window)
    segment = samples[start * channels:(start + window) * channels]
    mono = (
        audio_mixer.resample(segment, rate)
        if channels == 1
        else [
            (left + right) / 2
            for left, right in zip(
                audio_mixer.resample(segment[0::2], rate),
                audio_mixer.resample(segment[1::2], rate),
            )
        ]
    )
    pcm = array("h", (max(-32768, min(32767, round(value * 32768))) for value in mono))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(audio_mixer.SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue(), {
        "original_seconds": round(total / rate, 2),
        "original_sample_rate": rate,
        "original_channels": channels,
        "start_seconds": round(start / rate, 2),
        "seconds": round(len(pcm) / audio_mixer.SAMPLE_RATE, 2),
    }


# The full WAVs exceed the upload limits, so the hosted app receives shortened
# mono copies under the same audio/ names. The originals stay untouched.
inventory = audio_mixer.discover_recordings(root / "audio")
if inventory["archived_without_wav"]:
    parser.error(
        "These recordings have no WAV version; convert them first (see README): "
        + ", ".join(inventory["archived_without_wav"])
    )
if inventory["unusable"]:
    parser.error(
        "These WAV files cannot be mixed: "
        + "; ".join(f'{item["filename"]} ({item["reason"]})' for item in inventory["unusable"])
    )
if not inventory["recordings"]:
    parser.error("audio/ contains no WAV recordings to package.")
excerpts = {}
for recording in inventory["recordings"]:
    name = recording["filename"]
    data[f"audio/{name}"], excerpts[name] = excerpt(root / "audio" / name)
data[f"audio/{audio_mixer.EXCERPTS_NAME}"] = (json.dumps(excerpts, indent=2) + "\n").encode()
# Verified coordinates are optional; when present, every entry must be valid.
locations_path = root / "audio" / audio_mixer.LOCATIONS_NAME
if locations_path.is_file():
    _, issues = audio_mixer.load_locations(root / "audio", list(excerpts))
    if issues:
        parser.error(f"Fix audio/{audio_mixer.LOCATIONS_NAME}: " + " ".join(issues))
    data[f"audio/{audio_mixer.LOCATIONS_NAME}"] = locations_path.read_bytes()
# Recording metadata is optional too, and must name only packaged recordings.
if inventory["catalog_issues"]:
    parser.error(f"Fix audio/{audio_mixer.CATALOG_NAME}: " + " ".join(inventory["catalog_issues"]))
catalog_path = root / "audio" / audio_mixer.CATALOG_NAME
if catalog_path.is_file():
    data[f"audio/{audio_mixer.CATALOG_NAME}"] = catalog_path.read_bytes()
# City photos and city-level coordinates: package the metadata and every image it names.
cities_path = root / "cities" / "cities.json"
if cities_path.is_file():
    try:
        cities = json.loads(cities_path.read_text())["cities"]
    except (ValueError, KeyError) as exc:
        parser.error(f"cities/cities.json is invalid ({exc}).")
    data["cities/cities.json"] = cities_path.read_bytes()
    for name, city in cities.items():
        image = city.get("image") or {}
        if not image:
            continue
        missing_fields = [f for f in ("file", "author", "license", "source_url") if not image.get(f)]
        if missing_fields:
            parser.error(f"cities/cities.json: {name} image lacks {', '.join(missing_fields)}.")
        image_path = root / "cities" / image["file"]
        if not image_path.is_file():
            parser.error(f"cities/cities.json: {name} image {image['file']} is missing.")
        data[f"cities/{image['file']}"] = image_path.read_bytes()

forbidden = [name for name in data if FORBIDDEN.search(name)]
if forbidden:
    parser.error("Refusing to package private files: " + ", ".join(forbidden))
secrets = []
if (root / ".env").is_file():
    for line in (root / ".env").read_text().splitlines():
        key, _, value = line.partition("=")
        value = value.strip().strip("\"'")
        if re.search(r"TOKEN|KEY|SECRET|PASSWORD", key) and len(value) >= 8:
            secrets.append((key.strip(), value.encode()))
for key, value in secrets:
    leaked = [name for name, contents in data.items() if value in contents]
    if leaked:
        parser.error(f"The value of {key} from .env appears in: {', '.join(leaked)}")
expanded = sum(map(len, data.values()))
if len(data) > ENTRY_LIMIT:
    parser.error(f"The ZIP would have {len(data)} entries; the limit is {ENTRY_LIMIT}.")
if expanded > EXPANDED_LIMIT:
    parser.error(f"Expanded size {expanded} bytes exceeds {EXPANDED_LIMIT} bytes.")

buffer = io.BytesIO()
with ZipFile(buffer, "w") as archive:
    for name, contents in sorted(data.items()):
        entry = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
        # Read shared-font links, then write regular bytes into the portable ZIP.
        entry.create_system = 3
        entry.external_attr = 0o100644 << 16
        entry.compress_type = ZIP_DEFLATED
        archive.writestr(entry, contents, compresslevel=9)
payload = buffer.getvalue()
if len(payload) > COMPRESSED_LIMIT:
    parser.error(
        f"The ZIP is {len(payload)} bytes; the limit is {COMPRESSED_LIMIT}. "
        "Lower --max-seconds."
    )
output = args.output.expanduser().absolute()
if output.suffix != ".zip":
    parser.error("--output must name a .zip file.")
output.parent.mkdir(parents=True, exist_ok=True)
if output.is_symlink():
    parser.error("The ZIP destination must be a regular file.")
if output.exists() and output.read_bytes() != payload:
    trash = output.parent / ("trash-" + datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S"))
    trash.mkdir()
    output.rename(trash / output.name)
output.write_bytes(payload)
with ZipFile(output) as archive:
    assert set(archive.namelist()) == set(data)
    for name, contents in data.items():
        assert archive.read(name) == contents
print(
    json.dumps(
        {
            "zip": str(output),
            "slug": args.slug,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "compressed_bytes": len(payload),
            "expanded_bytes": expanded,
            "recordings": excerpts,
        },
        indent=2,
    )
)
