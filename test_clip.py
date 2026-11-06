"""End-to-end tests for clip.py.

    python test_clip.py            # spins its own server on port 8799, ~2 min
    python test_clip.py --keep     # leave the test job folders on disk

Needs ffmpeg/ffprobe. Builds its own media (colour bars, portrait, ultrawide, black)
and asserts what the browser and the user actually get.
"""
import json, os, shutil, subprocess, sys, threading, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TMP = Path(os.environ.get("TEMP", ".")) / "clip_tests"
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
KEEP = "--keep" in sys.argv

os.environ["CLIP_PORT"] = str(PORT)
os.environ["CLIP_WATCH"] = "0"          # no watcher during tests
os.environ["WHISPER_MODEL"] = "tiny"    # fast
os.environ["CLIP_LIB"] = str(TMP / "lib")
os.environ["CLIP_KEEP_CLIPS"] = "0"     # the retention test drives this itself

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def sh(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def probe(path):
    out = sh(["ffprobe", "-v", "error", "-show_entries",
              "stream=codec_type,width,height,duration:format=duration",
              "-of", "json", str(path)])
    data = json.loads(out.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), {})
    return {"w": v.get("width"), "h": v.get("height"),
            "dur": float(v.get("duration") or data["format"]["duration"]),
            "audio": bool(a)}


def http(path, method="GET", body=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def make_media():
    """Video fixtures: 16:9 with speech, portrait, ultrawide, all-black."""
    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "lib").mkdir(parents=True)

    wav = TMP / "speech.wav"
    if not wav.exists():  # reuse a TTS wav if the caller baked one
        subprocess.run(["powershell", "-NoProfile", "-Command", f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = -1
$s.SetOutputToWaveFile('{wav}')
$s.Speak("The mistake everyone makes is editing before they choose the moment. Here is what actually works. First you find the one sentence that stands on its own, then you build the clip around that sentence instead of around the timeline. That single change tripled my average view duration. Number one, hook rate. Number two, watch time. Number three, shares per thousand views. If those are flat the problem is never the edit, it is the first sentence. Write it ten times and pick the one that sounds like a secret.")
$s.Dispose()
"""], check=False)

    fixtures = {
        "wide.mp4": ("1280x720", "testsrc2=size=1280x720:rate=30"),
        "portrait.mp4": ("1080x1920", "testsrc2=size=1080x1920:rate=30"),
        "ultrawide.mp4": ("1280x536", "testsrc2=size=1280x536:rate=30"),
    }
    for name, (_size, src) in fixtures.items():
        dst = TMP / "lib" / name
        sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", src,
            "-i", str(wav), "-t", "40", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(dst)])
        assert dst.exists() and dst.stat().st_size > 1000, name

    black = TMP / "lib" / "black.mp4"
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "color=c=black:size=1280x720:rate=30", "-i", str(wav), "-t", "40", "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(black)])

    # game area on a wider black canvas, like the OBS captures
    slab = TMP / "lib" / "slab.mp4"
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc2=size=1440x1080:rate=30", "-i", str(wav), "-t", "40", "-shortest",
        "-vf", "pad=1920:1080:0:0:black", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", str(slab)])
    return black, slab


def wait_job(job_id, timeout=420):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        status, body, _ = http(f"/api/jobs/{job_id}")
        if status != 200:
            return None
        job = json.loads(body)
        if job["status"] != last:
            last = job["status"]
        if job["status"] in ("done", "error"):
            return job
        time.sleep(2)
    return {"status": "timeout", "stage": "timed out"}


def main():
    print("building fixtures...")
    black, slab = make_media()

    sys.path.insert(0, str(ROOT))
    import clip
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), clip.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)
    print(f"server on {BASE}\n")

    created = []

    # --- 1. library listing
    print("library")
    status, body, _ = http("/api/library")
    lib = json.loads(body)
    check("lists library files", status == 200 and len(lib) == 5, f"{status} n={len(lib)}")
    check("knows durations", all(f["duration"] > 1 for f in lib), str([f["duration"] for f in lib]))
    check("sizes are sane", all(f["gb"] >= 0 for f in lib))

    print("crop detection")
    check("finds the game area in a black canvas",
          clip.detect_crop(slab, 40) == "1440:1080:0:0", str(clip.detect_crop(slab, 40)))
    check("leaves a clean 16:9 alone",
          clip.detect_crop(TMP / "lib" / "wide.mp4", 40) is None)

    # --- 2. black recording is rejected fast
    print("black guard")
    bid = next(f["id"] for f in lib if f["name"] == "black.mp4")
    _, body, _ = http("/api/library/clip", "POST", json.dumps({"id": bid}).encode(),
                      {"Content-Type": "application/json"})
    bjob = wait_job(json.loads(body)["id"], timeout=60)
    created.append(bjob["id"])
    check("black video errors", bjob["status"] == "error", bjob.get("stage", ""))
    check("error explains it", "black" in bjob["stage"].lower(), bjob.get("stage", ""))

    # --- 3. wide video: the main path
    print("16:9 clip job")
    wid = next(f["id"] for f in lib if f["name"] == "wide.mp4")
    _, body, _ = http("/api/library/clip", "POST", json.dumps({"id": wid}).encode(),
                      {"Content-Type": "application/json"})
    job = wait_job(json.loads(body)["id"])
    created.append(job["id"])
    check("job finishes", job["status"] == "done", job.get("stage", ""))
    check("no source copy", not list((ROOT / "jobs" / job["id"]).glob("source.*")))
    check("clips exist", len(job["clips"]) >= 1, str(len(job["clips"])))

    jd = ROOT / "jobs" / job["id"]
    for c in job["clips"]:
        p = probe(jd / c["file"])
        check(f'{c["file"]} is 1080x1920', (p["w"], p["h"]) == (1080, 1920), f'{p["w"]}x{p["h"]}')
        check(f'{c["file"]} has audio', p["audio"])
        check(f'{c["file"]} length matches', abs(p["dur"] - (c["end"] - c["start"])) < 1.5,
              f'{p["dur"]:.1f} vs {c["end"] - c["start"]:.1f}')
        check(f'{c["file"]} is not black',
              not clip.looks_black(jd / c["file"], p["dur"]))
    spans = sorted((c["start"], c["end"]) for c in job["clips"])
    check("clips do not overlap",
          all(b[0] >= a[1] - 0.05 for a, b in zip(spans, spans[1:])), str(spans))
    check("clips are 15-60s", all(15 <= e - s <= 60.5 for s, e in spans), str(spans))
    check("titles are sentences", all(len(c["title"]) > 5 for c in job["clips"]))

    # --- 4. media streaming with Range
    print("media")
    first = job["clips"][0]["file"]
    status, body, hdrs = http(f'/media/{job["id"]}/{first}')
    check("full media 200", status == 200 and len(body) > 100_000, str(status))
    status, body, hdrs = http(f'/media/{job["id"]}/{first}', headers={"Range": "bytes=100-199"})
    check("range 206", status == 206 and len(body) == 100, f"{status} {len(body)}")
    check("range header", hdrs.get("Content-Range", "").startswith("bytes 100-199/"))
    status, _, _ = http(f'/media/{job["id"]}/nope.mp4')
    check("missing media 404", status == 404)
    status, _, _ = http('/media/../../clip.py')
    check("path traversal blocked", status == 404, str(status))

    # --- 5. transcript cache: same file, second job must not re-transcribe
    print("cache")
    cache_before = len(list((ROOT / "jobs" / ".cache").glob("*.json")))
    _, body, _ = http("/api/library/clip", "POST", json.dumps({"id": wid}).encode(),
                      {"Content-Type": "application/json"})
    job2 = wait_job(json.loads(body)["id"])
    created.append(job2["id"])
    check("second run finishes", job2["status"] == "done", job2.get("stage", ""))
    check("cache not duplicated",
          len(list((ROOT / "jobs" / ".cache").glob("*.json"))) == cache_before)

    # --- 6. portrait + ultrawide sources
    for name in ("portrait.mp4", "ultrawide.mp4"):
        print(name)
        fid = next(f["id"] for f in lib if f["name"] == name)
        _, body, _ = http("/api/library/clip", "POST", json.dumps({"id": fid}).encode(),
                          {"Content-Type": "application/json"})
        j = wait_job(json.loads(body)["id"])
        created.append(j["id"])
        check(f"{name} finishes", j["status"] == "done", j.get("stage", ""))
        if j["clips"]:
            p = probe(ROOT / "jobs" / j["id"] / j["clips"][0]["file"])
            check(f"{name} -> 1080x1920", (p["w"], p["h"]) == (1080, 1920), f'{p["w"]}x{p["h"]}')

    # --- 7. upload path
    print("upload")
    data = (TMP / "lib" / "wide.mp4").read_bytes()
    status, body, _ = http("/api/clip", "POST", data, {"X-Filename": "uploaded.mp4"})
    check("upload accepted", status == 200, str(status))
    up = wait_job(json.loads(body)["id"])
    created.append(up["id"])
    check("upload job finishes", up["status"] == "done", up.get("stage", ""))
    check("upload keeps a copy",
          bool(list((ROOT / "jobs" / up["id"]).glob("source.*"))))

    status, _, _ = http("/api/clip", "POST", b"x", {"X-Filename": "bad.exe"})
    check("bad extension rejected", status == 400, str(status))

    # --- 7b. manual delete
    print("delete")
    jid, n = job["id"], len(job["clips"])
    victim = job["clips"][0]["file"]
    status, _, _ = http(f"/api/jobs/{jid}/clips/{victim}", "DELETE")
    check("deletes one clip", status == 200, str(status))
    status, _, _ = http(f"/media/{jid}/{victim}")
    check("deleted clip is gone", status == 404)
    body = http(f"/api/jobs/{jid}")[1].decode()
    if n > 1:
        check("job.json updated", len(json.loads(body).get("clips", [])) == n - 1, body[:80])
    else:
        check("emptying a job removes it", "not found" in body, body[:80])
    status, _, _ = http(f"/api/jobs/{jid}/clips/nope.mp4", "DELETE")
    check("unknown clip 404", status == 404, str(status))
    status, _, _ = http(f"/api/jobs/{jid}/clips/..%2F..%2Fclip.py", "DELETE")
    check("delete traversal blocked", status == 404, str(status))

    fake = clip.JOBS / "testdelete"
    fake.mkdir(exist_ok=True)
    (fake / "clip00.mp4").write_bytes(b"x")
    (fake / "job.json").write_text(json.dumps({
        "id": "testdelete", "status": "done", "clips": [{"file": "clip00.mp4"}]}),
        encoding="utf-8")
    status, _, _ = http("/api/jobs/testdelete", "DELETE")
    check("deletes the whole job", status == 200 and not fake.exists(), str(status))

    busy = clip.JOBS / "testbusy"
    busy.mkdir(exist_ok=True)
    (busy / "job.json").write_text(json.dumps({
        "id": "testbusy", "status": "rendering", "clips": []}), encoding="utf-8")
    status, _, _ = http("/api/jobs/testbusy", "DELETE")
    check("running job is protected", status == 409 and busy.exists(), str(status))
    shutil.rmtree(busy, ignore_errors=True)

    # --- 8. watcher queues only finished, unseen recordings
    print("watcher")
    wdir = TMP / "watch"
    wdir.mkdir()
    shutil.copy(TMP / "lib" / "wide.mp4", wdir / "old.mp4")
    old_time = time.time() - 300
    os.utime(wdir / "old.mp4", (old_time, old_time))

    queued, made = [], []
    real_new_job, real_state, real_dirs = clip.new_job, clip.WATCH_STATE, clip.LIB_DIRS
    clip.WATCH_STATE = TMP / "watched.json"
    clip.LIB_DIRS = [wdir]

    def fake_new_job(d, path, name, stage=""):
        queued.append(name)
        made.append(d)
        return "fake"

    clip.new_job = fake_new_job
    try:
        clip.watcher()                      # first pass adopts what is already there
        check("first run adopts old recordings", not queued, str(queued))

        shutil.copy(TMP / "lib" / "wide.mp4", wdir / "fresh.mp4")
        os.utime(wdir / "fresh.mp4", (old_time, old_time))
        clip.watcher()                      # records its signature
        clip.watcher()                      # unchanged -> queue
        check("queues a finished recording", queued == ["fresh.mp4"], str(queued))

        shutil.copy(TMP / "lib" / "wide.mp4", wdir / "growing.mp4")
        clip.watcher()                      # just appeared, OBS still writing
        clip.watcher()
        check("waits on a just-written file", queued == ["fresh.mp4"], str(queued))

        os.utime(wdir / "growing.mp4", (old_time, old_time))   # OBS closed the file
        clip.watcher()                      # signature moved, so record it
        clip.watcher()                      # settled -> queue
        check("queues it once it settles", queued == ["fresh.mp4", "growing.mp4"], str(queued))
    finally:
        clip.new_job, clip.WATCH_STATE, clip.LIB_DIRS = real_new_job, real_state, real_dirs
        for d in made:
            shutil.rmtree(d, ignore_errors=True)

    # --- 9. retention: only the newest clips survive
    print("retention")
    real_jobs, real_keep = clip.JOBS, clip.KEEP_CLIPS
    clip.JOBS = TMP / "retention"
    clip.JOBS.mkdir()
    clip.KEEP_CLIPS = 2
    try:
        for i in range(4):
            jd = clip.JOBS / f"job{i}"
            jd.mkdir()
            (jd / "clip00.mp4").write_bytes(b"x" * 10)
            (jd / "job.json").write_text(json.dumps({
                "id": f"job{i}", "status": "done", "created": i,
                "clips": [{"file": "clip00.mp4"}]}), encoding="utf-8")
            stamp = time.time() - (100 - i * 10)      # job0 oldest, job3 newest
            os.utime(jd / "clip00.mp4", (stamp, stamp))
        clip.prune_clips()
        left = sorted(d.name for d in clip.JOBS.iterdir())
        check("keeps only the newest clips", left == ["job2", "job3"], str(left))
        check("deletes emptied job folders", not (clip.JOBS / "job0").exists())
    finally:
        clip.JOBS, clip.KEEP_CLIPS = real_jobs, real_keep

    # --- 10. misc endpoints
    print("api")
    status, body, _ = http("/")
    check("index served", status == 200 and b"Clip it" in body)
    status, body, _ = http("/api/jobs")
    check("jobs list", status == 200 and len(json.loads(body)) >= 4)
    status, _, _ = http("/api/jobs/deadbeef")
    check("unknown job 404", status == 404)

    srv.shutdown()
    if not KEEP:
        for jid in created:
            shutil.rmtree(ROOT / "jobs" / jid, ignore_errors=True)
        shutil.rmtree(TMP, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED {len(failures)}: " + ", ".join(failures))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
