"""Saved journeys: verified copies of generated WAVs, listed in a small manifest.

Generated journeys live in a temporary folder where only the newest few are
kept. Saving copies the exact file into saved/ and records what is known
about it, so it can be replayed later without the agent or the mixer.
"""
import hashlib
import json
import os
import re
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

import audio_mixer

SAVED_DIR = Path(os.environ.get("SONIC_JOURNEY_SAVED_DIR") or Path(__file__).with_name("saved"))
MANIFEST_NAME = "journeys.json"
FILE_PATTERN = re.compile(r"[a-z0-9-]+\.wav")
_lock = threading.Lock()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_path() -> Path:
    return SAVED_DIR / MANIFEST_NAME


def _read() -> list:
    try:
        value = json.loads(_manifest_path().read_text())
    except (OSError, ValueError):
        return []
    entries = value.get("journeys") if isinstance(value, dict) else None
    return entries if isinstance(entries, list) else []


def _write(entries: list) -> None:
    SAVED_DIR.mkdir(parents=True, exist_ok=True)
    partial = _manifest_path().with_suffix(".partial")
    partial.write_text(json.dumps({"journeys": entries}, indent=2, ensure_ascii=False) + "\n")
    os.replace(partial, _manifest_path())


def list_saved() -> list:
    """Saved journeys with their current availability, read from the files themselves."""
    result = []
    for entry in _read():
        if not isinstance(entry, dict) or not FILE_PATTERN.fullmatch(str(entry.get("file", ""))):
            continue
        path = SAVED_DIR / entry["file"]
        item = {key: entry.get(key) for key in ("id", "title", "file", "cities", "sources", "saved_at", "provenance", "origin")}
        if not path.is_file():
            item.update(status="missing", reason="The saved WAV file is missing. It will not be regenerated.")
        else:
            try:
                info = audio_mixer.inspect_wav(path)
            except ValueError as exc:
                item.update(status="unavailable", reason=f"The saved WAV cannot be read ({exc}).")
            else:
                item.update(status="available", duration_seconds=info["duration_seconds"], url=f"/saved/{entry['file']}")
        result.append(item)
    return result


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:40] or "journey"


def save(source: Path, title: str, cities: list, sources: list, provenance: str) -> dict:
    """Copy a generated WAV into saved/ without ever overwriting an existing file."""
    if not isinstance(title, str) or not title.strip() or len(title) > 80:
        raise ValueError("A saved journey needs a title of 1-80 characters.")
    if not source.is_file():
        raise ValueError("That journey file no longer exists, so it cannot be saved.")
    audio_mixer.inspect_wav(source)
    digest = sha256(source)
    with _lock:
        entries = _read()
        for entry in entries:
            if entry.get("sha256") == digest:
                return entry  # Already saved: keep the original entry and file.
        SAVED_DIR.mkdir(parents=True, exist_ok=True)
        base = _slug(title)
        for suffix in [""] + [f"-{n}" for n in range(2, 1000)]:
            target = SAVED_DIR / f"{base}{suffix}.wav"
            try:
                with target.open("xb") as copy, source.open("rb") as original:
                    shutil.copyfileobj(original, copy)
                break
            except FileExistsError:
                continue
        else:
            raise ValueError("Too many saved journeys share this title.")
        if sha256(target) != digest:
            target.unlink(missing_ok=True)
            raise ValueError("The copy did not match the original file; nothing was saved.")
        entry = {
            "id": target.stem,
            "title": title.strip(),
            "file": target.name,
            "cities": cities,
            "sources": sources,
            "sha256": digest,
            "origin": source.name,
            "saved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "provenance": provenance,
        }
        entries.append(entry)
        _write(entries)
    return entry


ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def delete(journey_id: str) -> dict:
    """Remove one saved journey from the manifest and delete its WAV inside saved/.

    The browser only names an id; the file comes from the manifest and must
    resolve to a file directly inside saved/. A missing WAV still lets the
    entry be removed.
    """
    if not isinstance(journey_id, str) or not ID_PATTERN.fullmatch(journey_id):
        raise ValueError("Invalid saved journey id.")
    with _lock:
        entries = _read()
        entry = next((item for item in entries if isinstance(item, dict) and item.get("id") == journey_id), None)
        if entry is None:
            raise LookupError("No saved journey has that id.")
        name = str(entry.get("file", ""))
        root = SAVED_DIR.resolve()
        target = (SAVED_DIR / name).resolve()
        if not FILE_PATTERN.fullmatch(name) or target.parent != root:
            raise ValueError("This saved journey points outside saved/; nothing was deleted.")
        remaining = [item for item in entries if item is not entry]
        _write(remaining)
        # Another entry may reference the same file; keep it in that case.
        if not any(isinstance(item, dict) and item.get("file") == name for item in remaining):
            target.unlink(missing_ok=True)
    return entry


def path_for(name: str) -> Path | None:
    """The saved WAV for a manifest file name, or None if it is not a saved journey."""
    if not FILE_PATTERN.fullmatch(name) or name not in {entry.get("file") for entry in _read()}:
        return None
    path = SAVED_DIR / name
    return path if path.is_file() else None
