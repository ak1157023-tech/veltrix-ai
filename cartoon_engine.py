"""Clipecut AI Cartoon Studio - engine.

Pipeline ("motion-comic"): story -> script (Claude) -> scene images (fal FLUX)
-> voice (edge-tts) -> animated scenes + subtitles + sound effects (FFmpeg)
-> background music -> final MP4.

No web framework in this file, so it can be tested on its own.
Needed environment variables (Render -> Environment):
    ANTHROPIC_API_KEY   story -> script
    FAL_KEY             scene images
Optional: CARTOON_LLM_MODEL, CARTOON_IMAGE_MODEL, CARTOON_FONT_BN, CARTOON_FONT_EN, CARTOON_DIR
"""
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(os.environ.get("CARTOON_DIR", tempfile.gettempdir())) / "clipecut_cartoon"
ROOT.mkdir(parents=True, exist_ok=True)

JOBS = {}                      # job id -> job dict
GATE = threading.Semaphore(1)  # one video at a time (protects RAM on small servers)
REQUIRED_KEYS = ("ANTHROPIC_API_KEY", "FAL_KEY")
LLM_MODEL = os.environ.get("CARTOON_LLM_MODEL", "claude-sonnet-5-5")
IMAGE_MODEL = os.environ.get("CARTOON_IMAGE_MODEL", "fal-ai/flux/schnell")
FPS = 25
KEEP_SECONDS = 2 * 60 * 60

VOICES = {
    ("bn", "male"): "bn-BD-PradeepNeural",
    ("bn", "female"): "bn-BD-NabanitaNeural",
    ("en", "male"): "en-US-GuyNeural",
    ("en", "female"): "en-US-JennyNeural",
}
STYLES = {
    "2d": "flat 2D cartoon illustration, bold clean outlines, bright vibrant colors, storybook style",
    "3d": "3D animated cartoon style, stylized cute characters, soft cinematic lighting, smooth render",
}


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- status
def status():
    missing = [k for k in REQUIRED_KEYS if not os.environ.get(k)]
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg (install it on the server)")
    try:
        import edge_tts  # noqa: F401
    except Exception:
        missing.append("edge-tts (add it to requirements.txt)")
    # rough numbers: scenes x image price + one script call. Check your providers' pricing pages.
    est = {m: f"{scene_count(int(m)) * 0.01 + 0.10:.2f}" for m in ("1", "3", "5")}
    return {"ready": not missing, "missing": missing, "estimate": est}


def scene_count(minutes):
    return max(3, (minutes * 60) // 8)


# --------------------------------------------------------------------------- jobs
def public(job):
    keys = ("id", "status", "phase", "detail", "pct", "files", "error")
    return {k: job[k] for k in keys}


def start_job(req):
    _cleanup()
    job = {"id": uuid.uuid4().hex[:12], "status": "queued", "phase": "Waiting in queue", "detail": "",
           "pct": 0, "files": [], "error": None, "cancel": False, "proc": None, "created": time.time()}
    job["dir"] = ROOT / job["id"]
    job["dir"].mkdir(parents=True, exist_ok=True)
    JOBS[job["id"]] = job
    threading.Thread(target=_worker, args=(job, req), daemon=True).start()
    return job


def cancel_job(job):
    job["cancel"] = True
    p = job.get("proc")
    if p and p.poll() is None:
        p.kill()
    if job["status"] in ("queued", "processing"):
        job["status"] = "cancelled"


def delete_files(job):
    shutil.rmtree(job["dir"], ignore_errors=True)


def _cleanup():
    now = time.time()
    for jid, j in list(JOBS.items()):
        if now - j["created"] > KEEP_SECONDS and j["status"] != "processing":
            shutil.rmtree(j["dir"], ignore_errors=True)
            JOBS.pop(jid, None)


def _set(job, phase=None, pct=None, detail=None):
    if job["cancel"]:
        raise Cancelled()
    if phase is not None:
        job["phase"] = phase
    if pct is not None:
        job["pct"] = round(pct)
    if detail is not None:
        job["detail"] = detail


def _worker(job, req):
    GATE.acquire()
    try:
        if job["cancel"]:
            return
        job["status"] = "processing"
        _run(job, req)
        job["status"] = "ready"
        job["pct"] = 100
        job["phase"] = "Done"
    except Cancelled:
        job["status"] = "cancelled"
    except Exception as e:  # shown to the user
        job["status"] = "error"
        job["error"] = str(e)[:400]
    finally:
        job["proc"] = None
        GATE.release()
        if job["status"] != "ready":
            delete_files(job)


# --------------------------------------------------------------------------- pipeline
def _run(job, req):
    d = job["dir"]
    minutes = int(req["minutes"])
    n = scene_count(minutes)
    W, H = (720, 1280) if req["ratio"] == "9:16" else (1280, 720)

    _set(job, "Writing the script", 3, "Reading your story")
    script = make_script(req, n)
    scenes = script["scenes"]
    total = len(scenes)
    looks = {c["name"]: c["look"] for c in script.get("characters", []) if c.get("name") and c.get("look")}
    seed = random.randint(1, 2**31 - 1)  # same seed for the whole video = steadier look

    # scene images (a few at a time)
    _set(job, "Drawing the scenes", 10, f"0 / {total}")
    done = [0]
    lock = threading.Lock()

    def draw(i):
        if job["cancel"]:
            raise Cancelled()
        s = scenes[i]
        who = "; ".join(f"{nm}: {looks[nm]}" for nm in s.get("characters", []) if nm in looks)
        prompt = f"{STYLES[req['style']]}. {s['image_prompt']}. Characters: {who}. No text, no letters, no watermark."
        (d / f"img_{i}.jpg").write_bytes(gen_image(prompt, req["ratio"], seed))
        with lock:
            done[0] += 1
            job["pct"] = round(10 + 35 * done[0] / total)
            job["detail"] = f"{done[0]} / {total}"

    with ThreadPoolExecutor(3) as ex:
        for f in [ex.submit(draw, i) for i in range(total)]:
            f.result()
    _set(job)

    # voice
    voice = VOICES[(req["lang"], req["voice"])]
    durs = []
    for i, s in enumerate(scenes):
        _set(job, "Recording the voice", 45 + 15 * i / total, f"{i + 1} / {total}")
        tts(s["narration"], voice, d / f"voice_{i}.mp3")
        durs.append(probe_duration(d / f"voice_{i}.mp3"))

    # sound effect + animated scenes with subtitles
    _ff(job, ["-f", "lavfi", "-i", "anoisesrc=d=0.6:c=pink:a=0.5", "-af",
              "highpass=f=300,lowpass=f=3000,afade=t=in:d=0.25,afade=t=out:st=0.25:d=0.35", "whoosh.wav"])
    for i, s in enumerate(scenes):
        _set(job, "Animating the scenes", 60 + 30 * i / total, f"{i + 1} / {total}")
        make_scene(job, i, s["narration"], durs[i], W, H, req["lang"])

    # join + music
    _set(job, "Adding music and finishing", 92, "")
    (d / "list.txt").write_text("".join(f"file 'scene_{i}.mp4'\n" for i in range(total)))
    _ff(job, ["-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy", "joined.mp4"])
    length = sum(probe_duration(d / f"scene_{i}.mp4") for i in range(total))
    _ff(job, ["-f", "lavfi", "-i", f"sine=f=220:d={length:.2f}", "-f", "lavfi", "-i", f"sine=f=277.18:d={length:.2f}",
              "-f", "lavfi", "-i", f"sine=f=329.63:d={length:.2f}", "-filter_complex",
              f"[0][1][2]amix=inputs=3:normalize=0,tremolo=f=0.25:d=0.5,lowpass=f=900,volume=0.3,"
              f"afade=t=in:d=3,afade=t=out:st={max(length - 3, 0):.2f}:d=3[m]", "-map", "[m]", "music.wav"])
    _ff(job, ["-i", "joined.mp4", "-i", "music.wav", "-filter_complex",
              "[1:a]volume=0.5[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
              "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
              "-movflags", "+faststart", "cartoon.mp4"])
    out = d / "cartoon.mp4"
    job["files"] = [{"i": 0, "name": "cartoon.mp4", "size": out.stat().st_size, "type": "video/mp4"}]


def make_scene(job, i, text, voice_dur, W, H, lang):
    d = job["dir"]
    dur = voice_dur + 0.3 + 0.5
    frames = max(2, int(dur * FPS))
    (d / f"s_{i}.srt").write_text(make_srt(text, 0.3, 0.3 + voice_dur), encoding="utf-8")
    step = 0.22 / frames
    z = f"min(zoom+{step:.6f},1.3)" if i % 2 == 0 else f"if(eq(on,0),1.22,max(zoom-{step:.6f},1.0))"
    font = os.environ.get("CARTOON_FONT_BN", "Noto Sans Bengali") if lang == "bn" else os.environ.get("CARTOON_FONT_EN", "DejaVu Sans")
    style = f"FontName={font},FontSize={11 if H > W else 13},Bold=1,Alignment=2,MarginV={45 if H > W else 22},Outline=2,Shadow=0"
    fc = (f"[0:v]scale={2 * W}:{2 * H}:force_original_aspect_ratio=increase,crop={2 * W}:{2 * H},"
          f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
          f"subtitles=s_{i}.srt:force_style='{style}',format=yuv420p[v];"
          f"[1:a]adelay=300|300,apad[a1];[2:a]volume=0.6[a2];"
          f"[a1][a2]amix=inputs=2:duration=longest:normalize=0,atrim=0:{dur:.2f},aresample=44100[a]")
    _ff(job, ["-i", f"img_{i}.jpg", "-i", f"voice_{i}.mp3", "-i", "whoosh.wav", "-filter_complex", fc,
              "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}", "-r", str(FPS), "-c:v", "libx264",
              "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-ar", "44100", "-ac", "2", f"scene_{i}.mp4"])


def make_srt(text, start, end):
    words = text.split()
    size = 6
    chunks = [" ".join(words[k:k + size]) for k in range(0, len(words), size)] or [text]
    total = sum(len(c) for c in chunks) or 1
    t, out = start, []

    def ts(x):
        return f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{int(x % 60):02d},{int(x % 1 * 1000):03d}"

    for n, c in enumerate(chunks, 1):
        e = t + (end - start) * len(c) / total
        out.append(f"{n}\n{ts(t)} --> {ts(e)}\n{c}\n")
        t = e
    return "\n".join(out)


def _ff(job, args):
    if job["cancel"]:
        raise Cancelled()
    p = subprocess.Popen(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], cwd=job["dir"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    job["proc"] = p
    err = p.communicate()[1]
    job["proc"] = None
    if job["cancel"]:
        raise Cancelled()
    if p.returncode != 0:
        raise RuntimeError("Video processing failed: " + (err or "")[-300:])


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=30)
    return float(r.stdout.strip())


# --------------------------------------------------------------------------- AI providers
def make_script(req, n):
    words = round((2.2 if req["lang"] == "bn" else 2.5) * 7)
    lang = "Bangla (Bengali script)" if req["lang"] == "bn" else "English"
    prompt = f"""Turn this story into a cartoon video script.

STORY:
{req['story']}

Return ONLY JSON in this shape:
{{"title": "...", "characters": [{{"name": "...", "look": "..."}}],
 "scenes": [{{"narration": "...", "characters": ["name"], "image_prompt": "..."}}]}}

Rules:
- Exactly {n} scenes, in story order, covering the whole story.
- "narration": {lang}, about {words} words per scene, natural spoken storytelling (any dialogue is read by the narrator).
- "characters": 1-4 main characters. "look" is an ENGLISH fixed visual description (species/age, face, hair, clothes, colors) that never changes.
- "image_prompt": ENGLISH, describes the setting, action and camera angle of that one scene. No text or letters in the picture. Do not repeat the characters' looks; list their names in "characters".
- Family-friendly content only."""
    last = None
    for _ in range(2):
        text = llm(prompt)
        try:
            data = parse_json(text)
            scenes = [s for s in data["scenes"] if s.get("narration") and s.get("image_prompt")]
            if scenes:
                data["scenes"] = scenes[: n + 4]
                return data
        except Exception as e:
            last = e
    raise RuntimeError(f"The AI did not return a valid script ({last}). Try again.")


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    return json.loads(text[text.index("{"): text.rindex("}") + 1])


def llm(prompt):
    r = httpx.post("https://api.anthropic.com/v1/messages", timeout=180,
                   headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                   json={"model": LLM_MODEL, "max_tokens": 8000,
                         "system": "You are a children's story screenwriter. Reply with JSON only.",
                         "messages": [{"role": "user", "content": prompt}]})
    if r.status_code >= 400:
        raise RuntimeError(f"Script AI error {r.status_code}: {r.text[:200]}")
    return "".join(b.get("text", "") for b in r.json()["content"])


def gen_image(prompt, ratio, seed):
    size = "portrait_16_9" if ratio == "9:16" else "landscape_16_9"
    last = None
    for attempt in range(3):
        try:
            r = httpx.post(f"https://fal.run/{IMAGE_MODEL}", timeout=120,
                           headers={"Authorization": "Key " + os.environ["FAL_KEY"]},
                           json={"prompt": prompt, "image_size": size, "num_images": 1, "seed": seed,
                                 "num_inference_steps": 4})
            if r.status_code >= 400:
                raise RuntimeError(f"Image AI error {r.status_code}: {r.text[:200]}")
            return httpx.get(r.json()["images"][0]["url"], timeout=120).content
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last))


def tts(text, voice, out):
    import edge_tts
    asyncio.run(edge_tts.Communicate(text, voice).save(str(out)))
