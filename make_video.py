#!/usr/bin/env python3
"""Daily Short generator for the the channel channel.

Pipeline (all free, no API keys):
  pick topic -> edge-tts narration -> 5 Pollinations images ->
  ffmpeg Ken Burns + burned-in captions -> upload to YouTube.

Usage:
  python daily/make_video.py                 # make + upload PUBLIC (the daily default)
  python daily/make_video.py --privacy unlisted
  python daily/make_video.py --no-upload     # build the mp4 only, no upload
  python daily/make_video.py --id horror-frog  # force a specific topic

Run from the project root (so token.json / youtube_upload.py resolve).
"""
import argparse
import csv
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

# Force UTF-8 stdio so emoji/unicode in titles don't crash on Windows consoles.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = HERE  # self-contained: this channel's token/upload live in finalseconds/
TOPICS_FILE = os.path.join(HERE, "topics.json")
STATE_FILE = os.path.join(HERE, "state.json")
RUNS_DIR = os.path.join(HERE, "runs")
LOGS_DIR = os.path.join(HERE, "logs")
HISTORY_CSV = os.path.join(LOGS_DIR, "history.csv")
TOKEN_EXPIRED_FLAG = os.path.join(LOGS_DIR, "TOKEN_EXPIRED.txt")
BANK_EMPTY_FLAG = os.path.join(LOGS_DIR, "BANK_EMPTY.txt")
LOCK_FILE = os.path.join(LOGS_DIR, "run.lock")
KEEP_RUNS_DAYS = 3  # auto-delete run folders older than this

# Daily publish schedule (channel-local time). One generation run builds all of
# these at once and hands them to YouTube's own scheduler via publishAt, so
# GitHub's flaky cron only has to fire ONCE/day (later runs just catch up).
TZ = ZoneInfo("America/New_York")
PUBLISH_SLOTS = [(12, 0), (15, 0), (18, 0), (21, 0)]  # 12p, 3p, 6p, 9p

# "the channel" — colder/calmer male narrator, distinct from channel.
VOICE = "en-US-AndrewMultilingualNeural"  # more expressive rise/fall (user pick)
VOICE_RATE = "-4%"
VOICE_PITCH = "-2Hz"
TOPIC_MODEL = "openai-fast"  # Pollinations free text model (unused; bank mode)
STYLE = (", hyper-realistic cinematic disaster photography, dramatic volumetric lighting, "
         "vivid color, bright highlights, well-lit clear subject, sharp high detail, "
         "sense of danger, first-person point of view, vertical 9:16 composition")
W, H, FPS = 1080, 1920, 30
XFADE = 0.5  # seconds of cross-dissolve between scenes
NUM_IMAGES = 8  # target scenes per video; bank prompts are expanded to reach this
PROMPT_VARIATIONS = [
    ", alternate camera angle, closer",
    ", extreme close-up detail shot",
    ", dramatic wide establishing shot",
]
ASSETS_DIR = os.path.join(HERE, "assets")
ATMOS_FILE = os.path.join(ASSETS_DIR, "atmosphere_1080x1920.mp4")
BED_FILE = os.path.join(ASSETS_DIR, "bed_dread.m4a")  # low rumble under narration

PRESET = os.environ.get("X264_PRESET", "medium")  # CI sets veryfast to save minutes
SUB_FONT = "Arial" if os.name == "nt" else "Liberation Sans"
TIMER_FONT = (r"C\:/Windows/Fonts/arialbd.ttf" if os.name == "nt"
              else "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")

# Burned-in ticking survival clock (0:SS), top-center, red — the retention engine.
# Verified ffmpeg escaping; ends with a comma to chain into the next filter.
TIMER = (
    f"drawtext=fontfile='{TIMER_FONT}':"
    r"text='0\:%{eif\:mod(floor(t)\,60)\:d\:2}':"
    r"fontcolor=0xFF3B30:fontsize=120:borderw=7:bordercolor=black:"
    r"x=(w-text_w)/2:y=120,"
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(msg):
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def run(cmd, cwd=None):
    """Run a command, raising with captured output on failure."""
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"command failed ({p.returncode}): {' '.join(map(str, cmd))}\n"
            f"STDERR:\n{p.stderr[-2000:]}"
        )
    return p


def ffprobe_duration(path):
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path])
    return float(p.stdout.strip())


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def cleanup_runs(days=KEEP_RUNS_DAYS):
    """Delete run folders older than `days` so disk use never builds up."""
    if not os.path.isdir(RUNS_DIR):
        return
    cutoff = time.time() - days * 86400
    removed = 0
    for name in os.listdir(RUNS_DIR):
        path = os.path.join(RUNS_DIR, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass  # in use / permission — just skip, retry next run
    if removed:
        log(f"Cleanup: removed {removed} run folder(s) older than {days} days.")


def _try_lock(stale_minutes):
    """One attempt to take the lock. Returns True on success."""
    try:
        if os.path.exists(LOCK_FILE):
            age = time.time() - os.path.getmtime(LOCK_FILE)
            if age < stale_minutes * 60:
                return False  # someone else is genuinely running
            log("Removing stale lock from a previous crashed run.")
            os.remove(LOCK_FILE)
        with open(LOCK_FILE, "x", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {dt.datetime.now().isoformat()}\n")
        global _LOCK_OWNED
        _LOCK_OWNED = True
        return True
    except (FileExistsError, OSError):
        return False


def acquire_lock(stale_minutes=45, wait_minutes=20, poll_seconds=30):
    """Prevent overlapping runs (would double-post the same topic).

    If another run holds the lock, WAIT for it to finish (builds take ~4-8 min)
    and then run — so a busy slot is delayed, not lost. Gives up only after
    wait_minutes, which normal builds never approach.
    """
    if _try_lock(stale_minutes):
        return True
    log("Another run is active — waiting for it to finish "
        f"(up to {wait_minutes} min)...")
    deadline = time.time() + wait_minutes * 60
    while time.time() < deadline:
        time.sleep(poll_seconds)
        if _try_lock(stale_minutes):
            log("Previous run finished — proceeding with this one.")
            return True
    log(f"Still locked after {wait_minutes} min — giving up on this slot.")
    return False


_LOCK_OWNED = False


def release_lock():
    global _LOCK_OWNED
    if not _LOCK_OWNED:
        return  # never delete a lock we don't own
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass
    _LOCK_OWNED = False


def clear_flag(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:40] or "topic"


def _extract_json(text):
    """Pull the first JSON object out of an LLM response (tolerates fences/prose)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text).rsplit("```", 1)[0]
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("no JSON object in response")
    obj = json.loads(text[a:b + 1])
    # unwrap chat/reasoning-style wrappers so the real episode object surfaces
    for _ in range(3):
        if not isinstance(obj, dict) or "subject" in obj:
            break
        if isinstance(obj.get("content"), str):
            obj = _extract_json(obj["content"])
        elif isinstance(obj.get("choices"), list) and obj["choices"]:
            obj = _extract_json(obj["choices"][0]["message"]["content"])
        else:
            break
    return obj


def _ask_json(prompt):
    """One GET to Pollinations' free text model; return parsed JSON dict."""
    enc = urllib.parse.quote(prompt)
    seed = random.randint(1, 10_000_000)
    url = (f"https://text.pollinations.ai/{enc}"
           f"?model={TOPIC_MODEL}&seed={seed}&json=true")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return _extract_json(r.read().decode("utf-8", "replace"))


def _fix5(items):
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not items:
        raise ValueError("empty list")
    while len(items) < 5:
        items.append(items[-1])
    return items[:5]


def generate_topic(avoid, tries=3):
    """Invent a fresh episode via Pollinations' free text API (two small asks).

    The only free model is a reasoning LLM that rambles on complex prompts, so we
    keep each request tiny and derive tags/description locally. Raises on failure.
    """
    avoid_str = ", ".join(avoid[-50:]) if avoid else "none"
    last = None
    for attempt in range(1, tries + 1):
        try:
            # Ask 1: the idea + narration (small prompt -> clean JSON)
            p1 = ("Give ONE dark, bizarre, genuinely TRUE animal or nature fact for a "
                   "short horror-documentary video. Do NOT reuse any of these subjects: "
                   f"{avoid_str}. Reply with ONLY one minified JSON object and nothing "
                   'else: {"subject":"creature name","title":"catchy title with one emoji '
                   'ending in #shorts","script":"an ~85 word narration that starts with '
                   'Did you know, is eerie and conversational, uses ... for pauses, and '
                   'ends on a chilling hook"}')
            d1 = _ask_json(p1)
            subject = str(d1["subject"]).strip()
            title = str(d1["title"]).strip()
            script = str(d1["script"]).strip()
            if len(script) < 120:
                raise ValueError("script too short")
            if subject.lower() in {a.lower() for a in avoid}:
                raise ValueError(f"repeat subject: {subject}")

            # Ask 2: captions + image prompts derived from that script
            p2 = ("For this narration, write 5 very short on-screen caption lines (max ~7 "
                   "words each, following the narration's beats) and 5 vivid concrete image "
                   "scene descriptions (subjects and composition only, no style words). "
                   'Reply with ONLY one minified JSON object: {"captions":["l1","l2","l3",'
                   '"l4","l5"],"image_prompts":["p1","p2","p3","p4","p5"]}. Narration: '
                   f'"{script}"')
            d2 = _ask_json(p2)
            caps = _fix5(d2["captions"])
            prompts = _fix5(d2["image_prompts"])

            title = title if "#shorts" in title.lower() else f"{title} #shorts"
            words = re.findall(r"[a-z]+", subject.lower())
            tags = list(dict.fromkeys(
                words + ["animals", "nature", "facts", "shorts", "wildlife",
                         "didyouknow"]))[:8]
            desc = (script.split("?")[0] + "?" if "?" in script else script[:110]) \
                + " " + " ".join(f"#{t}" for t in tags[:6])
            return {
                "id": _slug(subject),
                "subject": subject,
                "title": title,
                "description": desc.strip(),
                "tags": tags,
                "script": script,
                "captions": caps,
                "image_prompts": prompts,
            }
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  topic-gen attempt {attempt} failed: {e}; retrying...")
            time.sleep(3 * attempt)
    raise RuntimeError(f"topic generation failed after {tries} tries: {last}")


def next_bank(topics):
    """Return the next queued episode (top of the file), or stop cleanly if empty."""
    if not topics:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(BANK_EMPTY_FLAG, "w", encoding="utf-8") as f:
            f.write(dt.datetime.now().isoformat() + "\n")
            f.write("Topic bank is empty. Ask Claude to top up daily/topics.json.\n")
        log("BANK EMPTY — daily/topics.json has no episodes left. "
            "Ask Claude to add more. Skipping this run.")
        sys.exit(0)
    clear_flag(BANK_EMPTY_FLAG)  # bank has content again
    return topics[0]


def remove_from_bank(topic_id):
    """Delete a posted episode from topics.json so it's never reused."""
    topics = load_json(TOPICS_FILE, [])
    remaining = [t for t in topics if t.get("id") != topic_id]
    if len(remaining) != len(topics):
        save_json(TOPICS_FILE, remaining)
    return len(remaining)


def find_topic(topics, topic_id):
    """Look up a specific episode by id (used by --id)."""
    for t in topics:
        if t["id"] == topic_id:
            return t
    raise SystemExit(f"No topic with id '{topic_id}'")


# --------------------------------------------------------------------------- #
# generation steps
# --------------------------------------------------------------------------- #
def make_narration(script, out_path):
    run([sys.executable, "-m", "edge_tts", "--voice", VOICE,
         f"--rate={VOICE_RATE}", f"--pitch={VOICE_PITCH}",
         "--text", script, "--write-media", out_path])
    return ffprobe_duration(out_path)


def fetch_image(prompt, out_path, seed, tries=4):
    enc = urllib.parse.quote(prompt + STYLE)
    last = None
    for attempt in range(1, tries + 1):
        # new seed each retry: a bad prompt+seed combo 500s deterministically
        url = (f"https://image.pollinations.ai/prompt/{enc}"
               f"?width={W}&height={H}&nologo=true&model=flux"
               f"&seed={seed + (attempt - 1) * 7919}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) < 3000:
                raise RuntimeError(f"image too small ({len(data)} bytes)")
            # must actually be an image, not an HTML/JSON error page
            if not (data[:3] == b"\xff\xd8\xff"            # JPEG
                    or data[:8] == b"\x89PNG\r\n\x1a\n"     # PNG
                    or data[:4] == b"RIFF"):                # WebP
                raise RuntimeError(f"not an image (starts: {data[:24]!r})")
            with open(out_path, "wb") as f:
                f.write(data)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  image attempt {attempt} failed: {e}; retrying...")
            time.sleep(5 * attempt)
    raise RuntimeError(f"Pollinations failed after {tries} tries: {last}")


def kenburns_clip(img, out, dur, idx):
    """One animated clip: varied zoom + directional pan + gentle sway."""
    frames = round(dur * FPS)
    rnd = random.Random(idx * 97 + 13)  # deterministic-per-scene variety

    # zoom: alternate in/out by scene so the video breathes
    if idx % 2 == 1:
        z = "min(zoom+0.0010,1.20)"
    else:
        z = "if(lte(zoom,1.0),1.20,max(zoom-0.0010,1.0))"

    # directional pan across the (huge) prescaled source, driven by output frame 'on'
    dx = rnd.choice([-1, 0, 1])
    dy = rnd.choice([-1, 0, 1])
    if dx == 0 and dy == 0:
        dx = 1
    px = f"+({dx})*(iw*0.06)*(on/{frames})"
    py = f"+({dy})*(ih*0.06)*(on/{frames})"
    xexpr = f"iw/2-(iw/zoom/2){px}"
    yexpr = f"ih/2-(ih/zoom/2){py}"

    # gentle handheld sway: rotate a slightly oversized frame, then crop back
    amp = round(rnd.uniform(0.008, 0.016), 4)
    period = rnd.choice([6, 7, 8, 9])
    big_w, big_h = int(W * 1.12), int(H * 1.12)
    rot = f"{amp}*sin(2*PI*t/{period})"

    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"scale=5400:9600,"
        f"zoompan=z='{z}':d={frames}:x='{xexpr}':y='{yexpr}':s={W}x{H}:fps={FPS},"
        f"scale={big_w}:{big_h},"
        f"rotate='{rot}':ow={big_w}:oh={big_h}:c=black@0,"
        f"crop={W}:{H},setsar=1,format=yuv420p"
    )
    run(["ffmpeg", "-y", "-loop", "1", "-i", img, "-t", f"{dur}", "-r", f"{FPS}",
         "-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]",
         "-c:v", "libx264", "-preset", PRESET, "-crf", "18", out])


def write_srt(captions, total_dur, path):
    n = len(captions)
    seg = total_dur / n

    def ts(sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

    lines = []
    for i, cap in enumerate(captions):
        start = i * seg
        end = min((i + 1) * seg, total_dur)
        lines.append(str(i + 1))
        lines.append(f"{ts(start)} --> {ts(end)}")
        lines.append(cap)
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


SUB_STYLE = (f"FontName={SUB_FONT},Fontsize=13,Bold=1,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,Outline=3,Shadow=1,Alignment=2,MarginV=80")


def expand_prompts(base, target=NUM_IMAGES):
    """Grow the bank's prompt list to `target` scenes.

    Extra shots are angle/close-up variants inserted right after their base
    scene, so the narrative escalation order is preserved (film-style coverage).
    Each fetch also gets a distinct seed, so even same-text prompts differ.
    """
    if len(base) >= target:
        return base[:target]
    extra = target - len(base)
    out = []
    for i, p in enumerate(base):
        out.append(p)
        if i < extra:
            out.append(p + PROMPT_VARIATIONS[i % len(PROMPT_VARIATIONS)])
    return out


def build_audio_bed():
    """Generate (once, cached) a low dread-rumble bed mixed under narration."""
    if os.path.exists(BED_FILE):
        return BED_FILE
    os.makedirs(ASSETS_DIR, exist_ok=True)
    log("Generating cached audio bed (one-time)...")
    run(["ffmpeg", "-y", "-f", "lavfi",
         "-i", "anoisesrc=color=brown:sample_rate=44100:duration=20",
         "-af", "lowpass=f=110,tremolo=f=0.18:d=0.65,volume=4.5",
         "-c:a", "aac", "-b:a", "128k", BED_FILE])
    return BED_FILE


def build_atmosphere():
    """Generate (once, cached) a looping drifting-light overlay on black.

    Screen-blended over each video it adds slow moving haze/glow -> depth + motion.
    """
    if os.path.exists(ATMOS_FILE):
        return ATMOS_FILE
    os.makedirs(ASSETS_DIR, exist_ok=True)
    log("Generating cached atmosphere overlay (one-time)...")
    glow = (
        "140*exp(-(pow(X-(540+260*sin(2*PI*T/12)),2)"
        "+pow(Y-(650+380*sin(2*PI*T/10)),2))/38000)"
        "+100*exp(-(pow(X-(720-240*sin(2*PI*T/11)),2)"
        "+pow(Y-(1350+320*cos(2*PI*T/9)),2))/46000)"
    )
    vf = (f"format=gray,geq=lum='{glow}':cb=128:cr=128,"
          f"gblur=sigma=26,format=yuv420p")
    run(["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"color=c=black:s={W}x{H}:r={FPS}:d=12",
         "-vf", vf, "-c:v", "libx264", "-preset", PRESET,
         "-crf", "22", "-pix_fmt", "yuv420p", ATMOS_FILE])
    return ATMOS_FILE


def _build_xfade_visuals(run_dir, n, per):
    """Cross-dissolve the clips into visuals.mp4."""
    inputs = []
    for i in range(1, n + 1):
        inputs += ["-i", f"clip{i}.mp4"]
    steps, prev = [], "[0:v]"
    for k in range(1, n):
        off = k * (per - XFADE)
        out = f"[x{k}]"
        steps.append(f"{prev}[{k}:v]xfade=transition=fade:"
                     f"duration={XFADE}:offset={off:.3f}{out}")
        prev = out
    fc = ";".join(steps)
    run(["ffmpeg", "-y", *inputs, "-filter_complex", fc,
         "-map", prev, "-c:v", "libx264", "-preset", PRESET, "-crf", "18",
         "visuals.mp4"], cwd=run_dir)


def _finish(run_dir, audio_dur, atmos, bed):
    """Overlay atmosphere + grain + vignette, burn captions, mix narration+bed."""
    fade_out = max(audio_dur - 0.5, 0.1)
    fc = (
        f"[1:v]scale={W}:{H},format=yuv420p,setsar=1[atm];"
        f"[0:v]setsar=1[bg];"
        f"[bg][atm]blend=all_mode=screen:all_opacity=0.22[lit];"
        f"[lit]eq=contrast=1.08:saturation=0.9:brightness=0.04,"
        f"colorbalance=rs=-0.04:bs=0.05:rm=-0.02:bm=0.03,"
        f"noise=alls=4:allf=t,vignette=angle=PI/5[graded];"
        f"[graded]{TIMER}subtitles=captions.srt:force_style='{SUB_STYLE}'[subbed];"
        f"[subbed]fade=t=in:st=0:d=0.4,fade=t=out:st={fade_out:.2f}:d=0.5[v];"
        f"[3:a]volume=0.22[bed];"
        f"[2:a][bed]amix=inputs=2:duration=first:normalize=0[a]"
    )
    run(["ffmpeg", "-y", "-i", "visuals.mp4",
         "-stream_loop", "-1", "-i", atmos, "-i", "narration.mp3",
         "-stream_loop", "-1", "-i", bed,
         "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", PRESET, "-crf", "20",
         "-c:a", "aac", "-b:a", "192k", "-t", f"{audio_dur:.3f}",
         "-movflags", "+faststart", "out.mp4"], cwd=run_dir)


def _assemble_simple(run_dir, n, audio_dur):
    """Fallback: hard-cut concat + captions + narration (no overlays)."""
    with open(os.path.join(run_dir, "concat.txt"), "w", encoding="utf-8") as f:
        for i in range(1, n + 1):
            f.write(f"file 'clip{i}.mp4'\n")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt",
         "-c", "copy", "visuals.mp4"], cwd=run_dir)
    fade_out = max(audio_dur - 0.5, 0.1)
    vf = (f"[0:v]subtitles=captions.srt:force_style='{SUB_STYLE}',"
          f"fade=t=in:st=0:d=0.4,fade=t=out:st={fade_out:.2f}:d=0.5[v]")
    run(["ffmpeg", "-y", "-i", "visuals.mp4", "-i", "narration.mp3",
         "-filter_complex", vf, "-map", "[v]", "-map", "1:a",
         "-c:v", "libx264", "-preset", PRESET, "-crf", "20",
         "-c:a", "aac", "-b:a", "192k", "-t", f"{audio_dur:.3f}",
         "-movflags", "+faststart", "out.mp4"], cwd=run_dir)


def assemble(run_dir, n_images, audio_dur, atmos, bed):
    """Enhanced build (xfade + animated overlays); falls back to simple on error."""
    try:
        _build_xfade_visuals(run_dir, n_images, _per(audio_dur, n_images))
        _finish(run_dir, audio_dur, atmos, bed)
    except Exception as e:  # noqa: BLE001
        log(f"  enhanced assemble failed ({e}); falling back to simple build.")
        _assemble_simple(run_dir, n_images, audio_dur)
    return os.path.join(run_dir, "out.mp4")


def _per(audio_dur, n):
    """Per-clip length so n clips with (n-1) cross-dissolves == audio length."""
    return (audio_dur + (n - 1) * XFADE) / n


def upload(video_path, topic, privacy, publish_at=None):
    """Call youtube_upload.py from the project root. Returns youtube url.

    publish_at (RFC3339 UTC) schedules YouTube's own auto-publish; the video
    uploads private and YouTube makes it public at that time.
    """
    cmd = [sys.executable, os.path.join(PROJECT_ROOT, "youtube_upload.py"),
           video_path,
           "--title", topic["title"],
           "--description", topic["description"],
           "--tags", ",".join(topic["tags"]),
           "--privacy", privacy]
    if publish_at:
        cmd += ["--publish-at", publish_at]
    p = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        low = out.lower()
        if "uploadlimitexceeded" in low or "exceeded the number of videos" in low:
            raise RuntimeError(
                "DAILY UPLOAD LIMIT reached (YouTube caps uploads per 24h, "
                "stricter for new channels). Not an error in the pipeline — "
                "the topic stays queued and the next scheduled run will retry.")
        if any(k in low for k in ("invalid_grant", "refresherror", "expired",
                                  "insufficient", "token has been expired",
                                  "re-run: python youtube_authorize")):
            with open(TOKEN_EXPIRED_FLAG, "w", encoding="utf-8") as f:
                f.write(dt.datetime.now().isoformat() + "\n")
                f.write("YouTube token expired. Run:\n")
                f.write("  .venv\\Scripts\\python.exe youtube_authorize.py\n")
            raise RuntimeError("TOKEN EXPIRED — run youtube_authorize.py. "
                               f"Uploader output:\n{out[-1500:]}")
        raise RuntimeError(f"upload failed:\n{out[-1500:]}")
    # last line contains https://youtu.be/...
    clear_flag(TOKEN_EXPIRED_FLAG)  # a successful upload proves the token works
    url = ""
    for line in out.splitlines():
        if "youtu.be/" in line or "youtube.com/watch" in line:
            url = line.strip().split()[-1]
    return url or "(uploaded; url not parsed)"


def append_history(topic_id, url, privacy):
    new = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "topic_id", "privacy", "url"])
        w.writerow([dt.datetime.now(TZ).date().isoformat(), topic_id, privacy, url])


def slots_filled_today():
    """How many of today's publish slots already have a video (posted or scheduled)."""
    if not os.path.exists(HISTORY_CSV):
        return 0
    today = dt.datetime.now(TZ).date().isoformat()
    n = 0
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) >= 3 and row[0] == today and row[2] in ("public", "scheduled"):
                n += 1
    return n


def slot_publish_time(idx):
    """RFC3339 UTC publishAt for today's slot `idx`, or None if it's already past."""
    h, m = PUBLISH_SLOTS[idx]
    today = dt.datetime.now(TZ).date()
    when = dt.datetime(today.year, today.month, today.day, h, m, tzinfo=TZ)
    if when <= dt.datetime.now(TZ) + dt.timedelta(minutes=2):
        return None
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# produce one video
# --------------------------------------------------------------------------- #
def produce_one(topics, state, args, publish_at):
    """Build and upload a single video. publish_at (RFC3339 UTC) => scheduled."""
    avoid = state.setdefault("subjects", [])
    seed_avoid = [t.get("subject", t["id"]) for t in topics]

    generated = False
    if args.id:
        topic = find_topic(topics, args.id)
    elif args.source == "auto":
        try:
            log("Inventing a fresh topic (Pollinations free text)...")
            topic = generate_topic(avoid + seed_avoid)
            generated = True
        except Exception as e:  # noqa: BLE001
            log(f"  topic generation unavailable ({e}); using topic bank.")
            topic = next_bank(topics)
    else:  # bank (default): consume the top of the queue
        topic = next_bank(topics)
    log(f"Topic [{'AI' if generated else 'bank'}]: {topic['id']} — {topic['title']}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(RUNS_DIR, f"{stamp}-{topic['id']}")
    os.makedirs(run_dir, exist_ok=True)

    # 1) narration — open on the "What would happen if..." hook, spoken and shown
    hook = topic.get("hook", "").strip()
    script = f"{hook} {topic['script']}" if hook else topic["script"]
    captions = ([hook] + topic["captions"]) if hook else topic["captions"]
    log(f"Generating narration (edge-tts / {VOICE})...")
    audio = os.path.join(run_dir, "narration.mp3")
    audio_dur = make_narration(script, audio)
    log(f"  narration duration: {audio_dur:.2f}s")

    # 2) images
    prompts = expand_prompts(topic["image_prompts"])
    n = len(prompts)
    per = _per(audio_dur, n)  # per-clip length accounting for cross-dissolves
    base_seed = int(time.time()) % 100000
    for i, prompt in enumerate(prompts, 1):
        log(f"Generating image {i}/{n} (Pollinations)...")
        raw = os.path.join(run_dir, f"raw{i}.jpg")
        try:
            fetch_image(prompt, raw, base_seed + i)
        except Exception as e:  # noqa: BLE001
            if i == 1:
                raise  # nothing to fall back to
            log(f"  image {i} unavailable ({e}); reusing previous scene.")
            shutil.copyfile(os.path.join(run_dir, f"raw{i - 1}.jpg"), raw)
        # normalize to exact 1080x1920
        img = os.path.join(run_dir, f"scene{i}.png")
        run(["ffmpeg", "-y", "-i", raw,
             "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H}", img])
        log(f"Building animated clip {i}/{n}...")
        kenburns_clip(img, os.path.join(run_dir, f"clip{i}.mp4"), per, i)

    # 3) captions + assemble (with animated overlays)
    log("Writing captions and assembling video...")
    write_srt(captions, audio_dur, os.path.join(run_dir, "captions.srt"))
    atmos = build_atmosphere()
    bed = build_audio_bed()
    video = assemble(run_dir, n, audio_dur, atmos, bed)
    log(f"Video ready: {video}")

    # 4) upload (scheduled if publish_at given, else per --privacy)
    if args.no_upload:
        log("--no-upload set; skipping upload.")
        shutil.rmtree(run_dir, ignore_errors=True)
        return
    if publish_at:
        log(f"Uploading (scheduled for {publish_at})...")
        url = upload(video, topic, "private", publish_at=publish_at)
        logged_privacy = "scheduled"
    else:
        log(f"Uploading ({args.privacy})...")
        url = upload(video, topic, args.privacy)
        logged_privacy = args.privacy
    log(f"UPLOADED: {url}")

    # 5) record success
    if not generated:
        left = remove_from_bank(topic["id"])  # consume: delete from the queue
        log(f"Removed '{topic['id']}' from bank ({left} episodes left).")
    subject = topic.get("subject", topic["id"])
    if subject.lower() not in {s.lower() for s in state.setdefault("subjects", [])}:
        state["subjects"].append(subject)  # avoid future repeats (both sources)
    save_json(STATE_FILE, state)
    append_history(topic["id"], url, logged_privacy)

    # 6) the video is safely on YouTube -> delete this run's local files
    shutil.rmtree(run_dir, ignore_errors=True)
    log("Done. Bank + history updated; run files deleted.")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--privacy", default="public",
                    choices=["public", "unlisted", "private"])
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--id", help="force a specific topic id from the bank")
    ap.add_argument("--source", default="bank", choices=["auto", "bank"],
                    help="bank = use topics.json (Claude-authored, reliable, default); "
                         "auto = try the free text API first (unreliable), else bank")
    ap.add_argument("--fill-day", action="store_true",
                    help="build every remaining daily slot in one run and hand "
                         "them to YouTube's scheduler (past slots publish now).")
    args = ap.parse_args()

    os.makedirs(RUNS_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    if not acquire_lock():
        return
    cleanup_runs()

    if not args.fill_day:
        topics = load_json(TOPICS_FILE, [])
        state = load_json(STATE_FILE, {"used": []})
        produce_one(topics, state, args, publish_at=None)
        return

    # fill-day: build only the slots not yet covered today. Reload topics/state
    # between videos so bank-consume + repeat-avoidance stay correct.
    total = len(PUBLISH_SLOTS)
    done = slots_filled_today()
    if done >= total:
        log(f"All {total} slots already scheduled/posted today; nothing to do.")
        return
    log(f"Filling {total - done} of {total} slots for today...")
    for idx in range(done, total):
        publish_at = slot_publish_time(idx)
        log(f"--- slot {idx + 1}/{total} -> publish "
            f"{publish_at or 'now (slot already passed)'}")
        topics = load_json(TOPICS_FILE, [])
        state = load_json(STATE_FILE, {"used": []})
        try:
            produce_one(topics, state, args, publish_at=publish_at)
        except Exception as e:  # noqa: BLE001
            log(f"slot {idx + 1} failed ({e}); a later catch-up run will retry.")
            break  # stop; next cron run picks up remaining slots


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: {e}")
        sys.exit(1)
    finally:
        release_lock()
