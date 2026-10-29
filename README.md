## Sonic Journey

An agent that composes subway sound journeys from real transit recordings.
Python 3.12 standard library only; no FFmpeg is needed at runtime.

- `app.py` — system message, the `list_audio_files` and `create_journey`
  tools, the agent loop and HTTP routes.
- `audio_mixer.py` — finds the usable recordings, mixes them and verifies the
  exported WAV.
- `chat.js` / `index.html` — chat, tool results and the journey player.
- `package.py` — builds the upload ZIP. `test_app.py` — checks without paid inference.

The model reads the recording inventory and chooses an order and a duration.
The backend checks each file, mixes the audio and reports only what it
actually did. The result lists the verified duration and the sources used,
including each source's location and the section that was taken from it.
The browser shows "ready" and a player only for a successful `create_journey`
result.

### Recordings

`audio/` holds 16-bit PCM WAV files, the canonical input format. A recording
is identified by its filename stem, ignoring case, spaces, `_` and `-`. When a
`.wav` and an archived original such as `.m4a` share a stem, only the WAV is
used. A non-WAV file without a WAV version is listed as not converted and is
never mixed. Location and stage hints (`approaching`, `leaving`, …) come from
the filename, for example `NY-path- leaving.wav` → New York, train departing.

To add or convert a recording locally (FFmpeg is required only on your machine):

```sh
ffmpeg -i "audio/NAME.m4a" -c:a pcm_s16le -ar 48000 "audio/NAME.wav"
# or, on macOS without FFmpeg:
afconvert -f WAVE -d LEI16@48000 "audio/NAME.m4a" "audio/NAME.wav"
```

### Recording, city and location metadata

Three optional files describe the library. Each is validated, and invalid
entries are reported instead of shown:

- `audio/catalog.json` holds per-recording `id`, `title`, `city`, `category`,
  `tags`, `description`, `source` and `license`, keyed by WAV filename. The
  current entries describe only what the filenames say. `source` and `license`
  are `null` until documented. Durations always come from the WAV files.
- `audio/locations.json` holds **exact** recording spots (`lat`, `lng`,
  `place`, `source`). It does not exist yet, so no exact markers are shown.
- `cities/cities.json` holds **city-level** marker coordinates (from the
  cities' English Wikipedia articles) and one representative photo per city.
  The photos are bundled in `cities/` and come from Wikimedia Commons; the
  author, license and source page for each are listed in the file and shown
  in the app. They illustrate the city, not where the audio was recorded.

To add a recording, put its 16-bit WAV in `audio/` and, optionally, an entry
in `catalog.json`. The gallery, the globe counts and the agent's inventory
pick it up from the files at runtime. Use your own recordings, or recordings
whose license you have documented.

### Waveforms and speech analysis

`audio_analysis.py` measures each WAV with the standard library. Results are
cached by the file's content hash (`SONIC_JOURNEY_CACHE_DIR`, a temporary
folder by default). The server analyses every recording once in the
background at startup.

- **Waveforms** are peak amplitudes per slice of the real samples, served at
  `/recordings/<id>/waveform` and `/journeys/<file>/waveform`.
- **Recording levels** (`rms_dbfs`, quiet/loud dBFS, `level_range_db`)
  describe the file's recorded level, not real-world loudness. The agent uses
  them for mood requests and must call the match an inference.
- **Speech detection** (`speech-heuristic-v1`) is a heuristic, not a trained
  model. It flags intervals where the 300 Hz+ band rises above the
  recording's background, fluctuates at a syllable-like 2–8 Hz rate, and
  alternates between pitched and unpitched frames. In tests it found
  synthesised speech placed 6 dB above real station noise at the right time,
  found it partially at 0 dB, mostly missed it at −6 dB, and ignored silence,
  white noise and a steady tone. Its detections in the real recordings are
  unverified candidates: they may be announcements, conversation or noise.
- **No transcripts.** No speech-recognition model is installed or bundled, so
  `transcription` is reported as unavailable. Without a transcript the status
  stays `speech_type_uncertain`; `likely_announcement` requires transcript
  evidence.
- **Human review.** Each detected interval can be marked as a confirmed
  announcement, not an announcement, or uncertain. Reviews are stored apart
  from machine results in `analysis/reviews.json` (`SONIC_JOURNEY_REVIEWS`),
  keyed by file content, and are not packaged. Hosted copies are different
  files, so local reviews do not apply to them.

### Creation modes

The chat offers four optional modes: Explore a city, Mood, Sound journey and
Custom mix. The server validates the chosen mode against the real inventory
and adds a short `[Creation mode …]` line to the message the model receives;
the chat shows the mode as a chip. Custom mix can fix exact files and their
order, the duration, the crossfade and the fade lengths.
`create_journey` also accepts `start_offsets`, so a clip can start at a
chosen time, such as a speech candidate.

### How a journey is mixed

Each selected recording gets a share of the requested duration. The share
comes from the recording's most energetic section, and short recordings are
used in full. Each segment is resampled to 24 kHz stereo and level-matched to
about −20 dBFS RMS, with a peak ceiling. Segments are joined with 1.5 s
crossfades. If the selection is shorter than the request, the sequence repeats
and the result says so. The mix is trimmed to the exact length, faded at both
ends, written to a temporary folder and then re-read to verify its format,
length and that it is not silent. Journeys are served from `/journeys/…` and
kept until five newer ones exist. Set `SONIC_JOURNEY_OUTPUT_DIR` to change
the folder.

### Run locally

```sh
cp -n .env.example .env   # then paste your development token into .env
uv run --locked --env-file .env python app.py
```

Open `http://localhost:8080`. Locally the app mixes the full-length WAVs in
`audio/`. Run the checks without paid inference:

```sh
uv run --locked python -m unittest -v
```

### Package for the portal

The full WAVs (about 170 MB) exceed the upload limits of 20 MiB compressed
and 128 MiB expanded. The packager therefore writes hosted copies under the
same `audio/` names: mono, 24 kHz, and at most 45 s, cut from the most
energetic section. `audio/excerpts.json` records where each copy came from,
and journey results mention it. Your source files are not changed. Archived
`.m4a` files, `.env`, `.venv` and caches are never packaged. The packager
fails if a recording lacks a WAV version, if a WAV cannot be read, if a
`.env` secret appears in a packaged file, or if the ZIP exceeds a limit.

```sh
uv run --locked python package.py --slug sonic-journey \
  --output ~/Downloads/sonic-journey.zip
```

`--max-seconds` changes the excerpt cap. The slug must match the address
claimed in **My app**.

Montserrat uses `OFL.txt`.
