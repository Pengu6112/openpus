# Clips

Long video → vertical shorts with burned captions. Runs locally; no accounts, no uploads to anyone.

## Run

```
python clip.py
```

Opens http://127.0.0.1:8787. Drop a video, or pick one of the recordings listed from
`~/Videos/clips_raw` (Capture Panel output) and hit **Clip it** — library files are
read in place, never copied. Transcripts are cached per file in `jobs/.cache`, so re-clipping the
same recording skips Whisper.

From the Capture Panel: option **11** launches this and opens the browser.

### Requirements (fresh machine)

- Python 3.10+
- `ffmpeg` + `ffprobe` on PATH (`winget install Gyan.FFmpeg`)
- Python packages:

```
pip install -r requirements.txt
```

First run downloads the Whisper model to the Hugging Face cache (`small` ≈ 500 MB); needs
internet once. A CUDA GPU is used when it works, otherwise it falls back to CPU — slower but fine.

## What it does

1. `ffmpeg` extracts a 16 kHz mono track.
2. `faster-whisper` transcribes with word timings (model `small` by default, downloads once).
   Cached per file, keyed by path + size + mtime.
3. Transcript is cut into 30–60 s windows aligned to sentence ends, scored for speech rate, questions, numbers and hook words, with a small bonus for loud stretches (fights). Best non-overlapping ones are kept, then each cut runs on to the next quiet gap so the fight that starts after the callout is inside the clip instead of after it. Long sessions are sampled across the whole recording so six clips mean six different fights.
4. Each clip is rendered 1080×1920: dead black edges from the capture canvas are cropped away, the game sits centred (full-bleed for portrait sources) over a blurred copy of itself, uppercase captions with the power word highlighted sit just under the frame, loudness normalised to −14 LUFS, H.264 CRF 19 + AAC 192 kbps.
5. Captions are bilingual by default: the English translation in small grey text above the original. Whisper translates each finished clip (a 30 s slice, not the whole recording), so it costs seconds. English-only recordings stay single-line.

## Deleting output

Every clip in the browser has an × button, and finished/errored jobs have a "Remove" button.
The last 10 clips are kept; anything older is deleted automatically when a new job finishes
(`CLIP_KEEP_CLIPS=0` keeps everything). Deleting a job removes its cached audio and copies too.

## Watching for new recordings

On by default: every 12 s it looks at `clips_raw` and queues a recording once OBS has stopped
writing it (size stable, untouched for 45 s). It does **not** run while Valorant or CS2 is open —
process names come from `Videos\tools\capture\config.json` — so it never eats your FPS mid-match.

First run adopts the files already present instead of queueing 14 old recordings. `CLIP_WATCH=0` disables it.

## Tests

```
python clip.py --selftest   # pure logic, instant
python test_clip.py         # ~5 min, builds its own videos, hits the real HTTP API
```

`test_clip.py` covers library listing, the black-capture guard, 16:9 / portrait / ultrawide
sources, Range requests, path traversal, transcript caching, the upload path, clip/job
deletion, the retention window and 404s.

## Optional LLM picking

Without any key it uses the heuristic scorer. To let a model choose the moments instead:

```
CLIP_LLM_KEY=... CLIP_LLM_BASE=https://api.openai.com/v1 CLIP_LLM_MODEL=gpt-4o-mini python clip.py
```

Any OpenAI-compatible endpoint works. Falls back to the heuristic if the call fails.

## Knobs

`WHISPER_MODEL` (tiny/base/small/medium/large-v3), `MAX_CLIPS` (default 6), `CLIP_PORT` (8787),
`CLIP_LIB` (folders to list, `;` separated; default `~/Videos/clips_raw`),
`CLIP_LANG` (e.g. `pt`, pins the transcript language), `CLIP_DEVICE` (`auto`/`cpu`/`cuda`; `auto`
probes CUDA and falls back to CPU).

Port 8787, not 8765: something else on this PC (`ks-macro-launcher.exe`) already listens on 8765.

## Notes

- On this machine CUDA is unavailable (the PyTorch install is broken and `cublas64_12.dll` is
  missing), so it runs on CPU int8: roughly 5x faster than real time with `small`. Install
  `nvidia-cublas-cu12` + `nvidia-cudnn-cu12` and fix torch to use the GPU.
- Library jobs keep no copy of the source. Uploaded jobs (drag & drop) do keep `source.*` in
  `jobs/<id>/`, so delete old job folders when you are done with them.

## Known limits

- Captions are one style, Arial bold, bottom third. No word-by-word karaoke highlight, no emoji, no auto font choice.
- Crop is a fixed centered frame — no speaker tracking.
- One clip set per video; no timeline editor, no manual trim UI.
- Jobs live in `jobs/<id>/`; deleting the folder is the cleanup story.
