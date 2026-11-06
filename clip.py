"""Local video -> vertical clips with burned captions.

    python clip.py            # opens http://127.0.0.1:8787
    python clip.py --selftest # checks the pure logic, no media needed

Env: WHISPER_MODEL (default small), MAX_CLIPS (6), CLIP_PORT (8787),
     CLIP_LIB (library folders, ';' separated), CLIP_LLM_KEY / CLIP_LLM_BASE /
     CLIP_LLM_MODEL (optional, OpenAI-compatible), CLIP_LANG, CLIP_DEVICE,
     CLIP_BILINGUAL (1: English line above the original), CLIP_KEEP_CLIPS (10),
     CLIP_WATCH (1: queue new recordings automatically)
"""
import hashlib, json, os, re, shutil, subprocess, sys, threading, time, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
JOBS = ROOT / "jobs"
JOBS.mkdir(exist_ok=True)
CACHE = JOBS / ".cache"
MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
MAX_CLIPS = int(os.getenv("MAX_CLIPS", "6"))
KEEP_CLIPS = int(os.getenv("CLIP_KEEP_CLIPS", "10"))   # 0 = never delete old output
BILINGUAL = os.getenv("CLIP_BILINGUAL", "1") != "0"    # English line above the original
MIN_CLIP, MAX_CLIP, TARGET_CLIP = 15.0, 60.0, 30.0
LIB_DIRS = [Path(p) for p in (os.getenv("CLIP_LIB") or
                              str(Path.home() / "Videos" / "clips_raw")).split(";") if p.strip()]
HOOKS = {"how", "why", "what", "never", "always", "secret", "mistake", "best",
         "worst", "free", "money", "now", "nobody", "everyone", "truth", "stop"}


# ---------------------------------------------------------------- utilities
def run(args, cwd=None):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode:
        raise RuntimeError(f"{args[0]} failed:\n{p.stderr[-1500:]}")
    return p


def probe_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)]).stdout.strip()
    return float(out)


def save_state(job):
    tmp = job["dir"] / "job.json.tmp"
    tmp.write_text(json.dumps(job["state"], ensure_ascii=False), encoding="utf-8")
    tmp.replace(job["dir"] / "job.json")


def set_state(job, **kw):
    job["state"].update(kw)
    save_state(job)


# ---------------------------------------------------------------- library
_dur_cache = {}
_dur_lock = threading.Lock()


def duration_of(path):
    """ffprobe duration, cached per file version (10 GB files are probed once)."""
    st = path.stat()
    key = (str(path), st.st_size, int(st.st_mtime))
    with _dur_lock:
        if key in _dur_cache:
            return _dur_cache[key]
    dur = probe_duration(path)
    with _dur_lock:
        _dur_cache[key] = dur
    return dur


def library():
    """Videos sitting in the capture panel folders, clipped in place (never copied)."""
    items = []
    for root in LIB_DIRS:
        if not root.is_dir():
            continue
        for f in root.glob("*.mp4"):
            try:
                items.append({
                    "id": hashlib.sha1(str(f).encode()).hexdigest()[:12],
                    "path": str(f), "name": f.name,
                    "gb": round(f.stat().st_size / 1e9, 1),
                    "duration": round(duration_of(f)),
                })
            except Exception:
                pass
    return sorted(items, key=lambda i: i["name"], reverse=True)


def library_file(fid):
    for item in library():
        if item["id"] == fid:
            return Path(item["path"])
    return None


# ---------------------------------------------------------------- watcher
CAPTURE_CONFIG = Path(os.getenv("CLIP_CAPTURE_CONFIG") or
                      Path.home() / "Videos" / "tools" / "capture" / "config.json")
WATCH_STATE = JOBS / ".cache" / "watched.json"


def game_running():
    """True while one of the Capture Panel games is open, so clips never steal CPU mid-match."""
    try:
        cfg = json.loads(CAPTURE_CONFIG.read_text(encoding="utf-8"))
        procs = [g["process"] for g in cfg.get("games", []) if g.get("process")]
    except Exception:
        procs = ["VALORANT-Win64-Shipping", "cs2"]
    for p in procs:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {p}.exe", "/NH"],
                             capture_output=True, text=True, errors="replace").stdout
        if p.lower() in out.lower():
            return True
    return False


def job_active():
    for jd in JOBS.iterdir():
        f = jd / "job.json"
        if f.is_file():
            try:
                if json.loads(f.read_text(encoding="utf-8")).get("status") not in ("done", "error"):
                    return True
            except Exception:
                pass
    return False


def load_watched():
    try:
        return json.loads(WATCH_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_watched(state):
    WATCH_STATE.parent.mkdir(exist_ok=True)
    WATCH_STATE.write_text(json.dumps(state), encoding="utf-8")


def watcher():
    """Queue a recording once OBS has finished writing it (size stable, older than 45 s)."""
    adopt = not WATCH_STATE.exists()   # first run: adopt what is already there, do not queue it
    state = load_watched()
    for item in library():
        p = item["path"]
        s = Path(p).stat()
        sig = [s.st_size, int(s.st_mtime)]
        rec = state.get(p) or {}
        if adopt:
            state[p] = {"sig": sig, "queued": True}
        elif rec.get("sig") == sig:
            if not rec.get("queued") and time.time() - s.st_mtime > 45:
                d = JOBS / uuid.uuid4().hex[:12]
                d.mkdir()
                print(f"watch: queueing {item['name']}")
                new_job(d, Path(p), item["name"], "Auto (new recording)")
                state[p] = {"sig": sig, "queued": True}
        else:
            state[p] = {"sig": sig, "queued": bool(rec.get("queued"))}
    save_watched(state)


def watch_loop():
    while True:
        time.sleep(12)
        try:
            if not game_running() and not job_active():
                watcher()
        except Exception as e:
            print("watch:", e)


# ---------------------------------------------------------------- transcripts
def cache_key(path):
    st = path.stat()
    return hashlib.sha1(f"{path}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]


def cached_transcript(path):
    """(segments, language). Older cache files are a bare list of segments."""
    f = CACHE / (cache_key(path) + ".json")
    if not f.is_file():
        return None, None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if isinstance(data, list):
        return data, None
    return data.get("segments"), data.get("lang")


def store_transcript(path, segments, lang=None):
    CACHE.mkdir(exist_ok=True)
    (CACHE / (cache_key(path) + ".json")).write_text(
        json.dumps({"lang": lang, "segments": segments}, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- transcript
_model = None
_model_lock = threading.Lock()


def load_wav(path):
    """Decode the 16 kHz mono wav ourselves: faster-whisper's PyAV path breaks on av>=14."""
    import wave
    import numpy as np
    with wave.open(str(path)) as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def clip_audio(path, start, end):
    """Only the frames for one clip, so translating costs seconds instead of minutes."""
    import wave
    import numpy as np
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        w.setpos(max(0, int(start * rate)))
        frames = w.readframes(max(0, int((end - start) * rate)))
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def energy_profile(wav_path, total):
    """Loudness per second, 1.0 = a typical talking second.

    Cuts use this to find a quiet gap. Ending on the last word often lands right
    before the fight, which is the one thing the clip has to contain.
    """
    import wave
    import numpy as np
    with wave.open(str(wav_path)) as w:
        rate = w.getframerate()
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    out = np.zeros(max(1, int(total) + 1), np.float32)
    for i in range(len(out)):
        chunk = raw[i * rate:(i + 1) * rate].astype(np.float32)
        if chunk.size:
            out[i] = np.sqrt(np.mean(chunk * chunk))
    return out / max(1.0, float(np.percentile(out, 90)))


def get_model():
    """Load the Whisper model once per process, CUDA when it actually works."""
    global _model
    with _model_lock:
        if _model is None:
            import numpy as np
            from faster_whisper import WhisperModel
            want = os.getenv("CLIP_DEVICE", "auto")
            tries = [("cuda", "float16"), ("cpu", "int8")] if want == "auto" else \
                    [(want, "float16" if want == "cuda" else "int8")]
            for dev, ct in tries:
                try:
                    m = WhisperModel(MODEL_NAME, device=dev, compute_type=ct)
                    # probe: CUDA can load and still die on missing cublas/cudnn at first encode
                    list(m.transcribe(np.zeros(8000, np.float32), beam_size=1, vad_filter=False)[0])
                    _model = m
                    break
                except Exception as e:
                    print(f"whisper {dev} unavailable ({str(e)[:80]}), trying next")
            if _model is None:
                raise RuntimeError("Could not load the Whisper model on any device.")
        return _model


def transcribe(wav, duration=0.0, on_progress=None):
    """Whisper segments with word timings, plus the language it heard."""
    segs, info = get_model().transcribe(load_wav(wav), word_timestamps=True, vad_filter=True,
                                        language=os.getenv("CLIP_LANG") or None,
                                        condition_on_previous_text=False)
    out = []
    for s in segs:
        words = [{"w": w.word.strip(), "start": w.start, "end": w.end}
                 for w in (s.words or []) if w.word.strip()]
        text = " ".join(w["w"] for w in words).strip() or s.text.strip()
        if text:
            out.append({"start": s.start, "end": s.end, "text": text, "words": words})
        if on_progress and duration and len(out) % 15 == 0:
            on_progress(min(0.99, s.end / duration))
    return out, (info.language or "").lower()


def translate_words(wav_path, start, end, lang=None):
    """English words for one clip window, timed relative to the clip start.

    A few seconds of lead-in are translated too and then dropped: whisper hallucinates
    ("Hey! Hey! Hey!") when the slice starts on music or silence.
    """
    pad = min(3.0, start)
    audio = clip_audio(wav_path, start - pad, end)
    if audio.size < 1600:          # under 0.1 s, nothing to translate
        return []
    segs, _ = get_model().transcribe(audio, task="translate", word_timestamps=True,
                                     vad_filter=True, language=lang,
                                     condition_on_previous_text=False)
    return [{"w": w.word.strip(), "start": float(w.start) - pad, "end": float(w.end) - pad}
            for s in segs for w in (s.words or [])
            if w.word.strip() and float(w.end) > pad]


# ---------------------------------------------------------------- selection
def candidates(segments):
    """Windows of whole segments around the target length (30-60 s).

    The old loop accepted the first window over MIN_CLIP, so most clips came out
    15-22 s and ended on the callout just as the fight started.
    """
    out, n = [], len(segments)
    for i in range(n):
        start, j = segments[i]["start"], i
        while j < n and segments[j]["end"] - start <= MAX_CLIP:
            j += 1
            if segments[j - 1]["end"] - start >= TARGET_CLIP:
                break
        if j > i and segments[j - 1]["end"] - start >= MIN_CLIP:
            out.append({"start": start, "end": segments[j - 1]["end"],
                        "text": " ".join(s["text"] for s in segments[i:j])})
    return out


def title_of(text):
    m = re.match(r"(.{12,}?[.?!])(?=\s|$)", text)
    t = (m.group(1) if m else text).strip()
    return t if len(t) <= 80 else t[:77].rsplit(" ", 1)[0] + "â€¦"


def score(c):
    dur = max(0.1, c["end"] - c["start"])
    wps = len(c["text"].split()) / dur
    s = 2.0 * wps
    s += 1.2 * len(re.findall(r"[?!]", c["text"]))
    s += 1.0 * len(re.findall(r"\b\d+\b", c["text"]))
    s += 0.8 * sum(w.lower().strip(".,!?") in HOOKS for w in c["text"].split())
    return s


def pick(cands, limit=MAX_CLIPS, duration=0.0, energy=None):
    """Best non-overlapping moments. Long recordings get spread over the whole session so
    six clips are six different fights, not six from one firefight. Loud stretches
    (gunfights) get a small bonus on top of the speech score."""
    def heat(c):
        if energy is None or not len(energy):
            return 0.0
        a, b = max(0, int(c["start"])), min(len(energy), int(c["end"]) + 1)
        return float(sum(energy[a:b])) / (b - a) if b > a else 0.0

    ranked = sorted(cands, key=lambda c: score(c) + 3.0 * heat(c), reverse=True)
    block = max(240.0, duration / limit) if duration > 900 else 0.0

    def take(chosen, cap):
        for c in ranked:
            if len(chosen) >= limit:
                return
            if any(not (c["end"] <= o["start"] or c["start"] >= o["end"]) for o in chosen):
                continue
            if cap and block:
                b = int(c["start"] // block)
                if sum(int(o["start"] // block) == b for o in chosen) >= cap:
                    continue
            chosen.append(c)

    chosen = []
    if block:
        take(chosen, 1)   # one per slice first, then allow a second where slices ran dry
        take(chosen, 2)
    else:
        take(chosen, 0)
    return sorted(chosen, key=lambda c: c["start"])


def extend_clips(chosen, energy, total):
    """Make every clip at least 30 s and end it after the action, not on the callout.

    Walk forward from the last word to the first quiet stretch (two quiet seconds) and
    cut there, so the fight that follows the callout is inside the clip. Never grow
    into the next chosen clip.
    """
    def quiet(t):
        i = int(t)
        return i >= len(energy) or energy[i] <= 0.35

    prev_end = 0.0
    for i, c in enumerate(chosen):
        s, e = c["start"], c["end"]
        nxt = chosen[i + 1]["start"] if i + 1 < len(chosen) else total
        # a few seconds into the next pick is allowed: the cut moves to the quiet gap
        # and the next clip starts there instead, so no fight is split by a hard cut
        cap = min(nxt + 8.0, s + MAX_CLIP, total)
        s = max(prev_end, s - 0.3)           # tiny lead-in, no clipped first syllable
        t = e
        while t < cap and not (quiet(t) and quiet(t + 1)):
            t += 0.5
        e = min(cap, max(e, t + 0.5))
        if e - s < TARGET_CLIP:              # keep the promised 30 s
            s = max(prev_end, e - TARGET_CLIP)
            if e - s < TARGET_CLIP:
                e = min(cap, s + TARGET_CLIP)
        c["start"], c["end"] = round(s, 2), round(e, 2)
        prev_end = c["end"]

    # a stub at the end of the recording is not worth a clip: fold it into the one before
    if len(chosen) > 1 and chosen[-1]["end"] - chosen[-1]["start"] < TARGET_CLIP:
        if chosen[-1]["end"] - chosen[-2]["start"] <= MAX_CLIP:
            chosen[-2]["end"] = chosen[-1]["end"]
            chosen.pop()
    return chosen


def pick_llm(cands, limit=MAX_CLIPS):
    """Optional: ask an OpenAI-compatible model which windows are best."""
    key = os.getenv("CLIP_LLM_KEY")
    if not key or not cands:
        return None
    try:
        import urllib.request
        base = os.getenv("CLIP_LLM_BASE", "https://api.openai.com/v1").rstrip("/")
        listing = "\n".join(f'{i}. [{c["start"]:.0f}-{c["end"]:.0f}s] {c["text"][:600]}'
                            for i, c in enumerate(cands))
        body = json.dumps({
            "model": os.getenv("CLIP_LLM_MODEL", "gpt-4o-mini"),
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": "You pick short-form video moments. Reply with JSON only."},
                {"role": "user", "content":
                    f"Pick up to {limit} of these transcript windows that work as standalone "
                    "vertical clips: complete thought, strong hook, no long dead air.\n\n"
                    f"{listing}\n\n"
                    'Reply: {"picks":[{"index":0,"title":"<6 words>","why":"<12 words>"}]}'}],
        }).encode()
        req = urllib.request.Request(base + "/chat/completions", body, {
            "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        raw = urllib.request.urlopen(req, timeout=120).read().decode()
        txt = json.loads(raw)["choices"][0]["message"]["content"]
        picks = json.loads(re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M))
        out, used = [], set()
        for p in picks.get("picks", [])[:limit]:
            i = int(p["index"])
            if 0 <= i < len(cands) and i not in used:
                used.add(i)
                c = dict(cands[i])
                c["title"] = str(p.get("title", ""))[:80]
                c["why"] = str(p.get("why", ""))[:120]
                out.append(c)
        return sorted(out, key=lambda c: c["start"]) or None
    except Exception as e:
        print("LLM pick failed, using heuristic:", e)
        return None


# ---------------------------------------------------------------- subtitles
def ts(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,80,&H00FFFFFF,&H0000FFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,1,8,3,2,60,60,330,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

HILITE = r"{\c&H0033D6FF&}"  # BGR: warm yellow
WHITE = r"{\c&H00FFFFFF&}"
EN_LINE = r"{\fs52\c&H00CCCCCC&}"  # translation rides above, smaller and grey


def join_en(words):
    """Glue whisper tokens back together: "I" + "'m" -> "I'm", not "I 'm"."""
    out = ""
    for w in words:
        if not out or w[:1] in ",.!?;:'’" or out.endswith(" "):
            out += w
        else:
            out += " " + w
    return out.strip()


def ass_text(segments, t0, t1, en_words=None):
    """One cue per <=3 words / <=2.6s, uppercase, power word highlighted.

    en_words is the English translation of the whole clip. Its words are spilt across
    the cues in order, so both languages always say the same thing at the same time.
    """
    words = [w for s in segments for w in s["words"] if w["end"] > t0 and w["start"] < t1]
    cues, buf = [], []

    def flush():
        if buf:
            cues.append(list(buf))
            buf.clear()

    for w in words:
        buf.append(w)
        if len(buf) >= 3 or (buf[-1]["end"] - buf[0]["start"]) >= 2.6:
            flush()
    flush()

    en = [w for w in (en_words or []) if str(w.get("w", "")).strip()]
    tops = [""] * len(cues)
    if en:
        at = 0
        for i, c in enumerate(cues):
            b = c[-1]["end"] - t0
            got = []
            while at < len(en) and en[at].get("start", 0) <= b + 0.2:
                got.append(en[at]["w"])
                at += 1
            if not got and at < len(en) and en[at].get("start", 0) <= b + 0.9:
                got.append(en[at]["w"])      # keep the line from going blank
                at += 1
            tops[i] = join_en(got)
        if at < len(en):                     # whitelist words past the last cue
            rest = join_en([w["w"] for w in en[at:]])
            tops[-1] = f"{tops[-1]} {rest}".strip()

    lines = []
    for c, top in zip(cues, tops):
        a, b = c[0]["start"] - t0, c[-1]["end"] - t0
        plain = [w["w"].strip().upper().replace("{", "(").replace("}", ")") for w in c]
        # emphasise the longest word, or a hook word when there is one
        pick_at = max(range(len(plain)), key=lambda i: len(plain[i]))
        for i, w in enumerate(plain):
            if w.lower().strip(".,!?") in HOOKS:
                pick_at = i
                break
        body = " ".join((HILITE + w + WHITE) if i == pick_at else w for i, w in enumerate(plain))
        text = f"{EN_LINE}{top}{{\\r}}\\N{body}" if top else body
        lines.append(f"Dialogue: 0,{ts(a)},{ts(b + 0.03)},Default,,0,0,0,,{{\\fad(60,40)}}{text}")
    return ASS_HEAD + "\n".join(lines) + "\n"


# ---------------------------------------------------------------- rendering
# 16:9 sources sit centred with blurred fill above and below, captions just under
# the frame. Portrait sources go full-bleed and look identical either way.
def vframe(crop):
    pre = f"crop={crop}," if crop else ""
    return (
        f"[0:v]{pre}split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "gblur=sigma=32,eq=brightness=-0.12:saturation=1.02[bgb];"
        "[fg]scale=1060:1920:force_original_aspect_ratio=decrease[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
        "subtitles=sub.ass[v]"
    )


def detect_crop(src, duration=None):
    """Crop the dead black slab some OBS canvases leave beside the game.

    Returns 'w:h:x:y' or None. Samples three points and keeps the box they agree on.
    """
    dur = duration or probe_duration(src)
    boxes = []
    for frac in (0.15, 0.5, 0.85):
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-ss", f"{dur * frac:.1f}", "-i", str(src),
             "-frames:v", "25", "-vf", "cropdetect=limit=24:round=2:reset=0",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", p.stderr)
        if found:
            boxes.append(tuple(int(v) for v in found[-1]))
    if not boxes:
        return None
    w, h, x, y = min(boxes, key=lambda b: b[0] * b[1])   # never crop tighter than agreed
    full = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "stream=width,height", "-of", "csv=p=0",
                           str(src)], capture_output=True, text=True).stdout.strip()
    try:
        sw, sh = (int(v) for v in full.split(",")[:2])
    except ValueError:
        return None
    if w * h >= 0.98 * sw * sh or w < 320 or h < 240:
        return None
    return f"{w}:{h}:{x}:{y}"


AUDIO = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]


def source_of(job):
    """Absolute path of the video: library file in place, or an uploaded copy."""
    p = job["state"].get("source")
    if p and Path(p).is_file():
        return Path(p)
    for f in (JOBS / job["state"]["id"]).glob("source.*"):
        return f
    raise RuntimeError("Source video is gone.")


def render_clip(job, idx, clip, src):
    d = job["dir"]
    en = None
    if BILINGUAL and job.get("lang") != "en":
        try:
            en = translate_words(d / "audio.wav", clip["start"], clip["end"], job.get("lang"))
        except Exception as e:
            print("translate failed, captions stay one language:", e)
    (d / "sub.ass").write_text(ass_text(job["segments"], clip["start"], clip["end"], en),
                               encoding="utf-8")
    name = f"clip{idx:02d}.mp4"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{clip['start']:.3f}", "-t", f"{clip['end'] - clip['start']:.3f}",
         "-i", str(src), "-vf", vframe(job.get("crop")), *AUDIO,
         "-c:v", "libx264", "-preset", "fast", "-crf", "19", "-profile:v", "high",
         "-level", "4.2", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", name], cwd=d)
    return name


def looks_black(path, duration):
    """True when every sampled frame is black: a failed screen capture, not a style choice."""
    if duration < 1:
        return False
    vals = []
    for frac in (0.05, 0.3, 0.6, 0.9):
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-ss", f"{duration * frac:.1f}", "-i", str(path),
             "-frames:v", "1", "-vf",
             "scale=32:32,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = re.search(r"YAVG=([\d.]+)", p.stderr)
        if m:
            vals.append(float(m.group(1)))
    return len(vals) >= 3 and all(v < 24 for v in vals)


# ---------------------------------------------------------------- retention
def prune_clips():
    """Rolling window: only the newest KEEP_CLIPS rendered clips stay on disk."""
    if KEEP_CLIPS <= 0:
        return
    items = []
    for jd in JOBS.iterdir():
        f = jd / "job.json"
        if not f.is_file():
            continue
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in st.get("clips", []):
            p = jd / c.get("file", "")
            if p.is_file():
                items.append((p.stat().st_mtime, jd, st, c))
    items.sort(key=lambda i: i[0], reverse=True)
    for _, jd, st, c in items[KEEP_CLIPS:]:
        if not jd.exists():
            continue
        print(f"pruning old clip {jd.name}/{c['file']}")
        (jd / c["file"]).unlink(missing_ok=True)
        st["clips"] = [x for x in st["clips"] if x.get("file") != c["file"]]
        if st["clips"]:
            (jd / "job.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        else:
            shutil.rmtree(jd, ignore_errors=True)


# ---------------------------------------------------------------- pipeline
def process(job):
    d = job["dir"]
    try:
        src = source_of(job)
        if looks_black(src, job["duration"]):
            raise RuntimeError("No picture: every sampled frame is black, so the screen capture "
                               "failed (the audio is intact). Re-record the game, then re-upload.")
        set_state(job, status="extracting", stage="Reading audio")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-vn", "-ac", "1", "-ar", "16000", "audio.wav"], cwd=d)
        job["crop"] = detect_crop(src, job["duration"])
        if job["crop"]:
            set_state(job, stage=f"Trimming dead edges ({job['crop']})")

        segments, lang = cached_transcript(src)
        if segments:
            set_state(job, stage="Transcript cached from a previous run")
        else:
            set_state(job, status="transcribing", stage=f"Transcribing ({MODEL_NAME})")
            segments, lang = transcribe(
                d / "audio.wav", job["duration"],
                lambda f: set_state(job, stage=f"Transcribing ({MODEL_NAME}) {int(f * 100)}%"))
            store_transcript(src, segments, lang)
        if not segments:
            raise RuntimeError("No speech detected in this video.")
        job["segments"] = segments
        job["lang"] = lang
        set_state(job, stage=f"Transcribed {len(segments)} segments, picking moments")

        cands = candidates(segments)
        if not cands:
            raise RuntimeError("Video too short for a clip (needs ~15s of speech).")
        energy = energy_profile(d / "audio.wav", job["duration"])
        chosen = pick_llm(cands) or pick(cands, MAX_CLIPS, job["duration"], energy)
        chosen = extend_clips(chosen, energy, job["duration"])
        for c in chosen:                      # the window moved, so does its text
            c["text"] = " ".join(s["text"] for s in segments
                                 if s["end"] > c["start"] and s["start"] < c["end"])
        for c in chosen:
            c.setdefault("title", title_of(c["text"]))
            c.setdefault("why", "auto pick")

        clips = []
        for i, c in enumerate(chosen):
            set_state(job, status="rendering", stage=f"Rendering clip {i + 1}/{len(chosen)}")
            fname = render_clip(job, i, c, src)
            clips.append({"file": fname, "title": c["title"], "why": c["why"],
                          "start": round(c["start"], 1), "end": round(c["end"], 1)})
            set_state(job, clips=clips)

        set_state(job, status="done", stage=f"{len(clips)} clips ready")
        prune_clips()
    except Exception as e:
        set_state(job, status="error", stage=str(e)[:300], error=str(e))


def mark_interrupted():
    """Nothing survives a restart, so any job still mid-flight is dead - say so instead
    of letting it block the watcher forever."""
    for jd in JOBS.iterdir():
        f = jd / "job.json"
        if not f.is_file():
            continue
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if st.get("status") in ("done", "error"):
            continue
        st.update(status="error", error="interrupted",
                  stage="Interrupted: the server was restarted. Clip it again.")
        f.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def start_job(job):
    job["segments"] = []
    set_state(job, status="queued", stage="Starting")
    threading.Thread(target=lambda: process(job), daemon=True).start()


def new_job(d, path, name, stage="Queued"):
    """Register a job for an existing file (no copy) and start it."""
    try:
        duration = probe_duration(path)
    except Exception:
        return None
    job = {"dir": d, "duration": duration, "state": {
        "id": d.name, "name": name, "created": time.time(), "source": str(path),
        "status": "queued", "stage": stage, "clips": [], "error": None}}
    save_state(job)
    start_job(job)
    return job["state"]["id"]


# ---------------------------------------------------------------- http
MIME = {".html": "text/html; charset=utf-8", ".mp4": "video/mp4", ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, (ROOT / "index.html").read_bytes(), MIME[".html"])
        if path == "/api/library":
            return self._send(200, json.dumps(library()).encode())
        if path == "/api/jobs":
            jobs = []
            for jd in JOBS.iterdir():
                f = jd / "job.json"
                if f.is_file():
                    try:
                        jobs.append(json.loads(f.read_text(encoding="utf-8")))
                    except Exception:
                        pass
            jobs.sort(key=lambda j: j.get("created", 0), reverse=True)
            return self._send(200, json.dumps(jobs).encode())
        if path.startswith("/api/jobs/"):
            f = JOBS / path.split("/")[3] / "job.json"
            if not f.is_file():
                return self._send(404, b'{"error":"not found"}')
            return self._send(200, f.read_bytes())
        if path.startswith("/media/"):
            jobid, name = path.split("/")[2], path.split("/")[3]
            f = (JOBS / jobid / name).resolve()
            if not str(f).startswith(str(JOBS)) or not f.is_file():
                return self._send(404, b"")
            return self._stream(f)
        return self._send(404, b'{"error":"not found"}')

    def do_DELETE(self):
        """Delete one clip (?file=) or the whole job."""
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) < 3 or parts[:2] != ["api", "jobs"]:
            return self._send(404, b'{"error":"not found"}')
        jd = JOBS / parts[2]
        f = jd / "job.json"
        if not f.is_file():
            return self._send(404, b'{"error":"not found"}')
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return self._send(404, b'{"error":"not found"}')
        if st.get("status") not in ("done", "error"):
            return self._send(409, b'{"error":"job still running"}')

        if len(parts) == 3:                      # the whole job
            shutil.rmtree(jd, ignore_errors=True)
            return self._send(200, b'{"ok":true}')

        name = parts[-1]
        if parts[3] != "clips" or Path(name).name != name:
            return self._send(404, b'{"error":"not found"}')
        if not any(c.get("file") == name for c in st.get("clips", [])):
            return self._send(404, b'{"error":"not found"}')
        (jd / name).unlink(missing_ok=True)
        st["clips"] = [c for c in st.get("clips", []) if c.get("file") != name]
        if st["clips"]:
            f.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        else:
            shutil.rmtree(jd, ignore_errors=True)
        return self._send(200, b'{"ok":true}')

    def _stream(self, f):
        """Range support so the browser can seek in previews."""
        size, start, end = f.stat().st_size, 0, f.stat().st_size - 1
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
        body_len = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(body_len))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with f.open("rb") as fh:
                fh.seek(start)
                left = body_len
                while left > 0:
                    block = fh.read(min(1 << 20, left))
                    if not block:
                        break
                    self.wfile.write(block)
                    left -= len(block)
        except (ConnectionError, OSError):
            pass  # browser closed the preview mid-stream

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/library/clip":
            return self._clip_library()
        if route != "/api/clip":
            return self._send(404, b'{"error":"not found"}')
        raw = unquote(self.headers.get("X-Filename", "video.mp4"))
        ext = Path(raw).suffix.lower() or ".mp4"
        if ext not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts", ".mts"}:
            return self._send(400, b'{"error":"unsupported file type"}')
        try:
            total = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._send(400, b'{"error":"bad length"}')
        if total <= 0:
            return self._send(400, b'{"error":"empty upload"}')

        d = JOBS / uuid.uuid4().hex[:12]
        d.mkdir()
        dest = d / ("source" + ext)
        left = total
        with dest.open("wb") as out:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
        if left:
            return self._send(400, b'{"error":"upload interrupted"}')

        # dropping a file that already lives in the recordings folder should not double it
        twin = next((Path(i["path"]) for i in library()
                     if i["name"] == Path(raw).name and Path(i["path"]).stat().st_size == total),
                    None)
        if twin:
            dest.unlink(missing_ok=True)
            dest = twin

        job_id = new_job(d, dest, Path(raw).name, "Uploaded")
        if not job_id:
            return self._send(400, b'{"error":"not a readable video file"}')
        return self._send(200, json.dumps({"id": job_id}).encode())

    def _clip_library(self):
        try:
            fid = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            src = library_file(fid.get("id", ""))
        except Exception:
            src = None
        if not src:
            return self._send(404, b'{"error":"unknown library file"}')
        d = JOBS / uuid.uuid4().hex[:12]
        d.mkdir()
        job_id = new_job(d, src, src.name, "Queued from library")
        return self._send(200, json.dumps({"id": job_id}).encode())


def selftest():
    segs = [{"start": i * 4.0, "end": i * 4.0 + 4.0, "text": "word " * 8,
             "words": [{"w": f"w{i}", "start": i * 4.0, "end": i * 4.0 + 1.0}]}
            for i in range(30)]
    segs[3]["text"] = "Why does everyone get this wrong? 42 mistakes!"
    c = candidates(segs)
    assert c and all(MIN_CLIP <= x["end"] - x["start"] <= MAX_CLIP for x in c), "window lengths"
    got = pick(c)
    assert got and all(b["start"] >= a["end"] for a, b in zip(got, got[1:])), "overlap"
    assert "wrong" in max(c, key=score)["text"], "hook scoring"

    # a 45-minute session: clips must come from all over it, not one firefight
    long = [{"start": i * 30.0, "end": i * 30.0 + 20.0, "text": "word " * 8}
            for i in range(90)]
    spread = pick(long, 6, 2700.0)
    assert len(spread) == 6, "spread count"
    assert spread[0]["start"] < 0.3 * 2700, "spread starts early"
    assert spread[-1]["start"] > 0.7 * 2700, "spread reaches the end"
    a = ass_text(segs, 12.0, 30.0)
    assert "Dialogue: 0,0:00:00.00," in a, "ass timings"
    assert "W3" in a and "\\fad(60,40)" in a, "ass uppercase + fade"
    assert HILITE in a, "ass highlighted word"

    en = [{"w": "why", "start": 0.0, "end": 0.4}, {"w": "does", "start": 0.4, "end": 0.9}]
    b = ass_text(segs, 12.0, 30.0, en)
    first = b.split("\n")[-3] if b.count("\n") > 3 else b
    assert "\\N" in b and "why" in b, "bilingual cue"
    assert b.index("why") < b.index("\\N"), "english above the original"

    # a quiet callout followed by a loud fight: the clip must swallow the fight
    beats = [0.2] * 20 + [3.0] * 5 + [0.2] * 45
    two = [{"start": 10.0, "end": 18.0, "text": "a"},
           {"start": 60.0, "end": 70.0, "text": "b"}]
    grown = extend_clips(two, beats, 90.0)
    assert grown[0]["end"] > 25.0, "clip ends after the action"
    assert grown[0]["end"] - grown[0]["start"] >= TARGET_CLIP, "clip is at least 30 s"
    assert grown[0]["end"] <= grown[1]["start"], "extended clips do not overlap"

    # two picks that touch: the cut moves into the fight, the next clip starts there
    join = [{"start": 10.0, "end": 20.0, "text": "a"},
            {"start": 20.0, "end": 30.0, "text": "b"}]
    beat2 = [0.2] * 20 + [3.0] * 6 + [0.2] * 40
    joined = extend_clips(join, beat2, 80.0)
    assert joined[0]["end"] > 26.0, "cut moved past the fight"
    assert joined[1]["start"] == joined[0]["end"], "next clip starts at the new cut"

    stub = [{"start": 10.0, "end": 40.0, "text": "a"},
            {"start": 40.0, "end": 50.0, "text": "b"}]
    merged = extend_clips(stub, [0.2] * 80, 50.0)
    assert len(merged) == 1 and merged[0]["end"] == 50.0, "short tail folded in"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    port = int(os.getenv("CLIP_PORT", "8787"))
    url = f"http://127.0.0.1:{port}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        print(f"Port {port} is already in use - another clip.py is running.\n"
              f"Close it (Task Manager) or set CLIP_PORT to something else.")
        sys.exit(1)
    print(f"clip.py ready on {url}  (model: {MODEL_NAME}, ffmpeg required)")
    mark_interrupted()
    if os.getenv("CLIP_WATCH", "1") != "0":
        print("watching " + ", ".join(str(d) for d in LIB_DIRS) + " for new recordings")
        threading.Thread(target=watch_loop, daemon=True).start()
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    server.serve_forever()
